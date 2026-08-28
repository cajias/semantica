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

WRITER_THREAD_NAME = "semantica-snapshot-writer"


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


class OrderRecordingStore:
    """Records the payload each save *completes* with, in completion order.

    The first save blocks until a second one starts, so an implementation that
    lets two saves overlap deterministically completes them in the wrong order
    instead of failing only under an unlucky schedule.
    """

    description = "ordering store"

    def __init__(self):
        self.first_started = threading.Event()
        self.second_started = threading.Event()
        self.started = []
        self.completed = []

    def load(self, graph):
        return False

    def save(self, graph):
        payload = tuple(sorted(graph.nodes))
        self.started.append(payload)
        if self.first_started.is_set():
            self.second_started.set()
        else:
            self.first_started.set()
            # Times out only when the second save is serialised behind this
            # one. An overlapping second save sets it at once, and this save
            # then completes last, carrying the stale payload.
            self.second_started.wait(timeout=1.0)
        self.completed.append(payload)


class StuckThread:
    """Stands in for a writer that outlives ``stop()``'s bounded join.

    A genuinely stuck writer cannot be used: it is stuck *inside* the write
    lock, so ``stop()``'s own final snapshot would queue behind it forever and
    the test could never reach ``start()``. What is under test is the state
    ``stop()`` leaves behind when the join times out, which is exactly this.
    """

    daemon = True
    name = WRITER_THREAD_NAME

    def __init__(self):
        self.joins = 0

    def is_alive(self):
        return True

    def join(self, timeout=None):
        self.joins += 1


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
            # Read before stop(), which takes a final snapshot and clears it.
            dirty = service._dirty
        finally:
            service.stop()

        assert seen == ["ADD_NODE"]
        assert dirty is True

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
            # Read before stop(), which takes a final snapshot and clears them.
            first_dirty, second_dirty = first._dirty, second._dirty
        finally:
            first.stop()
            second.stop()

        assert seen == ["ADD_NODE"]
        assert first_dirty is True
        assert second_dirty is False

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

    def test_a_restarted_service_still_ticks(self, monkeypatch):
        """GIVEN a service that was stopped
        WHEN it is started again and the graph is mutated
        THEN the writer thread is alive and the mutation reaches a snapshot.

        ``stop()`` sets the stop event permanently, so a ``start()`` that does
        not clear it spawns a thread whose first ``wait()`` returns True at once:
        the thread exits while ``start()`` logs that it is snapshotting on an
        interval, and the interval guarantee is silently gone.
        """
        graph = _graph()
        store = FakeStore()
        service = _started(graph, store, monkeypatch, interval=0.02)
        _add(graph, "before_stop")
        service.stop()

        wrote = threading.Event()
        store.on_save = lambda _graph: wrote.set()
        service.start()
        try:
            _add(graph, "after_restart")

            assert wrote.wait(timeout=5), "the restarted writer never ticked"
            thread = service._thread
            assert thread is not None and thread.is_alive()
        finally:
            service.stop()

        assert store.saved_node_counts[-1] == 2

    def test_start_after_a_timed_out_stop_adds_no_second_writer(
        self, monkeypatch, caplog
    ):
        """GIVEN stop()'s bounded join timed out with the writer still running
        WHEN the service is started again
        THEN no second writer is spawned and the old one is still tracked.

        start() clears the stop event, so a second writer would not exit on its
        first wait(): two threads would then serialise the same graph to the
        same destination concurrently. Dropping the thread reference on a
        timed-out join is what would let that happen.
        """
        graph = _graph()
        monkeypatch.setattr(snapshot_module, "snapshot_interval_from_env", lambda: 3600)
        monkeypatch.setattr(snapshot_module, "STOP_JOIN_TIMEOUT", 0.01)
        service = SnapshotService(graph, FakeStore())
        zombie = StuckThread()
        service._thread = zombie

        with caplog.at_level("WARNING"):
            service.stop()

        assert zombie.joins == 1, "stop() did not even try to join the writer"
        assert service._thread is zombie, (
            "stop() dropped a writer that was still running, so start() can no "
            "longer see it"
        )
        assert "did not stop" in caplog.text

        before = [t for t in threading.enumerate() if t.name == WRITER_THREAD_NAME]
        service.start()
        after = [t for t in threading.enumerate() if t.name == WRITER_THREAD_NAME]

        assert after == before, "a second writer was started beside the first"
        assert service._thread is zombie

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


class TestShutdown:
    def test_a_dirty_graph_is_snapshotted_on_stop(self, monkeypatch):
        """GIVEN mutations and an interval far longer than the process lived
        WHEN the service is stopped
        THEN a final snapshot holding them is written.
        """
        graph = _graph()
        store = FakeStore()
        service = _started(graph, store, monkeypatch, interval=3600)

        _add(graph, "alpha")
        _add(graph, "beta")
        service.stop()

        assert store.saves == 1
        assert store.saved_node_counts == [2]

    def test_a_clean_graph_is_not_snapshotted_on_stop(self, monkeypatch):
        """GIVEN a graph unchanged since its last snapshot
        WHEN the service is stopped
        THEN nothing is written -- the stored snapshot already matches.
        """
        graph = _graph()
        store = FakeStore()
        service = _started(graph, store, monkeypatch, interval=3600)

        _add(graph, "alpha")
        service.snapshot_if_dirty()
        service.stop()

        assert store.saves == 1

    def test_stop_without_a_started_thread_still_snapshots(self, monkeypatch):
        """GIVEN a service that was never started
        WHEN it is stopped
        THEN a dirty graph is still snapshotted, and no thread is touched.
        """
        graph = _graph()
        store = FakeStore()
        monkeypatch.setattr(snapshot_module, "snapshot_interval_from_env", lambda: 3600)
        service = SnapshotService(graph, store)
        service._dirty = True

        service.stop()

        assert store.saves == 1
        assert service._thread is None

    def test_a_failing_final_snapshot_does_not_break_shutdown(
        self, monkeypatch, caplog
    ):
        """GIVEN a store whose save raises
        WHEN the service is stopped
        THEN stop() returns normally and the failure is logged at ERROR.
        """
        graph = _graph()
        store = FakeStore(error=RuntimeError("bucket went away"))
        service = _started(graph, store, monkeypatch, interval=3600)
        thread = service._thread

        _add(graph, "alpha")
        with caplog.at_level("ERROR"):
            service.stop()

        assert "bucket went away" in caplog.text
        assert thread is not None and not thread.is_alive()

    def test_stop_is_a_no_op_when_persistence_is_off(self):
        """GIVEN no snapshot destination configured
        WHEN the service is stopped
        THEN nothing happens and nothing raises.
        """
        service = SnapshotService(_graph(), None)
        service.start()

        service.stop()

        assert service._thread is None

    def test_the_final_snapshot_lands_after_an_upload_still_in_flight(
        self, monkeypatch
    ):
        """GIVEN a save still in flight when the bounded join times out
        WHEN stop() takes its final snapshot
        THEN the newest payload is the one that lands last.

        S3 resolves two PUTs against one key by *completion* order, so a final
        write that overlaps the upload it raced can complete first and leave the
        stale payload as the stored one -- silently, with stop() reporting no
        error. That is the lossless-redeploy case this feature exists for.
        """
        graph = _graph()
        store = OrderRecordingStore()
        monkeypatch.setattr(snapshot_module, "STOP_JOIN_TIMEOUT", 0.05)
        service = _started(graph, store, monkeypatch, interval=0.02)
        writer = service._thread
        try:
            _add(graph, "v1")
            assert store.first_started.wait(timeout=5)
            _add(graph, "v2")
        finally:
            service.stop()

        # stop() abandons the writer at the join timeout, which is the whole
        # premise here -- so wait for the in-flight save to actually finish
        # before reading the completion order.
        assert writer is not None
        writer.join(timeout=5)
        assert not writer.is_alive()

        assert store.started == [("v1",), ("v1", "v2")]
        assert store.completed[-1] == ("v1", "v2"), (
            "the stale payload completed last, so it is the one S3 would keep: "
            "{}".format(store.completed)
        )

    def test_stop_restores_the_previous_mutation_callback(self, monkeypatch):
        """GIVEN a bridge callback installed before the writer
        WHEN the service is stopped
        THEN the bridge is back in the slot and still receives mutations.

        The Explorer installs its WebSocket bridge before the writer, so the
        writer's captured ``previous_callback`` *is* the bridge and has to
        survive the writer being removed.
        """
        graph = _graph()
        seen = []

        def bridge(*args):
            seen.append(args[0])

        graph.mutation_callback = bridge
        service = _started(graph, FakeStore(), monkeypatch, interval=3600)

        service.stop()

        assert graph.mutation_callback is bridge
        _add(graph, "after_stop")
        assert seen == ["ADD_NODE"]

    def test_a_second_service_over_one_graph_still_snapshots(self, monkeypatch):
        """GIVEN a graph that outlives the service that snapshotted it
        WHEN a second service runs over the same graph and it is mutated
        THEN the mutation reaches a snapshot.

        ``create_app()`` builds its ``GraphSession`` outside the lifespan, so
        one graph survives across lifespan runs of one app object -- two
        ``with TestClient(app):`` blocks are enough. The idempotence guard
        lives on the graph and the dirty flag on the service, so a callback
        left installed by run 1 makes run 2 permanently clean: every snapshot,
        including the shutdown one, silently does nothing and reports success.
        """
        graph = _graph()
        first = _started(graph, FakeStore(), monkeypatch, interval=3600)
        _add(graph, "run_one")
        first.stop()

        second_store = FakeStore()
        second = _started(graph, second_store, monkeypatch, interval=3600)
        try:
            _add(graph, "run_two")
        finally:
            second.stop()

        assert second_store.saves == 1
        assert second_store.saved_node_counts == [2]
