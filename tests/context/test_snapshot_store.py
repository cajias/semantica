#!/usr/bin/env python3
"""Tests for ``semantica.context.snapshot`` -- the one-object snapshot store.

The store is the storage seam for the AWS deployment: one object key holding one
serialised ``ContextGraph``, written to a local path or to S3. Its whole
contract is a single asymmetry that is easy to get wrong in the direction that
loses data:

* **Absence** of a snapshot means "first ever deploy" -- load an empty graph.
* **Every other failure** -- a denied read, a missing bucket, a truncated
  payload -- must raise. Starting empty after a *permissions* failure would let
  the next interval tick overwrite a perfectly good snapshot with an empty one.
  That is data loss wearing a successful start-up as a disguise.

So the tests below are mostly about which failures are allowed to be quiet. The
S3 half uses ``botocore.stub.Stubber`` rather than a hand-written fake, because
Stubber validates the outgoing call against the real service model: a
misspelled parameter or a hard-coded bucket fails the stub, where a mock would
happily accept it. ``test_boto3_availability_flag_matches_the_environment``
exists so the boto3 skip cannot quietly hide a broken import guard.
"""

import io
import json
import logging
import os
import re
import threading

import pytest

from semantica.context import snapshot
from semantica.context.context_graph import ContextGraph
from semantica.utils.exceptions import ProcessingError, ValidationError

try:
    import boto3
    from botocore.exceptions import ClientError
    from botocore.response import StreamingBody
    from botocore.stub import ANY, Stubber

    BOTO3_INSTALLED = True
except (ImportError, OSError):  # pragma: no cover - exercised only without boto3
    BOTO3_INSTALLED = False
    ANY = None
    boto3 = None
    ClientError = None
    StreamingBody = None
    Stubber = None

requires_boto3 = pytest.mark.skipif(
    not BOTO3_INSTALLED, reason="boto3/botocore are optional (semantica[cloud])"
)

BUCKET = "graphs-bucket"
KEY = "snapshots/graph.json"
URI = "s3://{}/{}".format(BUCKET, KEY)


def _seeded_graph(node_count: int = 3, graph_id: str = "ctx-snapshot") -> ContextGraph:
    """A small graph with both nodes and edges, so a round trip proves both."""
    graph = ContextGraph(advanced_analytics=False)
    graph.graph_id = graph_id
    for i in range(node_count):
        graph.add_node("n{}".format(i), "entity", content="content {}".format(i))
    for i in range(node_count - 1):
        graph.add_edge("n{}".format(i), "n{}".format(i + 1), "related_to")
    return graph


def _snapshot_bytes(graph: ContextGraph, reference_path) -> bytes:
    """The exact bytes ``save_to_file`` produces for *graph*.

    The S3 payload is expected to be byte-identical to the local one -- that is
    the point of routing the upload through the same serialiser.
    """
    graph.save_to_file(str(reference_path))
    return reference_path.read_bytes()


def _s3_client():
    """A real boto3 client with dummy credentials, for ``Stubber`` to intercept.

    Explicit credentials and region keep the test off any ambient AWS profile.
    """
    return boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        aws_session_token="testing",
    )


def _streaming(body: bytes):
    return StreamingBody(io.BytesIO(body), len(body))


def _recording_graph():
    """A graph plus the list of mutation operations its callback saw."""
    seen = []
    graph = ContextGraph(
        advanced_analytics=False,
        mutation_callback=lambda operation, *_rest: seen.append(operation),
    )
    return graph, seen


def test_boto3_availability_flag_matches_the_environment():
    """GIVEN this interpreter can or cannot import boto3,
    THEN the module's own guard flag agrees with that.

    Every S3 test is skipped when boto3 is missing. Without this check a broken
    import guard would present as a clean run rather than a failure.
    """
    assert snapshot.BOTO3_AVAILABLE == BOTO3_INSTALLED, (
        "snapshot.BOTO3_AVAILABLE disagrees with whether boto3 actually imports "
        "here, so the S3 skips can no longer be trusted"
    )


class TestUriParsing:
    """One env var selects the destination, so its parsing is the whole API."""

    def test_s3_uri_splits_into_bucket_and_key(self):
        """GIVEN an ``s3://`` URI,
        THEN bucket and key are taken from it and no local path is set.
        """
        store = snapshot.SnapshotStore("s3://b/k", client=object())

        assert (store.bucket, store.key) == ("b", "k")
        assert store.path is None, "an s3:// URI was also treated as a local path"

    def test_s3_key_keeps_its_nested_prefix(self):
        """GIVEN a key containing slashes,
        THEN only the first segment is the bucket; the rest is the key verbatim.
        """
        store = snapshot.SnapshotStore("s3://b/nested/path/k.json", client=object())

        assert (store.bucket, store.key) == ("b", "nested/path/k.json")

    @pytest.mark.parametrize(
        "uri", ["graph.json", "data/graph.json", "/data/graph.json"]
    )
    def test_a_plain_path_selects_the_filesystem(self, uri):
        """GIVEN a value that is not an ``s3://`` URI, relative or absolute,
        THEN it is used as a filesystem path and no bucket is derived.
        """
        store = snapshot.SnapshotStore(uri)

        assert store.path == uri
        assert store.bucket is None and store.key is None

    @pytest.mark.parametrize("uri", ["s3://bucket", "s3://bucket/", "s3://bucket//"])
    def test_an_s3_uri_without_a_key_is_rejected(self, uri):
        """GIVEN an ``s3://`` URI naming a bucket but no object key,
        THEN construction raises rather than silently writing to an empty key.

        An empty key is not a usable object name, and defaulting one would put
        the snapshot somewhere the operator never named.
        """
        with pytest.raises(ValidationError, match="bucket and an object key"):
            snapshot.SnapshotStore(uri)

    @pytest.mark.parametrize(
        "uri",
        [
            "s3:/bucket/key",  # one slash
            "S3://bucket/key",  # wrong case
            "s3a://bucket/key",  # the Hadoop scheme
            "https://host/key",
            "file:///data/key",
        ],
    )
    def test_a_mistyped_scheme_is_rejected(self, uri):
        """GIVEN a URI that looks like a scheme but is not ``s3://``,
        THEN construction raises rather than treating it as a local path.

        Silently falling through to the filesystem is durability loss dressed
        as success: the service logs the destination, snapshots every interval
        and reports success, while writing the whole graph into the container's
        ephemeral layer -- gone on every redeploy, and ``makedirs`` happily
        creates a directory literally named ``s3:``.
        """
        with pytest.raises(ValidationError, match=r"s3://"):
            snapshot.SnapshotStore(uri)

    @pytest.mark.parametrize(
        "uri", ["/data/graph.json", "relative/graph.json", "C:\\data\\graph.json", URI]
    )
    def test_a_real_path_or_a_real_s3_uri_is_accepted(self, uri):
        """GIVEN a plain filesystem path or a well-formed ``s3://`` URI,
        THEN the scheme guard leaves it alone. A plain path has no ``://``.
        """
        assert snapshot.SnapshotStore(uri, client=object()).uri == uri

    @pytest.mark.parametrize("uri", ["", "   ", None])
    def test_an_empty_uri_is_rejected(self, uri):
        """GIVEN no URI at all,
        THEN construction raises. Disabling persistence is the *caller's*
        decision, expressed by not building a store -- see
        ``snapshot_store_from_env`` -- not by building a store that writes
        nowhere.
        """
        with pytest.raises(ValidationError):
            snapshot.SnapshotStore(uri)


class TestLocalSnapshots:
    """The filesystem destination, used for local runs and mounted volumes."""

    def test_save_then_load_round_trips_nodes_and_edges(self, tmp_path):
        """GIVEN a saved graph,
        WHEN a fresh graph loads the same snapshot,
        THEN nodes, edges and graph id all come back and ``load`` reports True.
        """
        path = tmp_path / "graph.json"
        store = snapshot.SnapshotStore(str(path))
        original = _seeded_graph(4, "local-round-trip")
        store.save(original)

        restored = ContextGraph(advanced_analytics=False)
        assert store.load(restored) is True, "a snapshot existed but load said False"

        assert restored.graph_id == "local-round-trip"
        assert sorted(restored.nodes) == sorted(original.nodes)
        assert len(restored.edges) == len(original.edges), (
            "nodes survived the round trip but edges did not -- half a graph is "
            "worse than none, because it looks like a successful restore"
        )

    def test_missing_snapshot_yields_an_empty_graph(self, tmp_path, caplog):
        """GIVEN no file at the snapshot path -- the first ever deployment,
        THEN ``load`` returns False, the graph is untouched, and nothing raises.
        """
        store = snapshot.SnapshotStore(str(tmp_path / "absent.json"))
        graph = ContextGraph(advanced_analytics=False)

        with caplog.at_level(logging.INFO):
            assert store.load(graph) is False

        assert graph.nodes == {} and graph.edges == []
        assert "empty graph" in caplog.text.lower(), (
            "a first-run start-up produced no log line explaining why the graph "
            "is empty"
        )

    def test_save_creates_a_missing_parent_directory(self, tmp_path):
        """GIVEN a snapshot path whose parent directory does not exist,
        THEN saving creates it.

        ``ContextGraph.save_to_file`` deliberately refuses to do this: the
        library caller owns the location. Here the location comes from
        deployment config, and a freshly mounted volume legitimately starts
        empty, so a missing directory is a first-run condition rather than a
        permanent failure.
        """
        path = tmp_path / "missing" / "deeper" / "graph.json"
        store = snapshot.SnapshotStore(str(path))

        store.save(_seeded_graph())

        assert path.exists(), "save did not create the missing parent directory"

    @pytest.mark.parametrize(
        "name,body,expected",
        [
            ("garbage", b"not json at all {{{", json.JSONDecodeError),
            (
                "truncated",
                b'{"graph_id": "cut", "nodes": [{"node_id": "n0"',
                json.JSONDecodeError,
            ),
            ("empty", b"", json.JSONDecodeError),
            # Not decodable at all, so it fails one layer earlier than JSON.
            ("non_utf8", b"\x00\xff\xfe binary", UnicodeDecodeError),
        ],
    )
    def test_a_damaged_snapshot_raises_rather_than_starting_empty(
        self, tmp_path, name, body, expected
    ):
        """GIVEN a snapshot file that exists but cannot be parsed,
        THEN ``load`` raises instead of reporting "no snapshot".

        Only *absence* is allowed to mean empty. Treating corruption as absence
        would start the process with an empty graph and let the next interval
        tick replace the damaged-but-recoverable object with an empty one.

        The 0-byte case is the file the atomic write exists to prevent; it is
        proved here to be classified as damage, not as absence.
        """
        path = tmp_path / "{}.json".format(name)
        path.write_bytes(body)
        store = snapshot.SnapshotStore(str(path))
        graph = ContextGraph(advanced_analytics=False)

        with pytest.raises(expected):
            store.load(graph)

    def test_description_names_the_local_path(self, tmp_path):
        """GIVEN a filesystem store,
        THEN its description names the path, for the start-up log line.
        """
        store = snapshot.SnapshotStore(str(tmp_path / "graph.json"))

        assert str(tmp_path / "graph.json") in store.description


@requires_boto3
class TestS3Snapshots:
    """The object-storage destination. ``Stubber`` validates every call."""

    def test_save_puts_the_object_at_the_parsed_bucket_and_key(self, tmp_path):
        """GIVEN a store built from ``s3://<bucket>/<key>``,
        WHEN a graph is saved,
        THEN ``put_object`` is called with exactly that bucket and key, and a
        streamed body byte-identical to the local snapshot of the same graph.

        Stubber fails the call if the bucket or key differs, so a hard-coded one
        cannot pass here. ``Body`` is matched as ``ANY`` because Stubber
        compares parameters by value and the body is deliberately a file object
        rather than bytes -- holding the whole serialised graph in memory a
        second time is what the streaming exists to avoid. The payload is
        instead read off that object in a ``before-parameter-build`` handler,
        which proves both properties at once: it is file-like, and it carries
        the right bytes.
        """
        graph = _seeded_graph(4, "s3-save")
        expected_body = _snapshot_bytes(graph, tmp_path / "reference.json")
        client = _s3_client()
        store = snapshot.SnapshotStore(URI, client=client)
        streamed = {}

        def capture(params, **_kwargs):
            body = params["Body"]
            streamed["is_file_like"] = hasattr(body, "read")
            streamed["bytes"] = body.read()
            body.seek(0)  # leave it as botocore found it

        client.meta.events.register("before-parameter-build.s3.PutObject", capture)

        with Stubber(client) as stubber:
            stubber.add_response(
                "put_object",
                {},
                {"Bucket": BUCKET, "Key": KEY, "Body": ANY},
            )

            store.save(graph)

            stubber.assert_no_pending_responses()

        assert streamed.get("is_file_like"), (
            "Body was passed as bytes, so the entire serialised graph is held in "
            "memory a second time on every 30-second snapshot"
        )
        assert streamed["bytes"] == expected_body, (
            "the uploaded payload differs from the local snapshot of the same "
            "graph, so the two destinations are no longer one serialiser"
        )

    def test_load_round_trips_nodes_and_edges(self, tmp_path):
        """GIVEN an object holding a snapshot,
        WHEN it is loaded,
        THEN nodes, edges and graph id come back and ``load`` reports True.
        """
        original = _seeded_graph(4, "s3-round-trip")
        body = _snapshot_bytes(original, tmp_path / "reference.json")
        client = _s3_client()
        store = snapshot.SnapshotStore(URI, client=client)
        restored = ContextGraph(advanced_analytics=False)

        with Stubber(client) as stubber:
            stubber.add_response(
                "get_object",
                {"Body": _streaming(body)},
                {"Bucket": BUCKET, "Key": KEY},
            )

            assert store.load(restored) is True

        assert restored.graph_id == "s3-round-trip"
        assert sorted(restored.nodes) == sorted(original.nodes)
        assert len(restored.edges) == len(original.edges), (
            "edges were dropped restoring from S3 -- a partial restore reports "
            "success and then gets overwritten by the next snapshot"
        )

    def test_a_missing_object_yields_an_empty_graph(self, caplog):
        """GIVEN no snapshot object yet -- the first ever deployment,
        THEN ``load`` returns False, the graph is empty, and nothing raises.
        """
        client = _s3_client()
        store = snapshot.SnapshotStore(URI, client=client)
        graph = ContextGraph(advanced_analytics=False)

        with Stubber(client) as stubber:
            stubber.add_client_error(
                "get_object", service_error_code="NoSuchKey", http_status_code=404
            )

            with caplog.at_level(logging.INFO):
                assert store.load(graph) is False

        assert graph.nodes == {} and graph.edges == []
        assert "empty graph" in caplog.text.lower()

    @pytest.mark.parametrize(
        "code,status",
        [("AccessDenied", 403), ("NoSuchBucket", 404)],
    )
    def test_a_non_absence_error_raises(self, code, status):
        """GIVEN the read fails for a reason other than the object not existing,
        THEN ``load`` raises and leaves the graph exactly as it was.

        ``NoSuchBucket`` also arrives as HTTP 404, which is why absence is
        decided on the error *code* and not the status. Swallowing either of
        these would start an empty graph on a misconfiguration and then let the
        interval writer destroy the real snapshot.
        """
        client = _s3_client()
        store = snapshot.SnapshotStore(URI, client=client)
        graph = _seeded_graph(2, "pre-existing")
        nodes_before = sorted(graph.nodes)

        with Stubber(client) as stubber:
            stubber.add_client_error(
                "get_object", service_error_code=code, http_status_code=status
            )

            with pytest.raises(ClientError) as raised:
                store.load(graph)

        assert raised.value.response["Error"]["Code"] == code
        assert sorted(graph.nodes) == nodes_before, (
            "the failed load cleared the graph before discovering the error, so "
            "the caller is left holding a half-loaded graph"
        )

    def test_a_corrupt_body_raises(self):
        """GIVEN an object whose body is not a parseable snapshot,
        THEN ``load`` raises rather than reporting "no snapshot".
        """
        client = _s3_client()
        store = snapshot.SnapshotStore(URI, client=client)
        graph = ContextGraph(advanced_analytics=False)

        with Stubber(client) as stubber:
            stubber.add_response(
                "get_object",
                {"Body": _streaming(b"}{ not json")},
                {"Bucket": BUCKET, "Key": KEY},
            )

            with pytest.raises(json.JSONDecodeError):
                store.load(graph)

    def test_the_graph_lock_is_free_while_the_upload_runs(self, monkeypatch):
        """GIVEN a save in progress,
        WHEN the upload call itself is executing,
        THEN another thread can still take ``graph._lock``.

        Serialisation must hold the lock; the upload must not, or every request
        blocks for the duration of a network round trip. The probe runs in a
        *separate* thread on purpose: ``_lock`` is an ``RLock``, so a same-thread
        acquire would succeed even if the lock were held, and prove nothing.
        """
        graph = _seeded_graph(3, "s3-lock")
        client = _s3_client()
        store = snapshot.SnapshotStore(URI, client=client)
        probed = []

        def put_object(**_kwargs):
            def probe():
                acquired = graph._lock.acquire(blocking=False)
                probed.append(acquired)
                if acquired:
                    graph._lock.release()

            worker = threading.Thread(target=probe, daemon=True)
            worker.start()
            worker.join(timeout=5.0)
            return {}

        monkeypatch.setattr(client, "put_object", put_object)

        store.save(graph)

        assert probed == [True], (
            "the graph lock was still held while put_object ran, so every "
            "mutation blocks for the length of the upload: {}".format(probed)
        )

    def test_description_names_the_bucket_and_key_without_a_credential(
        self, monkeypatch
    ):
        """GIVEN credentials present in the environment,
        THEN the description names bucket and key and repeats none of them.

        The description goes into a start-up log line, which is the easiest
        place in the system to leak a long-lived access key.
        """
        secrets = {
            "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
            "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "AWS_SESSION_TOKEN": "FwoGZXIvYXdzEXAMPLESESSIONTOKEN",
        }
        for name, value in secrets.items():
            monkeypatch.setenv(name, value)
        store = snapshot.SnapshotStore(URI, client=_s3_client())

        described = store.description

        assert BUCKET in described and KEY in described
        for name, value in secrets.items():
            assert value not in described, "{} leaked into the log line".format(name)


class TestS3WithoutBoto3:
    """boto3 is an optional extra, so the module must import without it."""

    def test_using_an_s3_store_names_the_extra_to_install(self, monkeypatch, tmp_path):
        """GIVEN boto3 is not importable,
        WHEN an S3 store is used,
        THEN the error names the extra that fixes it.

        Simulated by patching the module flag rather than uninstalling boto3, so
        the check runs in a normal environment.
        """
        monkeypatch.setattr(snapshot, "BOTO3_AVAILABLE", False)
        store = snapshot.SnapshotStore(URI)
        expected = re.escape("semantica[cloud]")

        with pytest.raises(ProcessingError, match=expected):
            store.save(_seeded_graph())
        with pytest.raises(ProcessingError, match=expected):
            store.load(ContextGraph(advanced_analytics=False))

    def test_a_local_store_still_works_without_boto3(self, monkeypatch, tmp_path):
        """GIVEN boto3 is not importable,
        THEN the filesystem destination is unaffected -- the guard must gate
        only the S3 path, not the module.
        """
        monkeypatch.setattr(snapshot, "BOTO3_AVAILABLE", False)
        path = tmp_path / "graph.json"
        store = snapshot.SnapshotStore(str(path))

        store.save(_seeded_graph(2, "no-boto3"))
        restored = ContextGraph(advanced_analytics=False)
        assert store.load(restored) is True
        assert restored.graph_id == "no-boto3"


class TestEnvironmentConfiguration:
    """``SEMANTICA_SNAPSHOT_URI`` and ``SEMANTICA_SNAPSHOT_INTERVAL``."""

    @pytest.mark.parametrize(
        "env", [{}, {"SEMANTICA_SNAPSHOT_URI": ""}, {"SEMANTICA_SNAPSHOT_URI": "   "}]
    )
    def test_no_uri_disables_persistence(self, env):
        """GIVEN the URI is unset or blank,
        THEN no store is built, which is how persistence is turned off.
        """
        assert snapshot.snapshot_store_from_env(env) is None

    def test_a_local_uri_builds_a_filesystem_store(self, tmp_path):
        """GIVEN a plain path in the environment,
        THEN the returned store targets that path.
        """
        path = str(tmp_path / "graph.json")

        store = snapshot.snapshot_store_from_env({"SEMANTICA_SNAPSHOT_URI": path})

        assert store is not None and store.path == path

    def test_an_s3_uri_builds_an_object_store(self):
        """GIVEN an ``s3://`` URI in the environment,
        THEN the returned store targets that bucket and key.
        """
        store = snapshot.snapshot_store_from_env({"SEMANTICA_SNAPSHOT_URI": URI})

        assert store is not None
        assert (store.bucket, store.key) == (BUCKET, KEY)

    def test_the_default_interval_is_thirty_seconds(self):
        """GIVEN no interval configured,
        THEN the documented default applies.
        """
        assert snapshot.snapshot_interval_from_env({}) == 30

    def test_a_valid_interval_is_used(self):
        """GIVEN a positive integer,
        THEN it is used verbatim.
        """
        assert (
            snapshot.snapshot_interval_from_env({"SEMANTICA_SNAPSHOT_INTERVAL": "5"})
            == 5
        )

    @pytest.mark.parametrize("raw", ["abc", "0", "-1", "", "  ", "1.5"])
    def test_an_unusable_interval_falls_back_with_a_warning(self, raw, caplog):
        """GIVEN a non-numeric or non-positive interval,
        THEN the default applies and the operator is warned.

        A bad interval must not crash the server at start-up: the value is a
        tuning knob, and refusing to boot over it trades a slightly wrong
        snapshot cadence for a total outage.
        """
        env = {"SEMANTICA_SNAPSHOT_INTERVAL": raw}

        with caplog.at_level(logging.WARNING):
            assert snapshot.snapshot_interval_from_env(env) == 30

        if raw.strip():
            assert (
                "SEMANTICA_SNAPSHOT_INTERVAL" in caplog.text
            ), "an unusable interval was silently replaced by the default"

    def test_the_environment_defaults_to_os_environ(self, monkeypatch, tmp_path):
        """GIVEN no explicit mapping,
        THEN the real process environment is read.
        """
        path = str(tmp_path / "graph.json")
        monkeypatch.setenv("SEMANTICA_SNAPSHOT_URI", path)
        monkeypatch.setenv("SEMANTICA_SNAPSHOT_INTERVAL", "7")

        assert snapshot.snapshot_store_from_env().path == path
        assert snapshot.snapshot_interval_from_env() == 7


class TestSuspendedMutations:
    """Restoring a snapshot replays every node and edge through ``add_*``.

    Those calls fire the mutation callback, so a restore would broadcast the
    entire graph to connected browsers and, once the interval writer is
    listening, mark the freshly loaded graph as dirty. The flag suppresses that.
    """

    def test_the_callback_is_silent_inside_the_block_and_live_after_it(self):
        """GIVEN a graph with a mutation callback,
        WHEN nodes are added inside the block and then after it,
        THEN only the mutation after the block is announced.
        """
        graph, seen = _recording_graph()

        with snapshot.suspended_mutations(graph):
            graph.add_node("inside", "entity")

        assert seen == [], "a mutation inside the block was still broadcast: {}".format(
            seen
        )

        graph.add_node("outside", "entity")

        assert seen == ["ADD_NODE"], (
            "the callback was not re-enabled after the block, so live updates "
            "stay dead for the rest of the process's life"
        )

    def test_an_already_suspended_graph_stays_suspended(self):
        """GIVEN the flag is already True -- a nested or outer suspension,
        THEN leaving the block restores True, not False.

        Restoring a hard-coded False would re-arm the callback in the middle of
        an outer restore that had deliberately silenced it.
        """
        graph, seen = _recording_graph()
        graph._suspend_mutation_callback = True

        with snapshot.suspended_mutations(graph):
            assert graph._suspend_mutation_callback is True

        assert (
            graph._suspend_mutation_callback is True
        ), "the outer suspension was cancelled by the inner one"
        graph.add_node("still-quiet", "entity")
        assert seen == []

    def test_the_flag_is_restored_when_the_body_raises(self):
        """GIVEN the block's body raises,
        THEN the exception propagates and the flag is still restored.

        A failed restore that leaves mutations permanently suspended is the
        worst outcome: the process keeps serving and silently stops persisting.
        """
        graph, seen = _recording_graph()

        with pytest.raises(RuntimeError, match="load failed"):
            with snapshot.suspended_mutations(graph):
                raise RuntimeError("load failed")

        assert graph._suspend_mutation_callback is False
        graph.add_node("after", "entity")
        assert seen == ["ADD_NODE"], (
            "mutations are still suspended after a failed restore, so nothing "
            "will ever be broadcast or persisted again"
        )

    def test_a_restore_through_the_store_announces_nothing(self, tmp_path):
        """GIVEN a snapshot on disk,
        WHEN it is loaded inside the block,
        THEN the whole graph arrives without a single mutation announcement.

        This is the shape W3 uses at start-up.
        """
        path = tmp_path / "graph.json"
        store = snapshot.SnapshotStore(str(path))
        store.save(_seeded_graph(4, "restored"))
        graph, seen = _recording_graph()

        with snapshot.suspended_mutations(graph):
            assert store.load(graph) is True

        assert len(graph.nodes) == 4 and len(graph.edges) == 3
        assert seen == [], "restoring a snapshot broadcast {} mutations".format(
            len(seen)
        )


def test_the_module_imports_without_boto3(tmp_path):
    """GIVEN an interpreter where ``import boto3`` fails,
    THEN ``semantica.context.snapshot`` still imports and reports the flag off.

    Run in a subprocess with an import hook, because the guard runs once at
    module import and cannot be re-tested by monkeypatching in this process.
    The Docker image installs only ``.[explorer]``, so this is the shipped
    configuration, not a hypothetical one.
    """
    script = tmp_path / "no_boto3.py"
    script.write_text(
        "import sys\n"
        "\n"
        "class Blocker:\n"
        "    def find_module(self, name, path=None):\n"
        "        return self if name.split('.')[0] in ('boto3', 'botocore') else None\n"
        "\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('boto3', 'botocore'):\n"
        "            raise ImportError('blocked: ' + name)\n"
        "        return None\n"
        "\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "for name in list(sys.modules):\n"
        "    if name.split('.')[0] in ('boto3', 'botocore'):\n"
        "        del sys.modules[name]\n"
        "\n"
        "from semantica.context import snapshot\n"
        "\n"
        "assert snapshot.BOTO3_AVAILABLE is False, 'boto3 was importable after all'\n"
        "assert snapshot.SnapshotStore('/tmp/g.json').path == '/tmp/g.json'\n"
        "print('ok')\n",
        encoding="utf-8",
    )
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        cwd=os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ),
    )

    assert result.returncode == 0, (
        "the module does not import without boto3, so the wheel's default "
        "install would fail at start-up:\n{}".format(result.stderr)
    )
    assert "ok" in result.stdout
