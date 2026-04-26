"""
events.py - the *shape* of parsed log data.

Flurry only cares about combat-related events. The bot project keeps a
much wider event vocabulary; here we focus.

Every line in the EQ log file, after parsing, becomes either one of these
dataclasses or None (for lines we skip). Lines that look like log entries
but don't match a known pattern become UnknownEvent so the parser can
report coverage.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List


@dataclass
class Event:
    """Base class - every event has a timestamp and the original raw line.

    Keeping `raw` around is invaluable for debugging: when something parses
    wrong, we can always go back to the source.
    """
    timestamp: datetime
    raw: str


# ----- Damage events -----

@dataclass
class MeleeHit(Event):
    """A successful melee swing.

    Examples:
      'You slash Shei Vinitras for 282413 points of damage. (Critical)'
      'Soloson slashes Shei Vinitras for 76100 points of damage.'
      'Soloson`s pet slashes Shei Vinitras for 54 points of damage.'

    Note: 'You' uses bare verbs ('slash'), third-person uses -es/-s ('slashes').
    Pets use a backtick ('Soloson`s pet') not an apostrophe - EQ quirk.
    """
    attacker: str
    verb: str
    target: str
    damage: int
    modifiers: List[str] = field(default_factory=list)


@dataclass
class MeleeMiss(Event):
    """A melee swing that missed.

    Examples:
      'You try to slash Shei Vinitras, but miss!'
      'Soloson tries to slash Shei Vinitras, but misses!'
    """
    attacker: str
    verb: str
    target: str
    modifiers: List[str] = field(default_factory=list)


@dataclass
class SpellDamage(Event):
    """Damage from a named spell, or a damage shield/proc.

    Examples (named spell):
      'You hit X for 6529 points of cold damage by Strike of Ice I.'
      'Sinsuous hit X for 1215 points of poison damage by Call for Blood.'

    Examples (damage shield/proc - 'is X by Y's Z' form):
      'Shei Vinitras is pierced by Soloson's thorns for 5175 points of non-melee damage.'

    For DSes/procs, we attribute damage to the source's OWNER (Soloson),
    not to the source itself, so DPS totals reflect player contribution.
    """
    attacker: str
    target: str
    damage: int
    damage_type: str         # 'magic', 'cold', 'fire', 'poison', 'non-melee', etc.
    spell: Optional[str]     # spell name, or source ('thorns'), or None
    modifiers: List[str] = field(default_factory=list)


@dataclass
class DeathMessage(Event):
    """Something died. Used to mark fight boundaries.

    Two formats appear in the wild:
      'X has been slain by Y!'    (common mobs, NPCs)
      'X was slain by Y!'         (raid bosses often use this form)

    Plus the player-death form:
      'You have been slain by X!'
    """
    victim: str
    killer: Optional[str]
    you_died: bool


# ----- Healing events -----

@dataclass
class HealEvent(Event):
    """A healing event — one heal landed.

    Examples:
      'Soloson healed Hacral for 50000 hit points by Word of Restoration.'
      'You healed yourself for 1500 hit points by Self Heal.'
      'Soloson healed Hacral for 8000 (12000) hit points by Healing Light.'
        (Parenthetical is the gross amount before overheal — we ignore it
         for now and record only the actual amount healed.)

    Self-targeting heals get normalized: a target of 'yourself' / 'himself'
    / 'herself' / 'itself' is rewritten to the healer's name so the matrix
    of (healer → target) doesn't fragment by pronoun.
    """
    healer: str
    target: str
    amount: int
    spell: Optional[str]
    modifiers: List[str] = field(default_factory=list)


# ----- Boundary events -----

@dataclass
class ZoneEntered(Event):
    """e.g. 'You have entered The Plane of Tranquility.'

    Used to detect zone changes for breaking up multi-zone log files.
    """
    zone: str


# ----- Catch-all -----

@dataclass
class UnknownEvent(Event):
    """A line that has a valid timestamp but didn't match any pattern.

    Kept for parser-coverage analysis - run flurry-dps with --unknown
    (when implemented) to see what's not being recognized.
    """
    body: str
