"""
_pyinstaller_entry.py - tiny launcher used as the PyInstaller entry point.

PyInstaller treats the entry script as a top-level script, not as part of
its package, so `flurry/__main__.py`'s relative imports (`from .cli import …`)
break when bundled directly. This launcher imports the flurry package
properly and delegates to its main(). End users never run this file
directly — it just exists for the build.
"""
from flurry.__main__ import main

if __name__ == '__main__':
    main()
