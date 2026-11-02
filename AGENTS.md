# Repository Guidelines

## Project Structure & Module Organization

`main.py` assembles the application and explicitly registers modules and event
handlers. `core/db.py` owns all LadybugDB queries; other `core/` services coordinate
atomic actions, history, logging, and context. Gameplay modules live in
`modules/{world,social,diplomacy,demo}` and export public interfaces through
`__init__.py`. Streamlit pages are rendered by `ui/app.py`; `ui/graph.py` builds
Plotly figures. Authoring data belongs in `content/`, saves in `data/`, and
rotating logs in `logs/`. Tests live in `tests/`, developer scripts in `scripts/`.

## Build, Test, and Development Commands

Run commands from the repository root on Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python scripts/seed_world.py
.\.venv\Scripts\python -m streamlit run main.py
.\.venv\Scripts\python -m pytest -q
```

Seed initializes a new world without resetting an existing save. Use
`scripts/seed_world.py --new --db data/campaign2.lbug` for another campaign.
Stop Streamlit before opening that save with `scripts/visualize_db.py`.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions and variables, and `PascalCase`
classes. Write comments in Russian. Prefer explicit, readable functions. No
formatter or linter is currently configured. Keep Cypher and driver objects
inside `core/db.py`; parameterize query values. Import gameplay modules through
their public packages, never their internal `logic.py` files.

## Testing Guidelines

Use pytest with temporary real LadybugDB databases. Name files `test_*.py` and
functions `test_*`. Exercise persistence, transaction rollback, immutable history,
ownership, event ordering, seed idempotency, bounded context, and graph filters.
Streamlit AppTest covers navigation and explicit actions. No coverage percentage
is mandated.

## Commit & Pull Request Guidelines

This workspace has no Git history or established commit convention. Use concise,
imperative subjects describing the change. PRs should explain user-visible
behavior, include test results, and attach screenshots for interface changes.
Describe schema changes and save compatibility explicitly.

## Content and Save Safety

Preserve `noweria.md` as canon. Mark invented content and MVP parameters clearly.
Never rewrite authoring JSON during gameplay. Route state changes through
`WorldService` so state and historical events commit together.
