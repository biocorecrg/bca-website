# CLAUDE.md - benchmarks/

Ad-hoc performance and correctness benchmarks for the Data Portal. Written to
answer a specific question at a specific moment, kept because the numbers are
worth being able to re-check.

**This whole directory is tracked only on the `local` branch and pushed only to
`origin` (the biocorecrg fork). It must never reach `upstream`
(biodiversitycellatlas) or `main`.** See [Kept off upstream](#kept-off-upstream)
below for the two guards that enforce that, and for what to recreate on a fresh
clone.

Nothing here is part of the request path, the test suite, or CI. `pytest` does
not collect it (`testpaths = app/tests, rest/tests`), and the scripts are not
imported by application code.

## Layout

```
benchmarks/
├── CLAUDE.md                       # this file
├── scripts/                        # the harnesses, no date suffix
└── reports/                        # written results, one file per run, date-suffixed
```

- **`scripts/`** holds the harness for each benchmark. A script is a living
  thing: edit it in place, add scenarios, generalise it. No date in the name.
- **`reports/`** holds the plain-text write-up of a *particular run*, with the
  date that run was performed appended as `-YYMMDD`, e.g.
  `cte-vs-orm-api-260729.txt` for a run on 2026-07-29. Reports are immutable
  once written: re-running a benchmark produces a *new* dated file rather than
  overwriting the old one, so a later run can be compared against an earlier
  one. Each report also carries its own `Date:` header, which must agree with
  the suffix.

## What is here

### `scripts/bench_marker_orm_vs_cte.py`

Compares the two implementations of the markers query **inside one database**,
so the only variable is the query itself:

- **CTE**: the raw SQL in `rest.views.MetacellMarkerViewSet._MARKER_SQL`, driven
  through the real `get_queryset()`.
- **ORM**: the django-filter annotation pipeline it replaced, which still lives
  in `rest.filters.MetacellMarkerFilter`.

Neither side is reconstructed by the harness; both are the shipped code. 16
scenarios, 2 runs per side with the execution order alternated, minimum
reported, plus `EXPLAIN ANALYZE` server-side times and a parameter-parity table.

```bash
docker exec -i bca-web-1 python manage.py shell < benchmarks/scripts/bench_marker_orm_vs_cte.py

# parity table only, skips the multi-minute timing sweep
docker exec -i -e PARITY_ONLY=1 bca-web-1 python manage.py shell \
    < benchmarks/scripts/bench_marker_orm_vs_cte.py
```

Runs inside the `web` container (needs Django). Read-only: SELECTs inside
transactions that are rolled back, with a 300s statement timeout per run.

### `scripts/bench_marker_api_cte_vs_orm.py`

The HTTP-level counterpart: the same 16 scenarios issued as real requests
against **two running deployments**, one on the CTE rewrite and one still on the
ORM pipeline. Verifies that the JSON payloads agree field by field, and times
each side.

Both hosts come from the environment, so one script covers every pairing:

```bash
# local container vs production (the defaults)
python3 benchmarks/scripts/bench_marker_api_cte_vs_orm.py

# staging vs production
CTE_BASE=https://bca-dev.hpc.crg.es CTE_LABEL=staging ORM_LABEL=prod \
    python3 benchmarks/scripts/bench_marker_api_cte_vs_orm.py

# parity table only, 5 requests per host
PARITY_ONLY=1 python3 benchmarks/scripts/bench_marker_api_cte_vs_orm.py
```

Runs on the **host**, not in the container: stdlib only, no Django import.

Two things to be deliberate about before running it:

1. **`ORM_BASE` defaults to the public production site.** A bare run with no
   environment variables set will hit `portal.biodiversitycellatlas.org`. That
   is intentional, since production is the ORM reference, but it means the
   default is not a purely local benchmark.
2. **Be a considerate client.** Requests are sequential with a 1s delay
   (`ORM_DELAY_S`) against the ORM host. A full run is 37 read-only GETs per
   host. Do not remove the delay or parallelise the requests.

### `reports/`

| report | run | what it establishes |
| --- | --- | --- |
| `cte-vs-orm-260729.txt` | CTE vs ORM in one database, local container | The rewrite is 2.2x to 5.3x faster (median 3.1x), server-side 2.5x to 5.4x, with identical output on all seven annotation columns. Explains why from the plans: the ORM adds three Nested Loop joins whose Memoize nodes run once per expression row. |
| `cte-vs-orm-api-260729.txt` | local container (CTE) vs production (ORM), over HTTP | Same content on both, but the JSON array order changed: fold-change descending instead of gene primary key ascending, in 13 of 16 scenarios. |
| `cte-vs-orm-api-staging-260729.txt` | staging (CTE) vs production (ORM), over HTTP | Confirms the above between two deployed sites, and finds `fg_mean_fc` drifting by one to two ULP (max 5.6e-16 relative) in 9 of 16 scenarios, from floating-point summation order in `AVG()`. |

All three also document two parameter-parity differences the rewrite
introduced: `fc_max_bg`/`fc_max_bg_type` are silently ignored, and
`fc_min_type` became optional.

## Kept off upstream

`.gitignore` cannot express this, because `.gitignore` is itself committed and
pushed, and cannot ignore files already tracked on `local`. Two untracked,
per-clone guards do it instead. Both live in the shared git dir
(`/data/remote-work/bio/bca-website/.git`), so they are never committed and
never pushed — see the parent
[CLAUDE.md](../CLAUDE.md#local-only-files-kept-off-upstream) for the full
mechanism shared with the other local-only paths:

1. **`.git/info/exclude`** lists `benchmarks/`, so `git add benchmarks/` is
   refused on every branch/worktree other than `local`, where it's already
   tracked.
2. **`.git/hooks/pre-push`** lists `benchmarks` in its `forbidden` set and
   aborts any push whose ref tree contains it, but only when the target remote
   matches `biodiversitycellatlas`/`upstream`. Note that the hook's bare
   `CLAUDE.md` pathspec matches only the *root* `CLAUDE.md`; this file is caught
   by the `benchmarks` entry, which is why that entry is listed in its own
   right.

Verify both still work:

```bash
git check-ignore -v benchmarks/CLAUDE.md      # -> .git/info/exclude:NN:benchmarks/ (outside `local`)

# exercise the hook without touching history (expect BLOCKED, exit 1)
printf 'refs/heads/local %s refs/heads/x 0000000000000000000000000000000000000000\n' "$(git rev-parse HEAD)" \
    | "$(git rev-parse --git-common-dir)/hooks/pre-push" upstream git@github.com:biodiversitycellatlas/bca-website.git
```

Caveats, all inherited from the mechanism:

- **The guards exist only in this clone.** They cannot be committed by design,
  so a fresh clone has neither, and this directory does not exist there either
  unless it checks out `local`. Recreate both guards before recreating any
  benchmark.
- **`git push --no-verify` bypasses the hook.** The `info/exclude` layer still
  holds, so the files cannot get into a commit on any other branch in the
  first place.
- **This is a git worktree**, so sibling worktrees on other branches share the
  same exclude file and hook.

## Adding a benchmark

- Put the harness in `scripts/`, the write-up in `reports/` with a `-YYMMDD`
  suffix. Do not overwrite an existing report.
- Drive the shipped code, do not reimplement it. The credibility of
  `bench_marker_orm_vs_cte.py` rests entirely on the fact that both sides are
  real code paths, one through `get_queryset()` and one through the filterset.
- Alternate the execution order between repetitions and report the minimum, so
  buffer-cache warming does not systematically favour one side.
- Keep it read-only. Wrap DB work in a transaction that rolls back, set a
  statement timeout, and only issue GETs over HTTP.
- Say in the report what the measurement *is not*. Both API reports carry an
  explicit warning that comparing two different hosts is not a controlled A/B;
  that warning is the reason the numbers can be trusted for what they do show.
- Report what was measured, including the parts that came out unexpectedly or
  disproved the hypothesis. The ULP drift in the staging report is an example:
  it was not the question being asked, and it belongs in the write-up anyway.
- The scripts are ordinary Python and the prek hooks apply to them:

  ```bash
  prek run --files benchmarks/scripts/*.py
  ```

  Reports are plain text; `codespell` and `editorconfig-checker` will look at
  them, so run the hooks over new reports too.
