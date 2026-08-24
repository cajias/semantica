"""Tests for :class:`semantica.context.snapshot.SnapshotService` restore."""

import json

import pytest

from semantica.context.context_graph import ContextGraph
from semantica.context.snapshot import SNAPSHOT_URI_ENV, SnapshotService, SnapshotStore


def _graph_with(node_id: str) -> ContextGraph:
    graph = ContextGraph(advanced_analytics=False)
    graph.add_node(node_id, node_type="concept", content="restored {}".format(node_id))
    return graph


def _written_snapshot(tmp_path, node_id: str = "alpha") -> str:
    path = str(tmp_path / "snapshot.json")
    SnapshotStore(path).save(_graph_with(node_id))
    return path


class TestRestore:
    def test_restores_a_snapshot_that_exists(self, tmp_path):
        """GIVEN a snapshot on disk
        WHEN the service restores into an empty graph
        THEN the graph holds the snapshot's nodes and True is returned.
        """
        path = _written_snapshot(tmp_path)
        graph = ContextGraph(advanced_analytics=False)

        restored = SnapshotService(graph, SnapshotStore(path)).restore()

        assert restored is True
        assert graph.find_node("alpha") is not None

    def test_absent_snapshot_leaves_an_empty_graph(self, tmp_path):
        """GIVEN no snapshot at the destination
        WHEN the service restores
        THEN False is returned and the graph is left empty, not an error.
        """
        graph = ContextGraph(advanced_analytics=False)
        store = SnapshotStore(str(tmp_path / "missing.json"))

        assert SnapshotService(graph, store).restore() is False
        assert graph.nodes == {}

    def test_disabled_service_restores_nothing(self):
        """GIVEN no snapshot URI configured (store is None)
        WHEN the service restores
        THEN it is a no-op returning False -- today's behaviour, unchanged.
        """
        graph = ContextGraph(advanced_analytics=False)

        assert SnapshotService(graph, None).restore() is False

    def test_from_env_is_disabled_without_the_uri(self, monkeypatch):
        """GIVEN SEMANTICA_SNAPSHOT_URI unset
        WHEN a service is built from the environment
        THEN no store is built at all.
        """
        monkeypatch.delenv(SNAPSHOT_URI_ENV, raising=False)
        graph = ContextGraph(advanced_analytics=False)

        service = SnapshotService.from_env(graph)

        assert service._store is None
        assert service.restore() is False

    def test_from_env_with_a_none_graph_is_disabled(self, monkeypatch, tmp_path):
        """GIVEN a caller whose graph failed to build
        WHEN a service is built from the environment with graph=None
        THEN it is disabled rather than raising on restore.
        """
        monkeypatch.setenv(SNAPSHOT_URI_ENV, str(tmp_path / "snapshot.json"))

        service = SnapshotService.from_env(None)

        assert service._store is None
        assert service.restore() is False

    def test_corrupt_snapshot_raises(self, tmp_path):
        """GIVEN a corrupt snapshot payload
        WHEN the service restores
        THEN the error surfaces instead of silently starting empty.
        """
        path = tmp_path / "snapshot.json"
        path.write_text("{not json", encoding="utf-8")
        graph = ContextGraph(advanced_analytics=False)

        with pytest.raises(json.JSONDecodeError):
            SnapshotService(graph, SnapshotStore(str(path))).restore()


class TestMutationSuspension:
    def test_restore_does_not_fire_the_mutation_callback(self, tmp_path):
        """GIVEN a mutation callback installed before a restore
        WHEN the snapshot is restored
        THEN it is not called once per restored entity.
        """
        graph = ContextGraph(advanced_analytics=False)
        source = _graph_with("alpha")
        source.add_node("beta", node_type="concept", content="beta")
        source.add_edge("alpha", "beta", edge_type="relates_to")
        path = str(tmp_path / "snapshot.json")
        SnapshotStore(path).save(source)

        calls = []
        graph.mutation_callback = lambda *args: calls.append(args)

        assert SnapshotService(graph, SnapshotStore(path)).restore() is True
        assert calls == []
        assert len(graph.nodes) == 2

    def test_restore_leaves_the_callback_installed(self, tmp_path):
        """GIVEN a mutation callback installed before a restore
        WHEN the snapshot is restored
        THEN the callback slot still holds it and later edits fire it.
        """
        path = _written_snapshot(tmp_path)
        graph = ContextGraph(advanced_analytics=False)
        calls = []

        def callback(*args):
            calls.append(args)

        graph.mutation_callback = callback

        SnapshotService(graph, SnapshotStore(path)).restore()
        graph.add_node("after", node_type="concept", content="after")

        assert graph.mutation_callback is callback
        assert len(calls) == 1

    def test_suspension_restores_a_previously_suspended_flag(self, tmp_path):
        """GIVEN mutations already suspended by an outer caller
        WHEN a restore suspends and un-suspends them
        THEN the outer suspension is left in place, not cleared.
        """
        path = _written_snapshot(tmp_path)
        graph = ContextGraph(advanced_analytics=False)
        graph._suspend_mutation_callback = True

        SnapshotService(graph, SnapshotStore(path)).restore()

        assert graph._suspend_mutation_callback is True


class TestAfterRestore:
    def test_after_restore_runs_once_on_a_successful_restore(self, tmp_path):
        """GIVEN an after_restore hook
        WHEN a snapshot is restored
        THEN the hook runs exactly once, after the graph is populated.
        """
        path = _written_snapshot(tmp_path)
        graph = ContextGraph(advanced_analytics=False)
        seen = []

        SnapshotService(
            graph,
            SnapshotStore(path),
            after_restore=lambda: seen.append(len(graph.nodes)),
        ).restore()

        assert seen == [1]

    def test_after_restore_is_skipped_when_no_snapshot_exists(self, tmp_path):
        """GIVEN an after_restore hook and no snapshot
        WHEN the service restores
        THEN the hook does not run -- nothing derived needs rebuilding.
        """
        graph = ContextGraph(advanced_analytics=False)
        store = SnapshotStore(str(tmp_path / "missing.json"))
        seen = []

        SnapshotService(graph, store, after_restore=lambda: seen.append(1)).restore()

        assert seen == []
