# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

The Biodiversity Cell Atlas website and data portal. Two user-facing surfaces served behind an Nginx reverse proxy:

- **Project website** (`http://localhost`): a [Ghost](https://ghost.org) CMS instance. Base theme overrides live in `ghost/` (templates mounted into the container). Not a Django concern.
- **Data Portal** (`http://portal.localhost`): the Django app. This is where nearly all code work happens.

The whole stack runs as Podman/Docker Compose services (`compose.yml` + `compose.prod.yml`). There is no local virtualenv; **all commands run inside containers** via `podman compose exec web ...`.

## Common commands

Everything runs against the `web` service container. `podman compose` and `docker compose` are interchangeable (CI uses `docker compose`).

```bash
# Bring the stack up (build after dependency changes)
podman compose up -d --build

# Shells
podman compose exec web bash
podman compose exec web python manage.py shell

# Tests: the source of truth is pytest (see [tool.pytest] in pyproject.toml).
# testpaths = app/tests, rest/tests
podman compose exec web pytest                          # all Python tests
podman compose exec web pytest app/tests/test_x.py      # single file
podman compose exec web pytest app/tests/test_x.py::TestClass::test_name   # single test
podman compose exec web coverage run -m pytest && podman compose exec web coverage html

# End-to-end tests (Playwright) live in e2e/, separate from unit tests
podman compose exec web pytest e2e/ -v

# TypeScript tests (Bun, happy-dom); test files under app/static/app/src/**/tests/
podman compose exec web bun test
podman compose exec web bun test --watch

# Deployment sanity check (README calls this "check-deploy"; the real command is:)
podman compose exec web python manage.py check --deploy

# Django template lint
podman compose exec web djlint .            # check
podman compose exec web djlint . --reformat # fix

# Super-Linter (runs in CI on every PR); replicate locally:
./superlinter.sh check                      # changed files
./superlinter.sh fix --python --js          # fix, specific linters
./superlinter.sh fix --all                  # whole codebase

# Pre-commit hooks (run with prek: https://github.com/j178/prek)
prek install                                # install the git pre-commit hook (one-time)
prek run --all-files                        # run all hooks across the repo
prek autoupdate                             # bump pinned hook revisions
```

Note: `README.md` shows `python manage.py test`, but the configured/CI test runner is **pytest**. Prefer pytest.

### Pre-commit hooks (prek)

`.pre-commit-config.yaml` provides a fast local pre-flight that mirrors the Python/formatting subset of the CI Super-Linter (ruff-format, a conservative `ruff-check --select=E,F`, djlint, codespell, editorconfig-checker, prettier for CSS/TS, plus basic hygiene incl. merge-conflict markers). It is meant to be run with [`prek`](https://github.com/j178/prek), a drop-in `pre-commit` replacement; `pre-commit` also works. Run `prek install` to enable it on every commit.

This is a local convenience, **not** a replacement for CI — Super-Linter remains the source of truth and runs the full ruff ruleset plus ESLint, Stylelint, gitleaks, checkov, jscpd, etc. (deliberately not mirrored locally, since they need the repo's Node plugin set — ESLint/Stylelint are not in `package.json` — or non-Python runtimes). `ruff-check` is scoped to `E,F` because bare ruff's defaults flag rules (import sorting, bugbear, simplify) that Super-Linter does not enforce, which would churn otherwise-accepted code. This config lives on the `local-meta` branch only.

### Local-only files (kept off upstream)

`compose.gambusia.yml`, `.pre-commit-config.yaml`, and `CLAUDE.md` are local conveniences that live on the `local`/`local-meta` branches (pushed only to the `origin` = biocorecrg fork) and must **never** reach the `upstream` (biodiversitycellatlas) repo. `.gitignore` can't express this — it is itself committed and pushed, and cannot ignore files already tracked on `local-meta`. Two untracked, per-clone guards enforce it instead (both stored in the shared git dir, so they are never committed and never pushed):

- **`.git/info/exclude`** lists the three files, so `git add` won't stage them on any branch/worktree where they aren't already tracked (i.e. every upstream-destined branch). No effect on `local`/`local-meta`, where they're intentionally tracked.
- **`.git/hooks/pre-push`** aborts a push whose ref tree contains any of the three files, but only when the target remote matches `biodiversitycellatlas`/`upstream`. Pushes to `origin` and clean upstream branches (e.g. `main`) pass.

Caveats: these guards exist only in this clone — recreate them on a fresh clone (they can't be committed by design); `git push --no-verify` bypasses the hook (the `info/exclude` layer still holds); and because this is a git worktree, the sibling `local` worktree shares the same exclude + hook (harmless, since those files are legitimately tracked there).

### Static assets (Bun)

Frontend JS/CSS is built by Bun, not Django. `entrypoint.sh` runs `bun install && bun run build` then `collectstatic` on container start. After editing TypeScript or adding JS/CSS deps, rebuild:

```bash
podman compose exec web bun run build          # build:portal + build:rest (see package.json scripts)
podman compose exec web python manage.py collectstatic --noinput
```

`build:portal` bundles every file under `app/static/app/src/` (except `tests/`) into `app/static/app/dist/` with code splitting. Static files use `config.storage.JSModuleManifestStorage` (hashed, manifest-based) — real-time volume mounts do **not** cover static files or model changes; rebuild/restart for those.

## Architecture

### Django project layout

- `config/` — project settings, URLs, WSGI/ASGI. `config/settings.py` reads env via helpers in `config/pre_settings.py` (`get_env`, plus `get_latest_git_tag` / `get_diamond_version` for version strings). `config/context_processors.py` exposes `BCA_*` globals to templates.
- `app/` — the Data Portal proper: models, class-based views, templates, template tags.
- `rest/` — the REST API (Django REST Framework), served under `/api/v1/`.

Root URLs (`config/urls.py`): `""` → `app.urls`, `api/v1/` → `rest.urls`, `api/` redirects to the API index, and `django_prometheus` metrics are mounted at root.

### Data model (`app/models.py`, ~34 classes)

The domain is single-cell atlas data. Core entities and their rough hierarchy:

- `Species` → `Dataset` (a species can have multiple datasets). Datasets carry `QualityControl` metrics and files.
- `Gene` (per species), grouped into `GeneModule`s and user-facing `GeneList`s; `Domain` and `Orthogroup`/ortholog relations connect genes across species.
- `Metacell` / `MetacellType` / `MetacellCount` / `MetacellLink` and `SingleCell` hold the expression data; gene expression is stored per metacell and per single cell.
- File models: `FileMixin` → `GlobalFile`, `SpeciesFile`, `DatasetFile`.

Reusable mixins at the top of the file define shared behavior: `AutoSlugMixin`/`DynamicSlugMixin` (slug generation), `ImageSourceMixin`, `HtmlLinkMixin` (canonical link building), and `ExternalQueryMixin` (links out to external DBs via a `Source.query_url` template).

### Views layer (`app/views/`)

Class-based views split by concern: `views.py` (home, downloads, docs, search, health, errors), `atlas_views.py` (the `atlas/<dataset>/...` explorer pages), `entry_views.py` (`entry/...` database browse/detail pages). URL names are referenced throughout templates — see `app/urls.py` for the full route map.

### REST API (`rest/`)

DRF with a `DefaultRouter` (`rest/routers.py`) registering ~25 viewsets. Notable pieces:

- `serializers.py` and `views.py` are large and hold most API logic; `filters.py` defines django-filter filtersets (extensive query params).
- Custom renderers (`renderers.py`): JSON via `drf_orjson_renderer`, plus **CSV and TSV** renderers — the API supports tabular downloads.
- `pagination.py` (`StandardPagination`), `aggregates.py`, custom `schema.py`.
- OpenAPI docs via `drf-spectacular` (`SPECTACULAR_SETTINGS` in `config/settings.py`; tag order from `rest.settings.sort_api_tags`).
- Compute-heavy endpoints delegate to `rest/services/`: `go_enrichment.py` (GO term enrichment via GOATOOLS + scikit-learn) and `module_similarity.py`. Sequence alignment uses DIAMOND (the `align` viewset), version detected at settings load.

Some `app` views fetch data through the REST layer / shared query mixins rather than duplicating ORM queries.

### Databases and environments

- PostgreSQL. Connection is normally via a **pg service name** (`POSTGRES_SERVICE` in `.env`, resolved through `.pg_service.conf` / `.pgpass`), which lets you point at the bundled `db` container or any remote/SSH-tunneled database. Tests fall back to explicit `POSTGRES_*` env vars because Django doesn't support pg services under test.
- `ENVIRONMENT=prod` flips security settings on, disables `DEBUG`, and serves via Gunicorn; otherwise `entrypoint.sh` runs migrations + `runserver` and enables `django-debug-toolbar`.
- Compose profiles (`COMPOSE_PROFILES` in `.env`) toggle the `nginx` and `db` services — drop `db` when using an external database.

### System checks and monitoring

Custom Django system checks in `app/systemchecks/` (`files.py`, `metacellgenexpression.py`, `postgresql_tables.py`) validate data/DB integrity and run with `manage.py check`. `django-prometheus` wraps the DB backend and middleware for metrics.

### Test data and data loading

- `app/management/commands/createtestdb.py` builds a synthetic database (Faker-based) used by the e2e/Playwright suite. `factories.py` holds factory_boy factories.
- Real data ingestion scripts live in `scripts/data/` (e.g. `add_data_to_db.py`, `add_gene_modules.py`, `add_SAMAP_to_db.py`), run inside the container and depend on the `dataload` optional deps (pandas, rds2py, scipy). These are operational scripts, not part of the request path.

## Conventions

- Python deps are pinned in `pyproject.toml` under `dependencies` and the `dev`/`test`/`e2e`/`dataload` optional groups. Requires Python 3.13+.
- Linting is enforced in CI by Super-Linter; its per-linter config lives in `.github/linters/` and repo-root dotfiles (`.pylintrc`, `.stylelintrc.json`, `.editorconfig-checker.json` are symlinks into there). Django templates follow the `.djlintrc` `django` profile.
- CI (`.github/workflows/tests.yml`) also runs `makemigrations --check` — commit migrations; a PR with un-generated migrations fails.
