"""Benchmark the CTE-based markers query against the original ORM pipeline.

Compares two implementations of the ``/api/v1/metacellmarkers`` payload:

* **CTE** -- the raw SQL in ``rest.views.MetacellMarkerViewSet._MARKER_SQL``,
  driven through the real viewset (``get_queryset()``), so parameter parsing and
  the ``fc_min_type`` -> ``having_col`` mapping are exercised as in production.
* **ORM** -- the django-filter annotation pipeline it replaced, which still
  lives in ``rest.filters.MetacellMarkerFilter``. The filterset is driven with a
  plain params dict, so the queryset is built by the shipped code rather than
  reconstructed here.

For every scenario the script reports wall-clock time to materialise the full
result set (what the endpoint does before paginating), the server-side
``EXPLAIN ANALYZE`` execution time, plan characteristics, and whether the two
implementations agree row-for-row and value-for-value.

Timings alternate the order of the two implementations across repetitions
(CTE-then-ORM, then ORM-then-CTE) and report the minimum of two runs, so
neither side gets a systematic advantage from a warm buffer cache.

Run inside the web container::

    docker exec -i bca-web-1 python manage.py shell < benchmarks/scripts/bench_marker_orm_vs_cte.py
    # or: podman compose exec -T web python manage.py shell < benchmarks/scripts/bench_marker_orm_vs_cte.py

Read-only: it issues SELECTs only, inside transactions that are rolled back.
"""

import json
import math
import os
import re
import time

from django.db import connection, transaction
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from app import models
from rest import filters
from rest.views import MetacellMarkerViewSet

# A slow scenario is a result, not a hang: cap each run instead of blocking.
STATEMENT_TIMEOUT_MS = 300_000
REPS = 2

# Columns the endpoint exposes as annotations on each gene.
COLS = (
    "bg_sum_umi",
    "fg_sum_umi",
    "umi_perc",
    "fg_mean_fc",
    "bg_mean_fc",
    "fg_median_fc",
    "bg_median_fc",
)

_factory = APIRequestFactory()


# --------------------------------------------------------------------------- #
# Query builders
# --------------------------------------------------------------------------- #
def cte_queryset(params):
    """Build the raw-SQL queryset exactly as the live viewset does."""

    view = MetacellMarkerViewSet()
    view.request = Request(_factory.get("/api/v1/metacellmarkers/", params))
    view.format_kwarg = None
    return view.get_queryset()


def orm_queryset(params):
    """Build the pre-rewrite queryset via the shipped django-filter filterset."""

    fset = filters.MetacellMarkerFilter(data=dict(params), queryset=models.Gene.objects.all())
    if not fset.is_valid():
        raise ValueError(f"invalid filter params: {fset.errors}")
    return fset.qs


def sql_of(queryset):
    """Return (sql, params) for either a RawQuerySet or a normal QuerySet."""

    if hasattr(queryset, "raw_query"):
        return queryset.raw_query, queryset.params
    return queryset.query.sql_with_params()


# --------------------------------------------------------------------------- #
# Measurement
# --------------------------------------------------------------------------- #
def timed_fetch(build, params):
    """Materialise the whole result set, returning (seconds, rows, error)."""

    started = time.perf_counter()
    try:
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = %s", [STATEMENT_TIMEOUT_MS])
            rows = list(build(params))
            elapsed = time.perf_counter() - started
            # Pull annotations out inside the transaction; they are plain
            # attributes, so this costs no extra queries.
            snapshot = {r.id: tuple(getattr(r, c, None) for c in COLS) for r in rows}
        return elapsed, snapshot, None
    except Exception as exc:  # noqa: BLE001 - a timeout/plan failure is a datum
        return time.perf_counter() - started, None, f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"


def explain(build, params):
    """Server-side EXPLAIN ANALYZE: execution time plus plan characteristics."""

    try:
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = %s", [STATEMENT_TIMEOUT_MS])
                sql, sql_params = sql_of(build(params))
                cur.execute("EXPLAIN (ANALYZE, TIMING off) " + sql, sql_params)
                plan = "\n".join(row[0] for row in cur.fetchall())
    except Exception as exc:  # noqa: BLE001
        return None, {"error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"}

    match = re.search(r"Execution Time: ([\d.]+) ms", plan)
    # Per-row join work is the headline difference between the two plans: the
    # ORM resolves metacell/cell-type names through joins driven once per
    # expression row, so its Memoize nodes report loops in the millions.
    loops = [int(n) for n in re.findall(r"Memoize.*?loops=(\d+)", plan)]
    return (
        float(match.group(1)) if match else None,
        {
            "subplans": len(set(re.findall(r"SubPlan (\d+)", plan))),
            "sort_methods": sorted(set(re.findall(r"Sort Method: ([a-z ]+)", plan))),
            "disk_sort": "external merge" in plan or "external sort" in plan,
            "aggregate_node": next(
                (n for n in ("GroupAggregate", "HashAggregate", "Aggregate") if n in plan),
                "?",
            ),
            "seq_scan_on_mge": bool(re.search(r"Seq Scan on app_metacellgeneexpression", plan)),
            "nested_loops": plan.count("Nested Loop"),
            "max_memoize_loops": max(loops) if loops else 0,
        },
    )


def compare(cte_rows, orm_rows):
    """Row-set and per-column agreement between the two implementations."""

    if cte_rows is None or orm_rows is None:
        return {"comparable": False}

    cte_ids, orm_ids = set(cte_rows), set(orm_rows)
    shared = cte_ids & orm_ids
    mismatches = {}
    for pos, col in enumerate(COLS):
        bad = [gid for gid in shared if not _close(cte_rows[gid][pos], orm_rows[gid][pos])]
        if bad:
            example = sorted(bad)[0]
            mismatches[col] = {
                "n": len(bad),
                "example_gene_id": example,
                "cte": _fmt(cte_rows[example][pos]),
                "orm": _fmt(orm_rows[example][pos]),
            }
    return {
        "comparable": True,
        "cte_rows": len(cte_ids),
        "orm_rows": len(orm_ids),
        "shared": len(shared),
        "only_cte": len(cte_ids - orm_ids),
        "only_orm": len(orm_ids - cte_ids),
        "same_gene_set": cte_ids == orm_ids,
        "column_mismatches": mismatches,
    }


def _close(a, b, rel=1e-9):
    if a is None or b is None:
        return a is None and b is None
    a, b = float(a), float(b)
    return math.isclose(a, b, rel_tol=rel, abs_tol=1e-12)


def _fmt(value):
    return None if value is None else float(value)


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #
def scenario(label, dataset, metacells, fc_min=2, fc_min_type="mean", note=""):
    return {
        "label": label,
        "note": note,
        "params": {
            "dataset": dataset,
            "metacells": metacells,
            "fc_min": str(fc_min),
            "fc_min_type": fc_min_type,
        },
    }


NUMERIC_20 = ",".join(str(i) for i in range(1, 21))
NUMERIC_200 = ",".join(str(i) for i in range(1, 201))

SCENARIOS = [
    # --- small dataset: does the CTE still pay off when there is little data? --
    scenario(
        "S01 small / 3 mcs / mean", "amphimedon-queenslandica-larva", "1,2,3", note="300k expression rows, 31 metacells"
    ),
    scenario(
        "S02 small / 3 mcs / median",
        "amphimedon-queenslandica-larva",
        "1,2,3",
        fc_min_type="median",
        note="median path: percentile_cont is the costly aggregate",
    ),
    scenario(
        "S03 small / cell type / mean",
        "amphimedon-queenslandica-larva",
        "Ciliated epithelium",
        note="foreground selected by cell type (15 metacells)",
    ),
    # --- mid-size dataset ----------------------------------------------------- #
    scenario("S04 mid / 3 mcs / mean", "trichoplax-adhaerens", "1,2,3", note="507k rows, 45 metacells"),
    scenario("S05 mid / 5 mcs / median", "xenia-sp", "1,2,3,4,5", fc_min_type="median", note="5.0M rows"),
    # --- largest dataset ------------------------------------------------------ #
    scenario("S06 large / 1 mc / mean", "mus-musculus", "7", note="9.9M rows; smallest possible foreground"),
    scenario("S07 large / 1 mc / median", "mus-musculus", "7", fc_min_type="median"),
    scenario("S08 large / 20 mcs / mean", "mus-musculus", NUMERIC_20),
    scenario("S09 large / 20 mcs / median", "mus-musculus", NUMERIC_20, fc_min_type="median"),
    scenario(
        "S10 large / cell type 94 mcs / mean",
        "mus-musculus",
        "mesenchymal cell",
        note="largest cell type in the dataset",
    ),
    scenario(
        "S11 large / 2 cell types / median",
        "mus-musculus",
        "mesenchymal cell,T cell",
        fc_min_type="median",
        note="137 metacells",
    ),
    scenario(
        "S12 large / type + mcs mixed / mean",
        "mus-musculus",
        "B cell,7,12,30",
        note="mixes the metacell-name and cell-type branches of the OR",
    ),
    # --- filter selectivity: fc_min drives how much survives the HAVING ------- #
    scenario(
        "S13 large / 20 mcs / fc_min=0",
        "mus-musculus",
        NUMERIC_20,
        fc_min=0,
        note="permissive threshold: most genes survive",
    ),
    scenario(
        "S14 large / 20 mcs / fc_min=-9 / median",
        "mus-musculus",
        NUMERIC_20,
        fc_min=-9,
        fc_min_type="median",
        note="effectively unfiltered, worst case row count",
    ),
    # --- degenerate foreground ------------------------------------------------ #
    scenario(
        "S15 large / empty foreground",
        "mus-musculus",
        "__no_such_metacell__",
        note="no metacell matches: every FILTER is false",
    ),
    # --- second large dataset, wide foreground -------------------------------- #
    scenario(
        "S16 other large / 200 mcs / mean",
        "schmidtea-mediterranea",
        NUMERIC_200,
        note="9.3M rows, 352 metacells; over half the dataset in foreground",
    ),
]


# --------------------------------------------------------------------------- #
# Parameter parity
# --------------------------------------------------------------------------- #
# Row counts only, no timing: these probe which query parameters each
# implementation honours, including the ones the rewrite dropped.
PARITY_BASE = {
    "dataset": "trichoplax-adhaerens",
    "metacells": "1,2,3",
    "fc_min": "2",
    "fc_min_type": "mean",
}

PARITY_CHECKS = [
    ("baseline", PARITY_BASE),
    ("fc_max_bg_type=ignore", {**PARITY_BASE, "fc_max_bg_type": "ignore", "fc_max_bg": "1.2"}),
    ("fc_max_bg_type=mean + fc_max_bg", {**PARITY_BASE, "fc_max_bg_type": "mean", "fc_max_bg": "1.2"}),
    ("fc_max_bg_type=median + fc_max_bg", {**PARITY_BASE, "fc_max_bg_type": "median", "fc_max_bg": "1.2"}),
    ("fc_min_type omitted", {k: v for k, v in PARITY_BASE.items() if k != "fc_min_type"}),
    ("fc_min omitted (defaults to 2)", {k: v for k, v in PARITY_BASE.items() if k != "fc_min"}),
    ("fc_min_type=bogus", {**PARITY_BASE, "fc_min_type": "bogus"}),
    ("dataset=bogus", {**PARITY_BASE, "dataset": "no-such-dataset"}),
    ("metacells empty", {**PARITY_BASE, "metacells": ""}),
]


def _row_count(build, params):
    try:
        return len(list(build(params)))
    except Exception as exc:  # noqa: BLE001 - rejection is the interesting answer
        fields = re.findall(r"'([a-z_]+)':", str(exc)) or re.findall(
            r"\b(dataset|metacells|fc_min_type|fc_min)\b", str(exc)
        )
        return "rejected:" + ",".join(sorted(set(fields))) if fields else "rejected"


def parity_table():
    print(f"\n{'query parameters':38s} {'CTE':>18} {'ORM':>18}")
    rows = []
    for label, params in PARITY_CHECKS:
        cte = _row_count(cte_queryset, params)
        orm = _row_count(orm_queryset, params)
        rows.append({"case": label, "cte": cte, "orm": orm, "agree": cte == orm})
        print(f"{label:38s} {str(cte):>18} {str(orm):>18}")
    return rows


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def main():
    print(f"scenarios={len(SCENARIOS)} reps={REPS} statement_timeout={STATEMENT_TIMEOUT_MS}ms")
    print(f"postgres={connection.pg_version}\n")

    # PARITY_ONLY=1 skips the (multi-minute) timing sweep and runs just the
    # cheap parameter-parity table.
    if os.environ.get("PARITY_ONLY"):
        print("--- parameter parity (row counts; no timing) ---")
        parity_table()
        return

    results = []
    for scn in SCENARIOS:
        params = scn["params"]
        cte_times, orm_times = [], []
        cte_rows = orm_rows = None
        cte_err = orm_err = None

        for rep in range(REPS):
            # Alternate which implementation runs first so cache warming does
            # not systematically favour one of them.
            order = (("cte", cte_queryset), ("orm", orm_queryset))
            if rep % 2:
                order = tuple(reversed(order))
            for name, build in order:
                secs, rows, err = timed_fetch(build, params)
                if name == "cte":
                    if err is None:
                        cte_times.append(secs)
                        cte_rows = rows
                    cte_err = cte_err or err
                else:
                    if err is None:
                        orm_times.append(secs)
                        orm_rows = rows
                    orm_err = orm_err or err

        cte_ms, cte_plan = explain(cte_queryset, params)
        orm_ms, orm_plan = explain(orm_queryset, params)

        record = {
            "label": scn["label"],
            "note": scn["note"],
            "params": params,
            "cte_wall_s": min(cte_times) if cte_times else None,
            "orm_wall_s": min(orm_times) if orm_times else None,
            "cte_exec_ms": cte_ms,
            "orm_exec_ms": orm_ms,
            "cte_plan": cte_plan,
            "orm_plan": orm_plan,
            "cte_error": cte_err,
            "orm_error": orm_err,
            "diff": compare(cte_rows, orm_rows),
        }
        if record["cte_wall_s"] and record["orm_wall_s"]:
            record["wall_speedup"] = record["orm_wall_s"] / record["cte_wall_s"]
        if cte_ms and orm_ms:
            record["exec_speedup"] = orm_ms / cte_ms
        results.append(record)

        diff = record["diff"]
        flag = "-"
        if diff.get("comparable"):
            flag = "same-genes" if diff["same_gene_set"] else f"+{diff['only_cte']}/-{diff['only_orm']}"
            if diff["column_mismatches"]:
                flag += f" val-diff:{','.join(diff['column_mismatches'])}"
        print(
            f"{record['label']:38s} "
            f"rows={diff.get('cte_rows', '?'):>6}/{diff.get('orm_rows', '?'):<6} "
            f"CTE={_secs(record['cte_wall_s'])} ORM={_secs(record['orm_wall_s'])} "
            f"x{record.get('wall_speedup', float('nan')):.2f}  {flag}"
        )

    print("\n--- parameter parity (row counts; no timing) ---")
    parity = parity_table()

    print("\n===JSON===")
    print(json.dumps({"timings": results, "parity": parity}, indent=1, default=str))


def _secs(value):
    return "  fail" if value is None else f"{value:6.2f}s"


main()
