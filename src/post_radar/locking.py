"""OS-released lock: one runtime owns a state directory, including during shutdown."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

from post_radar.models import ExternalError


class RuntimeLock:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._stream: BinaryIO | None = None

    def __enter__(self) -> RuntimeLock:
        self.directory.mkdir(parents=True, exist_ok=True)
        stream = (self.directory / "runtime.lock").open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise ExternalError("already_running", permanent=True) from None
        self._stream = stream
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._stream is not None:
            # Closing the descriptor releases the OS lock, also on process termination.
            self._stream.close()
            self._stream = None
