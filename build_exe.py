"""
build_exe.py - bundle Flurry into a single-file flurry.exe via PyInstaller.

Run from the repo root:
    pip install pyinstaller
    python build_exe.py

Output: dist/flurry.exe (Windows) or dist/flurry (macOS/Linux).

The bundled binary is fully self-contained — end users don't need Python
installed. Double-clicking opens a console with the server URL and
launches their default browser at the UI.

Why a script and not just a CLI invocation: this is the canonical recipe.
Stash it in source so future builds match the current one without anyone
having to remember which flags we passed.
"""

import os
import shutil
import subprocess
import sys


REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def main():
    # Clean previous build artifacts so stale files don't sneak in.
    for d in ('build', 'dist'):
        full = os.path.join(REPO_ROOT, d)
        if os.path.isdir(full):
            shutil.rmtree(full)

    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--onefile',                       # single self-contained .exe
        '--console',                       # show URL + Ctrl-C; no GUI yet
        '--name', 'flurry',
        '--noconfirm',
        '--distpath', os.path.join(REPO_ROOT, 'dist'),
        '--workpath', os.path.join(REPO_ROOT, 'build'),
        '--specpath', os.path.join(REPO_ROOT, 'build'),
        # Entry point is a tiny launcher that imports flurry as a package
        # and calls its main(). Bundling flurry/__main__.py directly would
        # turn it into a top-level script and break its relative imports.
        os.path.join(REPO_ROOT, '_pyinstaller_entry.py'),
    ]

    print('+ ' + ' '.join(cmd))
    subprocess.check_call(cmd, cwd=REPO_ROOT)

    exe_name = 'flurry.exe' if sys.platform == 'win32' else 'flurry'
    artifact = os.path.join(REPO_ROOT, 'dist', exe_name)
    print()
    print(f'Built: {artifact}')
    if os.path.exists(artifact):
        size_mb = os.path.getsize(artifact) / (1024 * 1024)
        print(f'Size:  {size_mb:.1f} MB')


if __name__ == '__main__':
    main()
