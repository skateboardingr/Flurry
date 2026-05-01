"""
test_enemy_classifier.py - tests for the recap's enemy filter.

Covers `_enemy_names` (flurry/server.py), the classifier the live
overlay's recap uses to drop mobs out of the top-damage list.
The classifier is two-pass; both passes are exercised here.
"""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flurry import AttackerStats, FightResult, Heal
from flurry.analyzer import DefenseStats
from flurry.server import _enemy_names


BASE = datetime(2026, 4, 30, 21, 0, 0)


def _fight(stats, defends):
    return FightResult(
        target='Encounter',
        start=BASE,
        end=BASE + timedelta(seconds=60),
        hits=[],
        stats_by_attacker={
            name: AttackerStats(attacker=name, damage=dmg)
            for name, dmg in stats.items()
        },
        fight_complete=True,
        defends_by_pair={
            (atk, dfn): DefenseStats(attacker=atk, defender=dfn,
                                     damage_taken=dmg)
            for (atk, dfn), dmg in defends.items()
        },
    )


def test_pass1_flags_tank_and_spank_boss():
    """Boss takes 800k, deals 20k back → enemy via received > dealt."""
    fight = _fight(
        stats={'Hacral': 800_000, 'a colossus': 20_000},
        defends={
            ('Hacral', 'a colossus'): 800_000,
            ('a colossus', 'Hacral'): 20_000,
        },
    )
    enemies = _enemy_names(fight, [])
    assert 'a colossus' in enemies
    assert 'hacral' not in enemies


def test_pass2_flags_hit_and_run_add():
    """Add deals 100k to a PC but only takes 50k from the raid (dealt >
    received → fails pass 1). Pass 2 sees its damage landed entirely on
    a still-friendly name and flips it to enemy."""
    fight = _fight(
        stats={
            'Hacral': 800_000,
            'a colossus': 20_000,
            'a nihil arcanist': 100_000,
        },
        defends={
            ('Hacral', 'a colossus'): 800_000,
            ('a colossus', 'Hacral'): 20_000,
            ('Hacral', 'a nihil arcanist'): 50_000,
            ('a nihil arcanist', 'Hacral'): 100_000,
        },
    )
    enemies = _enemy_names(fight, [])
    assert 'a colossus' in enemies          # pass 1
    assert 'a nihil arcanist' in enemies    # pass 2
    assert 'hacral' not in enemies


def test_pass2_skips_pets():
    """Pet dealt all its damage to a friendly (test artifact) — pass 2
    must not flip a `\\`s pet`-suffixed actor regardless."""
    fight = _fight(
        stats={
            'Hacral': 800_000,
            'a colossus': 20_000,
            'Hacral`s pet': 30_000,
        },
        defends={
            ('Hacral', 'a colossus'): 800_000,
            ('a colossus', 'Hacral'): 20_000,
            ('Hacral`s pet', 'Hacral'): 30_000,
        },
    )
    enemies = _enemy_names(fight, [])
    assert 'hacral`s pet' not in enemies


def test_pass2_skips_in_window_healers():
    """A healer's incidental damage (DS proc on a buffed tank) might
    land entirely on friendlies, but they're decisively friendly via
    their healing output. Pass 2 must skip them."""
    fight = _fight(
        stats={
            'Hacral': 800_000,
            'a colossus': 20_000,
            'Cleric': 5_000,
        },
        defends={
            ('Hacral', 'a colossus'): 800_000,
            ('a colossus', 'Hacral'): 20_000,
            ('Cleric', 'Hacral'): 5_000,    # all dmg lands on friendly
        },
    )
    heal = Heal(timestamp=BASE, healer='Cleric',
                target='Hacral', amount=200_000, spell='Complete Heal')
    enemies = _enemy_names(fight, [heal])
    assert 'cleric' not in enemies


def test_charmed_mob_fighting_for_raid_stays_friendly():
    """A charmed mob ('a nihil arcanist' charm-pet) deals heavy damage
    to the boss and takes some retaliation. Pass 1 leaves it friendly
    (dealt > received). Pass 2 must NOT flip it: its damage landed on
    the boss (an enemy), not on the raid. Real-world case from a
    Colossus of Skylance encounter where charmed adds were misread
    as enemy mobs by an earlier classifier draft."""
    fight = _fight(
        stats={
            'Hacral': 800_000,
            'a colossus': 20_000,
            'a nihil arcanist': 60_000,
        },
        defends={
            ('Hacral', 'a colossus'): 800_000,
            ('a nihil arcanist', 'a colossus'): 60_000,
            ('a colossus', 'Hacral'): 20_000,
            ('a colossus', 'a nihil arcanist'): 5_000,
        },
    )
    enemies = _enemy_names(fight, [])
    assert 'a colossus' in enemies
    assert 'a nihil arcanist' not in enemies
    assert 'hacral' not in enemies


def test_friendly_pc_with_only_dealt_stays_friendly():
    """A PC who dealt damage but never appears as a defender (didn't
    take a hit) — should stay friendly. Tests the case where pass 1 is
    silent on them and pass 2 has no signal either way."""
    fight = _fight(
        stats={'Hacral': 800_000, 'a colossus': 20_000, 'Bard': 100_000},
        defends={
            ('Hacral', 'a colossus'): 800_000,
            ('Bard',   'a colossus'): 100_000,
            ('a colossus', 'Hacral'): 20_000,
        },
    )
    enemies = _enemy_names(fight, [])
    assert 'a colossus' in enemies
    assert 'bard' not in enemies
    assert 'hacral' not in enemies


# ----- Manual entry point -----

if __name__ == '__main__':
    failures = 0
    tests = [v for k, v in globals().items()
             if k.startswith('test_') and callable(v)]
    for t in tests:
        try:
            t()
            print(f'  OK  {t.__name__}')
        except Exception as e:
            failures += 1
            print(f'  FAIL  {t.__name__}: {type(e).__name__}: {e}')
    print(f'\n{len(tests) - failures}/{len(tests)} passed')
    sys.exit(0 if failures == 0 else 1)
