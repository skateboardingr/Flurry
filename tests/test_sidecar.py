"""
test_sidecar.py - tests for the sidecar persistence layer.

Covers round-trip of pet owners + manual encounters through JSON,
mutation helpers, and the file-based load/save behavior (including
graceful handling of missing/corrupt sidecars).
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flurry.sidecar import (
    Sidecar, ManualEncounter, fight_key,
    sidecar_path, load_sidecar, save_sidecar, SIDECAR_VERSION,
)
from datetime import datetime


def _tmp_log():
    fd, path = tempfile.mkstemp(suffix='.txt', prefix='flurry_sidecar_test_')
    os.close(fd)
    return path


def _cleanup(path):
    for p in (path, sidecar_path(path)):
        try:
            os.unlink(p)
        except OSError:
            pass


# ----- fight_key -----

def test_fight_key_lowercases_target_and_uses_iso_start():
    ts = datetime(2026, 4, 25, 12, 0, 0)
    assert fight_key('Shei Vinitras', ts) == 'shei vinitras|2026-04-25T12:00:00'


def test_fight_key_returns_none_for_no_start():
    assert fight_key('any', None) is None


# ----- Sidecar dataclass -----

def test_sidecar_round_trip_through_json():
    sc = Sidecar(
        pet_owners={'Onyx Crusher': 'Soloson'},
        manual_encounters=[
            ManualEncounter(fight_keys=['a|t1', 'b|t2'], name='Phase 1'),
        ],
    )
    j = sc.to_json()
    assert j['version'] == SIDECAR_VERSION
    sc2 = Sidecar.from_json(j)
    assert sc2.pet_owners == sc.pet_owners
    assert len(sc2.manual_encounters) == 1
    m = sc2.manual_encounters[0]
    assert m.fight_keys == ['a|t1', 'b|t2']
    assert m.name == 'Phase 1'


def test_from_json_tolerates_missing_keys():
    """A v0 / hand-written file with no fields should still load empty,
    not crash."""
    sc = Sidecar.from_json({})
    assert sc.pet_owners == {}
    assert sc.manual_encounters == []


def test_set_pet_owner_assigns_and_clears():
    sc = Sidecar.empty()
    sc.set_pet_owner('Onyx Crusher', 'Soloson')
    assert sc.pet_owners == {'Onyx Crusher': 'Soloson'}
    sc.set_pet_owner('Onyx Crusher', None)
    assert sc.pet_owners == {}


def test_set_pet_owner_clears_case_insensitively():
    """Clearing should hit a stored entry regardless of casing in the
    request, so a UI-side case mismatch can't leave stale entries."""
    sc = Sidecar(pet_owners={'Onyx Crusher': 'Soloson'})
    sc.set_pet_owner('onyx crusher', '')
    assert sc.pet_owners == {}


def test_merge_encounter_rejects_singletons():
    """Manual groups need 2+ fights to be meaningful — a single fight
    matches its auto-grouped encounter. Caller's responsibility to
    build a real group; we just enforce the floor."""
    sc = Sidecar.empty()
    sc.merge_encounter(['only|one'], name=None)
    assert sc.manual_encounters == []


def test_merge_encounter_dedupes_existing_groups():
    """Adding a fight to a new group pulls it out of any prior group.
    Here the first group has exactly 2 fights; pulling one out leaves
    a singleton, which gets pruned, so only the new group survives."""
    sc = Sidecar.empty()
    sc.merge_encounter(['a', 'b'])
    sc.merge_encounter(['b', 'c'])
    assert len(sc.manual_encounters) == 1
    assert sorted(sc.manual_encounters[0].fight_keys) == ['b', 'c']


def test_merge_encounter_keeps_first_group_alive_when_2_plus_remain():
    sc = Sidecar.empty()
    sc.merge_encounter(['a', 'b', 'c'])
    sc.merge_encounter(['c', 'd'])  # pulls c out
    # First group had a, b, c — losing c leaves a, b which is still ≥2.
    sc.manual_encounters.sort(key=lambda m: m.fight_keys[0])
    assert len(sc.manual_encounters) == 2


def test_remove_keys_from_manual_prunes_below_two():
    sc = Sidecar.empty()
    sc.merge_encounter(['a', 'b', 'c'])
    sc.remove_keys_from_manual(['b', 'c'])
    # Only 'a' left → group dropped entirely.
    assert sc.manual_encounters == []


def test_remove_keys_from_manual_no_op_for_unknown_keys():
    sc = Sidecar.empty()
    sc.merge_encounter(['a', 'b'])
    sc.remove_keys_from_manual(['nonexistent'])
    assert len(sc.manual_encounters) == 1
    assert sorted(sc.manual_encounters[0].fight_keys) == ['a', 'b']


# ----- File I/O -----

def test_load_sidecar_returns_empty_when_missing():
    path = _tmp_log()
    try:
        sc = load_sidecar(path)
        assert sc.is_empty()
    finally:
        _cleanup(path)


def test_save_then_load_round_trip():
    path = _tmp_log()
    try:
        sc = Sidecar(
            pet_owners={'X': 'Y'},
            manual_encounters=[ManualEncounter(['a', 'b'], name='M')],
        )
        save_sidecar(path, sc)
        loaded = load_sidecar(path)
        assert loaded.pet_owners == {'X': 'Y'}
        assert len(loaded.manual_encounters) == 1
        assert loaded.manual_encounters[0].name == 'M'
    finally:
        _cleanup(path)


def test_save_is_atomic_via_tmp_rename():
    """The tmp file should not linger after a successful save — the
    rename is atomic and cleans up the staging file."""
    path = _tmp_log()
    try:
        sc = Sidecar(pet_owners={'X': 'Y'})
        save_sidecar(path, sc)
        assert os.path.isfile(sidecar_path(path))
        assert not os.path.isfile(sidecar_path(path) + '.tmp')
    finally:
        _cleanup(path)


def test_load_corrupt_json_falls_back_to_empty():
    """Manual edits or partial writes shouldn't crash the server — load
    silently returns empty so the UI keeps working."""
    path = _tmp_log()
    try:
        with open(sidecar_path(path), 'w', encoding='utf-8') as f:
            f.write('{this is not valid json')
        sc = load_sidecar(path)
        assert sc.is_empty()
    finally:
        _cleanup(path)


def test_load_non_object_root_falls_back_to_empty():
    """A JSON array / number / string at the root is also unrecognized."""
    path = _tmp_log()
    try:
        with open(sidecar_path(path), 'w', encoding='utf-8') as f:
            json.dump([1, 2, 3], f)
        sc = load_sidecar(path)
        assert sc.is_empty()
    finally:
        _cleanup(path)


def test_sidecar_path_appends_extension():
    """Sidecar lives next to the log with `.flurry.json` appended; this
    contract is what `_set_logfile` and the temp-upload save path both
    rely on."""
    assert sidecar_path('/x/y/z.txt') == '/x/y/z.txt.flurry.json'


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
            print(f'  FAIL  {t.__name__}: {e}')
    print(f'\n{len(tests) - failures}/{len(tests)} passed')
    sys.exit(0 if failures == 0 else 1)
