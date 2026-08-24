#!/usr/bin/env python3
"""Regression tests for atomic snapshot writes in ``ContextGraph.save_to_file``.

The write used to be a plain ``open(path, "w")`` + ``json.dump``, which
truncates the destination before a single byte of the new payload is written.
Any failure part-way through therefore replaced a previously good snapshot with
an unparseable one, and ``load_from_file`` tolerates a *missing* file but not a
malformed one -- it raises ``json.JSONDecodeError``. A half-written snapshot is
not a degraded snapshot, it is a dead one.

Failures below are forced deterministically by monkeypatching ``json.dump`` and
``os.replace``, not by racing threads or killing processes: a snapshot-integrity
test that only fails sometimes is not a test.
"""

import json
import os
import stat
from pathlib import Path

import pytest

from semantica.context._atomic_write import _DEFAULT_FILE_MODE
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


def _boom(*_args, **_kwargs):
    """Stand-in for a step that fails part-way through saving."""
    raise RuntimeError("simulated failure during save")


class TestFailedSaveLeavesTheOldSnapshotIntact:
    """The core guarantee: a failed save must not destroy a good snapshot."""

    def test_previous_snapshot_survives_a_failure_mid_write(
        self, tmp_path, monkeypatch
    ):
        """GIVEN a good snapshot already on disk,
        WHEN a later save raises part-way through writing, after real bytes have
        reached the open file,
        THEN the destination still holds the original, loadable snapshot and no
        temporary file is left behind.

        The caller raises inside the ``atomic_replace`` block, so this is also
        the proof that an exception from the body skips the replace.
        """
        path = tmp_path / "graph.json"
        _seeded_graph(3, "original").save_to_file(str(path))
        good_bytes = path.read_bytes()

        def dump_partially_then_fail(_data, fp, **_kwargs):
            fp.write('{"graph_id": "half-written", "nodes": [{"id": "n0"')
            raise RuntimeError("simulated failure during save")

        monkeypatch.setattr(json, "dump", dump_partially_then_fail)
        with pytest.raises(RuntimeError):
            _seeded_graph(5, "replacement").save_to_file(str(path))
        monkeypatch.undo()

        assert path.read_bytes() == good_bytes, (
            "the failed save overwrote the destination -- the previous "
            "snapshot has been destroyed by a save that did not even succeed"
        )
        listing = sorted(os.listdir(str(tmp_path)))
        assert listing == ["graph.json"], f"a failed write leaked a temp: {listing}"
        reloaded = ContextGraph(advanced_analytics=False)
        reloaded.load_from_file(str(path))
        assert reloaded.graph_id == "original"
        assert sorted(reloaded.nodes) == ["n0", "n1", "n2"]

    def test_payload_is_streamed_into_the_temp_file(self, tmp_path, monkeypatch):
        """GIVEN a save,
        THEN the payload is written straight into the open temp file rather than
        materialised as one big string first, so peak memory stays flat.
        """
        path = tmp_path / "graph.json"
        real_dump = json.dump
        targets = []

        def record_dump(data, fp, **kwargs):
            targets.append(getattr(fp, "name", None))
            return real_dump(data, fp, **kwargs)

        monkeypatch.setattr(json, "dump", record_dump)
        _seeded_graph().save_to_file(str(path))
        monkeypatch.undo()

        assert targets, (
            "json.dump was never called -- the payload is being serialised to a "
            "string first, which doubles peak memory for a large snapshot"
        )
        assert targets[0] != str(path), "json.dump wrote to the destination directly"
        assert str(tmp_path) in targets[0]

    def test_previous_snapshot_survives_a_failed_replace(self, tmp_path, monkeypatch):
        """GIVEN a good snapshot already on disk,
        WHEN the ``os.replace`` that swaps the new payload in fails,
        THEN the destination is untouched, the error propagates, and the fully
        written temporary file is cleaned up rather than leaked.

        This is the failure mode that reaches the write path with real bytes
        already on disk, so it is what proves the temp-file cleanup runs.
        """
        path = tmp_path / "graph.json"
        _seeded_graph(3, "original").save_to_file(str(path))
        good_bytes = path.read_bytes()

        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(RuntimeError):
            _seeded_graph(5, "replacement").save_to_file(str(path))
        monkeypatch.undo()

        assert path.read_bytes() == good_bytes, "a failed replace clobbered the target"
        assert sorted(os.listdir(str(tmp_path))) == ["graph.json"], (
            "a failed os.replace leaked its temporary file: "
            f"{sorted(os.listdir(str(tmp_path)))}"
        )

    def test_temporary_file_lives_in_the_destination_directory(
        self, tmp_path, monkeypatch
    ):
        """GIVEN a save in progress,
        THEN the in-progress file sits in the destination's own directory.

        A temporary file under ``tempfile.gettempdir()`` makes the final
        ``os.replace`` fail with ``OSError: Invalid cross-device link`` whenever
        the destination is on another filesystem -- the normal case for a
        container volume or mounted data disk. Observed by listing the directory
        from inside ``os.replace``, so it holds whichever ``tempfile`` API the
        implementation picks.
        """
        target_dir = tmp_path / "snapshots"
        target_dir.mkdir()
        path = target_dir / "graph.json"
        seen = []

        def replace_then_fail(*args, **kwargs):
            seen.append(sorted(os.listdir(str(target_dir))))
            raise RuntimeError("simulated failure during save")

        monkeypatch.setattr(os, "replace", replace_then_fail)
        with pytest.raises(RuntimeError):
            _seeded_graph().save_to_file(str(path))

        assert seen, "os.replace was never reached, so nothing was observed"
        assert seen[0], (
            "no in-progress file was present in the destination directory "
            "before the replace -- the temporary file is being created "
            "elsewhere, which breaks os.replace across filesystems"
        )


class TestSuccessfulSave:
    """The fix must not cost anything the plain write already delivered."""

    def test_round_trips_nodes_and_edges(self, tmp_path):
        """GIVEN a saved graph containing non-ASCII content,
        WHEN a fresh ``ContextGraph`` loads the file,
        THEN nodes, edges and graph id come back, the raw file holds the
        characters literally (``ensure_ascii=False`` preserved), and no
        temporary file remains beside the snapshot.
        """
        path = tmp_path / "graph.json"
        original = _seeded_graph(4, "round-trip")
        original.add_node("uni", "entity", content="Zürich — 東京")
        original.save_to_file(str(path))

        reloaded = ContextGraph(advanced_analytics=False)
        reloaded.load_from_file(str(path))

        assert reloaded.graph_id == "round-trip"
        assert sorted(reloaded.nodes) == sorted(original.nodes)
        assert len(reloaded.edges) == len(original.edges)
        assert reloaded.nodes["uni"].content == "Zürich — 東京"
        assert "Zürich — 東京" in path.read_text(
            encoding="utf-8"
        ), "non-ASCII content was escaped; ensure_ascii=False must survive"
        listing = sorted(os.listdir(str(tmp_path)))
        assert listing == ["graph.json"], f"temp file survived a good save: {listing}"

    def test_destination_is_untouched_until_the_replace(self, tmp_path, monkeypatch):
        """GIVEN an existing snapshot,
        WHEN a new save runs to completion,
        THEN the destination still held the old complete payload at the instant
        before ``os.replace`` ran, and holds the complete new payload after.
        """
        path = tmp_path / "graph.json"
        _seeded_graph(3, "before").save_to_file(str(path))
        good_bytes = path.read_bytes()

        real_replace = os.replace
        peek = {}

        def replace_and_peek(*args, **kwargs):
            peek["bytes"] = path.read_bytes()
            return real_replace(*args, **kwargs)

        monkeypatch.setattr(os, "replace", replace_and_peek)
        _seeded_graph(6, "after").save_to_file(str(path))
        monkeypatch.undo()

        assert peek["bytes"] == good_bytes, (
            "the destination changed before the replace -- a concurrent reader "
            "could see a partial file"
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["graph_id"] == "after"
        assert len(payload["nodes"]) == 6
        assert len(payload["edges"]) == 5

    def test_symlinked_destination_is_written_through(self, tmp_path):
        """GIVEN the destination is a symlink to a file in another directory,
        WHEN the graph is saved to the link,
        THEN the link still exists and its target holds the new payload.

        ``os.replace`` acts on the final path component, so without resolving
        the link first a save would destroy the symlink and leave the real
        snapshot behind untouched -- where ``open(path, "w")`` wrote through it.
        """
        real_dir = tmp_path / "snapshots"
        real_dir.mkdir()
        real_file = real_dir / "2026-08-24.json"
        _seeded_graph(2, "old").save_to_file(str(real_file))
        link = tmp_path / "current.json"
        link.symlink_to(real_file)

        _seeded_graph(5, "new").save_to_file(str(link))

        assert link.is_symlink(), "the save replaced the symlink with a regular file"
        payload = json.loads(real_file.read_text(encoding="utf-8"))
        assert payload["graph_id"] == "new", "the symlink's target was not updated"
        assert len(payload["nodes"]) == 5


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
class TestSnapshotPermissions:
    """``os.replace`` carries the *source* file's mode onto the destination.

    ``tempfile`` creates at 0600, so an atomic write silently demotes a snapshot
    the operator or a sidecar reader depends on. A save must reproduce what
    ``open(path, "w")`` did: keep an existing file's mode, and let the umask
    decide a new one.
    """

    def test_existing_mode_is_preserved(self, tmp_path):
        """GIVEN a snapshot already on disk at mode 0644,
        WHEN it is saved again,
        THEN it is still 0644 -- a save does not revoke anyone's read access.
        """
        path = tmp_path / "graph.json"
        _seeded_graph().save_to_file(str(path))
        os.chmod(str(path), 0o644)

        _seeded_graph(4).save_to_file(str(path))

        mode = stat.S_IMODE(os.stat(str(path)).st_mode)
        assert mode == 0o644, (
            f"saving demoted the snapshot from 0o644 to {oct(mode)}; readers "
            "that relied on the operator's mode silently lose access"
        )

    def test_new_snapshot_uses_the_umask(self, tmp_path):
        """GIVEN no file at the destination,
        THEN the new snapshot gets the umask-derived mode a plain open() would
        have produced, rather than tempfile's owner-only 0600.
        """
        path = tmp_path / "graph.json"

        _seeded_graph().save_to_file(str(path))

        mode = stat.S_IMODE(os.stat(str(path)).st_mode)
        assert mode == _DEFAULT_FILE_MODE, (
            f"new snapshot is {oct(mode)}, not the {oct(_DEFAULT_FILE_MODE)} a "
            "plain open() would have created under this umask"
        )
        assert _DEFAULT_FILE_MODE & 0o600, "the writer cannot read back its own file"

    def test_setgid_is_not_propagated(self, tmp_path):
        """GIVEN an existing snapshot carrying setgid,
        THEN a save copies the permission bits but drops setgid, matching a
        plain write (Linux strips setgid on write by a non-owner).
        """
        path = tmp_path / "graph.json"
        _seeded_graph().save_to_file(str(path))
        os.chmod(str(path), 0o2644)

        _seeded_graph(4).save_to_file(str(path))

        mode = os.stat(str(path)).st_mode
        assert not mode & stat.S_ISGID, "setgid was propagated onto the new file"
        assert stat.S_IMODE(mode) == 0o644


class TestPreservedFailureModes:
    """Behaviour deliberately left alone by the atomic-write change."""

    def test_missing_parent_directory_still_raises(self, tmp_path):
        """GIVEN a path whose parent directory does not exist,
        THEN saving raises rather than silently creating the directory. The
        caller chooses where snapshots live; ``save_to_file`` does not.
        """
        path = tmp_path / "no_such_dir" / "graph.json"

        with pytest.raises(FileNotFoundError):
            _seeded_graph().save_to_file(str(path))

        assert not Path(
            str(tmp_path / "no_such_dir")
        ).exists(), "save_to_file created the missing parent directory"
