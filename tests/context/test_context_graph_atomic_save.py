#!/usr/bin/env python3
"""Regression tests for atomic snapshot writes in ``ContextGraph.save_to_file``.

``save_to_file`` builds its JSON payload under ``self._lock`` and then writes it
out. The write used to be a plain ``open(path, "w")`` + ``json.dump``, which
truncates the destination *before* a single byte of the new payload is written.
Any failure part-way through serialisation -- an exception, a full disk, a
crashed process -- therefore replaced a previously good snapshot with a
truncated, unparseable file. ``load_from_file`` tolerates a *missing* file (it
logs a warning and returns) but not a malformed one: it raises
``json.JSONDecodeError``. So a half-written snapshot is not a degraded
snapshot, it is a dead one.

The fix serialises to a temporary file in the destination's own directory,
fsyncs it, and then ``os.replace``s it over the destination. ``os.replace`` is
atomic on POSIX and Windows, so a reader sees either the old snapshot or the
new one -- never a partial one.

The failures below are forced deterministically by monkeypatching
``json.dump``, not by racing threads or killing processes: a snapshot-integrity
test that only fails sometimes is not a test.
"""

import json
import os

import pytest

from semantica.context.context_graph import ContextGraph


def _seeded_graph(node_count: int = 3, graph_id: str = "ctx-atomic") -> ContextGraph:
    """A small graph with nodes and edges, enough to round trip meaningfully."""
    graph = ContextGraph(advanced_analytics=False)
    graph.graph_id = graph_id
    for i in range(node_count):
        graph.add_node(f"n{i}", "entity", content=f"content {i}")
    for i in range(node_count - 1):
        graph.add_edge(f"n{i}", f"n{i + 1}", "related_to")
    return graph


def _fail_after_partial_write(*_args, **_kwargs):
    """Stand-in for ``json.dump`` that emits real bytes and then blows up.

    Mirrors a disk filling up or a serialisation error on the last node: the
    file handle has already received output, so whatever that handle points at
    is now invalid JSON.
    """
    fp = _args[1]
    fp.write('{"graph_id": "half-written", "nodes": [{"id": "n0"')
    raise RuntimeError("simulated failure part-way through serialisation")


class TestFailedSaveLeavesTheOldSnapshotIntact:
    """The core guarantee: a failed save must not destroy a good snapshot."""

    def test_previous_snapshot_survives_a_failed_save(self, tmp_path, monkeypatch):
        """GIVEN a good snapshot already on disk,
        WHEN a later ``save_to_file`` to the same path fails mid-serialisation,
        THEN the file on disk is still the original, loadable snapshot.
        """
        path = str(tmp_path / "graph.json")
        _seeded_graph(3, "original").save_to_file(path)
        good_bytes = open(path, "rb").read()

        monkeypatch.setattr(json, "dump", _fail_after_partial_write)
        with pytest.raises(RuntimeError):
            _seeded_graph(5, "replacement").save_to_file(path)

        assert open(path, "rb").read() == good_bytes, (
            "the failed save overwrote the destination -- the previous "
            "snapshot has been destroyed by a save that did not even succeed"
        )

        monkeypatch.undo()
        reloaded = ContextGraph(advanced_analytics=False)
        reloaded.load_from_file(path)
        assert reloaded.graph_id == "original"
        assert sorted(reloaded.nodes) == ["n0", "n1", "n2"], (
            "the surviving file parsed but does not describe the original "
            "graph, so the destination was still modified"
        )

    def test_a_failing_save_raises_to_the_caller(self, tmp_path, monkeypatch):
        """GIVEN a save that cannot complete,
        THEN the error propagates -- it is never swallowed into a silent
        no-op that leaves the caller believing it persisted.
        """
        path = str(tmp_path / "graph.json")
        monkeypatch.setattr(json, "dump", _fail_after_partial_write)

        with pytest.raises(RuntimeError, match="part-way through serialisation"):
            _seeded_graph().save_to_file(path)

    def test_failed_save_leaves_no_files_behind(self, tmp_path, monkeypatch):
        """GIVEN a failed save over an existing snapshot,
        THEN the target directory contains exactly the destination file -- no
        orphaned temporary file is left to accumulate on every failure.
        """
        path = str(tmp_path / "graph.json")
        _seeded_graph().save_to_file(path)

        monkeypatch.setattr(json, "dump", _fail_after_partial_write)
        with pytest.raises(RuntimeError):
            _seeded_graph().save_to_file(path)

        assert sorted(os.listdir(str(tmp_path))) == ["graph.json"], (
            "leftover files in the target directory after a failed save: "
            f"{sorted(os.listdir(str(tmp_path)))}"
        )

    def test_temporary_file_lives_in_the_destination_directory(
        self, tmp_path, monkeypatch
    ):
        """GIVEN a save in progress,
        THEN the in-progress file sits in the destination's own directory.

        A temporary file under ``tempfile.gettempdir()`` makes the final
        ``os.replace`` fail with ``OSError: Invalid cross-device link`` whenever
        the destination is on a different filesystem -- which is the normal case
        for a container volume or a mounted data disk. Asserted by listing the
        directory from inside the serialisation step, so it holds regardless of
        which ``tempfile`` API the implementation picks.
        """
        target_dir = tmp_path / "snapshots"
        target_dir.mkdir()
        path = str(target_dir / "graph.json")
        seen = []

        def dump_then_fail(*args, **kwargs):
            # Appended as a single element so an empty listing still records
            # that serialisation was reached.
            seen.append(sorted(os.listdir(str(target_dir))))
            return _fail_after_partial_write(*args, **kwargs)

        monkeypatch.setattr(json, "dump", dump_then_fail)
        with pytest.raises(RuntimeError):
            _seeded_graph().save_to_file(path)

        assert seen, "json.dump was never reached, so nothing was observed"
        assert [name for name in seen[0] if name != "graph.json"], (
            "no in-progress file was present in the destination directory "
            f"during serialisation (saw {seen[0]}) -- the temporary file is "
            "being created elsewhere, which breaks os.replace across "
            "filesystems"
        )


class TestSuccessfulSave:
    """The fix must not cost anything the plain write already delivered."""

    def test_round_trips_nodes_and_edges(self, tmp_path):
        """GIVEN a saved graph,
        WHEN a fresh ``ContextGraph`` loads the file,
        THEN nodes, edges and graph id come back, and no temporary file
        remains next to the snapshot.
        """
        path = str(tmp_path / "graph.json")
        original = _seeded_graph(4, "round-trip")
        original.save_to_file(path)

        reloaded = ContextGraph(advanced_analytics=False)
        reloaded.load_from_file(path)

        assert reloaded.graph_id == "round-trip"
        assert sorted(reloaded.nodes) == sorted(original.nodes)
        assert len(reloaded.edges) == len(original.edges)
        assert sorted(os.listdir(str(tmp_path))) == ["graph.json"], (
            "a temporary file survived a successful save: "
            f"{sorted(os.listdir(str(tmp_path)))}"
        )

    def test_destination_is_never_observed_truncated(self, tmp_path, monkeypatch):
        """GIVEN an existing snapshot,
        WHEN a new save runs to completion,
        THEN the destination still held the *old* complete payload while the
        new one was being serialised, and holds the complete new payload
        afterwards. This is the ``os.replace`` guarantee: no window in which a
        reader can see a truncated file.
        """
        path = str(tmp_path / "graph.json")
        _seeded_graph(3, "before").save_to_file(path)
        good_bytes = open(path, "rb").read()

        real_dump = json.dump
        mid_write = {}

        def dump_and_peek(*args, **kwargs):
            result = real_dump(*args, **kwargs)
            mid_write["bytes"] = open(path, "rb").read()
            return result

        monkeypatch.setattr(json, "dump", dump_and_peek)
        _seeded_graph(6, "after").save_to_file(path)
        monkeypatch.undo()

        assert mid_write["bytes"] == good_bytes, (
            "the destination changed while the new payload was still being "
            "serialised -- a concurrent reader could see a partial file"
        )
        payload = json.loads(open(path, encoding="utf-8").read())
        assert payload["graph_id"] == "after"
        assert len(payload["nodes"]) == 6
        assert len(payload["edges"]) == 5

    def test_non_ascii_content_survives_unescaped(self, tmp_path):
        """GIVEN a node whose content is non-ASCII,
        THEN the raw file holds the characters literally, proving
        ``ensure_ascii=False`` was preserved through the rewrite.
        """
        path = str(tmp_path / "graph.json")
        graph = ContextGraph(advanced_analytics=False)
        graph.add_node("n0", "entity", content="Zürich — 東京 café")
        graph.save_to_file(path)

        raw = open(path, encoding="utf-8").read()
        assert "Zürich — 東京 café" in raw, (
            "non-ASCII content was escaped or mangled; ensure_ascii=False "
            "and encoding='utf-8' must both survive"
        )

        reloaded = ContextGraph(advanced_analytics=False)
        reloaded.load_from_file(path)
        assert reloaded.nodes["n0"].content == "Zürich — 東京 café"

    def test_existing_file_is_replaced_not_appended(self, tmp_path):
        """GIVEN a destination path that already holds a larger snapshot,
        THEN a successful save leaves exactly the new payload -- the old bytes
        are gone rather than appended to or partially overwritten.
        """
        path = str(tmp_path / "graph.json")
        _seeded_graph(20, "big").save_to_file(path)
        _seeded_graph(2, "small").save_to_file(path)

        payload = json.loads(open(path, encoding="utf-8").read())
        assert payload["graph_id"] == "small"
        assert len(payload["nodes"]) == 2, (
            "the destination holds more nodes than the last save wrote, so "
            "old content leaked through"
        )


class TestPreservedFailureModes:
    """Behaviour deliberately left alone by the atomic-write change."""

    def test_missing_parent_directory_still_raises(self, tmp_path):
        """GIVEN a path whose parent directory does not exist,
        THEN saving raises rather than silently creating the directory. The
        caller chooses where snapshots live; ``save_to_file`` does not.
        """
        path = str(tmp_path / "no_such_dir" / "graph.json")

        with pytest.raises(FileNotFoundError):
            _seeded_graph().save_to_file(path)

        assert not os.path.exists(str(tmp_path / "no_such_dir")), (
            "save_to_file created the missing parent directory; that is a "
            "behaviour change callers do not expect"
        )
