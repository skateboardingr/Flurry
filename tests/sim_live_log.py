"""sim_live_log.py — append fake combat lines to a log file so the live
follower has something to chew on. Use to verify live tail end-to-end
without needing to be in a real EQ raid.

Usage:
    # 1. Start fresh (overwrites any existing temp file):
    python tests/sim_live_log.py --reset

    # 2. Open Flurry pointing at the temp log (in another terminal):
    flurry.exe ui $TEMP/flurry-sim/eqlog_TestPlayer_firiona.txt

    # 3. Set min_damage=5 in the params panel + Apply (default 10k filters
    #    out most simulator hits). Then start appending combat:
    python tests/sim_live_log.py --append 30

    # The overlay should flip from 'idle' to a pulsing green dot, and the
    # active fight should show real-time DPS counters.

The simulator generates a small set of attackers hitting a single boss
target ('a sim grabber') with random damage every 100ms, with a death
event at the end of the run so the encounter completes and shows up in
the recap.
"""

import argparse
import os
import random
import sys
import tempfile
import time
from datetime import datetime


SIM_DIR = os.path.join(tempfile.gettempdir(), 'flurry-sim')
SIM_LOG = os.path.join(SIM_DIR, 'eqlog_TestPlayer_firiona.txt')

# A small cast: the player ('You'), a couple of friendlies, and the
# boss target. Each line below is a template; we splice in `{ts}` and
# `{dmg}` at runtime.
TEMPLATES_FRIENDLY = [
    "You slash a sim grabber for {dmg} points of damage.",
    "Soloson slashes a sim grabber for {dmg} points of damage.",
    "Soloson`s pet slashes a sim grabber for {dmg} points of damage.",
    "Keidara hit a sim grabber for {dmg} points of cold damage by Strike of Ice.",
    "Robbinwuud hit a sim grabber for {dmg} points of damage. (Headshot)",
]
TEMPLATES_INCOMING = [
    "a sim grabber hits YOU for {dmg} points of damage.",
    "a sim grabber tries to hit YOU, but YOU dodge!",
    "a sim grabber tries to hit YOU, but YOU parry!",
]
TEMPLATES_HEALS = [
    "Tudia healed you for {dmg} hit points by Word of Restoration.",
    "Tudia healed Soloson for {dmg} hit points by Word of Vivification.",
]
DEATH_LINE = "a sim grabber has been slain by You!"


def fmt_ts(when: datetime) -> str:
    # EQ log timestamp format: "[Sat Apr 25 12:07:46 2026]"
    return when.strftime('[%a %b %d %H:%M:%S %Y]')


def reset() -> None:
    os.makedirs(SIM_DIR, exist_ok=True)
    # Seed the file with one harmless line so the analyzer can compute a
    # "last timestamp" for since_hours. The follower will pick up any
    # subsequent appends.
    with open(SIM_LOG, 'w', encoding='utf-8', newline='\n') as f:
        f.write(fmt_ts(datetime.now()) + ' Welcome to EverQuest!\n')
    print(f'Reset {SIM_LOG}')


def append_one(line_body: str) -> None:
    # Open in append mode each call so the OS file size advances and a
    # tail-following process sees the bytes immediately.
    with open(SIM_LOG, 'a', encoding='utf-8', newline='\n') as f:
        f.write(fmt_ts(datetime.now()) + ' ' + line_body + '\n')
        f.flush()


def append(seconds: int) -> None:
    if not os.path.isfile(SIM_LOG):
        print('Run with --reset first.', file=sys.stderr)
        sys.exit(1)
    print(f'Appending combat for {seconds}s...')
    end = time.time() + seconds
    rng = random.Random(42)
    tick = 0
    while time.time() < end:
        tick += 1
        # Pick a random template + random damage. Friendlies do most of
        # the swings; incoming/heals are sprinkled in so the four
        # overlay counters all light up.
        roll = rng.random()
        if roll < 0.7:
            tpl = rng.choice(TEMPLATES_FRIENDLY)
            dmg = rng.randint(1000, 50_000)
        elif roll < 0.85:
            tpl = rng.choice(TEMPLATES_INCOMING)
            dmg = rng.randint(500, 5000) if '{dmg}' in tpl else 0
        else:
            tpl = rng.choice(TEMPLATES_HEALS)
            dmg = rng.randint(2000, 20_000)
        append_one(tpl.format(dmg=dmg))
        if tick % 25 == 0:
            print(f'  ...{tick} lines appended')
        time.sleep(0.1)
    # End the fight cleanly so the encounter completes and shows up in
    # the recap state.
    append_one(DEATH_LINE)
    print('Done. Killed `a sim grabber`.')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--reset', action='store_true',
                   help='Create a fresh empty log at the sim path.')
    g.add_argument('--append', type=int, metavar='SECONDS',
                   help='Append fake combat lines for N seconds.')
    g.add_argument('--path', action='store_true',
                   help='Print the sim log path and exit.')
    args = ap.parse_args()

    if args.path:
        print(SIM_LOG)
    elif args.reset:
        reset()
    elif args.append is not None:
        append(args.append)


if __name__ == '__main__':
    main()
