"""Suite-wide fixtures.

Anything that must hold for every test directory lives here — per-directory
conftests cover only their own subtree, which is not enough for state read at
FastAPI lifespan startup.
"""

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolate_snapshot_env():
    """Keep an exported SEMANTICA_SNAPSHOT_URI out of the whole suite.

    Both FastAPI lifespans call ``SnapshotService.from_env``, so an exported
    URI makes every TestClient restore from — and snapshot to — one shared
    real file: state bleeds across tests and the developer's graph gets
    clobbered. Any test that enters a lifespan is exposed, not just the ones
    under tests/explorer/, so the guard belongs at the suite root. Session
    scope is required because some clients are module-scoped and their
    lifespan runs before any function-scoped fixture;
    ``pytest.MonkeyPatch`` because the ``monkeypatch`` fixture is
    function-scoped. Snapshot tests that need the vars set them per-test.
    """
    mp = pytest.MonkeyPatch()
    mp.delenv("SEMANTICA_SNAPSHOT_URI", raising=False)
    mp.delenv("SEMANTICA_SNAPSHOT_INTERVAL", raising=False)
    yield
    mp.undo()
