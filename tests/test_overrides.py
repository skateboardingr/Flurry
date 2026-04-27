"""
test_overrides.py - tests for analyzer-side user overrides.

Covers:
  - apply_pet_owners: rewriting attacker/healer names so an actor is
    rolled up under "<owner>`s pet".
  - group_into_encounters with manual_groups: the user-pinned override
    that bypasses auto-grouping for specific fight sets.

Both layers are pure-Python and only depend on the dataclasses in
analyzer.py — no log files needed.
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flurry import (
    detect_fights, apply_pet_owners, group_into_encounters,
    AttackerStats, FightResult, Hit, Heal, fight_key,
)


BASE = datetime(2026, 4, 25, 12, 0, 0)


def _line(offset_s: int, body: str) -> str:
    ts = BASE + timedelta(seconds=offset_s)
    return f'[{ts.strftime("%a %b %d %H:%M:%S %Y")}] {body}'


def _write_log(events):
    fd, path = tempfile.mkstemp(suffix='.txt', prefix='flurry_overrides_test_')
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


# ----- apply_pet_owners -----

def test_apply_pet_owners_merges_hits_into_owner():
    """Onyx Crusher's hits are reattributed directly to Soloson, with
    pet_origin tagged so the UI can still surface the pet as a source."""
    path = _write_log([
        (0, 'Onyx Crusher slashes a goblin for 50000 points of damage.'),
        (1, 'Onyx Crusher slashes a goblin for 60000 points of damage.'),
        (2, 'a goblin has been slain by Onyx Crusher!'),
    ])
    try:
        fights = detect_fights(path)
        assert len(fights) == 1
        rewritten, _ = apply_pet_owners(fights, [], {'Onyx Crusher': 'Soloson'})
        f = rewritten[0]
        assert all(h.attacker == 'Soloson' for h in f.hits), \
            f'hits not merged into owner: {[h.attacker for h in f.hits]}'
        assert all(h.pet_origin == 'Onyx Crusher' for h in f.hits), \
            f'pet_origin missing: {[h.pet_origin for h in f.hits]}'
        assert 'Soloson' in f.stats_by_attacker
        assert 'Onyx Crusher' not in f.stats_by_attacker
        # Pet damage rolls up into owner's total — not into a separate
        # `<owner>'s pet` row.
        assert f.stats_by_attacker['Soloson'].damage == 110_000
    finally:
        _cleanup(path)


def test_apply_pet_owners_rewrites_defends_attacker_side():
    """A pet's swings against a defender should be reattributed to its
    owner in defends_by_pair too, so the tank UI shows 'Soloson hit me
    N times' rather than splitting the rollup across raw pet names."""
    path = _write_log([
        (0, 'Onyx Crusher slashes Tank for 5000 points of damage.'),
        (1, 'Onyx Crusher tries to slash Tank, but Tank parries!'),
        (2, 'Soloson slashes Tank for 1000 points of damage.'),
        (3, 'Tank has been slain by Onyx Crusher!'),
    ])
    try:
        fights = detect_fights(path, min_damage=0)
        rewritten, _ = apply_pet_owners(fights, [], {'Onyx Crusher': 'Soloson'})
        f = rewritten[0]
        # Pet's pair key should now be (Soloson, Tank), summed with
        # Soloson's own swing against Tank.
        assert ('Onyx Crusher', 'Tank') not in f.defends_by_pair
        pair = f.defends_by_pair[('Soloson', 'Tank')]
        assert pair.hits_landed == 2, f'hits_landed: {pair.hits_landed}'
        assert pair.damage_taken == 6_000
        assert pair.avoided.get('parry') == 1
    finally:
        _cleanup(path)


def test_apply_pet_owners_owner_own_hits_sum_with_pet_hits():
    """When the owner's own hits exist alongside pet hits, both
    contribute to the owner's AttackerStats. pet_origin distinguishes
    them at the per-hit level for source breakdowns."""
    path = _write_log([
        (0, 'Soloson slashes a goblin for 100000 points of damage.'),
        (1, 'Onyx Crusher slashes a goblin for 30000 points of damage.'),
        (2, 'a goblin has been slain by Soloson!'),
    ])
    try:
        fights = detect_fights(path)
        rewritten, _ = apply_pet_owners(fights, [], {'Onyx Crusher': 'Soloson'})
        f = rewritten[0]
        assert 'Soloson' in f.stats_by_attacker
        assert 'Onyx Crusher' not in f.stats_by_attacker
        # 100k from Soloson + 30k from the rewritten pet
        assert f.stats_by_attacker['Soloson'].damage == 130_000
        # Owner's own hits are NOT pet-tagged; pet hits ARE.
        owner_hits = [h for h in f.hits if h.pet_origin is None]
        pet_hits = [h for h in f.hits if h.pet_origin == 'Onyx Crusher']
        assert sum(h.damage for h in owner_hits) == 100_000
        assert sum(h.damage for h in pet_hits) == 30_000
    finally:
        _cleanup(path)


def test_apply_pet_owners_collapses_two_actors_to_same_owner():
    """Two distinct raw actors mapped to the same owner sum into one
    AttackerStats — the whole point of the merge for raid-DPS rollups."""
    path = _write_log([
        (0, 'Onyx Crusher slashes a goblin for 30000 points of damage.'),
        (1, 'Water Pet slashes a goblin for 20000 points of damage.'),
        (2, 'a goblin has been slain by You!'),
    ])
    try:
        fights = detect_fights(path)
        rewritten, _ = apply_pet_owners(fights, [],
            {'Onyx Crusher': 'Mage', 'Water Pet': 'Mage'})
        f = rewritten[0]
        assert 'Mage' in f.stats_by_attacker
        assert f.stats_by_attacker['Mage'].damage == 50_000
    finally:
        _cleanup(path)


def test_apply_pet_owners_is_case_insensitive_on_actor_lookup():
    path = _write_log([
        (0, 'Onyx Crusher slashes a goblin for 50000 points of damage.'),
        (1, 'a goblin has been slain by You!'),
    ])
    try:
        fights = detect_fights(path)
        rewritten, _ = apply_pet_owners(fights, [],
            {'onyx crusher': 'Soloson'})
        f = rewritten[0]
        assert 'Soloson' in f.stats_by_attacker
    finally:
        _cleanup(path)


def test_apply_pet_owners_empty_map_is_noop():
    """No mappings → return inputs unchanged (and ideally identical
    objects so the no-op stays cheap)."""
    fights = []
    heals = []
    out_fights, out_heals = apply_pet_owners(fights, heals, {})
    assert out_fights is fights
    assert out_heals is heals


def test_apply_pet_owners_does_not_mutate_input():
    """Original FightResult should keep its raw attacker keys after the
    rewrite, so the cache layer can return raw fights to callers that
    need the un-rewritten names (e.g. the pet-owner edit modal)."""
    path = _write_log([
        (0, 'Onyx Crusher slashes a goblin for 50000 points of damage.'),
        (1, 'a goblin has been slain by You!'),
    ])
    try:
        fights = detect_fights(path)
        original_keys = set(fights[0].stats_by_attacker.keys())
        apply_pet_owners(fights, [], {'Onyx Crusher': 'Soloson'})
        assert set(fights[0].stats_by_attacker.keys()) == original_keys
    finally:
        _cleanup(path)


def test_apply_pet_owners_rewrites_heal_healer():
    h = Heal(timestamp=BASE, healer='Onyx Crusher', target='Soloson',
             amount=5000, spell='Pet Bandage', modifiers=[])
    _, rewritten = apply_pet_owners([], [h], {'Onyx Crusher': 'Soloson'})
    assert rewritten[0].healer == 'Soloson'
    assert rewritten[0].pet_origin == 'Onyx Crusher'


# ----- group_into_encounters with manual groups -----

def _mkfight(target, start_offset, end_offset=None, damage=10_000, fid=None):
    """Build a minimal FightResult for grouping tests. We don't need
    real hits — just enough metadata that group_into_encounters can
    sequence the fights and pick a name."""
    start = BASE + timedelta(seconds=start_offset)
    end = BASE + timedelta(seconds=end_offset if end_offset is not None
                                  else start_offset + 5)
    stats = {
        'Hacral': AttackerStats(attacker='Hacral', damage=damage,
                                hits=1, biggest=damage),
    }
    return FightResult(
        target=target, start=start, end=end,
        hits=[Hit(timestamp=start, attacker='Hacral', target=target,
                  damage=damage, modifiers=[], specials=[], kind='melee')],
        stats_by_attacker=stats, fight_complete=True, fight_id=fid,
    )


def test_group_no_manual_groups_matches_old_behavior():
    """Two non-overlapping fights default to two encounters."""
    f1 = _mkfight('Boss A', 0)
    f2 = _mkfight('Boss B', 100)
    encs = group_into_encounters([f1, f2])
    assert len(encs) == 2


def test_group_manual_merges_distant_fights_into_one_encounter():
    """A manual group can pull two fights with a giant gap together,
    bypassing the auto-grouper that would otherwise leave them split."""
    f1 = _mkfight('Boss A', 0)
    f2 = _mkfight('Boss B', 100)
    keys = [fight_key('Boss A', f1.start), fight_key('Boss B', f2.start)]
    encs = group_into_encounters(
        [f1, f2],
        manual_groups=[{'fight_keys': keys, 'name': None}],
    )
    assert len(encs) == 1
    assert len(encs[0].members) == 2


def test_group_manual_overrides_auto_split():
    """If two adjacent fights would have been auto-merged, picking only
    one of them into a manual group still leaves the manual group alone
    (a single-key manual group is ignored, so the auto-merge stands)."""
    f1 = _mkfight('Boss A', 0)
    f2 = _mkfight('Boss B', 1)  # immediate auto-merge with default gap=0
    keys = [fight_key('Boss A', f1.start)]
    encs = group_into_encounters(
        [f1, f2],
        gap_seconds=10,
        manual_groups=[{'fight_keys': keys}],
    )
    # singleton manual group ignored → auto-grouping → one encounter
    assert len(encs) == 1


def test_group_manual_name_overrides_auto_name():
    f1 = _mkfight('Boss A', 0)
    f2 = _mkfight('Boss B', 100)
    keys = [fight_key('Boss A', f1.start), fight_key('Boss B', f2.start)]
    encs = group_into_encounters(
        [f1, f2],
        manual_groups=[{'fight_keys': keys, 'name': 'Phase 1'}],
    )
    assert encs[0].name == 'Phase 1'


def test_group_unknown_keys_in_manual_groups_are_silently_ignored():
    """A sidecar that references a fight that no longer matches
    detection params shouldn't break grouping — just skip the dead key."""
    f1 = _mkfight('Boss A', 0)
    f2 = _mkfight('Boss B', 100)
    keys = ['ghost|2026-01-01T00:00:00',  # not in input
            fight_key('Boss A', f1.start),
            fight_key('Boss B', f2.start)]
    encs = group_into_encounters(
        [f1, f2],
        manual_groups=[{'fight_keys': keys}],
    )
    assert len(encs) == 1


def test_group_encounters_renumbered_after_manual_split():
    """When manual groups pull fights out of the auto chain, the result
    is re-sorted by start time and re-numbered. This ensures the UI's
    encounter ids are still chronological."""
    f1 = _mkfight('Boss A', 0)
    f2 = _mkfight('Boss B', 50)
    f3 = _mkfight('Boss C', 100)
    keys = [fight_key('Boss A', f1.start), fight_key('Boss C', f3.start)]
    encs = group_into_encounters(
        [f1, f2, f3],
        manual_groups=[{'fight_keys': keys}],
    )
    # Encounter starts: f1 (manual A+C, earliest) at 0, f2 (auto) at 50.
    # The manual group's start time is min of its members' starts → 0.
    # Auto-grouped f2 → its own encounter at 50.
    assert len(encs) == 2
    starts_by_id = sorted([(e.encounter_id, e.start) for e in encs])
    # Earliest encounter is the manual group (contains f1 + f3).
    earliest_id, earliest_start = starts_by_id[0]
    earliest = next(e for e in encs if e.encounter_id == earliest_id)
    assert len(earliest.members) == 2
    # Later encounter is f2 alone.
    later_id, _ = starts_by_id[1]
    later = next(e for e in encs if e.encounter_id == later_id)
    assert len(later.members) == 1
    assert later.members[0].target == 'Boss B'


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
