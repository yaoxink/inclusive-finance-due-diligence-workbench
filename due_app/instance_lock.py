"""Exclusive ownership of a due-app data root for the service lifetime.

The open OS lock, rather than a PID or the existence of this file, establishes
ownership. Leave the file in place so a successor never locks a different inode.
"""
from __future__ import annotations

import os
from pathlib import Path
import threading


class InstanceBusy(RuntimeError):
    def __init__(self):
        super().__init__('该尽调数据目录已由另一个服务使用，请先关闭原服务。')


_guard = threading.RLock()
_held: set[str] = set()


class InstanceLock:
    def __init__(self, data_root):
        self.root = Path(data_root).resolve()
        self.path = self.root / '.due-instance.lock'
        self._key = os.path.normcase(str(self.root))
        self._fd: int | None = None

    def acquire(self):
        with _guard:
            if self._fd is not None:
                return self
            if self._key in _held:
                raise InstanceBusy()
            self.root.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                # Windows byte-range locking also works on an empty file, but a
                # permanent byte makes the covered range explicit across hosts.
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b'\0')
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == 'nt':
                    import msvcrt
                    try:
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    except OSError as exc:
                        if exc.errno not in (13, 11, 36):
                            raise
                        raise InstanceBusy() from exc
                else:
                    import fcntl
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as exc:
                        raise InstanceBusy() from exc
            except BaseException:
                os.close(fd)
                raise
            self._fd = fd
            _held.add(self._key)
            return self

    def release(self):
        with _guard:
            if self._fd is None:
                return
            fd, self._fd = self._fd, None
            try:
                if os.name == 'nt':
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
                _held.discard(self._key)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()
