"""
fs_utils — shared atomic file-writing helper.

All durable state written by CaddyPanel (users.json, preferences.json, the
Caddyfile) goes through atomic_write(): the data is written to a temporary
file created in the SAME directory as the target (hence the same filesystem),
flushed and fsync'd, then renamed onto the target with os.replace(). A crash
mid-write therefore can never leave a truncated or partially-written file
behind: readers see either the previous complete file or the new one.

The parent directory is fsync'd after the rename so the rename itself is also
durable across a power loss. Leftover temp files from a hard crash can be
removed at startup with cleanup_stale_tmp_files().
"""

import logging
import os
import stat
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def atomic_write(path, content, mode=None):
    """Write *content* (str) to *path* atomically and return True on success.

    Steps: create a temp file next to *path* -> write, flush and fsync ->
    chmod -> os.replace() onto the target (atomic on POSIX and Windows since
    both are on the same filesystem) -> best-effort fsync of the parent
    directory so the rename survives a crash/power loss. On failure the temp
    file is removed, the existing target is left untouched, the error is
    logged and False is returned.

    Permissions (important — the behaviour is explicit, not incidental):
        tempfile.mkstemp always creates the temp file 0o600 (owner-only).
        - mode given (e.g. 0o600): applied verbatim. Use this for files
          holding secrets (users.json, preferences.json).
        - mode=None (default): PRESERVE the permission bits of an existing
          target file, or fall back to 0o644 for a new file. This keeps the
          Caddyfile at its usual 0o644 instead of silently tightening it to
          the mkstemp default.

    Args:
        path: Target file path (str or Path).
        content: Full content to write (str, encoded as UTF-8).
        mode: Optional permission bits (e.g. 0o600) applied to the file;
              None preserves the existing file's mode (default 0o644).

    Returns:
        bool: True on success, False on any filesystem or encoding error.
    """
    path = Path(path)
    tmp_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent))
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        if mode is None:
            try:
                mode = stat.S_IMODE(path.stat().st_mode)
            except OSError:
                mode = 0o644
        os.chmod(str(tmp_path), mode)
        os.replace(str(tmp_path), str(path))
        tmp_path = None  # successfully moved into place
        _fsync_directory(path.parent)
        return True
    except (OSError, ValueError) as e:
        # OSError: filesystem errors. ValueError covers UnicodeEncodeError
        # (surrogates in *content*) so encoding failures behave like any
        # other failed write instead of leaking an exception.
        logger.error(f"atomic_write: failed writing '{path}': {e}")
        return False
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _fsync_directory(directory):
    """Best-effort fsync of a directory so a completed rename is durable.

    Without this, a power loss right after os.replace() can lose the rename
    itself (old file back on disk) even though the new content was fsync'd.
    Directory fsync is unsupported on some platforms/filesystems; failures
    are deliberately ignored because the file data is already durable.
    """
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def cleanup_stale_tmp_files(directory):
    """Delete leftover atomic_write() temp files ('.<name>.<random>.tmp').

    atomic_write removes its temp file even when the write fails, but a hard
    crash (SIGKILL, power loss) can leave some behind. They contain partial
    copies of potentially sensitive data (users.json, preferences.json, the
    Caddyfile) and must not linger readable in the data directory. Intended
    to run once at app startup; /api/readfile additionally refuses '*.tmp'
    names at read time (defense in depth).

    Returns the number of files removed.
    """
    removed = 0
    try:
        candidates = list(Path(directory).glob('.*.tmp'))
    except OSError as e:
        logger.warning(f"cleanup_stale_tmp_files: cannot scan '{directory}': {e}")
        return 0
    for leftover in candidates:
        try:
            leftover.unlink()
            removed += 1
            logger.warning(f"Removed stale temporary file left over by a crash: {leftover}")
        except OSError as e:
            logger.warning(f"Could not remove stale temporary file '{leftover}': {e}")
    return removed
