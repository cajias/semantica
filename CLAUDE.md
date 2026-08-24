# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Setup (pip + venv, **not** `uv` — see the lockfile note below):

```bash
python -m venv venv && source venv/bin/activate
pip install -e ".[dev]"
pre-commit install          # optional
```

Tests:

```bash
pytest                                   # all
pytest tests/kg/test_graph_builder.py    # one file
pytest tests/kg/test_graph_builder.py::test_name   # one test
pytest -m "not integration"              # skip tests needing external services / API keys
pytest --cov=semantica
```

Lint / format / types (line length 88 everywhere; flake8 ignores E203, W503):

```bash
black semantica/ tests/ && isort semantica/ tests/ && flake8 semantica/ tests/ && mypy semantica/
pre-commit run --all-files
```

`mypy`, `bandit` and `pytest` are deliberately **excluded** from pre-commit for speed — run them yourself.

Knowledge Explorer frontend (`explorer/`, Vite + TypeScript):

```bash
cd explorer && npm ci
npm run dev
npm run build            # emits to ../semantica/static/ — packaged into the wheel
npm run test:graph-store && npm run test:graph-workspace && npm run test:plugin-registry
```

Docs PRs must pass `python docs_check.py` (Mintlify integrity check over `docs/`).

## What CI does and does not check

`.github/workflows/ci.yml` runs the Explorer's three `node --test` suites, builds the frontend, installs `requirements-ci.txt`, builds the wheel, and asserts `semantica/static/index.html` + `semantica/static/assets/` are inside it. **It never runs `pytest`.** Python test breakage is caught only locally — run the suite before pushing.

## requirements-ci.txt

Hash-pinned lockfile (`--generate-hashes`) for CI, security scans, and release builds. Never `pip install` from it into a dev environment. Regenerate only after changing `pyproject.toml` dependencies, with the `uv` version CI pins:

```bash
pip install uv==0.12.1
uv pip compile pyproject.toml --python-version 3.11 --extra all --generate-hashes -o requirements-ci.txt
```

CI re-resolves using the committed file as a constraint and diffs version lines only, so upstream releases never fail the build — staleness appears only when `pyproject.toml` changed. Release builds run `python -m build --no-isolation` against pinned `setuptools==84.0.0` / `wheel==0.48.0`.

## Architecture

Linear pipeline, one subpackage per stage (see `ARCHITECTURE.md` for the full diagram):

`ingest` → `parse` → `normalize` → `split` → `semantic_extract` → `conflicts` → `deduplication` → `kg`

`kg` produces the knowledge graph; the intelligence layer (`ontology`, `reasoning`, `provenance`, `context`) enriches it; `vector_store` / `graph_store` / `triplet_store` persist it; `export` and `visualization` emit artifacts. `core/` (`Semantica`, `Config`, `LifecycleManager`, `PluginRegistry`, `MethodRegistry`) orchestrates; `pipeline/` holds the builder DSL and execution engine.

`semantica.context.ContextGraph` is the decision-intelligence entry point: `record_decision` → `add_causal_relationship` → `trace_decision_chain` / `find_similar_decisions` / `analyze_decision_impact` → `check_decision_rules` → W3C PROV-O export.

### Import convention

`semantica/__init__.py` exports **nothing** eagerly — `__all__ = []`, and a module-level `__getattr__` lazily proxies a fixed list of subpackage names. Always import from the subpackage:

```python
from semantica.context import ContextGraph     # correct
from semantica import ContextGraph             # AttributeError
```

Adding a new subpackage that should be reachable as `semantica.<name>` means adding it to both the `_SemanticaModules` proxy and the `__getattr__` allow-list.

### Optional dependencies

Every backend is an extra in `pyproject.toml` and every import of one is guarded at module scope with a matching availability flag — the module must import cleanly without the dependency and fail only on use:

```python
try:
    import faiss
    FAISS_AVAILABLE = True
except (ImportError, OSError):
    FAISS_AVAILABLE = False
    faiss = None
```

Catch `(ImportError, OSError)`, not just `ImportError` — native wheels fail with `OSError`. A new backend needs the extra, the guard, and inclusion in the relevant `*-all` / `all` aggregate. Two extras are intentionally out of `all`: `gpu` (not cross-platform) and `crewai` (pulls a chromadb advisory that fails the security gate).

### Two MCP servers

`semantica/mcp_server/` (single module, `semantica-mcp` entry point) and the standalone modular `mcp/` package (`mcp/server.py` + `mcp/tools/`) are separate implementations of the same surface. Behavior changes usually need to land in **both** — a recent fix had to be re-applied to `mcp/` after landing in `semantica/mcp_server/`.

### Entry points

`semantica` (CLI), `semantica-server`, `semantica-worker`, `semantica-explorer`, `semantica-mcp`. `semantica doctor` is the install smoke test.

Editor/agent plugin manifests live in `plugins/.<tool>-plugin/`; framework adapters in `integrations/` (`agno`, `crewai`, `openclaw`).

## Conventions

- `tests/` mirrors the package layout one directory per subpackage. Tests must be named `test_*.py` (pre-commit enforces pytest-first naming).
- Mark tests needing network, API keys, or a live backend with `@pytest.mark.integration`.
- Raise from `semantica.utils.exceptions` (`ProcessingError`, `ValidationError`); log via `semantica.utils.logging.get_logger`.
- Conventional Commits (`feat(kg):`, `fix(parse):`). Branches: `feature/*`, `fix/*`.
- Coverage goal: 80% minimum, 90%+ on critical modules.
