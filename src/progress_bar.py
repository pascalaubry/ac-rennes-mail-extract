"""A single-line, byte-based progress bar rendered on stderr."""

from __future__ import annotations

import shutil
import sys
import time


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


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

    def _render(self) -> None:
        frac = min(self.done / self.total, 1.0) if self.total else 1.0
        filled = round(frac * self.width)
        bar = "#" * filled + "-" * (self.width - filled)
        count = f"{self.msgs:,} msg  " if self.show_count else ""
        line = (f"[{bar}] {frac * 100:5.1f}%  "
                f"{human_bytes(self.done)}/{human_bytes(self.total)}  "
                f"{count}{self.label}")
        cols = shutil.get_terminal_size((80, 20)).columns
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


class CountingStream:
    """A read-only pass-through over a binary stream that ticks a ProgressBar
    with the number of bytes read (e.g. wrapping the compressed archive file
    while tarfile streams through it)."""

    def __init__(self, fh, bar: ProgressBar):
        self._fh = fh
        self._bar = bar

    def read(self, size: int = -1) -> bytes:
        chunk = self._fh.read(size)
        self._bar.update(add=len(chunk))
        return chunk

    def close(self) -> None:
        self._fh.close()
