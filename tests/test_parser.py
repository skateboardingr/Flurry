"""
test_parser.py - parser correctness tests for Flurry.

Run with:
    python tests/test_parser.py
or with pytest if installed.
"""

import os
import sys

# Add project root to path so we can import flurry without installing.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flurry import (
    parse_line, MeleeHit, MeleeMiss, SpellDamage, DeathMessage, ZoneEntered,
    HealEvent,
)


def assert_eq(actual, expected, label=''):
    if actual != expected:
        raise AssertionError(f'{label}: expected {expected!r}, got {actual!r}')


# ----- Melee parsing -----

def test_first_person_melee_hit():
    """First-person uses bare verbs ('You slash' not 'You slashes')."""
    line = '[Sat Apr 25 12:21:17 2026] You slash Shei Vinitras for 282413 points of damage. (Critical)'
    ev = parse_line(line)
    assert isinstance(ev, MeleeHit), f'expected MeleeHit, got {type(ev).__name__}'
    assert_eq(ev.attacker, 'You', 'attacker')
    assert_eq(ev.verb, 'slash', 'verb')
    assert_eq(ev.target, 'Shei Vinitras', 'target')
    assert_eq(ev.damage, 282413, 'damage')
    assert 'Critical' in ev.modifiers


def test_first_person_generic_hit():
    """'You hit X for N points of damage.' - no spell, no -es verb."""
    line = '[Sat Apr 25 12:21:19 2026] You hit Shei Vinitras for 60842 points of damage. (Lucky Critical)'
    ev = parse_line(line)
    assert isinstance(ev, MeleeHit), f'expected MeleeHit, got {type(ev).__name__}'
    assert_eq(ev.attacker, 'You')
    assert_eq(ev.damage, 60842)


def test_third_person_melee_hit():
    line = '[Sat Apr 25 12:21:17 2026] Soloson slashes Shei Vinitras for 76100 points of damage. (Lucky Critical)'
    ev = parse_line(line)
    assert isinstance(ev, MeleeHit)
    assert_eq(ev.attacker, 'Soloson')
    assert_eq(ev.verb, 'slashes')
    assert_eq(ev.damage, 76100)


def test_pet_attribution():
    """Pets use a backtick: 'Soloson`s pet slashes ...'."""
    line = '[Sat Apr 25 12:21:19 2026] Soloson`s pet slashes Shei Vinitras for 54 points of damage.'
    ev = parse_line(line)
    assert isinstance(ev, MeleeHit), f'expected MeleeHit, got {type(ev).__name__}'
    assert_eq(ev.attacker, 'Soloson`s pet')
    assert_eq(ev.damage, 54)


def test_first_person_miss():
    line = "[Sat Apr 25 12:21:17 2026] You try to slash Shei Vinitras, but miss!"
    ev = parse_line(line)
    assert isinstance(ev, MeleeMiss), f'expected MeleeMiss, got {type(ev).__name__}'
    assert_eq(ev.attacker, 'You')


def test_third_person_miss():
    line = "[Sat Apr 25 12:21:17 2026] Soloson tries to slash Shei Vinitras, but misses!"
    ev = parse_line(line)
    assert isinstance(ev, MeleeMiss)
    assert_eq(ev.attacker, 'Soloson')


def test_riposte_miss_third_person():
    """'X tries to hit Y, but Y ripostes!' is a missed attack — same as
    a regular miss for fight-detection purposes. The riposting party's
    counter-damage shows up as a separate MeleeHit line."""
    line = '[Sat Apr 25 12:21:17 2026] Keltakun, Last Word tries to hit Soloson, but Soloson ripostes! (Strikethrough)'
    ev = parse_line(line)
    assert isinstance(ev, MeleeMiss), f'expected MeleeMiss, got {type(ev).__name__}'
    assert_eq(ev.attacker, 'Keltakun, Last Word', 'comma-titled name should parse')
    assert_eq(ev.target, 'Soloson')
    assert 'Strikethrough' in ev.modifiers


def test_riposte_miss_first_person_target():
    """'X tries to bite YOU, but YOU riposte!' — bare 'riposte' for YOU."""
    line = '[Sat Apr 25 12:21:17 2026] Rector of the Skies tries to bite YOU, but YOU riposte!'
    ev = parse_line(line)
    assert isinstance(ev, MeleeMiss)
    assert_eq(ev.attacker, 'Rector of the Skies')


def test_comma_titled_name_in_hit():
    """Names with epithets like 'Keltakun, Last Word' must parse as a
    single attacker name, not be truncated at the comma."""
    line = '[Sat Apr 25 12:21:17 2026] Keltakun, Last Word hits Soloson for 31158 points of damage. (Riposte Strikethrough)'
    ev = parse_line(line)
    assert isinstance(ev, MeleeHit), f'expected MeleeHit, got {type(ev).__name__}'
    assert_eq(ev.attacker, 'Keltakun, Last Word')
    assert_eq(ev.target, 'Soloson')
    assert_eq(ev.damage, 31158)


# ----- Spell damage -----

def test_spell_damage_named():
    line = '[Sat Apr 25 12:21:18 2026] You hit Shei Vinitras for 6529 points of cold damage by Strike of Ice I.'
    ev = parse_line(line)
    assert isinstance(ev, SpellDamage)
    assert_eq(ev.attacker, 'You')
    assert_eq(ev.target, 'Shei Vinitras')
    assert_eq(ev.damage, 6529)
    assert_eq(ev.damage_type, 'cold')
    assert_eq(ev.spell, 'Strike of Ice I')


def test_passive_spell_line_does_not_misparse():
    """Regression: the loose `NAME` regex used to slurp 'Shei has been' as
    the attacker on a passive-form line (would have target='by Soloson').
    Splitting SPELL_DAMAGE into 1st/3rd person closes that — the line
    should not match as SpellDamage at all."""
    line = '[Sat Apr 25 12:21:18 2026] Shei has been hit by Soloson for 1234 points of cold damage by Strike of Ice.'
    ev = parse_line(line)
    assert not isinstance(ev, SpellDamage), \
        f'passive spell line matched as SpellDamage: {ev}'


def test_spell_damage_third_person_bare_hit():
    """Real EQ uses bare 'hit' for third-person spell damage too —
    only melee third-person uses '-s' verbs."""
    line = '[Sat Apr 25 12:21:18 2026] Onyx hit a Solusek foot soldier for 14605 points of fire damage by Flamebrand VII.'
    ev = parse_line(line)
    assert isinstance(ev, SpellDamage), f'expected SpellDamage, got {type(ev).__name__}'
    assert_eq(ev.attacker, 'Onyx')
    assert_eq(ev.target, 'a Solusek foot soldier')
    assert_eq(ev.damage, 14605)
    assert_eq(ev.damage_type, 'fire')
    assert_eq(ev.spell, 'Flamebrand VII')


def test_dot_damage_first_person_target():
    """DoT format: 'You have taken N damage from SPELL by ATTACKER.'"""
    line = '[Sat Apr 25 12:21:18 2026] You have taken 152563 damage from Gouging Strike by Feather Silver Sheen.'
    ev = parse_line(line)
    assert isinstance(ev, SpellDamage), f'expected SpellDamage, got {type(ev).__name__}'
    assert_eq(ev.target, 'You')
    assert_eq(ev.attacker, 'Feather Silver Sheen', 'attacker is the name after `by`')
    assert_eq(ev.damage, 152563)
    assert_eq(ev.spell, 'Gouging Strike')
    assert_eq(ev.damage_type, 'dot')


def test_dot_damage_third_person_target():
    """DoT format with named target: 'X has taken N damage from SPELL by Y.'"""
    line = '[Sat Apr 25 12:21:18 2026] Robbinwuud has taken 160717 damage from Gouging Strike by Feather Silver Sheen.'
    ev = parse_line(line)
    assert isinstance(ev, SpellDamage)
    assert_eq(ev.target, 'Robbinwuud')
    assert_eq(ev.attacker, 'Feather Silver Sheen')
    assert_eq(ev.damage, 160717)


def test_damage_shield():
    """DS/proc form attributes damage to the SOURCE OWNER."""
    line = "[Sat Apr 25 12:21:17 2026] Shei Vinitras is pierced by Soloson's thorns for 5175 points of non-melee damage."
    ev = parse_line(line)
    assert isinstance(ev, SpellDamage), f'expected SpellDamage, got {type(ev).__name__}'
    assert_eq(ev.attacker, 'Soloson')
    assert_eq(ev.target, 'Shei Vinitras')
    assert_eq(ev.damage, 5175)
    assert_eq(ev.spell, 'thorns')


def test_damage_shield_first_person():
    """First-person DS uses 'YOUR' (all caps) instead of '<name>'s'.
    Attacker is normalized to 'You' to match the rest of the parser."""
    line = '[Sat Apr 25 12:21:17 2026] A nilborien hawk is pierced by YOUR thorns for 298136 points of non-melee damage.'
    ev = parse_line(line)
    assert isinstance(ev, SpellDamage), f'expected SpellDamage, got {type(ev).__name__}'
    assert_eq(ev.attacker, 'You')
    assert_eq(ev.target, 'A nilborien hawk')
    assert_eq(ev.damage, 298136)
    assert_eq(ev.spell, 'thorns')


# ----- Heal events -----

def test_heal_third_person_with_spell():
    line = '[Sat Apr 25 12:21:30 2026] Soloson healed Hacral for 50000 hit points by Word of Restoration.'
    ev = parse_line(line)
    assert isinstance(ev, HealEvent), f'expected HealEvent, got {type(ev).__name__}'
    assert_eq(ev.healer, 'Soloson')
    assert_eq(ev.target, 'Hacral')
    assert_eq(ev.amount, 50000)
    assert_eq(ev.spell, 'Word of Restoration')


def test_heal_first_person():
    line = '[Sat Apr 25 12:21:30 2026] You healed Soloson for 12345 hit points by Healing Light.'
    ev = parse_line(line)
    assert isinstance(ev, HealEvent)
    assert_eq(ev.healer, 'You')
    assert_eq(ev.amount, 12345)


def test_heal_self_pronoun_normalized():
    """'X healed himself' should normalize target to X."""
    line = '[Sat Apr 25 12:21:30 2026] Soloson healed himself for 8000 hit points by Self Heal.'
    ev = parse_line(line)
    assert isinstance(ev, HealEvent)
    assert_eq(ev.healer, 'Soloson')
    assert_eq(ev.target, 'Soloson', 'self-heal target should normalize to healer')


def test_heal_with_overheal_parens():
    """'for N (M) hit points' — keep N, ignore M."""
    line = '[Sat Apr 25 12:21:30 2026] Soloson healed Hacral for 8000 (12000) hit points by Healing Light.'
    ev = parse_line(line)
    assert isinstance(ev, HealEvent)
    assert_eq(ev.amount, 8000)
    assert_eq(ev.spell, 'Healing Light')


def test_heal_critical_modifier():
    line = '[Sat Apr 25 12:21:30 2026] Soloson healed Hacral for 100000 hit points by Word of Restoration. (Critical)'
    ev = parse_line(line)
    assert isinstance(ev, HealEvent)
    assert 'Critical' in ev.modifiers


def test_heal_passive_over_time():
    """HoT ticks come through as 'X has been healed over time by Y...'.
    Healer is the name AFTER `by`, not 'X has been'."""
    line = '[Sat Apr 25 12:21:30 2026] Lunarya has been healed over time by Soloson for 86000 hit points by Healing Touch.'
    ev = parse_line(line)
    assert isinstance(ev, HealEvent), f'expected HealEvent, got {type(ev).__name__}'
    assert_eq(ev.healer, 'Soloson', 'healer should come from the `by` clause')
    assert_eq(ev.target, 'Lunarya', 'target is the subject of the passive form')
    assert_eq(ev.amount, 86000)
    assert_eq(ev.spell, 'Healing Touch')


def test_heal_passive_first_person_target():
    """'You have been healed by X for N hit points by Spell.'"""
    line = '[Sat Apr 25 12:21:30 2026] You have been healed by Soloson for 5000 hit points by Healing Light.'
    ev = parse_line(line)
    assert isinstance(ev, HealEvent)
    assert_eq(ev.healer, 'Soloson')
    assert_eq(ev.target, 'You')
    assert_eq(ev.amount, 5000)


def test_heal_passive_without_spell():
    """Passive form without `by SPELL` — still parses, spell is None."""
    line = '[Sat Apr 25 12:21:30 2026] Hacral has been healed by Soloson for 1234 hit points.'
    ev = parse_line(line)
    assert isinstance(ev, HealEvent)
    assert_eq(ev.healer, 'Soloson')
    assert_eq(ev.target, 'Hacral')
    assert ev.spell is None


# ----- Death events -----

def test_was_slain_format():
    """Raid bosses often use 'X was slain by Y!' instead of 'has been slain'."""
    line = '[Sat Apr 25 12:22:48 2026] Shei Vinitras was slain by Soloson!'
    ev = parse_line(line)
    assert isinstance(ev, DeathMessage)
    assert_eq(ev.victim, 'Shei Vinitras')
    assert_eq(ev.killer, 'Soloson')
    assert ev.you_died is False


def test_has_been_slain_format():
    line = '[Mon Apr 14 19:42:04 2025] An arachnae terranis has been slain by Kesobbi!'
    ev = parse_line(line)
    assert isinstance(ev, DeathMessage)
    assert_eq(ev.victim, 'An arachnae terranis')
    assert_eq(ev.killer, 'Kesobbi')


def test_player_death():
    line = '[Wed May 21 20:51:21 2025] You have been slain by a deadly cloudwalker!'
    ev = parse_line(line)
    assert isinstance(ev, DeathMessage)
    assert_eq(ev.victim, 'You')
    assert_eq(ev.killer, 'a deadly cloudwalker')
    assert ev.you_died is True


# ----- Zone tracking -----

def test_zone_entered():
    line = '[Sat Apr 25 12:18:51 2026] You have entered Ka Vethan.'
    ev = parse_line(line)
    assert isinstance(ev, ZoneEntered)
    assert_eq(ev.zone, 'Ka Vethan')


def test_zone_negative_lookahead():
    """Sub-zone notifications shouldn't be matched as zone changes."""
    line = '[Sat Apr 25 12:18:53 2026] You have entered an area where levitation effects do not function.'
    ev = parse_line(line)
    assert not isinstance(ev, ZoneEntered), \
        f'sub-zone matched as ZoneEntered: {ev}'


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
