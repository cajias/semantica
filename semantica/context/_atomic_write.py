"""Atomic text file replacement shared by the context package's writers."""

import contextlib
import os
import stat
import tempfile
from pathlib import Path
from typing import IO, Iterator, Optional, Union


def _default_file_mode() -> int:
    """The mode a plain ``open(path, "w")`` gives a new file under this umask."""
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


# Sampled once at import, while the process is still effectively single-threaded:
# os.umask(0) sets a process-global value, so reading it per write would briefly
# expose 0666 to a file another thread happens to create in that window.
_DEFAULT_FILE_MODE = _default_file_mode()


@contextlib.contextmanager
def atomic_replace(
    path: Union[str, Path], mode: Optional[int] = None
) -> Iterator[IO[str]]:
    """Yield a writable temp file whose content replaces *path* atomically.

    Writing in place truncates the destination before the new content lands, so
    a failure part-way through destroys a good file. An exception from the body
    skips the replace and removes the temp file, leaving *path* untouched.

    Callers own their symlink policy: os.replace swaps the directory entry
    itself, so a symlinked *path* is replaced rather than followed.

    Requires write permission on the destination's directory, not just the file.

    Args:
        path: Destination to replace.
        mode: Permission bits to enforce, applied whether or not the
            destination already exists -- pass ``0o600`` for anything private.
            When omitted, an existing destination keeps its own mode and a new
            one gets what a plain ``open(path, "w")`` would have given, which
            is what a snapshot an operator reads wants.
    """
    file_path = os.fspath(path)
    # The temp file must share a filesystem with the destination or os.replace
    # fails with EXDEV, hence dir= rather than /tmp. dirname of a bare filename
    # is "", so abspath first.
    directory = os.path.dirname(os.path.abspath(file_path))
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix="." + os.path.basename(file_path) + ".",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            yield temporary_file
            temporary_file.flush()
            # ponytail: the file is fsynced but its directory is not, so a power
            # loss just after the replace could lose the rename. Upgrade: fsync
            # an os.open(directory, os.O_RDONLY) fd after os.replace.
            os.fsync(temporary_file.fileno())

        # tempfile creates at 0600 and os.replace carries the temp file's mode
        # onto the destination. Without an explicit mode, keep an existing
        # destination's permission bits, masked to 0o777 because a plain write
        # would not have propagated setuid/setgid/sticky, and fall back to the
        # umask default for a new file. An explicit mode always wins: a caller
        # asking for 0600 means it, including when overwriting a 0644 file
        # left behind by an older build, a restore, or a tarball.
        if mode is None:
            try:
                final_mode = stat.S_IMODE(os.stat(file_path).st_mode) & 0o777
            except OSError:
                final_mode = _DEFAULT_FILE_MODE
        else:
            final_mode = mode
        os.chmod(temporary_path, final_mode)

        os.replace(temporary_path, file_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def atomic_write_text(
    path: Union[str, Path], text: str, mode: Optional[int] = None
) -> None:
    """Write *text* to *path* via a temp file in the same directory + os.replace."""
    with atomic_replace(path, mode) as temporary_file:
        temporary_file.write(text)
