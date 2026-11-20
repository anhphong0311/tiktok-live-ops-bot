from __future__ import annotations

import msvcrt
import time
from pathlib import Path
from typing import BinaryIO


class SingleInstance:
    def __init__(self, path: Path):
        self.path = path
        self.handle: BinaryIO | None = None

    def __enter__(self) -> "SingleInstance":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        try:
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("Bot đang chạy ở một tiến trình khác") from exc
        return self

    def __exit__(self, *_: object) -> None:
        if self.handle:
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            self.handle.close()
            self.handle = None


class TimedFileLock:
    """Small Windows inter-process mutex backed by a one-byte file lock."""

    def __init__(self, path: Path, timeout_seconds: float = 90):
        self.path = path
        self.timeout_seconds = max(0.0, float(timeout_seconds))
        self.handle: BinaryIO | None = None

    def __enter__(self) -> "TimedFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            self.handle.seek(0)
            try:
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                return self
            except OSError as exc:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise RuntimeError(
                        "Zalo đang được một tiến trình thông báo khác sử dụng"
                    ) from exc
                time.sleep(0.25)

    def __exit__(self, *_: object) -> None:
        if self.handle:
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            self.handle.close()
            self.handle = None

