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

### Extension points: `registry.py` + `methods.py`

16 subpackages carry the pair (`conflicts`, `core`, `deduplication`, `embeddings`, `export`, `graph_store`, `ingest`, `kg`, `normalize`, `ontology`, `parse`, `semantic_extract`, `split`, `triplet_store`, `vector_store`, `visualization`). Each exposes a module singleton `method_registry`, and every dispatcher checks it **first**. Adding a method needs no file edits:

```python
from semantica.semantic_extract.registry import method_registry
method_registry.register("entity", "my_ner", fn)   # method="my_ner" now works
```

Dispatcher behavior is **not** uniform across the 18 `get_*_method` functions — three shapes:

- registry → builtin dict → `raise ValueError`: only the three in `semantic_extract/methods.py` (`get_entity_method:2655`, `get_relation_method:2682`, `get_triplet_method:2711`), which take `(method_name)` with the task baked into the function name.
- registry → builtin dict → `None`: `conflicts/methods.py:472`, `core/methods.py:328`, `deduplication/methods.py:394`.
- registry only (`return method_registry.get(task, name)`) → `None`: the remaining 12 (`embeddings:294`, `export:973`, `graph_store:539`, `ingest:1435`, `kg:543`, `normalize:793`, `ontology:169`, `parse:785`, `split:1676`, `triplet_store:480`, `vector_store:439`, `visualization:530`). No builtin dict at all, and every registry ships with empty task dicts — so these return `None` for *every* name until something registers.

Outside `semantic_extract`, an unknown method name is therefore silently `None`, not an error. Check the return. All 15 non-`semantic_extract` dispatchers take `(task, name)`.

Task keys are per-registry — `core/registry.py:46` (pipeline, knowledge_base, orchestration, lifecycle), `semantic_extract/registry.py:80` (entity, relation, triplet, event, coreference) plus `ProviderRegistry:53`, `export/registry.py:51` (11 keys), `kg/registry.py:50` plus `AlgorithmRegistry:139`.

Two `MethodRegistry` shapes coexist: 12 packages use a class-level `_methods` dict with `@classmethod register(task, name, fn)`; `graph_store`, `triplet_store`, `vector_store` and `visualization` use an instance-level `self._registry` with `register(task, method_name, fn, **metadata)`. Positional calls work on both.

Registration is always explicit — no setuptools `entry_points`, no `__init_subclass__` anywhere. `PluginRegistry` (`core/plugin_registry.py:61`) scans directories via importlib but is inert unless constructed with `plugin_paths=[...]`.

DEAD: `PipelineBuilder.register_step_handler` (`pipeline/pipeline_builder.py:298`) writes `step_registry`, which nothing reads. `ExecutionEngine._execute_step` (`pipeline/execution_engine.py:332`) returns data unchanged when `step.handler` is None (`:382`), so the generic pipeline is a pass-through outside its `delta_mode` branch. Real work happens through per-stage CLI commands and direct module use — `ARCHITECTURE.md`'s diagram is the intended composition, not the executed one.

### Storage is string-dispatched, not inherited

There is no shared base class, ABC, or Protocol for the three stores. Backends are duck-typed and each facade dispatches on a backend-name string through an if/elif chain:

- LPG — `graph_store/graph_store.py:525`, `_initialize_store_backend:564`: `neo4j` | `falkordb` | `neptune`/`amazon_neptune` | `age`/`apache_age`, else `ValidationError` (`:595`)
- RDF lives in `triplet_store/` (triplet, not triple) — `triplet_store.py:40`, `SUPPORTED_BACKENDS:48` is `blazegraph` (default) | `jena` | `rdf4j` | `anzo` | `oxigraph`, dispatch `:96` keys off `self.backend_type`
- Vector — `vector_store/vector_store.py:100`, 8 backends (`:112`), dispatch `_init_backend_store:165`. `filter_by_metadata()` is a per-backend *convention*, independently reimplemented in every store (`faiss_store.py:493`, `qdrant_store.py:545`, `weaviate_store.py:491`, …), not inherited.

Adding a *method* means registering it. Adding a *store backend* is the one place you hand-wire: an `elif` in the facade plus the full method set including `filter_by_metadata`. Only `graph_store` additionally expects a `get_<backend>_config()` (`graph_store/config.py:277`-`:324`); the other two facades read kwargs directly.

### Core abstractions

- `Semantica` facade `core/orchestrator.py:38` — six lazy `@property` accessors that import on first touch (`embedding_generator:118`, `reasoner:137`, `graph_builder:154`, `document_parser:173`, `file_ingestor:192`, `pipeline_builder:211`).
- `ContextGraph` `context/context_graph.py:459` is **in-memory** dicts/lists behind one `threading.RLock` (`:498`), persisted only via `save_to_file:1104` / `load_from_file:1138`. This, not `graph_store`, is what the REST API and Explorer run on.
- `ContextNode:322` / `ContextEdge:365` — bi-temporality (`valid_from`/`valid_until`/`is_active`) lives on the node or edge itself. Edge IDs are content-addressed `uuid5` over canonical JSON (`_default_edge_id:221`, `_resolve_edge_identity:247`); `family_id` groups the temporal versions of one logical edge. `BiTemporalFact` (`kg/temporal_model.py:28`) is the `kg`-side equivalent.
- Decisions in `context/decision_models.py`: `Decision:87`, `Policy:175`, `Precedent:260` — written by `DecisionRecorder`, queried by `DecisionQuery`, gated by `PolicyEngine`, traced by `CausalChainAnalyzer`.
- Provenance `provenance/schemas.py:37` + `ProvenanceManager` `manager.py:64` over `ProvenanceStorage(ABC)` (`storage.py:35`). The ledger is **hash-chained** — `compute_checksum` (`integrity.py:27`) folds in `previous_checksum`. `entity_id` is deliberately excluded from the hash; read the rationale at `integrity.py:45` and do not "fix" it.
- 18 `*_provenance.py` files are PROV-O delegating wrappers with `__getattr__` passthrough. Reference impl `graph_store/graph_store_provenance.py:21`.

TRAP: two clashing `Entity` types — `semantic_extract/types.py:13` (char-offset extraction result) vs `utils/types.py:165` (id-bearing storage shape). Same name, not interchangeable, no adapter. Check which one a signature means.

### Config is two-tier

15 per-module singletons sit at the bottom of each `semantica/*/config.py` (`graph_store_config`, `ingest_config`, …), each with its OWN env prefix (`GRAPH_STORE_*`, `INGEST_*`, …), separate from the global `Config` (`core/config_manager.py:71`) and `ConfigManager` (`:401`). Module internals read their singleton and never see the global config — change a backend default in the right singleton. Global env vars use single underscores (`SEMANTICA_PROCESSING_BATCH_SIZE`); precedence is defaults → dict → kwargs → env.

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

Catch `(ImportError, OSError)`, not just `ImportError` — native wheels fail with `OSError`. A new backend needs the extra, the guard, and inclusion in the relevant `*-all` / `all` aggregate.

`all` (`pyproject.toml:254`) is two `semantica[...]` self-reference strings, so what it covers is easy to misread. Of the 46 other extras, seven are unreachable from it. Two carry comments explaining why (`:249`-`:253`): `gpu` (not cross-platform — install `semantica[gpu]` separately on Linux) and `crewai` (hard-requires `chromadb~=1.1.0`, which carries CVE-2026-45829 with no fixed release, so including it would fail the CI dependency-audit gate). The other five are excluded silently, with no comment: `explorer-lite` (streamlit — `all` takes `explorer` instead) and the whole `db-*` family (`db-snowflake`, `db-databricks`, `db-arrow`, `db-all`); `all` does pull `pyarrow` via `ingest-parquet`/`ingest-arrow`, so only the snowflake/databricks connectors are actually missing.

### Two MCP servers

`semantica/mcp_server/` (single module, `semantica-mcp` entry point) and the standalone modular `mcp/` package (`mcp/server.py` + `mcp/tools/`) are separate implementations of the same surface. They share no code, and only `semantica/mcp_server/` ships in the wheel (`pyproject.toml` includes `semantica*` and `integrations*` only). Behavior changes usually need to land in **both** — a recent fix had to be re-applied to `mcp/` after landing in `semantica/mcp_server/`. Note the root `mcp/` package name shadows the official `mcp` PyPI SDK on `sys.path` from the repo root.

### Server, Explorer auth, routers

`semantica/explorer/` is the FastAPI router package; `explorer/` at the repo root is the frontend. `semantica/static/` is a build artifact absent from a fresh checkout, served by the SPA catch-all at `server.py:198` — no UI until you build.

`require_auth` (`explorer/dependencies.py:46`) compares `X-API-Key` against `SEMANTICA_API_KEY` with `hmac.compare_digest`. A missing key yields **503, not open access**. `SEMANTICA_ALLOW_ANONYMOUS=true` is the dev opt-out, set autouse by `tests/explorer/conftest.py`.

Two FastAPI apps mount the same 11 routers — `server.py:176`-`:186` and `explorer/app.py:180`-`:190`. A router change needs both edits.

### Docs gates (`docs_check.py`, 10 checks)

Scope is `docs/**/*.md` only (`ALL_MD:49`) — markdown outside `docs/`, including this file, is not scanned.

- No Python 3.9+ type syntax in fenced code blocks: write `List[...]`, not `list[...]`
- Canonical repo slug is `semantica-agi/semantica`; `Hawksight-AI/semantica` and `semantica-dev/semantica` are CI-failing strings (`:126`)
- A new top-level module requires editing BOTH `docs/index.md` and the hardcoded 27-module list in `docs_check.py:183`
- Known-wrong class names are banned from reference pages (`:149`): `BaseIngestor`, `BaseExtractor`, `BasePlugin`, `DataNormalizer`, `EntityResolver`, `DeductiveEngine`, `AbductiveEngine`, `GraphMLExporter`, `ArangoExporter` (unless AQL), bare `ReasoningEngine`

### Entry points

`semantica` (CLI), `semantica-server`, `semantica-worker`, `semantica-explorer`, `semantica-mcp`. `semantica doctor` is the install smoke test.

Editor/agent plugin manifests live in `plugins/.<tool>-plugin/`; framework adapters in `integrations/` (`agno`, `crewai`, `openclaw`).

## Conventions

- `tests/` mirrors the package layout one directory per subpackage. Tests must be named `test_*.py` (pre-commit enforces pytest-first naming).
- Mark tests needing network, API keys, or a live backend with `@pytest.mark.integration`.
- Raise from `semantica.utils.exceptions` (`ProcessingError`, `ValidationError`); log via `semantica.utils.logging.get_logger`.
- Conventional Commits (`feat(kg):`, `fix(parse):`). Branches: `feature/*`, `fix/*`.
- Coverage goal: 80% minimum, 90%+ on critical modules.
- `semantic_extract` and `ingest` gate their public exports through `_LAZY_EXPORTS` + a module `__getattr__` (`semantic_extract/__init__.py:56`, `ingest/__init__.py:162`). A new public class must be added to BOTH `_LAZY_EXPORTS` and `__all__` or it is unimportable.
- Version is hand-duplicated in `pyproject.toml:7` and `semantica/__init__.py:13`. Anything that reports a version must derive it from `semantica.__version__` — hardcoded literals caused #863.
- Python >=3.8 in library code: no `X | Y` unions, no `match`. All library code is synchronous; `async def` exists only in `semantica/explorer/*` and `server.py`, and parallelism is threads/processes via `pipeline/parallelism_manager.py`.
- Dataclasses are mutable and mutated in place — that is the dominant convention and there is no functional-update helper anywhere. Match it unless you have a reason not to. Three `frozen=True` exceptions exist, all value objects: `explorer/search_index.py:80`, `kg/temporal_reasoning.py:20`, `reasoning/datalog_reasoner.py:18`.
- Use the existing security helpers: `ingest/ssrf.py`, `graph_store/query_sanitize.py:20` (`sanitize_identifier` for Cypher), `triplet_store/sparql_escaping.py` (SPARQL literals and URIs).
- CHANGELOG is not enforced by any check but is user-visible (published as a docs tab). House style: bolded one-line symptom, then `(#PR, closes #ISSUE) by @author`, then sub-bullets naming exact files and new test files. Add an `[Unreleased]` entry for anything user-visible.

## Known code/doc discrepancies — verify before relying on these

- `semantica init` (`cli.py:952`) writes `~/.semantica/config.yaml`, but nothing auto-loads it: `--config` defaults to None (`cli.py:503`). Pass `--config ~/.semantica/config.yaml` explicitly. `--profile` (`:527`) is declared, bound to a `profile` parameter at `:539`, and then never read — `main()` never puts it on `CLIContext`.
- `provenance/__init__.py:23` documents `NERExtractor(provenance=True)`; that parameter does not exist and lands silently in `**config`. Use `NERExtractorWithProvenance`.
- `GraphStoreWithProvenance.add_node` (`graph_store/graph_store_provenance.py:46`) calls `self._store.add_node`, which `GraphStore` does not define (it has `create_node:626` / `add_nodes:798`) — raises `AttributeError`.
- `semantica/worker.py:34` is a stub: `run()` sleeps in a loop with no queue. `semantica-worker` does nothing.
