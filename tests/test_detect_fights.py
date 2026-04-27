"""
test_detect_fights.py - tests for the auto fight-detection layer.

These tests don't need the real Hacral fixture log. We synthesize tiny
EQ-formatted log files in tempfiles, run detect_fights() against them,
and assert on the resulting list of FightResults. Same matrix as
test_analyzer.py but covering segmentation behavior.

Run with:
    python tests/test_detect_fights.py
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flurry import detect_fights


# ----- Helpers -----

BASE = datetime(2026, 4, 25, 12, 0, 0)


def _line(offset_s: int, body: str) -> str:
    """Format one EQ-style log line at base + offset seconds."""
    ts = BASE + timedelta(seconds=offset_s)
    return f'[{ts.strftime("%a %b %d %H:%M:%S %Y")}] {body}'


def _write_log(events):
    """events: list of (offset_seconds, body_string). Returns tmp path."""
    fd, path = tempfile.mkstemp(suffix='.txt', prefix='flurry_test_')
    os.close(fd)
    with open(path, 'w', newline='') as f:
        for offset, body in events:
            f.write(_line(offset, body) + '\r\n')
    return path


def _cleanup(path):
    try:
        os.unlink(path)
    except OSError:
        pass


# ----- Tests -----

def test_basic_kill():
    """One target, You attacks, mob dies. One complete fight."""
    path = _write_log([
        (0,  'You slash a goblin for 50000 points of damage.'),
        (1,  'You slash a goblin for 60000 points of damage. (Critical)'),
        (2,  'a goblin has been slain by You!'),
    ])
    try:
        fights = detect_fights(path)
        assert len(fights) == 1, f'expected 1 fight, got {len(fights)}'
        f = fights[0]
        assert f.target == 'a goblin'
        assert f.fight_complete is True
        assert f.total_damage == 110_000
        assert f.fight_id == 1
        assert (f.end - f.start).total_seconds() == 2.0
    finally:
        _cleanup(path)


def test_incomplete_fight_when_log_ends_mid_combat():
    """Damage but no death event => fight_complete=False, end=last hit."""
    path = _write_log([
        (0, 'You slash Klandicar for 50000 points of damage.'),
        (1, 'You slash Klandicar for 60000 points of damage.'),
    ])
    try:
        fights = detect_fights(path)
        assert len(fights) == 1
        assert fights[0].fight_complete is False
        assert fights[0].target == 'Klandicar'
    finally:
        _cleanup(path)


def test_gap_splits_same_target_into_two_fights():
    """Two engagements on the same mob with a >gap_seconds idle between
    them produce two distinct fights."""
    path = _write_log([
        (0,   'You slash a yeti for 50000 points of damage.'),
        (1,   'a yeti has been slain by You!'),
        # 90 seconds of nothing — a SECOND yeti gets engaged later.
        # (EQ reuses generic mob names; same string, different instance.)
        (91,  'You slash a yeti for 70000 points of damage.'),
        (92,  'a yeti has been slain by You!'),
    ])
    try:
        fights = detect_fights(path, gap_seconds=60)
        assert len(fights) == 2, f'expected 2 fights, got {len(fights)}'
        assert fights[0].fight_id == 1 and fights[1].fight_id == 2
        assert fights[0].total_damage == 50_000
        assert fights[1].total_damage == 70_000
        assert all(f.fight_complete for f in fights)
    finally:
        _cleanup(path)


def test_no_split_within_gap_window():
    """Two hits on the same target with gap < gap_seconds = one fight."""
    path = _write_log([
        (0,  'You slash a wolf for 30000 points of damage.'),
        (50, 'You slash a wolf for 40000 points of damage.'),
        (51, 'a wolf has been slain by You!'),
    ])
    try:
        fights = detect_fights(path, gap_seconds=60)
        assert len(fights) == 1
        assert fights[0].total_damage == 70_000
    finally:
        _cleanup(path)


def test_concurrent_targets_become_separate_fights():
    """Boss + add engaged in the same combat window become two fights.
    The UI will group them into an encounter; the detector keeps them
    separate so per-target stats are clean."""
    path = _write_log([
        (0, 'You slash Klandicar for 100000 points of damage.'),
        (1, 'You hit a frost drake for 25000 points of fire damage by Strike of Ice.'),
        (2, 'You slash Klandicar for 110000 points of damage.'),
        (3, 'Soloson slashes a frost drake for 30000 points of damage.'),
        (4, 'a frost drake has been slain by You!'),
        (5, 'You slash Klandicar for 120000 points of damage.'),
        (6, 'Klandicar has been slain by You!'),
    ])
    try:
        fights = detect_fights(path)
        assert len(fights) == 2, f'expected 2 fights, got {len(fights)}'
        targets = {f.target for f in fights}
        assert targets == {'Klandicar', 'a frost drake'}
        klandicar = next(f for f in fights if f.target == 'Klandicar')
        drake = next(f for f in fights if f.target == 'a frost drake')
        assert klandicar.total_damage == 330_000
        assert drake.total_damage == 55_000
        assert klandicar.fight_complete and drake.fight_complete
    finally:
        _cleanup(path)


def test_min_damage_filter():
    """Trivial damage events on passing trash get filtered."""
    path = _write_log([
        # Real fight: 200k damage on a goblin — kept.
        (0, 'You slash a goblin for 200000 points of damage.'),
        (1, 'a goblin has been slain by You!'),
        # Accidental DoT tick on passing rat — under min_damage, dropped.
        (3, 'You hit a rat for 500 points of cold damage by Strike of Ice.'),
    ])
    try:
        fights = detect_fights(path, min_damage=10_000)
        assert len(fights) == 1
        assert fights[0].target == 'a goblin'
    finally:
        _cleanup(path)


def test_filter_drops_player_self_target():
    """A fight where target=='You' (player taking damage) is filtered out."""
    path = _write_log([
        # Player gets hit hard by the boss, then dies. Becomes a "fight"
        # with target=You — filter must drop it.
        (0, 'Klandicar slashes You for 50000 points of damage.'),
        (1, 'Klandicar slashes You for 60000 points of damage.'),
        (2, 'You have been slain by Klandicar!'),
    ])
    try:
        fights = detect_fights(path)
        assert len(fights) == 0, f'expected 0 fights (player death), got {len(fights)}'
    finally:
        _cleanup(path)


def test_filter_drops_backtick_pet_target():
    """A fight where target ends in `s pet (pet death) is filtered out."""
    path = _write_log([
        (0, 'a goblin slashes Soloson`s pet for 30000 points of damage.'),
        (1, 'a goblin slashes Soloson`s pet for 25000 points of damage.'),
        (2, 'Soloson`s pet has been slain by a goblin!'),
    ])
    try:
        fights = detect_fights(path)
        # Pet "fight" must be filtered. (No goblin fight either since
        # nobody attacked the goblin in this synthetic log.)
        assert all(f.target != 'Soloson`s pet' for f in fights)
    finally:
        _cleanup(path)


def test_fight_ids_assigned_by_start_time():
    """Even if combat events are interleaved across targets in the log,
    fight_ids are assigned by each fight's start time (the first hit)."""
    path = _write_log([
        # Klandicar engaged FIRST (id should be 1).
        (0, 'You slash Klandicar for 50000 points of damage.'),
        # Drake engaged later (id should be 2).
        (5, 'You hit a drake for 25000 points of fire damage by Flame Strike.'),
        # Drake dies first (chronologically).
        (6, 'a drake has been slain by You!'),
        # Klandicar dies later.
        (10, 'You slash Klandicar for 60000 points of damage.'),
        (11, 'Klandicar has been slain by You!'),
    ])
    try:
        fights = detect_fights(path)
        assert len(fights) == 2
        # Sorted by start, IDs are 1, 2 in that order.
        assert fights[0].fight_id == 1 and fights[0].target == 'Klandicar'
        assert fights[1].fight_id == 2 and fights[1].target == 'a drake'
    finally:
        _cleanup(path)


def test_multiple_attackers_attributed_correctly():
    """Per-attacker stats roll up across the fight regardless of order."""
    path = _write_log([
        (0, 'You slash Klandicar for 100000 points of damage.'),
        (1, 'Soloson slashes Klandicar for 80000 points of damage.'),
        (2, 'Soloson`s pet slashes Klandicar for 5000 points of damage.'),
        (3, 'You slash Klandicar for 110000 points of damage. (Critical)'),
        (4, 'Klandicar has been slain by You!'),
    ])
    try:
        fights = detect_fights(path)
        assert len(fights) == 1
        f = fights[0]
        attackers = f.stats_by_attacker
        assert attackers['You'].damage == 210_000
        assert attackers['You'].crits == 1
        assert attackers['Soloson'].damage == 80_000
        assert attackers['Soloson`s pet'].damage == 5_000
    finally:
        _cleanup(path)


def test_defends_by_pair_populated_from_hits_and_misses():
    """Each landed hit and each avoided swing should bump the per-pair
    DefenseStats from the defender's perspective. Outcome counts (parry,
    block, dodge, rune, invuln, riposte, miss) are all tracked by label."""
    path = _write_log([
        # A boss whacking on the tank with a varied mix of outcomes.
        (0,  'A nasty boss hits Tank for 9000 points of damage.'),
        (1,  'A nasty boss hits Tank for 11000 points of damage. (Critical)'),
        (2,  'A nasty boss tries to hit Tank, but Tank parries!'),
        (3,  'A nasty boss tries to hit Tank, but Tank dodges!'),
        (4,  'A nasty boss tries to hit Tank, but Tank blocks with his shield!'),
        (5,  "A nasty boss tries to hit Tank, but Tank's magical skin absorbs the blow!"),
        (6,  'A nasty boss tries to hit Tank, but Tank is INVULNERABLE!'),
        (7,  'A nasty boss tries to hit Tank, but Tank ripostes!'),
        (8,  'A nasty boss tries to hit Tank, but misses!'),
        # And a parallel attacker so we know the pair key actually splits.
        (9,  'An add hits Tank for 500 points of damage.'),
        (10, 'A nasty boss has been slain by You!'),
        (11, 'You slash A nasty boss for 30000 points of damage.'),
    ])
    try:
        fights = detect_fights(path, min_damage=0)
        # `detect_fights` produces one fight per *target*, so the boss-
        # hitting-tank events live in the Tank-as-target fight, not the
        # boss-as-target fight. The encounter view later merges these.
        tank_fight = next(f for f in fights if f.target.lower() == 'tank')
        pair = tank_fight.defends_by_pair[('A nasty boss', 'Tank')]
        assert pair.hits_landed == 2, f'hits_landed: {pair.hits_landed}'
        assert pair.damage_taken == 20_000, f'damage_taken: {pair.damage_taken}'
        assert pair.biggest_taken == 11_000, f'biggest_taken: {pair.biggest_taken}'
        assert pair.avoided == {
            'parry': 1, 'dodge': 1, 'block': 1, 'rune': 1,
            'invulnerable': 1, 'riposte': 1, 'miss': 1,
        }, f'avoided: {pair.avoided}'
        assert pair.total_avoided == 7
        assert pair.total_swings == 9
        # Second attacker → separate pair on the same Tank fight.
        add_pair = tank_fight.defends_by_pair[('An add', 'Tank')]
        assert add_pair.hits_landed == 1
        assert add_pair.damage_taken == 500
    finally:
        _cleanup(path)


def test_misses_extend_fight_window():
    """A run of misses against a target keeps its fight alive past gap."""
    path = _write_log([
        (0, 'You slash a tough mob for 10000 points of damage.'),
        # Misses for 50s — should keep the fight open past the default 60s
        # gap because misses count as combat activity.
        (10, 'You try to slash a tough mob, but miss!'),
        (30, 'You try to slash a tough mob, but miss!'),
        (50, 'You try to slash a tough mob, but miss!'),
        (90, 'You slash a tough mob for 5000 points of damage.'),
        (91, 'a tough mob has been slain by You!'),
    ])
    try:
        fights = detect_fights(path, gap_seconds=60)
        assert len(fights) == 1, \
            f'misses should have extended the fight; got {len(fights)} fights'
        f = fights[0]
        assert f.total_damage == 15_000
        assert f.stats_by_attacker['You'].misses == 3
    finally:
        _cleanup(path)


# ----- Test runner -----

def main():
    tests = [v for k, v in globals().items()
             if k.startswith('test_') and callable(v)]
    failed = []
    for t in tests:
        try:
            t()
            print(f'  PASS  {t.__name__}')
        except AssertionError as e:
            print(f'  FAIL  {t.__name__}: {e}')
            failed.append(t.__name__)
        except Exception as e:
            print(f'  ERROR {t.__name__}: {type(e).__name__}: {e}')
            failed.append(t.__name__)

    print()
    if failed:
        print(f'{len(failed)} of {len(tests)} tests failed')
        sys.exit(1)
    else:
        print(f'All {len(tests)} tests passed')


if __name__ == '__main__':
    main()
