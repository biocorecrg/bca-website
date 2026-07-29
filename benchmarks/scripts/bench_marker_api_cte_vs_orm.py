"""Compare the markers API across two live deployments: a CTE host vs an ORM host.

This is the HTTP-level counterpart to ``bench_marker_orm_vs_cte.py``, which
compares the two query implementations inside one database. Here the same 16
parameter combinations are issued as real requests against two running sites,
one serving the ``_MARKER_SQL`` rewrite and one still on the django-filter
annotation pipeline.

Both hosts are configurable, so the same script covers every pairing::

    # local container vs production (default)
    python3 benchmarks/scripts/bench_marker_api_cte_vs_orm.py

    # staging vs production
    CTE_BASE=https://bca-dev.hpc.crg.es CTE_LABEL=staging \
        python3 benchmarks/scripts/bench_marker_api_cte_vs_orm.py

    # parity table only (5 requests per host)
    PARITY_ONLY=1 python3 benchmarks/scripts/bench_marker_api_cte_vs_orm.py

The user-facing page is ``/atlas/<dataset>/markers/?metacells=...``, an HTML
template whose JavaScript then fetches the JSON. This script benchmarks that
JSON call -- ``/api/v1/markers/`` with ``limit=0`` -- exactly as
``app/static/app/src/atlas/markers.ts`` builds it, because the HTML page itself
carries none of the marker data. The equivalent page URL is recorded alongside
each scenario for reference.

IMPORTANT, on reading the timings: this is not a controlled A/B. The two hosts
differ in hardware, network distance and concurrent load, so wall times mix
query cost with environment. The JSON comparison, by contrast, is exact and is
the reliable half of this report.

Read-only GETs. Requests to the ORM host are sequential with a delay between
them, to keep the load on the public production site modest.
"""

import json
import math
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CTE_BASE = os.environ.get("CTE_BASE", "http://portal-bca-gambusia:8000")
ORM_BASE = os.environ.get("ORM_BASE", "https://portal.biodiversitycellatlas.org")
CTE_LABEL = os.environ.get("CTE_LABEL", "cte")
ORM_LABEL = os.environ.get("ORM_LABEL", "orm")

HOSTS = {"cte": CTE_BASE, "orm": ORM_BASE}
LABELS = {"cte": CTE_LABEL, "orm": ORM_LABEL}

API_PATH = "/api/v1/markers/"
REPS = 2
TIMEOUT_S = 300
# Be a considerate client against the public production site.
ORM_DELAY_S = float(os.environ.get("ORM_DELAY_S", "1.0"))

# Numeric payload fields (the serializer exposes five of the seven annotations;
# bg_mean_fc and bg_median_fc are not declared on MetacellMarkerSerializer).
NUMERIC_FIELDS = ("bg_sum_umi", "fg_sum_umi", "umi_perc", "fg_mean_fc", "fg_median_fc")
OTHER_FIELDS = ("id", "description", "domains", "genelists")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def build_url(base, dataset, params):
    query = urllib.parse.urlencode({"dataset": dataset, **params, "limit": 0})
    return f"{base}{API_PATH}?{query}"


def page_url(base, dataset, params):
    """The human-facing atlas page that triggers the API call above."""

    return f"{base}/atlas/{dataset}/markers/?{urllib.parse.urlencode(params)}"


def fetch(url):
    """GET the URL, returning (seconds, parsed_json, n_bytes, error)."""

    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "bca-markers-benchmark/1.0"}
    )
    context = ssl.create_default_context()
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S, context=context) as response:
            body = response.read()
            elapsed = time.perf_counter() - started
            status = response.status
    except urllib.error.HTTPError as exc:
        return time.perf_counter() - started, None, 0, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - a dead host is a result
        return time.perf_counter() - started, None, 0, f"{type(exc).__name__}: {str(exc)[:100]}"

    if status != 200:
        return elapsed, None, len(body), f"HTTP {status}"
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return elapsed, None, len(body), f"bad JSON: {exc}"
    # limit=0 returns a bare list; keep the paginated envelope working anyway.
    rows = payload if isinstance(payload, list) else payload.get("results", [])
    return elapsed, rows, len(body), None


# --------------------------------------------------------------------------- #
# JSON comparison
# --------------------------------------------------------------------------- #
def compare(cte_rows, orm_rows):
    """Exact content comparison, keyed on gene name (stable across deployments)."""

    if cte_rows is None or orm_rows is None:
        return {"comparable": False}

    left = {r["name"]: r for r in cte_rows}
    right = {r["name"]: r for r in orm_rows}
    shared = sorted(set(left) & set(right))

    field_diffs = {}
    for field in NUMERIC_FIELDS + OTHER_FIELDS:
        bad = [n for n in shared if not _same(left[n].get(field), right[n].get(field))]
        if bad:
            field_diffs[field] = {
                "n": len(bad),
                "example_gene": bad[0],
                "cte": left[bad[0]].get(field),
                "orm": right[bad[0]].get(field),
            }

    cte_order = [r["name"] for r in cte_rows]
    orm_order = [r["name"] for r in orm_rows]
    # Ordering aside, do the payloads compare equal once sorted the same way?
    same_sorted_by_id = sorted(cte_rows, key=lambda r: r["id"]) == sorted(orm_rows, key=lambda r: r["id"])
    return {
        "comparable": True,
        "n_cte": len(cte_rows),
        "n_orm": len(orm_rows),
        "same_gene_set": set(left) == set(right),
        "only_cte": sorted(set(left) - set(right))[:5],
        "only_orm": sorted(set(right) - set(left))[:5],
        "n_only_cte": len(set(left) - set(right)),
        "n_only_orm": len(set(right) - set(left)),
        "keys_cte": sorted({k for r in cte_rows for k in r}),
        "keys_orm": sorted({k for r in orm_rows for k in r}),
        "same_keys": {k for r in cte_rows for k in r} == {k for r in orm_rows for k in r},
        "field_diffs": field_diffs,
        "same_order": cte_order == orm_order,
        "same_sorted_by_id": same_sorted_by_id,
        "cte_ids_ascending": [r["id"] for r in cte_rows] == sorted(r["id"] for r in cte_rows),
        "orm_ids_ascending": [r["id"] for r in orm_rows] == sorted(r["id"] for r in orm_rows),
        "cte_first3": cte_order[:3],
        "orm_first3": orm_order[:3],
    }


def _same(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-12)
    if isinstance(a, list) and isinstance(b, list):
        return sorted(map(str, a)) == sorted(map(str, b))
    return a == b


# --------------------------------------------------------------------------- #
# Scenarios (mirrors bench_marker_orm_vs_cte.py, plus fc_max_bg_type=ignore,
# which the atlas page always sends)
# --------------------------------------------------------------------------- #
def scenario(label, dataset, metacells, fc_min=2, fc_min_type="mean", extra=None, note=""):
    params = {
        "metacells": metacells,
        "fc_min_type": fc_min_type,
        "fc_min": fc_min,
        "fc_max_bg_type": "ignore",
    }
    params.update(extra or {})
    return {"label": label, "dataset": dataset, "params": params, "note": note}


NUMERIC_20 = ",".join(str(i) for i in range(1, 21))
NUMERIC_200 = ",".join(str(i) for i in range(1, 201))

SCENARIOS = [
    scenario("S01 small / 3 mcs / mean", "amphimedon-queenslandica-larva", "1,2,3", note="300k expression rows"),
    scenario("S02 small / 3 mcs / median", "amphimedon-queenslandica-larva", "1,2,3", fc_min_type="median"),
    scenario(
        "S03 small / cell type / mean",
        "amphimedon-queenslandica-larva",
        "Ciliated epithelium",
        note="foreground selected by cell type",
    ),
    scenario("S04 mid / 3 mcs / mean", "trichoplax-adhaerens", "1,2,3", note="507k rows"),
    scenario("S05 mid / 5 mcs / median", "xenia-sp", "1,2,3,4,5", fc_min_type="median", note="5.0M rows"),
    scenario("S06 large / 1 mc / mean", "mus-musculus", "7", note="9.9M rows"),
    scenario("S07 large / 1 mc / median", "mus-musculus", "7", fc_min_type="median"),
    scenario("S08 large / 20 mcs / mean", "mus-musculus", NUMERIC_20),
    scenario("S09 large / 20 mcs / median", "mus-musculus", NUMERIC_20, fc_min_type="median"),
    scenario("S10 large / cell type 94 mcs / mean", "mus-musculus", "mesenchymal cell"),
    scenario("S11 large / 2 cell types / median", "mus-musculus", "mesenchymal cell,T cell", fc_min_type="median"),
    scenario(
        "S12 large / type + mcs mixed / mean",
        "mus-musculus",
        "B cell,7,12,30",
        note="the URL shape from the original request",
    ),
    scenario("S13 large / 20 mcs / fc_min=0", "mus-musculus", NUMERIC_20, fc_min=0),
    scenario(
        "S14 large / 20 mcs / fc_min=-9 / median",
        "mus-musculus",
        NUMERIC_20,
        fc_min=-9,
        fc_min_type="median",
        note="worst case row count",
    ),
    scenario("S15 large / empty foreground", "mus-musculus", "__no_such_metacell__"),
    scenario("S16 other large / 200 mcs / mean", "schmidtea-mediterranea", NUMERIC_200, note="9.3M rows"),
]

# Parameters the two implementations may not honour identically. Row counts
# only, one request per host.
PARITY = [
    scenario("fc_max_bg_type=ignore", "trichoplax-adhaerens", "1,2,3", extra={"fc_max_bg": 1.2}),
    scenario(
        "fc_max_bg_type=mean + fc_max_bg=1.2",
        "trichoplax-adhaerens",
        "1,2,3",
        extra={"fc_max_bg_type": "mean", "fc_max_bg": 1.2},
    ),
    scenario(
        "fc_max_bg_type=median + fc_max_bg=1.2",
        "trichoplax-adhaerens",
        "1,2,3",
        extra={"fc_max_bg_type": "median", "fc_max_bg": 1.2},
    ),
    scenario("fc_min_type omitted", "trichoplax-adhaerens", "1,2,3", extra={"fc_min_type": None}),
    scenario("metacells empty", "trichoplax-adhaerens", ""),
]


def _clean(params):
    return {k: v for k, v in params.items() if v is not None}


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def request_scenario(host_key, scn):
    url = build_url(HOSTS[host_key], scn["dataset"], _clean(scn["params"]))
    result = fetch(url)
    if host_key == "orm":
        time.sleep(ORM_DELAY_S)
    return url, result


def main():
    print(f"{CTE_LABEL} (CTE) = {CTE_BASE}")
    print(f"{ORM_LABEL} (ORM) = {ORM_BASE}")
    print(f"api={API_PATH} reps={REPS} timeout={TIMEOUT_S}s orm_delay={ORM_DELAY_S}s\n")

    if os.environ.get("PARITY_ONLY"):
        parity_table()
        return

    results = []
    for scn in SCENARIOS:
        times = {"cte": [], "orm": []}
        rows = {"cte": None, "orm": None}
        errors = {"cte": None, "orm": None}
        sizes = {"cte": 0, "orm": 0}
        urls = {}

        for rep in range(REPS):
            order = ("cte", "orm") if rep % 2 == 0 else ("orm", "cte")
            for host_key in order:
                urls[host_key], (secs, payload, nbytes, err) = request_scenario(host_key, scn)
                if err is None:
                    times[host_key].append(secs)
                    rows[host_key] = payload
                    sizes[host_key] = nbytes
                errors[host_key] = errors[host_key] or err

        diff = compare(rows["cte"], rows["orm"])
        record = {
            "label": scn["label"],
            "note": scn["note"],
            "dataset": scn["dataset"],
            "params": _clean(scn["params"]),
            "api_url_cte": urls.get("cte"),
            "page_url_cte": page_url(CTE_BASE, scn["dataset"], _clean(scn["params"])),
            "page_url_orm": page_url(ORM_BASE, scn["dataset"], _clean(scn["params"])),
            "cte_s": min(times["cte"]) if times["cte"] else None,
            "orm_s": min(times["orm"]) if times["orm"] else None,
            "cte_bytes": sizes["cte"],
            "orm_bytes": sizes["orm"],
            "cte_error": errors["cte"],
            "orm_error": errors["orm"],
            "diff": diff,
        }
        if record["cte_s"] and record["orm_s"]:
            record["speedup"] = record["orm_s"] / record["cte_s"]
        results.append(record)
        print(_line(record))

    print("\n--- parameter parity (item counts; one request per host) ---")
    parity = parity_table()

    print("\n===JSON===")
    print(
        json.dumps(
            {"cte_base": CTE_BASE, "orm_base": ORM_BASE, "scenarios": results, "parity": parity},
            indent=1,
            default=str,
        )
    )


def _line(record):
    diff = record["diff"]
    if not diff.get("comparable"):
        verdict = f"NOT COMPARABLE {CTE_LABEL}_err={record['cte_error']} {ORM_LABEL}_err={record['orm_error']}"
    elif diff["field_diffs"] or not diff["same_gene_set"]:
        verdict = "CONTENT DIFFERS"
        if not diff["same_gene_set"]:
            verdict += f" genes +{diff['n_only_cte']}/-{diff['n_only_orm']}"
        if diff["field_diffs"]:
            verdict += " fields:" + ",".join(diff["field_diffs"])
    else:
        verdict = "same content"
        verdict += ", same order" if diff["same_order"] else ", DIFFERENT ORDER"
    return (
        f"{record['label']:38s} items={diff.get('n_cte', '?'):>6}/{diff.get('n_orm', '?'):<6} "
        f"{CTE_LABEL}={_secs(record['cte_s'])} {ORM_LABEL}={_secs(record['orm_s'])} "
        f"x{record.get('speedup', float('nan')):5.2f}  {verdict}"
    )


def _secs(value):
    return "  fail" if value is None else f"{value:6.2f}s"


def parity_table():
    print(f"\n{'query parameters':40s} {CTE_LABEL + ' (CTE)':>16} {ORM_LABEL + ' (ORM)':>16}")
    rows = []
    for scn in PARITY:
        counts = {}
        for host_key in ("cte", "orm"):
            _, (_, payload, _, err) = request_scenario(host_key, scn)
            counts[host_key] = err if err else len(payload)
        rows.append({"case": scn["label"], **counts, "agree": counts["cte"] == counts["orm"]})
        print(f"{scn['label']:40s} {str(counts['cte']):>16} {str(counts['orm']):>16}")
    return rows


if __name__ == "__main__":
    sys.exit(main())
