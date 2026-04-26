"""
__main__.py - canonical entry point for `python -m flurry`.

With no subcommand, launches the web UI (the player-facing tool). With
a subcommand, dispatches to the matching CLI entry point so power users
can still run dps/timeline/session reports without going through the UI.

Usage:
    python -m flurry                       # launch UI
    python -m flurry ui [LOG]              # launch UI explicitly
    python -m flurry session LOG           # session report
    python -m flurry dps LOG TARGET        # per-attacker breakdown
    python -m flurry timeline LOG TARGET   # timeline + optional HTML
"""

import sys

from .cli import dps_main, timeline_main, session_main, ui_main


SUBCOMMANDS = {
    'ui': ui_main,
    'session': session_main,
    'dps': dps_main,
    'timeline': timeline_main,
}


def main():
    args = sys.argv[1:]
    if args and args[0] in SUBCOMMANDS:
        cmd = args[0]
        # Rewrite argv so the underlying argparse setup sees the right prog
        # name and the rest of the flags. Each *_main builds its own parser.
        sys.argv = [f'flurry {cmd}'] + args[1:]
        SUBCOMMANDS[cmd]()
    elif args and args[0] in ('-h', '--help'):
        print(__doc__.strip())
    else:
        # No subcommand — default to launching the UI. Pass remaining args
        # through (e.g. `python -m flurry path/to/log.txt --port 9000`).
        sys.argv = ['flurry'] + args
        ui_main()


if __name__ == '__main__':
    main()
