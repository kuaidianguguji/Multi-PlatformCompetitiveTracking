from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path


def atomic_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


class JsonSink:
    def __init__(self, directory: Path):
        self.directory = directory

    def write(self, result: dict):
        atomic_json(self.directory / f"run_{result['run_id']}.json", result)
        atomic_json(self.directory / "latest.json", result)


@contextmanager
def single_instance(directory: Path):
    """OS file lock releases automatically on crash; no stale PID lock cleanup needed."""
    directory.mkdir(parents=True, exist_ok=True)
    file = (directory / "runner.lock").open("a+b")
    if file.tell() == 0:
        file.write(b"0")
        file.flush()
    file.seek(0)
    try:
        if __import__("os").name == "nt":
            import msvcrt
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        file.close()
        raise RuntimeError("已有 CompetitiveTracking 实例使用此会话目录") from None
    try:
        yield
    finally:
        file.close()
