"""Snapshot persistence wired into the two FastAPI lifespans.

``semantica/server.py`` and ``semantica/explorer/app.py`` each own a lifespan
and each mounts the same routers, so every snapshot behaviour is asserted
against both apps. The Explorer builds its session before the lifespan runs;
``server.py`` builds it inside, so the ordering differs and the two cannot
share one parametrised client fixture.

``TestClient`` is used as a context manager throughout: without the ``with``,
Starlette never runs the lifespan and the assertions would be hollow.
"""

import importlib
import json

import pytest

from semantica.context.context_graph import ContextGraph
from semantica.context.snapshot import SNAPSHOT_URI_ENV, SnapshotStore
from semantica.explorer.app import create_app
from semantica.explorer.session import GraphSession

try:
    from starlette.testclient import TestClient
except ImportError:  # pragma: no cover - explorer extra not installed
    pytest.skip(
        "starlette TestClient is required for explorer tests. "
        "Install semantica[explorer].",
        allow_module_level=True,
    )


def _snapshot_on_disk(tmp_path, node_id="restored_node"):
    """Write a one-node, one-edge snapshot and return its path."""
    source = ContextGraph(advanced_analytics=False)
    source.add_node(node_id, node_type="concept", content="Restored from snapshot")
    source.add_node("companion", node_type="concept", content="Second entry")
    source.add_edge(node_id, "companion", edge_type="relates_to")
    path = str(tmp_path / "snapshot.json")
    SnapshotStore(path).save(source)
    return path


def _explorer_app():
    return create_app(session=GraphSession(ContextGraph(advanced_analytics=False)))


def _server_app():
    """A freshly imported ``semantica.server`` app.

    The module builds its app and its lifespan at import time, so it is
    reloaded per test to pick up the monkeypatched environment.
    """
    import semantica.server as server_module

    return importlib.reload(server_module).app


class TestExplorerRestore:
    def test_restored_nodes_and_edges_are_served(self, tmp_path, monkeypatch):
        """GIVEN a snapshot on disk
        WHEN the Explorer app starts
        THEN /api/graph/nodes and /api/graph/edges return its content.
        """
        monkeypatch.setenv(SNAPSHOT_URI_ENV, _snapshot_on_disk(tmp_path))

        with TestClient(_explorer_app()) as client:
            nodes = client.get("/api/graph/nodes").json()
            edges = client.get("/api/graph/edges").json()

        assert {node["id"] for node in nodes["nodes"]} == {
            "restored_node",
            "companion",
        }
        assert edges["total"] == 1

    def test_restored_nodes_are_searchable(self, tmp_path, monkeypatch):
        """GIVEN a snapshot on disk
        WHEN the Explorer app starts
        THEN POST /api/graph/search finds a restored node.

        GraphSession builds its search index in __init__ from an empty graph,
        and the restore runs with mutation notifications suspended, so without
        an explicit RELOAD_GRAPH the restored nodes are invisible to search
        while /api/graph/nodes happily lists them.
        """
        monkeypatch.setenv(SNAPSHOT_URI_ENV, _snapshot_on_disk(tmp_path))

        with TestClient(_explorer_app()) as client:
            results = client.post(
                "/api/graph/search", json={"query": "restored_node"}
            ).json()

        assert [item["node"]["id"] for item in results["results"]] == ["restored_node"]

    def test_no_snapshot_serves_an_empty_graph(self, tmp_path, monkeypatch):
        """GIVEN a configured destination with no snapshot yet
        WHEN the Explorer app starts
        THEN it serves an empty graph and /api/health is ok.
        """
        monkeypatch.setenv(SNAPSHOT_URI_ENV, str(tmp_path / "absent.json"))

        with TestClient(_explorer_app()) as client:
            assert client.get("/api/health").json() == {"status": "ok"}
            assert client.get("/api/graph/nodes").json()["total"] == 0

    def test_unset_uri_restores_nothing(self, tmp_path, monkeypatch):
        """GIVEN SEMANTICA_SNAPSHOT_URI unset
        WHEN the Explorer app starts
        THEN nothing is restored even though a snapshot file exists.
        """
        _snapshot_on_disk(tmp_path)
        monkeypatch.delenv(SNAPSHOT_URI_ENV, raising=False)

        with TestClient(_explorer_app()) as client:
            assert client.get("/api/graph/nodes").json()["total"] == 0

    def test_corrupt_snapshot_fails_start_up(self, tmp_path, monkeypatch):
        """GIVEN a corrupt snapshot
        WHEN the Explorer app starts
        THEN start-up raises instead of quietly serving an empty graph.
        """
        path = tmp_path / "snapshot.json"
        path.write_text("{truncated", encoding="utf-8")
        monkeypatch.setenv(SNAPSHOT_URI_ENV, str(path))

        with pytest.raises(json.JSONDecodeError):
            with TestClient(_explorer_app()):
                pass

    def test_restore_does_not_clear_the_mutation_bridge(self, tmp_path, monkeypatch):
        """GIVEN the Explorer's mutation bridge installed at start-up
        WHEN a snapshot has been restored
        THEN a later edit still reaches the session's search index.
        """
        monkeypatch.setenv(SNAPSHOT_URI_ENV, _snapshot_on_disk(tmp_path))
        app = _explorer_app()

        with TestClient(app) as client:
            app.state.session.graph.add_node(
                "liveedit", node_type="concept", content="Added while running"
            )
            results = client.post(
                "/api/graph/search", json={"query": "liveedit"}
            ).json()

        assert [item["node"]["id"] for item in results["results"]] == ["liveedit"]


class TestServerRestore:
    def test_restored_nodes_are_served_and_searchable(self, tmp_path, monkeypatch):
        """GIVEN a snapshot on disk
        WHEN semantica.server starts
        THEN its graph routes and search both see the restored content.
        """
        monkeypatch.setenv(SNAPSHOT_URI_ENV, _snapshot_on_disk(tmp_path))

        with TestClient(_server_app()) as client:
            nodes = client.get("/api/graph/nodes").json()
            results = client.post(
                "/api/graph/search", json={"query": "restored_node"}
            ).json()

        assert {node["id"] for node in nodes["nodes"]} == {
            "restored_node",
            "companion",
        }
        assert [item["node"]["id"] for item in results["results"]] == ["restored_node"]

    def test_no_snapshot_serves_an_empty_graph(self, tmp_path, monkeypatch):
        """GIVEN a configured destination with no snapshot yet
        WHEN semantica.server starts
        THEN /health is ok and the graph is empty.
        """
        monkeypatch.setenv(SNAPSHOT_URI_ENV, str(tmp_path / "absent.json"))

        with TestClient(_server_app()) as client:
            assert client.get("/health").json() == {"status": "healthy"}
            assert client.get("/api/graph/nodes").json()["total"] == 0

    def test_unset_uri_restores_nothing(self, tmp_path, monkeypatch):
        """GIVEN SEMANTICA_SNAPSHOT_URI unset
        WHEN semantica.server starts
        THEN nothing is restored.
        """
        _snapshot_on_disk(tmp_path)
        monkeypatch.delenv(SNAPSHOT_URI_ENV, raising=False)

        with TestClient(_server_app()) as client:
            assert client.get("/api/graph/nodes").json()["total"] == 0

    def test_corrupt_snapshot_fails_start_up(self, tmp_path, monkeypatch):
        """GIVEN a corrupt snapshot
        WHEN semantica.server starts
        THEN start-up raises instead of quietly serving an empty graph.
        """
        path = tmp_path / "snapshot.json"
        path.write_text("{truncated", encoding="utf-8")
        monkeypatch.setenv(SNAPSHOT_URI_ENV, str(path))

        with pytest.raises(json.JSONDecodeError):
            with TestClient(_server_app()):
                pass
