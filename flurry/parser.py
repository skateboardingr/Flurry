"""
parser.py - turn raw log lines into Event objects.

Strategy: a list of (compiled_regex, builder) pairs. We try each pattern
in order until one matches. First match wins, so order patterns from
most-specific to most-general (otherwise a general pattern eats lines
that a specific one should have caught).

EQ log line format:
    [Mon Apr 14 19:37:24 2025] <body>\\r\\n

We split timestamp from body once, then match patterns against the body.
"""

import re
from datetime import datetime
from typing import Optional, List, Tuple, Callable

from .events import (
    Event, MeleeHit, MeleeMiss, SpellDamage, SpellResist, DeathMessage,
    HealEvent, ZoneEntered, UnknownEvent,
)


# ----- Timestamp extraction -----

# Matches '[Mon Apr 14 19:37:24 2025] ' and captures the timestamp + body.
TIMESTAMP_RE = re.compile(
    r'^\[(?P<ts>[A-Z][a-z]{2} [A-Z][a-z]{2} ?\d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] (?P<body>.*)$'
)

TS_FORMAT = '%a %b %d %H:%M:%S %Y'


def parse_timestamp(ts_str: str) -> datetime:
    """Parse '[Mon Apr 14 19:37:24 2025]' style timestamp.

    EQ sometimes pads day with a space instead of zero ('Apr  4' vs 'Apr 04').
    Collapse double-spaces so strptime is happy.
    """
    normalized = re.sub(r'\s+', ' ', ts_str)
    return datetime.strptime(normalized, TS_FORMAT)


# ----- Body patterns -----

# Melee verbs.
# Third-person form (X slashes Y): -s/-es ending.
# First-person form (You slash Y): bare verb.
# Both variants exist in the log because the player's own log captures
# both their own attacks (first-person) and other players' (third-person).
MELEE_VERBS_HIT_3RD = (
    r'(?:slashes|crushes|punches|pierces|claws|kicks|bites|hits|gores|strikes|'
    r'mauls|smashes|stings|backstabs|frenzies on|maims|slices|bashes|shoots|'
    r'rends|stabs)'
)
MELEE_VERBS_HIT_1ST = (
    r'(?:slash|crush|punch|pierce|claw|kick|bite|hit|gore|strike|maul|smash|'
    r'sting|backstab|frenzy on|maim|slice|bash|shoot|rend|stab)'
)
MELEE_VERBS_MISS = MELEE_VERBS_HIT_1ST  # 'tries to slash' uses bare form

# Name pattern that allows pets (e.g. 'Soloson`s pet' - note BACKTICK,
# not apostrophe - that's an EQ quirk going back forever), possessives,
# comma-titles like 'Keltakun, Last Word' (mob with epithet), and hyphens
# in proper nouns (e.g. 'Cazic-Thule', 'Terris-Thule'). The separator
# alternation is `[ '`]` for word breaks and `, ` for the title-comma case.
NAME = r"[A-Z][\w'`-]*(?:(?:[ '`]|, )[\w'`-]+)*"

# Variant that also accepts a lowercase first letter, used at body-start
# positions where EQ writes mob names with lowercase articles ('a Solusek
# mage hit X for N points by Spell.', 'an astral barnacle ...'). Keeping
# this separate from NAME means mid-line NAME usage (after `by`, in heal
# clauses, etc.) keeps the strict capital-start guard against slurping
# common English words from neighboring text.
BODY_NAME = r"[A-Za-z][\w'`-]*(?:(?:[ '`]|, )[\w'`-]+)*"


# --- Spell damage: 'X hit Y for N points of TYPE damage by SPELL.' ---
# Real EQ uses bare 'hit' for BOTH first and third person here (only melee
# uses '-s' for third person, e.g. 'Soloson slashes'). The previous split
# into 1ST/3RD with '-s' broke parsing of every third-person spell hit.
#
# Passive form 'X has been hit by Y for N points of damage by Spell' would
# otherwise let `NAME` slurp 'X has been' as the attacker; the
# `(?!by )` lookahead on `target` blocks that — with the guard, the
# would-be attacker can't backtrack into a working configuration because
# the only target text available starts with 'by '.
SPELL_DAMAGE_RE = re.compile(
    rf'^(?P<attacker>You|{BODY_NAME}) hit (?P<target>(?!by )[^.]+?) '
    rf'for (?P<dmg>\d+) points? of (?P<dtype>[\w-]+) damage '
    rf'by (?P<spell>[^.]+?)\.(?P<rest>.*)$'
)

# --- Damage shield / proc: 'X is pierced by Y's thorns for N points of non-melee damage.' ---
# Attacker is the OWNER of the source (e.g. Soloson), so DPS totals reflect player contribution.
# First-person variant uses 'YOUR' (all caps) instead of '<name>\'s' for the
# possessive: 'A nilborien hawk is pierced by YOUR thorns for 298136 points of non-melee damage.'
# When YOUR matches, the named-attacker group is None and the builder
# substitutes 'You'.
DAMAGE_SHIELD_RE = re.compile(
    rf"^(?P<target>.+?) is [\w ]+? by (?:(?P<attacker>{NAME})'s|YOUR) "
    rf"(?P<source>[\w ]+?) for (?P<dmg>\d+) points? of non-melee damage\."
    rf"(?P<rest>.*)$"
)

# --- Generic non-melee: 'X was chilled to the bone for 45 points of non-melee damage.' ---
# No attacker named in this format - typically environmental or DoT residue.
NONMELEE_DAMAGE_RE = re.compile(
    r'^(?P<target>.+?) was (?P<effect>[\w \-]+?) '
    r'for (?P<dmg>\d+) points? of non-melee damage\.(?P<rest>.*)$'
)

# --- First-person bare non-melee: 'You were hit by non-melee for N damage.' ---
# A passive form with no source named — EQ literally writes 'non-melee' as
# the source descriptor, so there's nothing further to attribute. Routed
# to '(unattributed)' so it still flows into damage-taken views.
YOU_NONMELEE_RE = re.compile(
    r'^You were hit by non-melee for (?P<dmg>\d+) damage\.(?P<rest>.*)$'
)

# --- Falling damage: 'You take N points of falling damage.' ---
# UNVERIFIED — none of our six fixture logs contain a single fall-damage
# event, so this format is from community memory rather than direct
# observation. If you encounter a real fall-damage line and the format
# differs, replace this regex with the actual one. Tagged with attacker
# '(falling)' (separate sentinel from '(unattributed)') so a tank flying
# off a platform during a boss mechanic shows up labeled rather than
# disappearing into the generic non-melee bucket.
FALLING_DAMAGE_RE = re.compile(
    r'^You take (?P<dmg>\d+) points? of falling damage\.(?P<rest>.*)$'
)

# --- DoT damage: 'X has/have taken N damage from SPELL by Y.' ---
# Modern EQ writes DoT ticks in this passive form with the source named
# *after* `by`. Matches:
#   'You have taken 152563 damage from Gouging Strike by Feather Silver Sheen.'
#   'Robbinwuud has taken 160717 damage from Gouging Strike by Feather Silver Sheen.'
# 'has' for third-person targets, 'have' for first-person (You). No
# damage type word in this format — we tag it as 'dot' for the type.
DOT_DAMAGE_RE = re.compile(
    rf'^(?P<target>You|{BODY_NAME}) (?:has|have) taken (?P<dmg>\d+) damage '
    rf'from (?P<spell>.+?) by (?P<attacker>You|{NAME})\.(?P<rest>.*)$'
)

# --- Third-person melee: 'X slashes Y for N points of damage.' ---
# Spaces are written as `[ ]+` (one or more) because EQ occasionally
# injects an extra space between tokens — observed forms include
# 'Redfreddy slashes  Sigismond Windwalker' (verb→target gap doubled)
# and 'A Valorian Sentry  punches Rimcaster' (subject→verb doubled).
# We tolerate the variation rather than reject those lines.
MELEE_HIT_3RD_RE = re.compile(
    rf'^(?P<attacker>{BODY_NAME})[ ]+(?P<verb>{MELEE_VERBS_HIT_3RD})[ ]+(?P<target>.+?)[ ]+'
    rf'for (?P<dmg>\d+) points? of damage\.(?P<rest>.*)$'
)

# --- First-person melee: 'You slash Y for N points of damage.' ---
# Note: bare verb (no -s/-es). 'You hit X for N points of damage' (no spell)
# is also caught here.
MELEE_HIT_1ST_RE = re.compile(
    rf'^(?P<attacker>You)[ ]+(?P<verb>{MELEE_VERBS_HIT_1ST})[ ]+(?P<target>.+?)[ ]+'
    rf'for (?P<dmg>\d+) points? of damage\.(?P<rest>.*)$'
)

# --- Miss / avoidance tail ---
# A successful avoidance line ends in one of these clauses. Used by both
# the 1st- and 3rd-person miss patterns below. We capture the whole tail
# in one named group and let `_classify_miss_tail` pick the outcome word
# in Python — keeps the regex readable and one place to extend.
#
# Avoider names in the tail can be lowercase-articled mid-sentence (e.g.
# 'a Solusek foot soldier dodges!'), so we use `[^!]+?` rather than NAME.
# Each branch ends with `!` so the lazy quantifier is self-bounded.
#
# Branches:
#   miss(es)! / fail(s)!                    — pure miss/fail
#   <name> ripostes? !                      — riposte (1st: 'YOU riposte!')
#   <name> dodges?|parr(y|ies)|blocks? !    — avoidance (1st: bare verb)
#   <name>['s|R] magical skin absorbs the blow! — rune (1st: 'YOUR ...')
#   <name> (is|are) INVULNERABLE!           — divine aura / god mode
_MISS_TAIL = (
    r'(?:miss(?:es)?!|fail(?:s)?!'
    r'|[^!]+? ripostes?!'
    # Block has an optional ' with <his/her/its> <shield/staff/...>' suffix
    # ('Tira blocks with her shield!'); dodge and parry never do.
    r'|[^!]+? (?:dodges?|parr(?:y|ies)|blocks?(?: with [^!]+)?)!'
    r'|[^!]+? magical skin absorbs the blow!'
    r'|[^!]+? (?:is|are) INVULNERABLE!)'
)


# --- Third-person miss: 'X tries to slash Y, but misses!' ---
# `[ ]+` rather than literal space at token joins, see MELEE_HIT_3RD_RE.
MELEE_MISS_3RD_RE = re.compile(
    rf'^(?P<attacker>{BODY_NAME})[ ]+tries to[ ]+(?P<verb>{MELEE_VERBS_MISS})[ ]+'
    rf'(?P<target>.+?), but (?P<tail>{_MISS_TAIL})(?P<rest>.*)$'
)

# --- First-person miss: 'You try to slash Y, but miss!' ---
MELEE_MISS_1ST_RE = re.compile(
    rf'^(?P<attacker>You)[ ]+try to[ ]+(?P<verb>{MELEE_VERBS_MISS})[ ]+'
    rf'(?P<target>.+?), but (?P<tail>{_MISS_TAIL})(?P<rest>.*)$'
)


# --- Spell resist: 'X resisted your <Spell>!' ---
# Only first-person form exists in the log (EQ filters to your perspective —
# you never see other players' targets resist their casts).
SPELL_RESIST_RE = re.compile(
    rf'^(?P<target>{BODY_NAME}) resisted your (?P<spell>.+?)!$'
)

# --- Passive heal: 'X has been healed (over time) by Y for N hit points by SPELL.' ---
# EQ writes HoT ticks and many proc heals in the passive form with the
# healer named AFTER `by`. This pattern must run before HEAL_RE because
# our `NAME` regex is loose enough that it would otherwise greedily slurp
# 'Lunarya has been' as the healer in 'Lunarya has been healed by ...'.
HEAL_PASSIVE_RE = re.compile(
    rf'^(?P<target>You|{BODY_NAME}) (?:has been|have been) healed '
    rf'(?:over time )?by (?P<healer>You|{NAME}) '
    rf'for (?P<amt>\d+)(?:\s+\(\d+\))? hit points?'
    rf'(?: by (?P<spell>[^.]+?))?\.(?P<rest>.*)$'
)


# --- Heal: 'X healed Y for N hit points by SPELL.' ---
# The auxiliary 'has'/'have' shows up in some EQ versions ('You have healed
# Soloson for ...'). The optional `(N)` parenthetical is the gross amount
# before overheal capping — we don't track overheal yet, so it's discarded.
# The 'over time' qualifier marks a HoT tick rather than a direct heal.
# `by SPELL` is optional because some lines omit it.
HEAL_RE = re.compile(
    rf'^(?P<healer>You|{BODY_NAME}) (?:has |have )?healed (?P<target>.+?) '
    rf'(?:over time )?for (?P<amt>\d+)(?:\s+\(\d+\))? hit points?'
    rf'(?: by (?P<spell>[^.]+?))?\.(?P<rest>.*)$'
)


# --- Death: 'X has been slain by Y!' or 'X was slain by Y!' ---
SLAIN_RE = re.compile(r'^(?P<victim>.+?) (?:has been slain|was slain) by (?P<killer>.+?)!$')

# --- Death: 'You have been slain by Y!' ---
YOU_SLAIN_RE = re.compile(r'^You have been slain by (?P<killer>.+?)!$')

# --- Zone: 'You have entered ZONE.' ---
# Negative lookahead for 'an area' to skip sub-zone messages like
# 'You have entered an area where levitation effects do not function.'
ZONE_ENTERED_RE = re.compile(r'^You have entered (?!an area )(?P<zone>.+?)\.$')


# ----- Helpers -----

def _strip_modifiers(text: str) -> Tuple[str, List[str]]:
    """Pull trailing '(Critical)', '(Flurry)', etc. off a line.

    EQ writes modifiers as parenthesized space-separated keywords like
    '(Lucky Critical Headshot)' - we keep them as raw strings and let the
    analyzer split on spaces if it needs to detect specific ones.
    """
    modifiers = re.findall(r'\(([^)]+)\)', text)
    cleaned = re.sub(r'\s*\([^)]+\)', '', text).strip()
    return cleaned, modifiers


# ----- Builders -----

def _build_spell_damage(ts, raw, m):
    _, modifiers = _strip_modifiers(m.group('rest'))
    return SpellDamage(timestamp=ts, raw=raw,
                       attacker=m.group('attacker'),
                       target=m.group('target'),
                       damage=int(m.group('dmg')),
                       damage_type=m.group('dtype'),
                       spell=m.group('spell'),
                       modifiers=modifiers)


def _build_damage_shield(ts, raw, m):
    _, modifiers = _strip_modifiers(m.group('rest'))
    # `attacker` group is None for the YOUR variant — that's the player's
    # own DS, so attribute it to 'You' to match the rest of the parser's
    # first-person convention.
    return SpellDamage(timestamp=ts, raw=raw,
                       attacker=m.group('attacker') or 'You',
                       target=m.group('target'),
                       damage=int(m.group('dmg')),
                       damage_type='non-melee',
                       spell=m.group('source'),
                       modifiers=modifiers)


def _build_dot_damage(ts, raw, m):
    _, modifiers = _strip_modifiers(m.group('rest'))
    return SpellDamage(timestamp=ts, raw=raw,
                       attacker=m.group('attacker'),
                       target=m.group('target'),
                       damage=int(m.group('dmg')),
                       damage_type='dot',
                       spell=m.group('spell'),
                       modifiers=modifiers)


def _build_nonmelee_damage(ts, raw, m):
    # EQ writes some non-melee damage with no source named in the line —
    # DoT ticks of the form "X was struck for N points of non-melee damage."
    # have no `by Y` suffix. We can't attribute the damage to anyone, so
    # we use a sentinel name. Calling it "(unattributed)" rather than "?"
    # so it reads sensibly in per-attacker tables.
    return SpellDamage(timestamp=ts, raw=raw,
                       attacker='(unattributed)',
                       target=m.group('target'),
                       damage=int(m.group('dmg')),
                       damage_type='non-melee',
                       spell=None,
                       modifiers=[])


def _build_you_nonmelee(ts, raw, m):
    # 'You were hit by non-melee for N damage.' — same convention as the
    # generic non-melee builder: no source available, attribute to
    # '(unattributed)' so the damage still reaches damage-taken views.
    _, modifiers = _strip_modifiers(m.group('rest'))
    return SpellDamage(timestamp=ts, raw=raw,
                       attacker='(unattributed)',
                       target='You',
                       damage=int(m.group('dmg')),
                       damage_type='non-melee',
                       spell=None,
                       modifiers=modifiers)


def _build_falling_damage(ts, raw, m):
    # See FALLING_DAMAGE_RE — pattern is unverified. Attacker tagged
    # '(falling)' so a tank thrown off a platform mid-fight shows up
    # labeled rather than collapsing into '(unattributed)'.
    _, modifiers = _strip_modifiers(m.group('rest'))
    return SpellDamage(timestamp=ts, raw=raw,
                       attacker='(falling)',
                       target='You',
                       damage=int(m.group('dmg')),
                       damage_type='falling',
                       spell=None,
                       modifiers=modifiers)


def _build_melee_hit(ts, raw, m):
    """Builder for both first- and third-person melee hits.
    Both regex variants name the same groups (attacker, verb, target, dmg, rest)
    so we can share one builder."""
    _, modifiers = _strip_modifiers(m.group('rest'))
    return MeleeHit(timestamp=ts, raw=raw,
                    attacker=m.group('attacker'),
                    verb=m.group('verb'),
                    target=m.group('target'),
                    damage=int(m.group('dmg')),
                    modifiers=modifiers)


def _classify_miss_tail(tail: str) -> str:
    """Map a captured miss-tail string to its outcome label.

    Order matters: more-specific markers first. 'magical skin' before
    'absorb' (single keyword anyway), 'INVULNERABLE' before 'are'/'is',
    'riposte' before bare 'dodge/parry/block' (no overlap, but kept for
    clarity)."""
    if 'magical skin' in tail:
        return 'rune'
    if 'INVULNERABLE' in tail:
        return 'invulnerable'
    if 'riposte' in tail:
        return 'riposte'
    if 'parr' in tail:           # parry / parries
        return 'parry'
    if 'block' in tail:
        return 'block'
    if 'dodge' in tail:
        return 'dodge'
    return 'miss'                # miss / misses / fail / fails


def _build_melee_miss(ts, raw, m):
    _, modifiers = _strip_modifiers(m.group('rest'))
    return MeleeMiss(timestamp=ts, raw=raw,
                     attacker=m.group('attacker'),
                     verb=m.group('verb'),
                     target=m.group('target'),
                     outcome=_classify_miss_tail(m.group('tail')),
                     modifiers=modifiers)


def _build_spell_resist(ts, raw, m):
    return SpellResist(timestamp=ts, raw=raw,
                       caster='You',
                       target=m.group('target'),
                       spell=m.group('spell'))


def _build_you_slain(ts, raw, m):
    return DeathMessage(timestamp=ts, raw=raw,
                        victim='You',
                        killer=m.group('killer'),
                        you_died=True)


def _build_slain(ts, raw, m):
    return DeathMessage(timestamp=ts, raw=raw,
                        victim=m.group('victim'),
                        killer=m.group('killer'),
                        you_died=False)


def _build_zone_entered(ts, raw, m):
    return ZoneEntered(timestamp=ts, raw=raw, zone=m.group('zone'))


# Pronouns that EQ uses for self-targeted heals. We normalize them to the
# healer's own name so the per-(healer, target) matrix doesn't get a
# spurious "Soloson → himself" row alongside "Soloson → Soloson".
_SELF_TARGET_PRONOUNS = {'yourself', 'himself', 'herself', 'itself'}


def _build_heal(ts, raw, m):
    _, modifiers = _strip_modifiers(m.group('rest'))
    healer = m.group('healer')
    target = m.group('target')
    if target.lower() in _SELF_TARGET_PRONOUNS:
        target = healer
    return HealEvent(timestamp=ts, raw=raw,
                     healer=healer,
                     target=target,
                     amount=int(m.group('amt')),
                     spell=m.group('spell'),
                     modifiers=modifiers)


def _build_heal_passive(ts, raw, m):
    """Builder for passive-form heals ('X has been healed by Y for N...').
    Group names are still healer/target so the dataclass shape matches the
    active-form builder exactly."""
    _, modifiers = _strip_modifiers(m.group('rest'))
    return HealEvent(timestamp=ts, raw=raw,
                     healer=m.group('healer'),
                     target=m.group('target'),
                     amount=int(m.group('amt')),
                     spell=m.group('spell'),
                     modifiers=modifiers)


# Order: most-specific first. SPELL_DAMAGE has 'by SPELL' which is more
# specific than the bare melee patterns; we put it before MELEE_HIT.
PATTERNS: List[Tuple[re.Pattern, Callable]] = [
    (SPELL_DAMAGE_RE,    _build_spell_damage),
    (DOT_DAMAGE_RE,      _build_dot_damage),
    (DAMAGE_SHIELD_RE,   _build_damage_shield),
    (NONMELEE_DAMAGE_RE, _build_nonmelee_damage),
    (YOU_NONMELEE_RE,    _build_you_nonmelee),
    (FALLING_DAMAGE_RE,  _build_falling_damage),
    (MELEE_HIT_1ST_RE,   _build_melee_hit),
    (MELEE_HIT_3RD_RE,   _build_melee_hit),
    (MELEE_MISS_1ST_RE,  _build_melee_miss),
    (MELEE_MISS_3RD_RE,  _build_melee_miss),
    (SPELL_RESIST_RE,    _build_spell_resist),
    (HEAL_PASSIVE_RE,    _build_heal_passive),
    (HEAL_RE,            _build_heal),
    (YOU_SLAIN_RE,       _build_you_slain),
    (SLAIN_RE,           _build_slain),
    (ZONE_ENTERED_RE,    _build_zone_entered),
]


def parse_line(line: str) -> Optional[Event]:
    """Parse one log line into an Event.

    Returns None if the line doesn't have a valid timestamp prefix
    (blank line, partial line during file rotation, etc.).
    Returns UnknownEvent if the timestamp parses but no body pattern matches.
    """
    line = line.rstrip('\r\n').rstrip()
    if not line:
        return None

    ts_match = TIMESTAMP_RE.match(line)
    if not ts_match:
        return None

    ts = parse_timestamp(ts_match.group('ts'))
    body = ts_match.group('body')

    for pattern, builder in PATTERNS:
        m = pattern.match(body)
        if m:
            return builder(ts, line, m)

    return UnknownEvent(timestamp=ts, raw=line, body=body)
