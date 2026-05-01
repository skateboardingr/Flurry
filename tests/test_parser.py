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
    parse_line, MeleeHit, MeleeMiss, SpellDamage, SpellResist, DeathMessage,
    ZoneEntered, HealEvent,
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
    assert_eq(ev.outcome, 'miss')


def test_third_person_miss():
    line = "[Sat Apr 25 12:21:17 2026] Soloson tries to slash Shei Vinitras, but misses!"
    ev = parse_line(line)
    assert isinstance(ev, MeleeMiss)
    assert_eq(ev.attacker, 'Soloson')
    assert_eq(ev.outcome, 'miss')


def test_riposte_miss_third_person():
    """'X tries to hit Y, but Y ripostes!' is a missed attack — same as
    a regular miss for fight-detection purposes. The riposting party's
    counter-damage shows up as a separate MeleeHit line."""
    line = '[Sat Apr 25 12:21:17 2026] Keltakun, Last Word tries to hit Soloson, but Soloson ripostes! (Strikethrough)'
    ev = parse_line(line)
    assert isinstance(ev, MeleeMiss), f'expected MeleeMiss, got {type(ev).__name__}'
    assert_eq(ev.attacker, 'Keltakun, Last Word', 'comma-titled name should parse')
    assert_eq(ev.target, 'Soloson')
    assert_eq(ev.outcome, 'riposte')
    assert 'Strikethrough' in ev.modifiers


def test_riposte_miss_first_person_target():
    """'X tries to bite YOU, but YOU riposte!' — bare 'riposte' for YOU."""
    line = '[Sat Apr 25 12:21:17 2026] Rector of the Skies tries to bite YOU, but YOU riposte!'
    ev = parse_line(line)
    assert isinstance(ev, MeleeMiss)
    assert_eq(ev.attacker, 'Rector of the Skies')
    assert_eq(ev.outcome, 'riposte')


# ----- Avoidance: parry / block / dodge / rune / invulnerable -----

def test_parry_third_person_avoider():
    """'X tries to hit Y, but Y parries!' — 3rd-person avoidance with -ies."""
    line = '[Sat Apr 25 12:21:28 2026] Shei Vinitras tries to hit Soloson, but Soloson parries! (Flurry)'
    ev = parse_line(line)
    assert isinstance(ev, MeleeMiss), f'expected MeleeMiss, got {type(ev).__name__}'
    assert_eq(ev.attacker, 'Shei Vinitras')
    assert_eq(ev.target, 'Soloson')
    assert_eq(ev.outcome, 'parry')
    assert 'Flurry' in ev.modifiers


def test_parry_first_person_avoider():
    """'X tries to hit YOU, but YOU parry!' — 1st-person uses bare 'parry'."""
    line = '[Sat Apr 25 12:21:50 2026] Shei Vinitras tries to hit YOU, but YOU parry! (Rampage)'
    ev = parse_line(line)
    assert isinstance(ev, MeleeMiss)
    assert_eq(ev.target, 'YOU')
    assert_eq(ev.outcome, 'parry')
    assert 'Rampage' in ev.modifiers


def test_block_third_person_avoider():
    line = '[Mon Jun 16 22:53:28 2025] A water elemental invader tries to hit Utishulla, but Utishulla blocks!'
    ev = parse_line(line)
    assert isinstance(ev, MeleeMiss)
    assert_eq(ev.attacker, 'A water elemental invader')
    assert_eq(ev.target, 'Utishulla')
    assert_eq(ev.outcome, 'block')


def test_block_with_shield_suffix():
    """'X blocks with her shield!' — extended block form. Pronoun and item
    word vary (her/his/its + shield/staff/...). Should still classify as block."""
    line = '[Sat Apr 25 14:18:13 2026] A gilded guardian tries to bite Tira, but Tira blocks with her shield!'
    ev = parse_line(line)
    assert isinstance(ev, MeleeMiss)
    assert_eq(ev.target, 'Tira')
    assert_eq(ev.outcome, 'block')


def test_block_with_staff_suffix_and_modifier():
    """Block-with suffix + trailing modifier paren."""
    line = '[Mon Apr 14 20:21:47 2025] Halgoz Rellinic tries to hit Vikolas, but Vikolas blocks with his shield! (Rampage)'
    ev = parse_line(line)
    assert isinstance(ev, MeleeMiss)
    assert_eq(ev.outcome, 'block')
    assert 'Rampage' in ev.modifiers


def test_dodge_third_person_avoider_lowercase_article():
    """Mob avoider mid-sentence uses lowercase article ('a Solusek...')."""
    line = "[Tue Mar 31 10:40:17 2026] Sweetlysingin`s pet tries to slash a Solusek foot soldier, but a Solusek foot soldier dodges!"
    ev = parse_line(line)
    assert isinstance(ev, MeleeMiss)
    assert_eq(ev.attacker, 'Sweetlysingin`s pet')
    assert_eq(ev.target, 'a Solusek foot soldier')
    assert_eq(ev.outcome, 'dodge')


def test_dodge_first_person_avoider():
    """'X tries to ... YOU, but YOU dodge!' — bare verb for 1st-person."""
    line = '[Wed Apr 16 19:20:06 2025] A Stone Abomination tries to smash YOU, but YOU dodge!'
    ev = parse_line(line)
    assert isinstance(ev, MeleeMiss)
    assert_eq(ev.target, 'YOU')
    assert_eq(ev.outcome, 'dodge')


def test_rune_third_person_absorb():
    """'X tries to hit Y, but Y's magical skin absorbs the blow!' — rune."""
    line = "[Mon Apr 14 19:50:27 2025] The Fabled Grummus tries to hit Vikolas, but Vikolas's magical skin absorbs the blow!"
    ev = parse_line(line)
    assert isinstance(ev, MeleeMiss), f'expected MeleeMiss, got {type(ev).__name__}'
    assert_eq(ev.attacker, 'The Fabled Grummus')
    assert_eq(ev.target, 'Vikolas')
    assert_eq(ev.outcome, 'rune')


def test_rune_first_person_absorb():
    """'X tries to hit YOU, but YOUR magical skin absorbs the blow!' — 1st-person rune."""
    line = '[Sat Apr 25 12:21:37 2026] Shei Vinitras tries to hit YOU, but YOUR magical skin absorbs the blow! (Riposte Strikethrough Rampage)'
    ev = parse_line(line)
    assert isinstance(ev, MeleeMiss)
    assert_eq(ev.target, 'YOU')
    assert_eq(ev.outcome, 'rune')
    # Multi-keyword paren stays as one entry — _strip_modifiers doesn't
    # split inside the parens, the analyzer does that on demand.
    assert ev.modifiers == ['Riposte Strikethrough Rampage'], f'got {ev.modifiers!r}'


def test_invulnerable_third_person_target():
    """'X tries to bite Y, but Y is INVULNERABLE!' — divine aura / god mode."""
    line = '[Sat Apr 25 14:56:40 2026] An acolyte tries to bite Tira, but Tira is INVULNERABLE! (Strikethrough)'
    ev = parse_line(line)
    assert isinstance(ev, MeleeMiss)
    assert_eq(ev.target, 'Tira')
    assert_eq(ev.outcome, 'invulnerable')


def test_invulnerable_first_person_target():
    """'X tries to bash YOU, but YOU are INVULNERABLE!' — 1st-person form."""
    line = '[Sat Apr 25 14:56:41 2026] An acolyte tries to bash YOU, but YOU are INVULNERABLE! (Riposte Strikethrough)'
    ev = parse_line(line)
    assert isinstance(ev, MeleeMiss)
    assert_eq(ev.target, 'YOU')
    assert_eq(ev.outcome, 'invulnerable')


# ----- Spell resists -----

def test_spell_resist():
    """'<target> resisted your <spell>!' — only first-person form exists."""
    line = '[Sat Apr 25 12:21:20 2026] Shei Vinitras resisted your Hammer of Magic!'
    ev = parse_line(line)
    assert isinstance(ev, SpellResist), f'expected SpellResist, got {type(ev).__name__}'
    assert_eq(ev.caster, 'You')
    assert_eq(ev.target, 'Shei Vinitras')
    assert_eq(ev.spell, 'Hammer of Magic')


def test_spell_resist_lowercase_article_target():
    """Mob targets resist too, e.g. 'A Stone Abomination resisted your ...'."""
    line = '[Wed Apr 16 19:20:03 2025] A Stone Abomination resisted your Bliss of the Nihil!'
    ev = parse_line(line)
    assert isinstance(ev, SpellResist)
    assert_eq(ev.target, 'A Stone Abomination')
    assert_eq(ev.spell, 'Bliss of the Nihil')


def test_spell_resist_with_apostrophe_in_spell_name():
    """Spell names can contain apostrophes (e.g. 'Rimeclaw's Assonant Binding')."""
    line = "[Fri Apr 18 01:18:06 2025] An inferno mephit resisted your Rimeclaw's Assonant Binding!"
    ev = parse_line(line)
    assert isinstance(ev, SpellResist)
    assert_eq(ev.target, 'An inferno mephit')
    assert_eq(ev.spell, "Rimeclaw's Assonant Binding")


def test_hit_with_new_verb_rend():
    """`rend` / `rends` are valid melee verbs (some mob attack types)."""
    line = '[Fri Apr 18 01:58:37 2025] An astral barnacle rends Roobius for 12970 points of damage. (Riposte Strikethrough)'
    ev = parse_line(line)
    assert isinstance(ev, MeleeHit), f'expected MeleeHit, got {type(ev).__name__}'
    assert_eq(ev.attacker, 'An astral barnacle')
    assert_eq(ev.verb, 'rends')
    assert_eq(ev.damage, 12970)


def test_miss_with_new_verb_rend():
    """`tries to rend` should classify as a normal miss."""
    line = '[Mon Jun 30 19:43:50 2025] An arisen spectre tries to rend Soloson, but Soloson ripostes!'
    ev = parse_line(line)
    assert isinstance(ev, MeleeMiss), f'expected MeleeMiss, got {type(ev).__name__}'
    assert_eq(ev.verb, 'rend')
    assert_eq(ev.outcome, 'riposte')


def test_hit_with_new_verb_stab():
    """`stab` / `stabs` (rogue-style verb) — appears as a melee hit verb."""
    line = '[Wed Jul 23 21:00:17 2025] Zun`Muram Votal stabs Frostyman for 930 points of damage.'
    ev = parse_line(line)
    assert isinstance(ev, MeleeHit), f'expected MeleeHit, got {type(ev).__name__}'
    assert_eq(ev.attacker, 'Zun`Muram Votal')
    assert_eq(ev.verb, 'stabs')
    assert_eq(ev.target, 'Frostyman')
    assert_eq(ev.damage, 930)


def test_hyphenated_attacker_name():
    """Names with hyphens like 'Cazic-Thule' must parse as one NAME, not
    truncate at the dash. Real raid-boss names use this form."""
    line = '[Wed Jul 23 21:00:17 2025] Cazic-Thule hit Hammerbeard for 254 points of poison damage by Greenmist Touch.'
    ev = parse_line(line)
    assert isinstance(ev, SpellDamage), f'expected SpellDamage, got {type(ev).__name__}'
    assert_eq(ev.attacker, 'Cazic-Thule')
    assert_eq(ev.target, 'Hammerbeard')
    assert_eq(ev.damage, 254)


def test_hyphenated_attacker_in_miss():
    """Hyphenated NAME on the attacker side of a miss line."""
    line = '[Wed Jul 23 21:00:17 2025] Terris-Thule tries to hit Vikolas, but Vikolas dodges!'
    ev = parse_line(line)
    assert isinstance(ev, MeleeMiss), f'expected MeleeMiss, got {type(ev).__name__}'
    assert_eq(ev.attacker, 'Terris-Thule')
    assert_eq(ev.target, 'Vikolas')
    assert_eq(ev.outcome, 'dodge')


def test_double_space_subject_to_verb_in_miss():
    """EQ occasionally injects a double space between the subject and the
    verb keyword (observed for `A Valorian Sentry  ` in real logs).
    Tolerate `[ ]+` rather than rejecting the line."""
    line = '[Wed May 21 19:57:55 2025] A Valorian Sentry  tries to punch Rimcaster, but misses!'
    ev = parse_line(line)
    assert isinstance(ev, MeleeMiss), f'expected MeleeMiss, got {type(ev).__name__}'
    assert_eq(ev.attacker, 'A Valorian Sentry')
    assert_eq(ev.outcome, 'miss')


def test_double_space_verb_to_target_in_hit():
    """Same EQ quirk between the verb and the target name."""
    line = '[Wed May 21 19:57:55 2025] Redfreddy slashes  Sigismond Windwalker for 245 points of damage. (Riposte)'
    ev = parse_line(line)
    assert isinstance(ev, MeleeHit), f'expected MeleeHit, got {type(ev).__name__}'
    assert_eq(ev.attacker, 'Redfreddy')
    assert_eq(ev.target, 'Sigismond Windwalker')
    assert_eq(ev.damage, 245)
    assert 'Riposte' in ev.modifiers


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


def test_spell_damage_lowercase_article_attacker():
    """Mob spell-damage lines start with lowercase article ('a foo hit X
    for N points by Spell.'). The body-start NAME variant accepts these."""
    line = '[Wed Jul 23 21:00:17 2025] a shadowstone grabber hit Robbinwuud for 173424 points of unresistable damage by Fracturing Stomp.'
    ev = parse_line(line)
    assert isinstance(ev, SpellDamage), f'expected SpellDamage, got {type(ev).__name__}'
    assert_eq(ev.attacker, 'a shadowstone grabber')
    assert_eq(ev.target, 'Robbinwuud')
    assert_eq(ev.damage, 173424)
    assert_eq(ev.damage_type, 'unresistable')
    assert_eq(ev.spell, 'Fracturing Stomp')


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


def test_falling_damage_speculative_format():
    """UNVERIFIED format — see FALLING_DAMAGE_RE. None of the fixture
    logs contain real fall damage; this asserts the speculative pattern
    parses cleanly. If a real EQ fall-damage line appears with a
    different shape, update both the regex and this test."""
    line = '[Sat Apr 25 13:00:00 2026] You take 250 points of falling damage.'
    ev = parse_line(line)
    assert isinstance(ev, SpellDamage), f'expected SpellDamage, got {type(ev).__name__}'
    assert_eq(ev.attacker, '(falling)')
    assert_eq(ev.target, 'You')
    assert_eq(ev.damage, 250)
    assert_eq(ev.damage_type, 'falling')


def test_pain_and_suffering_unconscious_bleed():
    """'Pain and suffering strikes you for N damage!' — bleed-out damage
    when the player is at 0 HP / unconscious. Routed to attacker
    '(unconscious)' so the damage shows up in damage-taken views without
    faking a real mob attacker."""
    line = '[Sat Apr 25 19:20:40 2026] Pain and suffering strikes you for 39155 damage!'
    ev = parse_line(line)
    assert isinstance(ev, SpellDamage), f'expected SpellDamage, got {type(ev).__name__}'
    assert_eq(ev.attacker, '(unconscious)')
    assert_eq(ev.target, 'You')
    assert_eq(ev.damage, 39155)
    assert_eq(ev.damage_type, 'unconscious')
    assert_eq(ev.spell, 'Pain and suffering')


def test_you_were_hit_by_non_melee():
    """'You were hit by non-melee for N damage.' — EQ doesn't name a
    source for these (literal 'non-melee' is the source descriptor).
    Routed to '(unattributed)' attacker so the damage still reaches
    damage-taken views."""
    line = '[Wed May 21 19:40:08 2025] You were hit by non-melee for 10 damage.'
    ev = parse_line(line)
    assert isinstance(ev, SpellDamage), f'expected SpellDamage, got {type(ev).__name__}'
    assert_eq(ev.attacker, '(unattributed)')
    assert_eq(ev.target, 'You')
    assert_eq(ev.damage, 10)
    assert_eq(ev.damage_type, 'non-melee')
    assert ev.spell is None


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


def test_heal_passive_no_healer_pet_self_heal():
    """Pet self-heal proc — 'X`s pet has been healed for N hit points by SPELL.'
    No `by HEALER` clause because the source is a proc on the pet itself.
    Healer is set equal to target so it rolls up as a self-heal."""
    line = "[Sat Apr 25 17:00:48 2026] Hacral`s pet has been healed for 45000 hit points by Enhanced Theft of Essence Effect XVI."
    ev = parse_line(line)
    assert isinstance(ev, HealEvent), f'expected HealEvent, got {type(ev).__name__}'
    assert_eq(ev.healer, 'Hacral`s pet', 'self-heal: healer == target')
    assert_eq(ev.target, 'Hacral`s pet')
    assert_eq(ev.amount, 45000)
    assert_eq(ev.spell, 'Enhanced Theft of Essence Effect XVI')


def test_heal_passive_no_healer_with_overheal_parens():
    """Same shape with the `(N)` gross-amount parenthetical — applied
    amount can be 0 when target is already at full HP."""
    line = "[Sat Apr 25 13:51:25 2026] Hacral`s pet has been healed for 0 (45000) hit points by Enhanced Theft of Essence Effect XVI."
    ev = parse_line(line)
    assert isinstance(ev, HealEvent)
    assert_eq(ev.healer, 'Hacral`s pet')
    assert_eq(ev.target, 'Hacral`s pet')
    assert_eq(ev.amount, 0)
    assert_eq(ev.spell, 'Enhanced Theft of Essence Effect XVI')


def test_heal_passive_no_healer_named_pet():
    """Mage pets with proper names (Onyx, Rover, Knothead, etc.) appear
    without the backtick `'s pet` suffix because EQ uses their proper
    name in this proc message. Still a self-heal."""
    line = "[Sat Apr 25 17:36:15 2026] Onyx has been healed for 50000 hit points by Theft of Essence Effect XVII."
    ev = parse_line(line)
    assert isinstance(ev, HealEvent)
    assert_eq(ev.healer, 'Onyx')
    assert_eq(ev.target, 'Onyx')
    assert_eq(ev.amount, 50000)


def test_heal_passive_no_healer_does_not_steal_with_healer_form():
    """The new no-healer pattern must not match lines that have a
    `by HEALER` clause — those should still go through HEAL_PASSIVE_RE."""
    line = '[Sat Apr 25 12:21:30 2026] Lunarya has been healed by Soloson for 86000 hit points by Healing Touch.'
    ev = parse_line(line)
    assert isinstance(ev, HealEvent)
    assert_eq(ev.healer, 'Soloson', 'with-healer form should still win')
    assert_eq(ev.target, 'Lunarya')


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


def test_you_slew_format():
    """When YOU deliver the killing blow, EQ writes 'You have slain X!'
    instead of the third-person 'X was/has been slain by ...' form. Without
    this pattern every solo kill silently expires via gap_seconds and gets
    marked Incomplete in the UI."""
    line = '[Sat Apr 25 12:32:47 2026] You have slain Pli Liako!'
    ev = parse_line(line)
    assert isinstance(ev, DeathMessage)
    assert_eq(ev.victim, 'Pli Liako')
    assert_eq(ev.killer, 'You')
    assert ev.you_died is False


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
