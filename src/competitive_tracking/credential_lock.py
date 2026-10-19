"""Cooperative, process-scoped lock for a Chromium user-data directory.

Other projects sharing credentials must lock the same first byte of the same
``.login_credentials.lock`` file before launching Chromium, and hold it until
the browser process has exited. The persistent lock file must never be removed
or replaced: ownership is represented by the operating system's lock only.
"""

from __future__ import annotations

import errno
import math
import os
from pathlib import Path
import time


class CredentialDirectoryLock:
    """An exclusive, bounded-wait lock, released automatically on process exit."""

    def __init__(self, profile_dir: str | Path):
        self.profile_dir = Path(profile_dir).resolve()
        self.path = self.profile_dir / ".login_credentials.lock"
        self._file = None

    @property
    def locked(self) -> bool:
        return self._file is not None

    @staticmethod
    def _try_lock(file):
        file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(file):
        file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _busy(exc: OSError) -> bool:
        # File opening/initialization errors are handled separately; EACCES
        # here is the OS's documented nonblocking lock-conflict result.
        return exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) or (
            os.name == "nt" and getattr(exc, "winerror", None) in (32, 33)
        )

    def acquire(self, timeout_seconds: float = 60, poll_seconds: float = 0.5):
        if self.locked:
            raise RuntimeError(f"当前实例已持有登录凭证目录锁：{self.profile_dir}")
        if (not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool)
                or not math.isfinite(timeout_seconds) or timeout_seconds < 0):
            raise ValueError("timeout_seconds 必须为不小于 0 的有限秒数")
        if (not isinstance(poll_seconds, (int, float)) or isinstance(poll_seconds, bool)
                or not math.isfinite(poll_seconds) or poll_seconds <= 0):
            raise ValueError("poll_seconds 必须为大于 0 的有限秒数")
        file = None
        try:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            file = self.path.open("a+b")
            if os.fstat(file.fileno()).st_size == 0:
                file.write(b"\0")
                file.flush()
        except OSError as exc:
            if file is not None:
                file.close()
            raise RuntimeError(f"无法创建或打开登录凭证目录锁：{self.profile_dir}（errno={exc.errno}）") from exc
        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                try:
                    self._try_lock(file)
                except OSError as exc:
                    if not self._busy(exc):
                        raise RuntimeError(f"登录凭证目录加锁失败：{self.profile_dir}（errno={exc.errno}）") from exc
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError(
                            f"登录凭证目录正在被其他程序占用，等待 {timeout_seconds:g} 秒后仍未释放：{self.profile_dir}"
                        ) from exc
                    time.sleep(min(poll_seconds, remaining))
                else:
                    self._file = file
                    return self
        except BaseException:
            file.close()
            raise

    def release(self):
        """Release ownership without deleting the shared protocol file."""
        file, self._file = self._file, None
        if file is None:
            return
        try:
            self._unlock(file)
        except OSError as exc:
            raise RuntimeError(f"登录凭证目录解锁失败：{self.profile_dir}（errno={exc.errno}）") from exc
        finally:
            file.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()
