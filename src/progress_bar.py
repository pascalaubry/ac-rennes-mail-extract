"""A single-line, byte-based progress bar rendered on stderr."""

from __future__ import annotations

import io
import shutil
import sys
import time
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
    from _typeshed import WriteableBuffer


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


class ProgressBar:
    """A single-line byte-based progress bar rendered on stderr."""

    def __init__(self, total: int, width: int = 30, show_count: bool = True):
        self.total = total
        self.enabled = total > 0
        self.width = width
        self.show_count = show_count
        self.done = 0
        self.msgs = 0
        self.label = ""
        self._last = 0.0
        self._prevlen = 0
        self._start = time.monotonic()

    def update(self, *, add: int = 0, msgs_add: int = 0, done: int | None = None,
               label: str | None = None, force: bool = False) -> None:
        if not self.enabled:
            return
        self.done = done if done is not None else self.done + add
        self.msgs += msgs_add
        if label is not None:
            self.label = label
        now = time.monotonic()
        if force or now - self._last >= 0.15:
            self._last = now
            self._render()

    def _eta(self) -> str:
        if self.done <= 0:
            return "--"
        if self.done >= self.total:
            return "0s"
        elapsed = time.monotonic() - self._start
        rate = self.done / elapsed  # units per second so far
        if rate <= 0:
            return "--"
        return format_duration((self.total - self.done) / rate)

    def _render(self) -> None:
        frac = min(self.done / self.total, 1.0) if self.total else 1.0
        filled = round(frac * self.width)
        bar = "#" * filled + "-" * (self.width - filled)
        count = f"{self.msgs:,} msg  " if self.show_count else ""
        line = (f"[{bar}] {frac * 100:5.1f}%  "
                f"{human_bytes(self.done)}/{human_bytes(self.total)}  "
                f"ETA {self._eta()}  {count}{self.label}")
        cols = shutil.get_terminal_size((120, 20)).columns
        line = line[:cols - 1]
        pad = max(0, self._prevlen - len(line))
        sys.stderr.write("\r" + line + " " * pad)
        sys.stderr.flush()
        self._prevlen = len(line)

    def log(self, text: str) -> None:
        """Print a line without clobbering the bar."""
        if self.enabled and self._prevlen:
            sys.stderr.write("\r" + " " * self._prevlen + "\r")
            sys.stderr.flush()
            self._prevlen = 0
        print(text)
        if self.enabled:
            self._render()

    def close(self) -> None:
        if self.enabled and self._prevlen:
            self.update(force=True)
            sys.stderr.write("\n")
            sys.stderr.flush()
            self._prevlen = 0


class CountingStream(io.RawIOBase):
    """A read-only pass-through over a binary stream that ticks a ProgressBar
    with the number of bytes read (e.g. wrapping the compressed archive file
    while tarfile streams through it).

    Subclassing ``io.RawIOBase`` makes this a genuine binary file object, so it
    is accepted wherever an ``IO[bytes]`` is expected (``tarfile.open``'s
    ``fileobj``). ``RawIOBase`` implements ``read()`` on top of ``readinto()``.
    """

    def __init__(self, fh: IO[bytes], bar: ProgressBar) -> None:
        super().__init__()
        self._fh = fh
        self._bar = bar

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: WriteableBuffer) -> int:
        view = memoryview(buffer).cast("B")
        chunk = self._fh.read(len(view))
        view[: len(chunk)] = chunk
        self._bar.update(add=len(chunk))
        return len(chunk)

    def close(self) -> None:
        try:
            self._fh.close()
        finally:
            super().close()
