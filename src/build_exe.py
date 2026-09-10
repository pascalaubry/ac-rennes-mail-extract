"""Build a standalone executable of the mail-extract CLI with PyInstaller.

Usage::

    pip install -e ".[build_exe]"      # once, to get PyInstaller
    python scripts/build_exe.py        # -> dist/mail-extract(.exe)

Any extra arguments are forwarded to PyInstaller, e.g.::

    python scripts/build_exe.py --clean --log-level DEBUG

The one-file executable bundles ``src/main.py`` and ``src/progress_bar.py``
(standard library only, no other dependencies). At runtime it reads
``./archives`` and writes ``./tmp`` / ``./output`` relative to the directory
that holds the executable (see the ``sys.frozen`` branch in ``main.py``).
"""

from __future__ import annotations

import sys
from pathlib import Path

from main import APP_NAME, APP_VERSION

sys.path.extend(
    map(
        str,
        [
            Path(__file__).parents[1],  # The root path
            Path(__file__).parents[1]
            / 'src',  # The path to the sources of the application
        ],
    )
)

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
ENTRY = SRC / "main.py"
DIST = ROOT / "dist"
BUILD = ROOT / "build"


def main(argv: list[str] | None = None) -> int:

    try:
        import PyInstaller.__main__ as pyinstaller
    except ModuleNotFoundError:
        print(
            'PyInstaller is not installed. Run:  pip install -e ".[build_exe]"',
            file=sys.stderr,
        )
        return 1

    if not ENTRY.is_file():
        print(f"entry point not found: {ENTRY}", file=sys.stderr)
        return 1

    exe_stem: str = f'{APP_NAME}-{APP_VERSION}'

    args = [
        str(ENTRY),
        '--name', exe_stem,
        '--onefile',
        '--console',
        '--paths',
        str(SRC),  # resolve `import progress_bar`
        '--distpath',
        str(DIST),
        '--workpath',
        str(BUILD / 'pyinstaller'),
        '--specpath',
        str(BUILD),
        '--noconfirm',
        '--copy-metadata', APP_NAME,
    ]
    print("pyinstaller " + " ".join(args))
    pyinstaller.run(args)

    exe = DIST / f'{exe_stem}.exe'
    if not exe.is_file():
        print(f"build reported success but {exe} is missing", file=sys.stderr)
        return 1
    print(f"\nbuilt: {exe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
