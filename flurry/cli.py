"""
cli.py - command-line entry points.

These are the functions referenced by `[project.scripts]` in pyproject.toml.
After `pip install`, you get `flurry-dps` and `flurry-timeline` commands
on your PATH that call into here.

The bin/ scripts are thin shims so the tool also works if you just clone
the repo without pip-installing.
"""

import argparse
import sys

from . import analyze_fight, bucket_hits, detect_fights
from .report import (
    text_dps_report, text_timeline_report, html_timeline_report,
    text_session_report,
)


def dps_main():
    """Entry point for the `flurry-dps` command."""
    ap = argparse.ArgumentParser(
        prog='flurry-dps',
        description='Per-attacker damage breakdown for a single fight.'
    )
    ap.add_argument('logfile', help='Path to eqlog_<char>_<server>.txt')
    ap.add_argument('target', help='Target name (the boss/mob to analyze)')
    args = ap.parse_args()

    result = analyze_fight(args.logfile, args.target)
    print(text_dps_report(result))

    if not result.fight_complete:
        sys.exit(2)


def timeline_main():
    """Entry point for the `flurry-timeline` command."""
    ap = argparse.ArgumentParser(
        prog='flurry-timeline',
        description='Bucketed damage timeline for a single fight.'
    )
    ap.add_argument('logfile', help='Path to eqlog_<char>_<server>.txt')
    ap.add_argument('target', help='Target name (the boss/mob to analyze)')
    ap.add_argument('--bucket', type=int, default=5,
                    help='Bucket size in seconds (default 5)')
    ap.add_argument('--html', default=None,
                    help='If set, write an interactive HTML report to this path')
    ap.add_argument('--top-hits', type=int, default=10,
                    help='Number of biggest hits to list (default 10)')
    ap.add_argument('--min-dps', type=int, default=100,
                    help='Hide attackers with avg DPS below this (default 100)')
    args = ap.parse_args()

    result = analyze_fight(args.logfile, args.target)
    if result.start is None:
        print(f'No damage events found targeting "{args.target}".',
              file=sys.stderr)
        sys.exit(1)

    timeline = bucket_hits(result, bucket_seconds=args.bucket)
    print(text_timeline_report(result, timeline,
                               min_dps_to_show=args.min_dps,
                               top_n_hits=args.top_hits))

    if args.html:
        with open(args.html, 'w') as f:
            f.write(html_timeline_report(result, timeline))
        print(f'\nWrote HTML report: {args.html}')

    if not result.fight_complete:
        sys.exit(2)


def session_main():
    """Entry point for the `flurry-session` command.

    Auto-detects every fight in the log and prints a session-overview table.
    Each detected fight has a stable 1-indexed ID so the user (or a future
    UI) can reference it for grouping into encounters.
    """
    ap = argparse.ArgumentParser(
        prog='flurry-session',
        description='Auto-detect every fight in an EQ log and list them.'
    )
    ap.add_argument('logfile', help='Path to eqlog_<char>_<server>.txt')
    ap.add_argument('--gap', type=int, default=15,
                    help='Seconds of inactivity before a fight is considered '
                         'over (default 15)')
    ap.add_argument('--min-damage', type=int, default=10_000,
                    help='Hide fights below this total damage threshold '
                         '(default 10,000)')
    args = ap.parse_args()

    fights = detect_fights(args.logfile,
                           gap_seconds=args.gap,
                           min_damage=args.min_damage)
    print(text_session_report(fights, logfile=args.logfile))


def ui_main():
    """Entry point for the `flurry-ui` command.

    Spins up a tiny local web server backed by the Python analyzer and
    opens a browser tab. The UI is hash-routed: index lists every detected
    fight, click a row to drill into per-attacker stats and a timeline.
    """
    from .server import serve

    ap = argparse.ArgumentParser(
        prog='flurry-ui',
        description='Browse detected fights interactively in a local web UI.'
    )
    ap.add_argument('logfile', nargs='?', default=None,
                    help='Optional path to eqlog_<char>_<server>.txt. '
                         'If omitted, pick a log from inside the UI.')
    ap.add_argument('--port', type=int, default=8765,
                    help='Port to bind on localhost (default 8765)')
    ap.add_argument('--gap', type=int, default=15,
                    help='detect_fights gap_seconds (default 15)')
    ap.add_argument('--min-damage', type=int, default=10_000,
                    help='detect_fights min_damage (default 10,000)')
    ap.add_argument('--min-duration', type=int, default=10,
                    help='Drop fights shorter than this in seconds (default 10)')
    ap.add_argument('--bucket', type=int, default=5,
                    help='Timeline bucket size in seconds (default 5)')
    ap.add_argument('--encounter-gap', type=int, default=10,
                    help='Seconds between fights to still consider them one '
                         'encounter (default 10). Use 0 for strict overlap '
                         'only; bump higher to merge phase-pause splits.')
    ap.add_argument('--since-hours', type=int, default=8,
                    help='Analyze only the last N hours of log activity '
                         '(anchored to the log\'s last timestamp). 0 = whole '
                         'log. Default 8 covers a typical raid night while '
                         'keeping the first parse fast on multi-day logs.')
    ap.add_argument('--no-browser', action='store_true',
                    help="Don't auto-open a browser tab")
    args = ap.parse_args()

    serve(args.logfile,
          port=args.port,
          gap_seconds=args.gap,
          min_damage=args.min_damage,
          min_duration_seconds=args.min_duration,
          bucket_seconds=args.bucket,
          encounter_gap_seconds=args.encounter_gap,
          since_hours=args.since_hours,
          open_browser=not args.no_browser)
