"""Extract a personal mailbox archive into a per-year tree of messages.

Usage:
    python src/main.py [options]

The script:

1. lists the archives in ``archives/`` and asks which one to process (if there
   is only one, it is used without asking); its name gives the mailbox base;
2. unpacks it into ``tmp/<base>/`` (any previous extraction is wiped first);
   each unpacked mbox file is deleted once processed, and the tree removed at
   the end, unless --keep-tmp / --skip-extract;
3. walks every mbox file in the extracted tree (folder files *and* the
   non-empty ``<Name>.msg`` files), and for every message reads its ``Date:``
   and ``Subject:`` headers;
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
import re
import shutil
import sys
import tarfile
from email.header import decode_header
from email.parser import BytesHeaderParser
from email.policy import compat32
from email.utils import parsedate_to_datetime
from pathlib import Path

from progress_bar import ProgressBar, human_bytes

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


def extract_archive(archive: Path, dest: Path) -> None:
    if dest.exists():
        print(f"  clearing previous extraction: {dest}")
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    print(f"  extracting {archive.name} -> {dest} ...")
    mode = "r:gz" if archive.suffix != ".tar" else "r:"
    with tarfile.open(archive, mode) as tar:
        # filter="data" (3.12+) blocks absolute paths, traversal and specials.
        tar.extractall(dest, filter="data")
    print("  extraction done")


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


def message_meta(raw: bytes) -> tuple[dt.datetime | None, str]:
    """Return (datetime_or_None, subject) for a raw message."""
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

    return when, _decode_subject(headers.get("Subject"))


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


def find_mail_root(extract_root: Path) -> Path:
    """Descend through single-child ``store*/part*/<box>.export`` wrappers to
    the directory that actually holds the mail folders."""
    root = extract_root
    while True:
        entries = [e for e in root.iterdir() if not e.name.startswith(".")]
        if len(entries) == 1 and entries[0].is_dir():
            root = entries[0]
        else:
            return root


def logical_folder(mbox: Path, mail_root: Path) -> Path:
    """Map an mbox file to the mail folder it belongs to, relative to the mail
    root: ``INBOX`` -> ``INBOX``, ``JRES.msg`` -> ``JRES`` (a parent folder's
    own messages live in ``<Name>.msg``), ``JRES/Conf-NG`` -> ``JRES/Conf-NG``."""
    rel = mbox.relative_to(mail_root)
    name = rel.name[:-4] if rel.name.endswith(".msg") else rel.name
    parts = [_UNSAFE.sub("_", p) for p in (*rel.parent.parts, name) if p not in ("", ".")]
    return Path(*parts) if parts else Path(".")


def is_mbox_file(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    if path.suffix.lower() in _ARCHIVE_SUFFIXES or path.suffix.lower() == ".gz":
        return False
    with path.open("rb") as fh:
        return fh.read(5) == b"From "


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
def process(base: str, archives_dir: Path, tmp_dir: Path, output_dir: Path,
            limit: int | None) -> int:
    extract_root = tmp_dir / base
    archive = find_archive(base, archives_dir)
    print(f"archive: {archive}")
    extract_archive(archive, extract_root)

    mail_root = find_mail_root(extract_root)
    mbox_files = sorted(p for p in mail_root.rglob("*") if is_mbox_file(p))
    total_bytes = sum(p.stat().st_size for p in mbox_files)
    print(f"mail root: {mail_root}")
    print(f"found {len(mbox_files)} mbox file(s), {human_bytes(total_bytes)} to read")

    base_dir = output_dir / base
    if base_dir.exists():
        print(f"clearing previous output: {base_dir}")
        shutil.rmtree(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    index_path = base_dir / "index.csv"
    made_dirs: set[Path] = set()

    bar = ProgressBar(total_bytes)
    total = 0
    no_date = 0
    with index_path.open("w", newline="", encoding="utf-8") as index_fh:
        writer = csv.writer(index_fh, delimiter=";")
        writer.writerow(["folder", "year", "date", "subject", "output_file"])

        file_base = 0  # bytes of the mbox files fully processed so far
        for mbox in mbox_files:
            folder = logical_folder(mbox, mail_root)
            folder_posix = folder.as_posix()
            size = mbox.stat().st_size
            bar.update(done=file_base, label=folder_posix, force=True)
            count = 0
            in_file = 0
            for raw in iter_mbox_messages(mbox):
                when, subject = message_meta(raw)
                if when is None:
                    no_date += 1
                    year = "unknown"
                    stamp = f"unknown-{total:06d}"
                    iso = ""
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
                out_path.write_bytes(raw)

                writer.writerow(
                    [folder_posix, year, iso, subject,
                     out_path.relative_to(base_dir).as_posix()]
                )

                count += 1
                total += 1
                in_file += len(raw)
                bar.update(done=file_base + in_file, msgs_add=1)
                if limit is not None and total >= limit:
                    bar.close()
                    print(f"  {folder_posix}: {count} message(s) [limit reached]")
                    shutil.rmtree(extract_root, ignore_errors=True)
                    print(f"removed scratch tree: {extract_root}")
                    print(f"\ndone: {total} message(s) -> {base_dir} "
                          f"({no_date} without a usable date)")
                    return 0
            file_base += size  # self-corrects any per-file drift
            mbox.unlink()  # done with this file, reclaim the space now
            bar.update(done=file_base, label=folder_posix)
            bar.log(f"  {folder_posix}: {count} message(s)")

    bar.close()
    shutil.rmtree(extract_root, ignore_errors=True)
    print(f"removed scratch tree: {extract_root}")
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
    args = build_parser().parse_args(argv)
    try:
        base = choose_base(args.archives_dir)
        return process(
            base, args.archives_dir, args.tmp_dir, args.output_dir, args.limit,
        )
    except (FileNotFoundError, tarfile.TarError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
