"""Atomic text file replacement shared by the context package's writers."""

import os
import stat
import tempfile
from pathlib import Path
from typing import Union


def atomic_write_text(
    path: Union[str, Path], text: str, encoding: str = "utf-8"
) -> None:
    """Write *text* to *path* via a temp file in the same directory + os.replace.

    Writing in place truncates the destination before the new content lands, so
    a failure part-way through destroys a good file. Callers own their symlink
    policy: os.replace swaps the directory entry itself, so a symlinked *path*
    is replaced rather than followed.

    Requires write permission on the destination's directory, not just the file.
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
            encoding=encoding,
            dir=directory,
            prefix="." + os.path.basename(file_path) + ".",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(text)
            temporary_file.flush()
            # ponytail: the file is fsynced but its directory is not, so a power
            # loss just after the replace could lose the rename. Upgrade: fsync
            # an os.open(directory, os.O_RDONLY) fd after os.replace.
            os.fsync(temporary_file.fileno())

        # tempfile creates at 0600 and os.replace carries the temp file's mode
        # onto the destination, so copy an existing destination's permission bits
        # over rather than silently narrowing it. Masked to 0o777 because a plain
        # write would not have propagated setuid/setgid/sticky.
        try:
            existing_mode = stat.S_IMODE(os.stat(file_path).st_mode)
        except OSError:
            pass  # the normal first-write path: no destination to copy from
        else:
            os.chmod(temporary_path, existing_mode & 0o777)

        os.replace(temporary_path, file_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
