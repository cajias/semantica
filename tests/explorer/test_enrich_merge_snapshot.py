"""POST /api/enrich/merge must announce the node it deletes.

The merge route collapses a duplicate by direct in-place mutation of
``graph.nodes`` / ``graph.edges``, bypassing every ContextGraph write method.
``SnapshotService`` learns about changes only through
``ContextGraph.mutation_callback``, so a merge that re-points no edge used to
leave the writer clean: the deletion was never persisted and the next process
start restored the duplicate from the stale snapshot.
"""

from typing import List, Tuple

import pytest

from semantica.context.context_graph import ContextGraph
from semantica.context.snapshot import SnapshotService, SnapshotStore
from semantica.explorer.app import create_app
from semantica.explorer.session import GraphSession

try:
    from starlette.testclient import TestClient
except ImportError:
    pytest.skip(
        "starlette TestClient is required for explorer tests. "
        "Install semantica[explorer].",
        allow_module_level=True,
    )


def _merge(graph: ContextGraph, duplicate_ids: List[str]) -> dict:
    session = GraphSession(graph)
    with TestClient(create_app(session=session)) as client:
        response = client.post(
            "/api/enrich/merge",
            json={"primary_id": "primary", "duplicate_ids": duplicate_ids},
        )
    assert response.status_code == 200, response.text
    return response.json()


def _two_nodes() -> ContextGraph:
    graph = ContextGraph(advanced_analytics=False)
    graph.add_node("primary", node_type="entity", content="Primary")
    graph.add_node("dup", node_type="entity", content="Dup")
    return graph


def test_isolated_duplicate_merge_is_snapshotted(tmp_path):
    """An edge-free merge must dirty the writer and survive a restart."""
    path = str(tmp_path / "snapshot.json")
    graph = _two_nodes()
    service = SnapshotService(graph, SnapshotStore(path)).start()
    try:
        payload = _merge(graph, ["dup"])
        assert payload["removed_ids"] == ["dup"]
        assert payload["edges_updated"] == 0
        # The writer gate: clean means the deletion is never written, not even
        # by the final flush in stop().
        assert service.snapshot_if_dirty() is True
    finally:
        service.stop()

    restored = ContextGraph(advanced_analytics=False)
    restored.load_from_file(path)
    assert "dup" not in restored.nodes
    assert "primary" in restored.nodes


def test_self_loop_collapse_merge_emits_remove_node():
    """A duplicate whose only edge points at the primary re-points nothing."""
    graph = _two_nodes()
    graph.add_edges([{"source_id": "dup", "target_id": "primary", "type": "same_as"}])
    events: List[Tuple[str, str]] = []
    graph.mutation_callback = lambda event, entity_id, payload: events.append(
        (event, entity_id)
    )

    payload = _merge(graph, ["dup"])

    assert payload["edges_updated"] == 0
    assert ("REMOVE_NODE", "dup") in events
