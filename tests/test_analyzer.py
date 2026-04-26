"""
test_analyzer.py - tests for the analyzer layer.

Uses the real Hacral log as a fixture. If the file isn't present these
tests are skipped (so the suite still passes for someone who clones the
repo without the sample data).

Run with:
    python tests/test_analyzer.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flurry import analyze_fight, bucket_hits, is_crit, extract_specials


# Fixture path. If you have the sample log, set this; otherwise tests skip.
SAMPLE_LOG = '/mnt/user-data/uploads/eqlog_Hacral_firiona.txt'


def have_sample():
    return os.path.exists(SAMPLE_LOG)


# ----- Pure-function tests (no fixture needed) -----

def test_is_crit_basic():
    assert is_crit(['Critical']) is True
    assert is_crit(['Lucky Critical']) is True
    assert is_crit(['Crippling Blow']) is True
    assert is_crit(['Lucky Crippling Blow']) is True
    assert is_crit([]) is False
    assert is_crit(['Flurry']) is False
    assert is_crit(['Strikethrough']) is False


def test_is_crit_combined_modifiers():
    """Modifiers are space-separated keywords inside one paren group."""
    assert is_crit(['Lucky Critical Headshot']) is True
    assert is_crit(['Strikethrough Critical Flurry']) is True
    assert is_crit(['Riposte Strikethrough Lucky Crippling Blow']) is True
    assert is_crit(['Riposte Strikethrough']) is False


def test_extract_specials():
    assert extract_specials(['Lucky Critical Headshot']) == ['Headshot']
    assert extract_specials(['Critical Flurry']) == ['Flurry']
    assert extract_specials(['Critical Twincast']) == ['Twincast']
    # No specials
    assert extract_specials(['Critical']) == []
    assert extract_specials([]) == []


# ----- Fixture-based tests -----

def test_analyze_fight_shei_basic():
    if not have_sample():
        print('  SKIP  (no sample log)')
        return
    result = analyze_fight(SAMPLE_LOG, 'Shei Vinitras')

    assert result.fight_complete, 'fight should have a death event'
    assert result.start is not None
    assert result.end is not None

    # 91-second fight
    duration = result.duration_seconds
    assert 90 <= duration <= 92, f'expected ~91s, got {duration}'

    # Known totals from prior verified runs
    assert result.total_damage == 322_252_497, f'total damage {result.total_damage}'

    # 7 attackers including 2 pets
    assert len(result.stats_by_attacker) == 7, \
        f'expected 7 attackers, got {len(result.stats_by_attacker)}'


def test_analyze_fight_shei_attackers():
    if not have_sample():
        print('  SKIP')
        return
    result = analyze_fight(SAMPLE_LOG, 'Shei Vinitras')
    by_dmg = result.attackers_by_damage()

    # Soloson is #1
    assert by_dmg[0].attacker == 'Soloson'
    assert by_dmg[0].damage == 130_025_461

    # Hacral (You) is #2
    assert by_dmg[1].attacker == 'You'
    assert by_dmg[1].damage == 66_577_052


def test_analyze_fight_shei_pet():
    """Pets (with backticks) get tracked as separate attackers."""
    if not have_sample():
        print('  SKIP')
        return
    result = analyze_fight(SAMPLE_LOG, 'Shei Vinitras')
    pet_names = [a for a in result.stats_by_attacker if 'pet' in a.lower()]
    assert 'Hacral`s pet' in pet_names, f'pet attribution missing: {pet_names}'
    assert 'Soloson`s pet' in pet_names


def test_analyze_fight_shei_specials():
    if not have_sample():
        print('  SKIP')
        return
    result = analyze_fight(SAMPLE_LOG, 'Shei Vinitras')

    # Keidara: 8 Headshots, 47.8M damage
    keidara = result.stats_by_attacker['Keidara']
    assert keidara.special_hits.get('Headshot') == 8
    assert keidara.special_damage.get('Headshot') == 47_882_984

    # Hacral (You): 36 Flurries
    hacral = result.stats_by_attacker['You']
    assert hacral.special_hits.get('Flurry') == 36


def test_analyze_fight_missing_target():
    """Asking for a target that wasn't in the log returns empty result."""
    if not have_sample():
        print('  SKIP')
        return
    result = analyze_fight(SAMPLE_LOG, 'A Mob That Did Not Exist')
    assert result.start is None
    assert result.fight_complete is False
    assert result.total_damage == 0


def test_bucket_hits_count():
    if not have_sample():
        print('  SKIP')
        return
    result = analyze_fight(SAMPLE_LOG, 'Shei Vinitras')
    timeline = bucket_hits(result, bucket_seconds=5)

    # 91s / 5s = 18.2 -> 19 buckets (we round up so the last bucket holds tail data)
    assert timeline.n_buckets == 19, f'expected 19 buckets, got {timeline.n_buckets}'
    assert timeline.bucket_seconds == 5

    # Sum across all buckets and attackers should equal raid total
    total_in_timeline = sum(
        sum(series) for series in timeline.per_attacker.values()
    )
    assert total_in_timeline == result.total_damage, \
        f'timeline sum {total_in_timeline} != raid total {result.total_damage}'


def test_bucket_hits_different_bucket_size():
    """Bucket count should change with bucket size, total damage shouldn't."""
    if not have_sample():
        print('  SKIP')
        return
    result = analyze_fight(SAMPLE_LOG, 'Shei Vinitras')
    t5 = bucket_hits(result, bucket_seconds=5)
    t10 = bucket_hits(result, bucket_seconds=10)
    t30 = bucket_hits(result, bucket_seconds=30)

    assert t5.n_buckets > t10.n_buckets > t30.n_buckets

    for tl in (t5, t10, t30):
        total = sum(sum(s) for s in tl.per_attacker.values())
        assert total == result.total_damage, \
            f'bucket={tl.bucket_seconds}s lost damage: {total} != {result.total_damage}'


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
