"""
_pyinstaller_entry.py - tiny launcher used as the PyInstaller entry point.

PyInstaller treats the entry script as a top-level script, not as part of
its package, so `flurry/__main__.py`'s relative imports (`from .cli import …`)
break when bundled directly. This launcher imports the flurry package
properly and delegates to its main(). End users never run this file
directly — it just exists for the build.
"""
import os
import sys


def _set_console_icon():
    """Apply the bundled icon to the running console window.

    Three Windows APIs are involved because no single one is enough:

    - `SetCurrentProcessExplicitAppUserModelID` gives the process its
      own taskbar identity. Without this, Windows groups our console
      window under conhost.exe's AppUserModelID and shows conhost's
      icon on the taskbar regardless of any window-level icon we set.
    - `WM_SETICON` (via SendMessage) sets the icon on the title bar
      and Alt-Tab list.
    - `SetClassLongPtrW` updates the window class's icon, which the
      taskbar consults in addition to the per-window icon.

    Frozen builds only — running from source skips this entirely."""
    if sys.platform != 'win32' or not getattr(sys, 'frozen', False):
        return
    try:
        import ctypes
        from ctypes import wintypes

        # Distinguish ourselves from conhost on the taskbar. The AppID is
        # arbitrary — pick any stable string and Windows treats it as a
        # separate group.
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                ctypes.c_wchar_p('Flurry.LogAnalyzer'))
        except Exception:
            pass  # not fatal — older Windows versions might lack it

        icon_path = os.path.join(sys._MEIPASS, 'icon.ico')
        if not os.path.isfile(icon_path):
            return

        # IMAGE_ICON = 1, LR_LOADFROMFILE = 0x10, LR_DEFAULTSIZE = 0x40
        # LoadImage signatures: hInst, name, type, cx, cy, fuLoad
        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        h_icon = user32.LoadImageW(None, icon_path, 1, 0, 0, 0x10 | 0x40)
        if not h_icon:
            return

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if not hwnd:
            return

        # Window-level icon (title bar, Alt-Tab).
        # WM_SETICON = 0x0080, ICON_SMALL = 0, ICON_BIG = 1
        user32.SendMessageW(hwnd, 0x0080, 0, h_icon)
        user32.SendMessageW(hwnd, 0x0080, 1, h_icon)

        # Class-level icon (taskbar). GCLP_HICON = -14, GCLP_HICONSM = -34.
        # SetClassLongPtrW exists on 64-bit; on 32-bit it's SetClassLongW.
        # Pick the right one to stay portable across the bitness PyInstaller
        # might bundle.
        setter = getattr(user32, 'SetClassLongPtrW', None) \
            or user32.SetClassLongW
        setter(hwnd, -14, h_icon)
        setter(hwnd, -34, h_icon)
    except Exception:
        # Icon application is purely cosmetic — never let it abort startup.
        pass


_set_console_icon()

from flurry.__main__ import main

if __name__ == '__main__':
    main()
