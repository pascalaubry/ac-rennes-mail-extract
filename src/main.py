"""Extract a personal mailbox archive into a per-year tree of messages.

Usage:
    python src/main.py [options]

The script:

1. lists the archives in ``archives/`` and asks which one to process (if there
   is only one, it is used without asking); its name gives the mailbox base;
2. streams the archive member by member: each file is extracted into
   ``tmp/<base>/``, processed, then deleted before moving on, so the whole
   archive is never on disk at once (``tmp/<base>`` is removed at the end);
3. every mbox member (folder files *and* the non-empty ``<Name>.msg`` files) is
   parsed message by message, reading each one's ``Date:`` and ``Subject:``;
4. writes each message as an ``.eml`` file into
   ``output/<base>/<year>/<mail folder hierarchy>/<YYYYMMDD-HHMMSS>_<subject>.eml``
   -- mailbox name, then message year, then the archive's own folder tree --
   and appends a row to ``output/<base>/index.csv``.

Standard library only (Python 3.13).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import re
import shutil
import stat
import sys
import tarfile
import time
from email.header import decode_header
from email.parser import BytesHeaderParser
from email.policy import compat32
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

from progress_bar import CountingStream, ProgressBar

ROOT = Path(__file__).resolve().parent.parent

# A message boundary: a line like
#   From julien@kokan.fr Wed Nov 20 19:10:03 2019 +0100
# We additionally require that it follows a blank line (or starts the file) so
# that body lines beginning with "From " cannot be mistaken for a separator.
_FROM_RE = re.compile(
    rb"^From \S+ (?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
    rb"[A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d \d{4}"
)
_ENVELOPE_DATE_RE = re.compile(
    r"^From \S+\s+"
    r"(?P<rest>[A-Z][a-z]{2} [A-Z][a-z]{2} +\d+ \d+:\d+:\d+ \d{4}(?: [+-]\d{4})?)"
)
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_ARCHIVE_SUFFIXES = (".tgz", ".tar.gz", ".tar")


# --------------------------------------------------------------------------- #
# archive handling
# --------------------------------------------------------------------------- #
def archive_base(path: Path) -> str:
    """Strip the archive extension from a filename to get the mailbox base."""
    for suffix in _ARCHIVE_SUFFIXES:
        if path.name.endswith(suffix):
            return path.name[: -len(suffix)]
    return path.stem


def discover_archives(archives_dir: Path) -> list[Path]:
    if not archives_dir.is_dir():
        return []
    return sorted(
        p for p in archives_dir.iterdir()
        if p.is_file() and any(p.name.endswith(s) for s in _ARCHIVE_SUFFIXES)
    )


def choose_base(archives_dir: Path) -> str:
    """Ask the user which archive of ``archives_dir`` to process."""
    archives = discover_archives(archives_dir)
    if not archives:
        raise FileNotFoundError(f"no archive found in {archives_dir}")
    if len(archives) == 1:
        base = archive_base(archives[0])
        print(f"using the only archive found: {archives[0].name} (base {base!r})")
        return base

    print(f"archives available in {archives_dir}:")
    for i, p in enumerate(archives, 1):
        size_mib = p.stat().st_size / (1024 * 1024)
        print(f"  {i}. {p.name}  ({size_mib:,.0f} MiB)")
    while True:
        try:
            reply = input(f"choose an archive [1-{len(archives)}]: ").strip()
        except EOFError:
            raise SystemExit("no archive chosen")
        if reply.isdigit() and 1 <= int(reply) <= len(archives):
            return archive_base(archives[int(reply) - 1])
        print("  invalid choice, try again")


def find_archive(base: str, archives_dir: Path) -> Path:
    for suffix in _ARCHIVE_SUFFIXES:
        candidate = archives_dir / f"{base}{suffix}"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"no archive for {base!r} in {archives_dir} "
        f"(looked for {', '.join(base + s for s in _ARCHIVE_SUFFIXES)})"
    )


# --------------------------------------------------------------------------- #
# mbox parsing
# --------------------------------------------------------------------------- #
def iter_mbox_messages(path: Path):
    """Yield the raw bytes of each message in an mbox file."""
    lines: list[bytes] = []
    prev_blank = True  # start of file counts as "after a blank line"
    with path.open("rb") as fh:
        for line in fh:
            stripped = line.rstrip(b"\r\n")
            if prev_blank and _FROM_RE.match(stripped) and lines:
                yield b"".join(lines)
                lines = []
            lines.append(line)
            prev_blank = stripped == b""
    if lines and any(l.strip() for l in lines):
        yield b"".join(lines)


def _decode_subject(raw_value: str | None) -> str:
    if not raw_value:
        return ""
    out: list[str] = []
    for text, enc in decode_header(raw_value):
        if isinstance(text, bytes):
            try:
                out.append(text.decode(enc or "utf-8", errors="replace"))
            except LookupError:
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _envelope_datetime(raw: bytes) -> dt.datetime | None:
    first = raw.split(b"\n", 1)[0].decode("latin-1", errors="replace")
    m = _ENVELOPE_DATE_RE.match(first)
    if not m:
        return None
    rest = re.sub(r"\s+", " ", m.group("rest")).strip()
    for fmt in ("%a %b %d %H:%M:%S %Y %z", "%a %b %d %H:%M:%S %Y"):
        try:
            return dt.datetime.strptime(rest, fmt)
        except ValueError:
            continue
    return None


def _addresses(headers, name: str) -> str:
    """Comma-separated, de-duplicated address list from the given header(s)."""
    seen: list[str] = []
    for _name, addr in getaddresses(headers.get_all(name, [])):
        addr = addr.strip()
        if addr and addr not in seen:
            seen.append(addr)
    return ",".join(seen)


def message_meta(raw: bytes) -> tuple[dt.datetime | None, str, str, str, str, str]:
    """Return (datetime_or_None, subject, from, to, cc, bcc) for a raw message."""
    headers = BytesHeaderParser(policy=compat32).parsebytes(raw)

    when: dt.datetime | None = None
    date_hdr = headers.get("Date")
    if date_hdr:
        try:
            when = parsedate_to_datetime(date_hdr)
        except (TypeError, ValueError):
            when = None
    if when is None:
        when = _envelope_datetime(raw)

    return (
        when,
        _decode_subject(headers.get("Subject")),
        _addresses(headers, "From"),
        _addresses(headers, "To"),
        _addresses(headers, "Cc"),
        _addresses(headers, "Bcc"),
    )


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #
def sanitize(subject: str, maxlen: int = 120) -> str:
    s = _UNSAFE.sub("_", subject)
    s = re.sub(r"\s+", " ", s).strip().strip(". ")
    if len(s) > maxlen:
        s = s[:maxlen].rstrip()
    return s or "no-subject"


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    candidate = directory / f"{stem}{suffix}"
    n = 1
    while candidate.exists():
        candidate = directory / f"{stem}_{n}{suffix}"
        n += 1
    return candidate


def logical_folder(member_name: str) -> Path:
    """Map an archive member path to the mail folder it belongs to: drop the
    ``store*/part*/<box>.export/`` wrapper and the ``.msg`` suffix that marks a
    parent folder's own messages.
    ``.../paubry.export/INBOX`` -> ``INBOX``,
    ``.../paubry.export/JRES.msg`` -> ``JRES``,
    ``.../paubry.export/JRES/Conf-NG`` -> ``JRES/Conf-NG``."""
    parts = [p for p in member_name.split("/") if p not in ("", ".")]
    for i, p in enumerate(parts):
        if p.endswith(".export"):
            parts = parts[i + 1:]
            break
    else:
        parts = parts[1:]  # no .export wrapper: drop the top-level base dir
    if parts and parts[-1].endswith(".msg"):
        parts[-1] = parts[-1][:-4]
    parts = [_UNSAFE.sub("_", p) for p in parts]
    return Path(*parts) if parts else Path(".")


def is_mbox_file(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    if path.suffix.lower() in _ARCHIVE_SUFFIXES or path.suffix.lower() == ".gz":
        return False
    with path.open("rb") as fh:
        return fh.read(5) == b"From "


# --------------------------------------------------------------------------- #
# filesystem helpers
# --------------------------------------------------------------------------- #
def _clear_readonly(func, path, _exc):
    """``rmtree`` error handler: drop the read-only bit and retry the failed op.

    On Windows ``os.unlink`` / ``os.rmdir`` raise ``PermissionError`` on a
    read-only entry; clearing the attribute and re-calling ``func`` gets past it.
    """
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass
    func(path)


def robust_rmtree(path: Path, *, retries: int = 5, delay: float = 0.5) -> None:
    """``shutil.rmtree`` hardened for Windows: clear read-only bits (via
    ``_clear_readonly``) and retry a few times with growing back-off so a
    transient lock (OneDrive sync, Search indexer, antivirus, an open Explorer
    window) has a chance to release before we give up."""
    if not path.exists():
        return
    for attempt in range(1, retries + 1):
        try:
            shutil.rmtree(path, onexc=_clear_readonly)  # onexc: 3.12+
            return
        except OSError as exc:
            if attempt == retries:
                raise
            print(f"  could not remove {path} ({exc.__class__.__name__}); "
                  f"retrying in {delay:.0f}s ({attempt}/{retries - 1})")
            time.sleep(delay)
            delay *= 2


def rmtree_interactive(path: Path) -> None:
    """Like ``robust_rmtree`` but, when a file is still held open by another
    application (typically ``index.csv`` open in Excel), keep asking the user to
    close it and retry until the tree can be removed."""
    while path.exists():
        try:
            robust_rmtree(path)
            return
        except OSError as exc:
            locked = getattr(exc, "filename", None) or path
            print(f"cannot remove {locked}: it is open in another application.")
            try:
                input("close it, then press Enter to retry (Ctrl-C to abort)... ")
            except EOFError:
                raise SystemExit(f"aborted: {locked} is still locked")


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
def process(base: str, archives_dir: Path, tmp_dir: Path, output_dir: Path,
            limit: int | None) -> int:
    archive = find_archive(base, archives_dir)
    print(f"archive: {archive}")

    extract_root = tmp_dir / base
    if extract_root.exists():
        print(f"clearing previous extraction: {extract_root}")
        robust_rmtree(extract_root)
    extract_root.mkdir(parents=True, exist_ok=True)

    base_dir = output_dir / base
    if base_dir.exists():
        print(f"clearing previous output: {base_dir}")
        rmtree_interactive(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    index_path = base_dir / "index.csv"
    made_dirs: set[Path] = set()

    mode = "r|" if archive.suffix == ".tar" else "r|gz"
    bar = ProgressBar(archive.stat().st_size)  # measured against compressed size
    total = 0
    no_date = 0
    hit_limit = False

    with index_path.open("w", newline="", encoding="utf-8") as index_fh:
        writer = csv.writer(index_fh, delimiter=";")
        writer.writerow(
            ["year", "folder", "date", "subject",
             "from", "to", "cc", "bcc", "output_file"]
        )

        with archive.open("rb") as raw:
            stream = CountingStream(raw, bar)
            with tarfile.open(fileobj=stream, mode=mode) as tar:
                for member in tar:  # sequential: extract the current one, then advance
                    if hit_limit:
                        break
                    if not member.isfile():
                        continue
                    # filter="data" (3.12+) blocks absolute paths, traversal, specials.
                    tar.extract(member, extract_root, filter="data")
                    fpath = extract_root / member.name

                    if is_mbox_file(fpath):
                        folder = logical_folder(member.name)
                        folder_posix = folder.as_posix()
                        bar.update(label=folder_posix)
                        count = 0
                        messages = iter_mbox_messages(fpath)
                        for msg in messages:
                            when, subject, from_addr, to_addrs, cc_addrs, bcc_addrs = \
                                message_meta(msg)
                            if when is None:
                                no_date += 1
                                year, iso = "unknown", ""
                                stamp = f"unknown-{total:06d}"
                            else:
                                year = f"{when.year:04d}"
                                stamp = when.strftime("%Y%m%d-%H%M%S")
                                iso = when.isoformat()

                            dest_dir = base_dir / year / folder
                            if dest_dir not in made_dirs:
                                dest_dir.mkdir(parents=True, exist_ok=True)
                                made_dirs.add(dest_dir)
                            out_path = unique_path(
                                dest_dir, f"{stamp}_{sanitize(subject)}", ".eml"
                            )
                            out_path.write_bytes(msg)
                            writer.writerow(
                                [year, folder_posix, iso, subject,
                                 from_addr, to_addrs, cc_addrs, bcc_addrs,
                                 out_path.relative_to(base_dir).as_posix()]
                            )
                            count += 1
                            total += 1
                            bar.update(msgs_add=1)
                            if limit is not None and total >= limit:
                                hit_limit = True
                                break
                        messages.close()  # release the handle (no-op if exhausted)
                        if hit_limit:
                            print(f"limit reached: {count} message(s)")
                    fpath.unlink()  # processed -> drop it before the next member

    bar.close()
    try:
        robust_rmtree(extract_root)
        print(f"removed scratch tree: {extract_root}")
    except OSError as exc:
        print(f"warning: could not fully remove {extract_root}: {exc}")
    print(f"\ndone: {total} message(s) -> {base_dir} "
          f"({no_date} without a usable date)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mail-extract",
        description="Extract a mailbox archive into .eml files, grouped by "
                    "message year then mirroring the archive folder tree.",
    )
    p.add_argument("--archives-dir", type=Path, default=ROOT / "archives",
                   help="directory holding <base>.tgz (default: ./archives)")
    p.add_argument("--tmp-dir", type=Path, default=ROOT / "tmp",
                   help="extraction directory (default: ./tmp)")
    p.add_argument("--output-dir", type=Path, default=ROOT / "output",
                   help="output directory (default: ./output)")
    p.add_argument("--limit", type=int, default=None,
                   help="stop after N messages (for testing)")
    return p


def main(argv: list[str] | None = None) -> int:
    # Line-buffer stdout so every print() is flushed on its trailing newline
    # (progress runs on stderr; keep the two interleaving in real time).
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    args = build_parser().parse_args(argv)
    try:
        base = choose_base(args.archives_dir)
        return process(
            base, args.archives_dir, args.tmp_dir, args.output_dir, args.limit,
        )
    except (FileNotFoundError, tarfile.TarError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"stop")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
