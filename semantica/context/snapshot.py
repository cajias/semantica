"""One-object snapshot store for a :class:`~semantica.context.ContextGraph`.

The graph lives in memory, so durability is added around it rather than inside
it: a single object key holding a single serialised graph, written to a local
filesystem path or to S3. See ``docs/design/aws-integration/README.md`` section 5.

Serialisation is not reimplemented here. ``ContextGraph.save_to_file`` /
``load_from_file`` are the only pair that produce and consume the snapshot
payload, so both destinations route through them and the S3 object is
byte-identical to the local file.

One asymmetry carries the whole design. *Absence* of a snapshot means "first ever
deployment" and yields an empty graph. Every other failure -- a denied read, a
missing bucket, a truncated payload -- raises, because starting empty after a
permissions failure would let the next interval tick overwrite a perfectly good
snapshot with an empty one. That is data loss disguised as a successful start-up.
Section 6 of the same document has the application fail closed on
misconfiguration; this is the same principle applied to storage.
"""

import contextlib
import os
import tempfile
import threading
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

from ..utils.exceptions import ProcessingError, ValidationError
from ..utils.logging import get_logger

try:
    # No stubs are shipped and types-boto3 would be a new dependency.
    import boto3  # type: ignore[import-untyped]
    from botocore.exceptions import ClientError  # type: ignore[import-untyped]

    BOTO3_AVAILABLE = True
except (ImportError, OSError):  # native wheels fail with OSError, not ImportError
    BOTO3_AVAILABLE = False
    boto3 = None
    ClientError = None

SNAPSHOT_URI_ENV = "SEMANTICA_SNAPSHOT_URI"
SNAPSHOT_INTERVAL_ENV = "SEMANTICA_SNAPSHOT_INTERVAL"
DEFAULT_SNAPSHOT_INTERVAL = 30
# Seconds a shutdown will wait for an upload already in flight. Long enough for
# an S3 round trip, short enough to leave room in a platform grace period for
# the final snapshot that follows.
STOP_JOIN_TIMEOUT = 10.0

_S3_SCHEME = "s3://"
# S3 answers a missing object with NoSuchKey, and a HeadObject-shaped 404 with
# no code at all. NoSuchBucket is *also* a 404, so absence is decided on the
# code, never on the status.
_ABSENT_OBJECT_CODES = ("NoSuchKey", "404")

logger = get_logger("context_snapshot")


def _parse_s3_uri(uri: str) -> Tuple[str, str]:
    """Split ``s3://<bucket>/<key>`` into its two halves."""
    bucket, _, key = uri[len(_S3_SCHEME) :].partition("/")
    if not bucket or not key.strip("/"):
        raise ValidationError(
            "{} must name a bucket and an object key, as s3://<bucket>/<key>; "
            "got {!r}".format(SNAPSHOT_URI_ENV, uri)
        )
    return bucket, key


def _is_absent_object(error: BaseException) -> bool:
    """True when *error* means the snapshot object does not exist yet."""
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        # A stub may raise a bare ``client.exceptions.NoSuchKey`` with no
        # response payload attached.
        return type(error).__name__ == "NoSuchKey"
    code = response.get("Error", {}).get("Code")
    return code in _ABSENT_OBJECT_CODES


class SnapshotStore:
    """Reads and writes one serialised graph at one destination.

    The graph is a parameter, never a base class or a wrapped attribute:
    ``decision_recorder`` and ``decision_query`` dispatch on ``type(x) is
    ContextGraph``, so a subclass would be routed to their Cypher branch.
    """

    def __init__(self, uri: Optional[str], client: Optional[Any] = None):
        """
        Args:
            uri: ``s3://<bucket>/<key>``, or any other value as a local path.
            client: Pre-built S3 client, for tests. Built on first use otherwise.
        """
        if not uri or not uri.strip():
            raise ValidationError(
                "{} must be a non-empty path or s3:// URI; to disable "
                "persistence, do not build a store at all".format(SNAPSHOT_URI_ENV)
            )
        self.uri = uri
        self._client = client
        self.bucket: Optional[str]
        self.key: Optional[str]
        self.path: Optional[str]
        if uri.startswith(_S3_SCHEME):
            self.bucket, self.key = _parse_s3_uri(uri)
            self.path = None
        else:
            self.bucket, self.key = None, None
            self.path = uri

    @property
    def description(self) -> str:
        """Destination as a start-up log line. Never includes a credential."""
        if self.path is not None:
            return "file {}".format(self.path)
        return "s3://{}/{}".format(self.bucket, self.key)

    def load(self, graph: Any) -> bool:
        """Restore the snapshot into *graph*.

        Returns:
            True if a snapshot was restored, False if none existed yet.
        """
        path = self.path
        if path is not None:
            return self._load_file(graph, path)
        return self._load_object(graph)

    def save(self, graph: Any) -> None:
        """Write *graph* to the destination, replacing whatever is there."""
        path = self.path
        if path is not None:
            self._save_file(graph, path)
        else:
            self._save_object(graph)

    def _s3(self) -> Any:
        if self._client is None:
            if not BOTO3_AVAILABLE:
                raise ProcessingError(
                    "S3 snapshots need boto3, an optional dependency: install "
                    "semantica[cloud] (requested destination: {})".format(
                        self.description
                    )
                )
            self._client = boto3.client("s3")
        return self._client

    def _load_file(self, graph: Any, path: str) -> bool:
        if not os.path.exists(path):
            logger.info("no snapshot at %s; starting with an empty graph", path)
            return False
        graph.load_from_file(path)
        logger.info("restored context graph from %s", path)
        return True

    def _save_file(self, graph: Any, path: str) -> None:
        # save_to_file deliberately refuses to create the parent directory --
        # the library caller owns the location. Here the location is deployment
        # config and a freshly mounted volume legitimately starts empty, so a
        # missing directory is a first-run condition, not a permanent failure.
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        graph.save_to_file(path)

    def _load_object(self, graph: Any) -> bool:
        client = self._s3()
        try:
            response = client.get_object(Bucket=self.bucket, Key=self.key)
            body = response["Body"].read()
        # The tuple is assembled and checked by _absent_object_errors; mypy
        # cannot verify a runtime-computed except clause.
        except self._absent_object_errors(client) as error:  # type: ignore[misc]
            if not _is_absent_object(error):
                # AccessDenied, NoSuchBucket, a throttle: raise. Reporting
                # "no snapshot" here would start an empty graph and let the
                # next interval tick replace a good object with an empty one.
                raise
            logger.info(
                "no snapshot object at %s; starting with an empty graph",
                self.description,
            )
            return False

        with tempfile.TemporaryDirectory() as staging:
            staged = os.path.join(staging, "snapshot.json")
            with open(staged, "wb") as handle:
                handle.write(body)
            # A corrupt or truncated body raises out of here, deliberately.
            graph.load_from_file(staged)
        logger.info("restored context graph from %s", self.description)
        return True

    def _save_object(self, graph: Any) -> None:
        client = self._s3()
        # ponytail: a temp-file round trip per upload. Deliberate -- it keeps
        # ONE serialiser, so the object is byte-identical to a local snapshot.
        # Upgrade path: extract a payload seam out of save_to_file and stream it
        # straight into put_object. Per HLD section 5 the serialisation cost,
        # not the I/O, is the real bound, so this buys correctness against the
        # cheaper half of the budget.
        with tempfile.TemporaryDirectory() as staging:
            staged = os.path.join(staging, "snapshot.json")
            graph.save_to_file(staged)
            # put_object streams a file object, so the payload is never held in
            # memory a second time. save_to_file has already released the graph
            # lock by here: an upload must not block every mutation for a
            # network round trip.
            with open(staged, "rb") as handle:
                client.put_object(Bucket=self.bucket, Key=self.key, Body=handle)
        logger.info("wrote context graph snapshot to %s", self.description)

    @staticmethod
    def _absent_object_errors(client: Any) -> Tuple[type, ...]:
        """Exception types a missing object can arrive as.

        ``client.exceptions.NoSuchKey`` is a ``ClientError`` subclass in
        botocore, but a stub need not be, so both are named.
        """
        errors = []
        no_such_key = getattr(getattr(client, "exceptions", None), "NoSuchKey", None)
        if isinstance(no_such_key, type):
            errors.append(no_such_key)
        if ClientError is not None:
            errors.append(ClientError)
        return tuple(errors) or (OSError,)


def snapshot_store_from_env(
    env: Optional[Dict[str, str]] = None
) -> Optional[SnapshotStore]:
    """Build a store from ``SEMANTICA_SNAPSHOT_URI``, or None if it is unset."""
    source = os.environ if env is None else env
    uri = (source.get(SNAPSHOT_URI_ENV) or "").strip()
    if not uri:
        return None
    return SnapshotStore(uri)


def snapshot_interval_from_env(env: Optional[Dict[str, str]] = None) -> int:
    """Seconds between snapshots, from ``SEMANTICA_SNAPSHOT_INTERVAL``.

    An unusable value warns and falls back to the default rather than raising:
    the interval is a tuning knob, and refusing to boot over it would trade a
    slightly wrong snapshot cadence for a total outage.
    """
    source = os.environ if env is None else env
    raw = (source.get(SNAPSHOT_INTERVAL_ENV) or "").strip()
    if not raw:
        return DEFAULT_SNAPSHOT_INTERVAL
    try:
        seconds = int(raw)
    except ValueError:
        seconds = 0
    if seconds <= 0:
        logger.warning(
            "%s=%r is not a positive whole number of seconds; using %d",
            SNAPSHOT_INTERVAL_ENV,
            raw,
            DEFAULT_SNAPSHOT_INTERVAL,
        )
        return DEFAULT_SNAPSHOT_INTERVAL
    return seconds


@contextlib.contextmanager
def suspended_mutations(graph: Any) -> Iterator[None]:
    """Silence *graph*'s mutation callback for the duration of the block.

    ``load_from_file`` replays every node and edge through ``add_nodes`` /
    ``add_edges``, which announce each one. A restore would therefore broadcast
    the whole graph to connected browsers and mark the freshly loaded graph as
    dirty.

    ``ContextGraph._suspend_mutation_callback`` is a plain boolean flag despite
    the name, not a context manager. This is the shared form of the save/set/
    restore that ``change_management/managers.py`` writes out by hand. The
    *previous* value is restored, not False, so nesting works.
    """
    previous = getattr(graph, "_suspend_mutation_callback", False)
    graph._suspend_mutation_callback = True
    try:
        yield
    finally:
        graph._suspend_mutation_callback = previous


class SnapshotService:
    """Ties a :class:`SnapshotStore` to the lifetime of a running process.

    One object per process, wired into a server's lifespan. It exists so the
    two FastAPI lifespans (``server.py`` and ``explorer/app.py``) share one
    implementation instead of two copies that drift apart.

    A ``store`` of None means ``SEMANTICA_SNAPSHOT_URI`` is unset: persistence
    is off and every method is a no-op, so the wiring is unconditional and the
    decision stays where it belongs, in deployment config. A ``graph`` of None
    disables it the same way, which is what a caller whose graph failed to
    build passes.
    """

    def __init__(
        self,
        graph: Any,
        store: Optional[SnapshotStore],
        after_restore: Optional[Callable[[], None]] = None,
    ):
        """
        Args:
            graph: The ``ContextGraph`` to restore into and snapshot from.
            store: Destination, or None to disable persistence entirely.
            after_restore: Called once after a successful restore, so a caller
                can refresh state derived from the graph. The restore runs with
                mutation notifications suspended, so nothing downstream --
                the Explorer's search index, its embedding cache -- otherwise
                learns that the nodes arrived.
        """
        self._graph = graph
        self._store = store
        self._after_restore = after_restore
        self._interval = snapshot_interval_from_env()
        self._dirty = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @classmethod
    def from_env(
        cls, graph: Any, after_restore: Optional[Callable[[], None]] = None
    ) -> "SnapshotService":
        """Build a service from ``SEMANTICA_SNAPSHOT_URI``, disabled if unset."""
        store = None if graph is None else snapshot_store_from_env()
        return cls(graph, store, after_restore=after_restore)

    def restore(self) -> bool:
        """Load the snapshot into the graph before the process serves traffic.

        Returns:
            True if a snapshot was restored, False if none existed or
            persistence is disabled.
        """
        store = self._store
        if store is None:
            return False
        logger.info("context graph snapshots: %s", store.description)
        with suspended_mutations(self._graph):
            restored = store.load(self._graph)
        if restored and self._after_restore is not None:
            self._after_restore()
        return restored

    def start(self) -> "SnapshotService":
        """Watch the graph for changes and snapshot it on an interval.

        Returns self, so a lifespan can build and start in one statement.
        """
        if self._store is None or self._thread is not None:
            return self
        self._mark_dirty_on_mutation()
        # A daemon thread rather than an asyncio task: the save is blocking
        # network I/O that would stall the event loop, asyncio.to_thread is
        # 3.9+ while library code targets >=3.8, and threads are the house
        # mechanism already (pipeline/parallelism_manager.py). Daemon, so a
        # hard kill cannot be held up by the writer.
        self._thread = threading.Thread(
            target=self._run, name="semantica-snapshot-writer", daemon=True
        )
        self._thread.start()
        logger.info(
            "snapshotting %s every %ds while dirty",
            self._store.description,
            self._interval,
        )
        return self

    def stop(self) -> None:
        """Stop the interval writer, then take a final snapshot if one is due.

        The platform signals a container before stopping it, and this is that
        chance: it is what makes an ordinary redeployment lossless, including
        for a container that never lived a full interval. The write is still
        gated on the dirty flag -- a clean graph already matches what is
        stored, so writing again would be pure I/O on every redeploy.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None:
            # Bounded: the wait() returns at once, but a save already under way
            # is a network round trip, and a shutdown grace period is finite.
            thread.join(timeout=STOP_JOIN_TIMEOUT)
            if thread.is_alive():
                logger.warning(
                    "snapshot writer did not stop within %ss", STOP_JOIN_TIMEOUT
                )
            self._thread = None
        # snapshot_if_dirty logs a failure at ERROR and returns rather than
        # raising: an exception here would escape into the lifespan's teardown
        # and mask whatever else was shutting down.
        self.snapshot_if_dirty()

    def _mark_dirty_on_mutation(self) -> None:
        """Chain a dirty-flag setter onto the graph's mutation callback.

        ``mutation_callback`` is a single attribute slot, not a listener list,
        and the Explorer's WebSocket bridge and change_management both want it.
        The previous occupant is captured and called, so whoever installs
        second wraps the first instead of silencing it. Installation is
        idempotent, or wiring twice would call the previous callback twice per
        mutation.
        """
        graph = self._graph
        if getattr(graph, "_snapshot_writer_installed", False):
            return
        graph._snapshot_writer_installed = True
        previous_callback = getattr(graph, "mutation_callback", None)

        def on_mutation(
            event_type: str, entity_id: str, payload: Dict[str, Any]
        ) -> None:
            self._dirty = True
            if callable(previous_callback):
                previous_callback(event_type, entity_id, payload)

        graph.mutation_callback = on_mutation

    def _run(self) -> None:
        # wait() rather than sleep(): a stop is picked up immediately instead
        # of after the rest of the interval.
        while not self._stop.wait(self._interval):
            self.snapshot_if_dirty()

    def snapshot_if_dirty(self) -> bool:
        """Write a snapshot if the graph changed since the last one.

        One tick of the interval writer, exposed so it can be driven directly.

        Returns:
            True if a snapshot was written.
        """
        store = self._store
        if store is None or not self._dirty:
            return False
        # Cleared BEFORE the write. A mutation landing mid-upload then re-marks
        # the flag and the next tick picks it up; clearing afterwards would
        # swallow that edit until some unrelated later one.
        self._dirty = False
        try:
            # No lock is taken here. save_to_file builds its payload under the
            # graph lock and releases it before writing, so an upload does not
            # block every mutation for a network round trip.
            store.save(self._graph)
        except Exception:
            # One transient S3 failure must not stop all persistence, so the
            # thread logs and keeps looping.
            # ponytail: no back-off. A failed write leaves the graph dirty, so
            # the retry is already one interval away -- that IS the back-off,
            # and stretching it would only widen the window of loss.
            self._dirty = True
            logger.exception(
                "snapshot to %s failed; retrying at the next interval",
                store.description,
            )
            return False
        return True
