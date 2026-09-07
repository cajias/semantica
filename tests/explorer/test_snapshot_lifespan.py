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
import os
import threading

import pytest

from semantica.context import snapshot as snapshot_module
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


def _writer_threads():
    """Live snapshot writer threads, by the name ``start()`` gives them."""
    return [
        thread
        for thread in threading.enumerate()
        if thread.name == "semantica-snapshot-writer" and thread.is_alive()
    ]


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

    def test_the_snapshot_hook_does_not_disable_index_maintenance(
        self, tmp_path, monkeypatch
    ):
        """GIVEN semantica.server running with snapshots enabled
        WHEN a node is written through the session after start-up
        THEN search finds it and the graph revision has advanced.

        ``SnapshotService.start()`` installs its dirty hook into
        ``ContextGraph.mutation_callback``, and GraphSession reads an occupied
        slot as "someone else bumps the revision and maintains the search
        index" (``session.py`` ``add_nodes`` and friends). Unless the lifespan
        installs the session's own handler *first*, so the snapshot hook chains
        onto it, nobody does that work: writes stay invisible to
        /api/graph/search and the stale embedding cache is never invalidated.
        """
        monkeypatch.setenv(SNAPSHOT_URI_ENV, str(tmp_path / "snapshot.json"))
        app = _server_app()

        with TestClient(app) as client:
            session = app.state.session
            revision_before = session._graph_revision
            session.add_nodes(
                [
                    {
                        "id": "liveedit",
                        "type": "concept",
                        "content": "Added while running",
                    }
                ]
            )
            revision_after = session._graph_revision
            results = client.post(
                "/api/graph/search", json={"query": "liveedit"}
            ).json()

        assert [item["node"]["id"] for item in results["results"]] == ["liveedit"]
        assert revision_after > revision_before, (
            "the graph revision never advanced, so get_cached_embeddings keeps "
            "serving vectors from before the write"
        )


def _node_ids(path):
    """Node ids in the snapshot at *path*."""
    graph = ContextGraph(advanced_analytics=False)
    graph.load_from_file(path)
    return set(graph.nodes)


class TestShutdownSnapshot:
    """A redeployment must be lossless even inside one interval.

    The interval is patched far beyond the life of each test, so any snapshot
    these assert on can only have come from the shutdown path.
    """

    @pytest.fixture(autouse=True)
    def _never_tick(self, monkeypatch):
        monkeypatch.setattr(snapshot_module, "snapshot_interval_from_env", lambda: 3600)

    def test_explorer_writes_edits_made_after_the_last_tick(
        self, tmp_path, monkeypatch
    ):
        """GIVEN edits and no interval tick
        WHEN the Explorer app shuts down
        THEN the snapshot on disk holds them.
        """
        path = str(tmp_path / "snapshot.json")
        monkeypatch.setenv(SNAPSHOT_URI_ENV, path)
        app = _explorer_app()

        with TestClient(app):
            app.state.session.graph.add_node(
                "shutdown_node", node_type="concept", content="Added while running"
            )

        assert _node_ids(path) == {"shutdown_node"}

    def test_server_writes_edits_made_after_the_last_tick(self, tmp_path, monkeypatch):
        """GIVEN edits and no interval tick
        WHEN semantica.server shuts down
        THEN the snapshot on disk holds them.
        """
        path = str(tmp_path / "snapshot.json")
        monkeypatch.setenv(SNAPSHOT_URI_ENV, path)
        app = _server_app()

        with TestClient(app):
            app.state.session.graph.add_node(
                "shutdown_node", node_type="concept", content="Added while running"
            )

        assert _node_ids(path) == {"shutdown_node"}

    def test_a_clean_explorer_graph_is_not_rewritten(self, tmp_path, monkeypatch):
        """GIVEN a restored graph nothing edited
        WHEN the Explorer app shuts down
        THEN the snapshot is left untouched rather than redundantly rewritten.
        """
        path = _snapshot_on_disk(tmp_path)
        monkeypatch.setenv(SNAPSHOT_URI_ENV, path)
        before = os.stat(path).st_mtime_ns

        with TestClient(_explorer_app()) as client:
            assert client.get("/api/graph/nodes").json()["total"] == 2

        assert os.stat(path).st_mtime_ns == before

    def test_a_clean_server_graph_is_not_rewritten(self, tmp_path, monkeypatch):
        """GIVEN a restored graph nothing edited
        WHEN semantica.server shuts down
        THEN the snapshot is left untouched rather than redundantly rewritten.
        """
        path = _snapshot_on_disk(tmp_path)
        monkeypatch.setenv(SNAPSHOT_URI_ENV, path)
        before = os.stat(path).st_mtime_ns

        with TestClient(_server_app()) as client:
            assert client.get("/api/graph/nodes").json()["total"] == 2

        assert os.stat(path).st_mtime_ns == before

    def test_the_writer_thread_is_stopped_on_explorer_shutdown(
        self, tmp_path, monkeypatch
    ):
        """GIVEN the Explorer app running with an interval writer
        WHEN it shuts down
        THEN no snapshot writer thread is left running.
        """
        monkeypatch.setenv(SNAPSHOT_URI_ENV, str(tmp_path / "snapshot.json"))

        with TestClient(_explorer_app()):
            assert _writer_threads(), "expected a writer thread while running"

        assert _writer_threads() == []

    def test_the_writer_thread_is_stopped_on_server_shutdown(
        self, tmp_path, monkeypatch
    ):
        """GIVEN semantica.server running with an interval writer
        WHEN it shuts down
        THEN no snapshot writer thread is left running.
        """
        monkeypatch.setenv(SNAPSHOT_URI_ENV, str(tmp_path / "snapshot.json"))

        with TestClient(_server_app()):
            assert _writer_threads(), "expected a writer thread while running"

        assert _writer_threads() == []

    def test_a_failing_final_write_does_not_break_explorer_shutdown(
        self, tmp_path, monkeypatch, caplog
    ):
        """GIVEN a store whose save raises
        WHEN the Explorer app shuts down with a dirty graph
        THEN shutdown completes and the failure is logged at ERROR.

        The save is made to raise rather than the destination made unwritable:
        chmod is no obstacle to a process holding CAP_DAC_OVERRIDE, so under
        root -- as in most CI images and Docker-based dev -- the write would
        quietly succeed and this test would fail for having nothing to log.
        """

        def _failing_save(self, graph):
            raise OSError("no space left on device")

        monkeypatch.setenv(SNAPSHOT_URI_ENV, str(tmp_path / "snapshot.json"))
        monkeypatch.setattr(SnapshotStore, "save", _failing_save)
        app = _explorer_app()

        with caplog.at_level("ERROR"):
            with TestClient(app):
                app.state.session.graph.add_node(
                    "doomed", node_type="concept", content="Never stored"
                )

        assert "snapshot to" in caplog.text
