"""Cross-platform advisory file locks.

The core assumed POSIX `fcntl.flock` everywhere; Windows has no `fcntl`, so the service either
locked nothing (import-time fallback) or could not be imported at all. `msvcrt.locking` provides
the same OS-level advisory lock on Windows, but only in non-blocking mode, so a blocking acquire
polls. The lock covers byte 0 of the file and is released explicitly or when the handle closes
(Windows honors both; it also blocks a second lock of the same region even from the same process
through a different handle, which is stricter than flock — our callers never rely on the looser
same-process sharing, so this is safe).
"""
import errno
import os

__all__ = ["acquire", "release"]


def acquire(fh, *, blocking: bool = True) -> bool:
    """Take an exclusive advisory lock on `fh`'s open file.

    Returns True when the lock is held. A non-blocking acquire that finds the region already
    locked returns False; a blocking acquire waits (polling on Windows, `flock` semantics on
    POSIX) and raises only on real errors.
    """
    fno = fh.fileno()
    try:
        import fcntl
    except ImportError:
        import msvcrt
        import time

        while True:
            # Seek failures are descriptor/filesystem faults, never lock contention.
            os.lseek(fno, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fno, msvcrt.LK_NBLCK, 1)
                return True
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                # LK_NBLCK reports an occupied byte range as EACCES on Windows.
                if exc.errno != errno.EACCES:
                    raise
                if not blocking:
                    return False
                time.sleep(0.01)
    else:
        while True:
            try:
                fcntl.flock(fno, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                if not blocking and exc.errno in (errno.EACCES, errno.EAGAIN):
                    return False
                raise


def release(fh) -> None:
    """Release the lock taken by `acquire`. A failed unlock is not fatal: closing the handle
    drops the lock anyway."""
    fno = fh.fileno()
    try:
        import fcntl
    except ImportError:
        import msvcrt

        try:
            os.lseek(fno, 0, os.SEEK_SET)
            msvcrt.locking(fno, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    fcntl.flock(fno, fcntl.LOCK_UN)
