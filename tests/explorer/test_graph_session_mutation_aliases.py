"""Pin ``GraphSession.handle_graph_mutation``'s tolerant operation vocabulary.

``ContextGraph.mutation_callback`` is a public single-slot attribute, so the
library's own shared write path is not the only emitter that can occupy it —
``semantica/change_management/managers.py`` already installs its own, and a
third-party integration may do the same. ``DELETE_NODE``, ``DELETE_EDGE`` and
``RESET_GRAPH`` therefore exist as synonyms of ``REMOVE_NODE``, ``REMOVE_EDGE``
and ``RELOAD_GRAPH``: nothing in ``semantica/`` emits them today, and these
tests are what stops the next reader mistaking them for dead branches.
"""

from semantica.context import ContextGraph
from semantica.explorer.session import GraphSession

SEARCH_TERM = "Zebranomicon"


def _session_with_indexed_node():
    """A session whose search index already contains one findable node."""
    graph = ContextGraph(advanced_analytics=False)
    graph.add_nodes(
        [{"id": "n1", "type": "Concept", "properties": {"name": SEARCH_TERM}}]
    )
    session = GraphSession(graph)
    session.rebuild_search_index()
    assert [hit["node"]["id"] for hit in session.search(SEARCH_TERM)] == ["n1"]
    return session


class TestNodeRemovalSynonym:
    def test_delete_node_drops_the_node_from_the_search_index(self):
        session = _session_with_indexed_node()

        session.handle_graph_mutation("DELETE_NODE", "n1", {})

        assert session.search(SEARCH_TERM) == []

    def test_delete_node_is_case_insensitive(self):
        session = _session_with_indexed_node()

        session.handle_graph_mutation("delete_node", "n1", {})

        assert session.search(SEARCH_TERM) == []


class TestReloadSynonym:
    def test_reset_graph_rebuilds_the_index_from_the_graph(self):
        graph = ContextGraph(advanced_analytics=False)
        session = GraphSession(graph)
        # Written with no callback installed, so the index is stale on purpose.
        graph.add_nodes(
            [{"id": "n1", "type": "Concept", "properties": {"name": SEARCH_TERM}}]
        )
        assert session.search(SEARCH_TERM) == []

        session.handle_graph_mutation("RESET_GRAPH", "", {})

        assert [hit["node"]["id"] for hit in session.search(SEARCH_TERM)] == ["n1"]


class TestRevisionBump:
    def test_every_synonym_invalidates_the_cached_embeddings(self):
        session = GraphSession(ContextGraph(advanced_analytics=False))

        for operation in ("DELETE_NODE", "DELETE_EDGE", "RESET_GRAPH"):
            before = session._graph_revision
            session.handle_graph_mutation(operation, "n1", {})
            assert session._graph_revision == before + 1, operation


class TestUnknownOperation:
    def test_unknown_operation_is_a_silent_no_op(self):
        session = _session_with_indexed_node()
        before = session._graph_revision

        # Must not raise: this runs inside a caller's write.
        session.handle_graph_mutation("PURGE_EVERYTHING", "n1", {})
        session.handle_graph_mutation("", "n1", {})

        assert session._graph_revision == before
        assert [hit["node"]["id"] for hit in session.search(SEARCH_TERM)] == ["n1"]
