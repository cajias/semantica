"""Tests for the interval snapshot writer in ``semantica.context.snapshot``.

Ticks are driven directly through ``snapshot_if_dirty`` wherever possible, so
the coalescing and ordering assertions do not depend on wall-clock timing. The
two tests that must exercise the real thread patch
``snapshot_interval_from_env`` down to a fraction of a second, since the env
var is whole seconds.
"""

import threading

from semantica.context import snapshot as snapshot_module
from semantica.context.context_graph import ContextGraph
from semantica.context.snapshot import SnapshotService


class FakeStore:
    """Counts saves. ``on_save`` runs inside the save, before it returns."""

    description = "fake store"

    def __init__(self, on_save=None, error=None):
        self.saves = 0
        self.saved_node_counts = []
        self.on_save = on_save
        self.error = error

    def load(self, graph):
        return False

    def save(self, graph):
        self.saves += 1
        self.saved_node_counts.append(len(graph.nodes))
        if self.on_save is not None:
            self.on_save(graph)
        if self.error is not None:
            raise self.error


def _graph():
    return ContextGraph(advanced_analytics=False)


def _add(graph, node_id):
    graph.add_node(node_id, node_type="concept", content=node_id)


def _started(graph, store, monkeypatch, interval=None):
    """A started service, with the interval patched when one is given."""
    if interval is not None:
        monkeypatch.setattr(
            snapshot_module, "snapshot_interval_from_env", lambda: interval
        )
    return SnapshotService(graph, store).start()


class TestMutationHook:
    def test_previous_callback_and_dirty_flag_both_fire(self, monkeypatch):
        """GIVEN a mutation callback already installed on the graph
        WHEN the writer is installed and a node is added
        THEN the previous callback still runs and the graph is marked dirty.
        """
        graph = _graph()
        seen = []
        graph.mutation_callback = lambda *args: seen.append(args[0])
        store = FakeStore()

        service = _started(graph, store, monkeypatch, interval=3600)
        try:
            _add(graph, "alpha")
        finally:
            service.stop()

        assert seen == ["ADD_NODE"]
        assert service._dirty is True

    def test_installing_twice_calls_the_previous_callback_once(self, monkeypatch):
        """GIVEN a mutation callback already installed on the graph
        WHEN two writers are installed on the same graph
        THEN one mutation still reaches the previous callback exactly once.
        """
        graph = _graph()
        seen = []
        graph.mutation_callback = lambda *args: seen.append(args[0])
        monkeypatch.setattr(snapshot_module, "snapshot_interval_from_env", lambda: 3600)
        first = SnapshotService(graph, FakeStore()).start()
        second = SnapshotService(graph, FakeStore()).start()

        try:
            _add(graph, "alpha")
        finally:
            first.stop()
            second.stop()

        assert seen == ["ADD_NODE"]
        assert first._dirty is True
        assert second._dirty is False

    def test_no_hook_and_no_thread_when_persistence_is_off(self):
        """GIVEN no snapshot destination configured
        WHEN the service starts
        THEN no callback is installed and no thread is running.
        """
        graph = _graph()
        service = SnapshotService(graph, None).start()

        assert service._thread is None
        assert getattr(graph, "mutation_callback", None) is None


class TestCoalescing:
    def test_many_mutations_in_one_interval_write_once(self, monkeypatch):
        """GIVEN several mutations between two ticks
        WHEN a tick runs
        THEN exactly one snapshot is written, holding all of them.
        """
        graph = _graph()
        store = FakeStore()
        service = _started(graph, store, monkeypatch, interval=3600)
        try:
            for index in range(5):
                _add(graph, "node_{}".format(index))

            assert service.snapshot_if_dirty() is True
        finally:
            service.stop()

        assert store.saves == 1
        assert store.saved_node_counts == [5]

    def test_a_clean_graph_is_not_written(self, monkeypatch):
        """GIVEN no mutations since the last snapshot
        WHEN a tick runs
        THEN nothing is written -- the dirty flag really gates.
        """
        graph = _graph()
        store = FakeStore()
        service = _started(graph, store, monkeypatch, interval=3600)
        try:
            assert service.snapshot_if_dirty() is False
            _add(graph, "alpha")
            assert service.snapshot_if_dirty() is True
            assert service.snapshot_if_dirty() is False
        finally:
            service.stop()

        assert store.saves == 1

    def test_a_mutation_during_a_save_is_not_swallowed(self, monkeypatch):
        """GIVEN a mutation that lands while a save is in progress
        WHEN the next tick runs
        THEN it is snapshotted too, because the flag is cleared before the write.
        """
        graph = _graph()
        landed = []

        def mutate_mid_save(saved_graph):
            if not landed:
                landed.append(True)
                _add(saved_graph, "during_save")

        store = FakeStore(on_save=mutate_mid_save)
        service = _started(graph, store, monkeypatch, interval=3600)
        try:
            _add(graph, "before_save")

            assert service.snapshot_if_dirty() is True
            assert service.snapshot_if_dirty() is True
        finally:
            service.stop()

        assert store.saves == 2
        assert store.saved_node_counts == [1, 2]


class TestFailureHandling:
    def test_a_failing_save_is_logged_and_retried(self, monkeypatch, caplog):
        """GIVEN a store whose save raises
        WHEN a tick runs and a later tick succeeds
        THEN the failure is logged at ERROR and the later tick still writes.
        """
        graph = _graph()
        store = FakeStore(error=RuntimeError("s3 is having a day"))
        service = _started(graph, store, monkeypatch, interval=3600)
        try:
            _add(graph, "alpha")

            with caplog.at_level("ERROR"):
                assert service.snapshot_if_dirty() is False

            store.error = None
            assert service.snapshot_if_dirty() is True
        finally:
            service.stop()

        assert store.saves == 2
        assert "s3 is having a day" in caplog.text

    def test_the_writer_thread_survives_a_failing_save(self, monkeypatch):
        """GIVEN a store whose save raises
        WHEN the writer thread ticks over it
        THEN the thread is still alive and writes once the store recovers.
        """
        graph = _graph()
        wrote = threading.Event()
        store = FakeStore(
            on_save=lambda _graph: wrote.set(), error=RuntimeError("transient")
        )
        service = _started(graph, store, monkeypatch, interval=0.02)
        try:
            _add(graph, "alpha")
            assert wrote.wait(timeout=5)
            wrote.clear()
            store.error = None

            assert wrote.wait(timeout=5)
            assert service._thread is not None and service._thread.is_alive()
        finally:
            service.stop()

        assert store.saves >= 2


class TestThreadBehaviour:
    def test_the_thread_is_a_daemon_and_stops_promptly(self, monkeypatch):
        """GIVEN a writer running on a long interval
        WHEN it is stopped
        THEN the thread is a daemon and is dead well before that interval.
        """
        graph = _graph()
        service = _started(graph, FakeStore(), monkeypatch, interval=3600)
        thread = service._thread

        assert thread is not None
        assert thread.daemon is True

        service.stop()

        thread.join(timeout=2)
        assert not thread.is_alive()
        assert service._thread is None

    def test_the_graph_lock_is_free_during_a_save(self, monkeypatch, tmp_path):
        """GIVEN a save in progress on another thread
        WHEN a second thread asks for the graph lock
        THEN it gets it, because the service adds no lock of its own.

        ``graph._lock`` is an RLock, so the probe must come from a different
        thread than the save -- a same-thread acquire would succeed spuriously.
        The fake save serialises through ``save_to_file`` first, the way the
        real store does, then blocks where a network upload would be.
        """
        graph = _graph()
        target = str(tmp_path / "snapshot.json")
        saving = threading.Event()
        release = threading.Event()

        def block_mid_save(saved_graph):
            saved_graph.save_to_file(target)
            saving.set()
            release.wait(timeout=5)

        store = FakeStore(on_save=block_mid_save)
        service = _started(graph, store, monkeypatch, interval=3600)
        try:
            _add(graph, "alpha")
            saver = threading.Thread(target=service.snapshot_if_dirty)
            saver.start()

            assert saving.wait(timeout=5)
            acquired = graph._lock.acquire(timeout=2)
            if acquired:
                graph._lock.release()
            release.set()
            saver.join(timeout=5)
        finally:
            release.set()
            service.stop()

        assert acquired is True
