"""
analyzer.py - the data layer of fight analysis.

Pure computation, no I/O formatting. Reads a log file, isolates a single
fight by target name, and produces structured data about who did what.

Render this with `flurry.report` (text or HTML), or consume it directly
from another tool (Discord bot, web UI, JSON export, etc.).

Design note: this module should never `print()`. Everything goes into
returned dataclasses. That separation lets the same analysis power
text reports, charts, dashboards, and downstream automation.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .events import (
    MeleeHit, MeleeMiss, SpellDamage, DeathMessage, HealEvent, UnknownEvent,
)
from .parser import parse_line
from .tail import tail_file


# Special-attack modifier names that the analyzer breaks out separately.
# These are EQ-mechanic-specific; users can extend by passing extra names
# to `analyze_fight(special_mods=...)`.
DEFAULT_SPECIAL_MODS = (
    'Headshot',          # ranger archery one-shot
    'Assassinate',       # rogue backstab one-shot
    'Slay Undead',       # paladin undead one-shot
    'Decapitate',        # berserker one-shot
    'Double Bow Shot',   # ranger AA double-tap
    'Flurry',            # extra melee swing
    'Twincast',          # double-cast spell proc
    'Rampage',           # warrior AoE swing
)

# Modifier substrings that indicate a critical hit. EQ writes these as
# space-separated keywords inside parens, e.g. '(Lucky Critical Headshot)'.
CRIT_MOD_SUBSTRINGS = (
    'Critical',
    'Crippling Blow',
    'Lucky Critical',
    'Lucky Crippling Blow',
)


# ----- Data structures -----

@dataclass
class Hit:
    """One landed damage event, attributed to one attacker."""
    timestamp: datetime
    attacker: str
    target: str
    damage: int
    modifiers: List[str] = field(default_factory=list)
    specials: List[str] = field(default_factory=list)
    kind: str = 'melee'  # 'melee' or 'spell'
    # For spell hits / DS procs / DoT ticks this is the ability name (e.g.
    # 'Strike of Ice I', 'thorns', 'Gouging Strike'). None for plain melee
    # swings, which have no source name in the EQ log.
    spell: Optional[str] = None
    # The melee verb captured from the log line (`slashes`, `backstabs`,
    # `hits`, etc.). Lets the UI break melee damage down by attack type so
    # a rogue's backstabs are distinguishable from auto-attack.
    verb: Optional[str] = None


@dataclass
class AttackerStats:
    """Per-attacker accumulator. Tracks both totals and special-attack
    breakdowns so reports can answer 'what *kind* of damage'."""
    attacker: str
    damage: int = 0
    hits: int = 0
    misses: int = 0
    crits: int = 0
    biggest: int = 0
    # special_damage[mod_name] -> total damage from hits with that modifier
    special_damage: Dict[str, int] = field(default_factory=dict)
    special_hits: Dict[str, int] = field(default_factory=dict)


@dataclass
class Heal:
    """One heal event. Parallel to Hit; lives at the encounter level rather
    than per-fight since heals don't define fight boundaries."""
    timestamp: datetime
    healer: str
    target: str
    amount: int
    spell: Optional[str] = None
    modifiers: List[str] = field(default_factory=list)


@dataclass
class HealerStats:
    """Per-healer accumulator across an encounter."""
    healer: str
    healing: int = 0
    casts: int = 0
    crits: int = 0
    biggest: int = 0
    spell_amount: Dict[str, int] = field(default_factory=dict)
    spell_casts: Dict[str, int] = field(default_factory=dict)


@dataclass
class FightResult:
    """Everything we computed about one fight.

    Render with flurry.report.text_report() or flurry.report.html_report(),
    or consume the fields directly.
    """
    target: str
    start: Optional[datetime]
    end: Optional[datetime]
    hits: List[Hit]
    stats_by_attacker: Dict[str, AttackerStats]
    fight_complete: bool       # True only if we saw the target's death event
    fight_id: Optional[int] = None  # set by detect_fights(); None for analyze_fight()

    @property
    def duration_seconds(self) -> float:
        if self.start is None or self.end is None:
            return 0.0
        return (self.end - self.start).total_seconds()

    @property
    def total_damage(self) -> int:
        return sum(s.damage for s in self.stats_by_attacker.values())

    @property
    def raid_dps(self) -> float:
        d = self.duration_seconds
        return self.total_damage / d if d > 0 else 0.0

    def attackers_by_damage(self) -> List[AttackerStats]:
        """Attackers sorted by damage descending."""
        return sorted(self.stats_by_attacker.values(),
                      key=lambda s: s.damage, reverse=True)


# ----- Helpers -----

def is_crit(modifiers: List[str]) -> bool:
    """A hit is a 'crit' if any of the recognized crit substrings appears
    inside any of the modifier strings.

    Modifiers come as space-separated combo strings ('Lucky Critical Headshot'),
    so we substring-search rather than exact-match.
    """
    for m in modifiers:
        for c in CRIT_MOD_SUBSTRINGS:
            if c in m:
                return True
    return False


def extract_specials(modifiers: List[str], special_mods=DEFAULT_SPECIAL_MODS) -> List[str]:
    """Find which 'special attack' types appear in this hit's modifiers."""
    return [s for s in special_mods if any(s in m for m in modifiers)]


# ----- Internal accumulator -----

class _FightBuilder:
    """Mutable accumulator that turns a stream of damage/miss events into a
    FightResult. Shared by analyze_fight (one named target) and detect_fights
    (every target in a session) so attribution logic lives in one place.
    """

    def __init__(self, target: str, start: datetime,
                 special_mods=DEFAULT_SPECIAL_MODS):
        self.target = target
        self.start = start
        self.last_ts = start
        self.special_mods = special_mods
        self.hits: List[Hit] = []
        self.stats: Dict[str, AttackerStats] = {}

    def _get_stats(self, attacker: str) -> AttackerStats:
        s = self.stats.get(attacker)
        if s is None:
            s = AttackerStats(attacker=attacker)
            self.stats[attacker] = s
        return s

    def record_hit(self, ev, kind: str):
        specials = extract_specials(ev.modifiers, self.special_mods)
        # SpellDamage / DS / DoT events carry `spell`; MeleeHit carries
        # `verb`. Use whichever is set on the event so the per-hit record
        # has both pieces available downstream (the UI builds a 'source'
        # label from them).
        spell = getattr(ev, 'spell', None)
        verb = getattr(ev, 'verb', None)
        self.hits.append(Hit(
            timestamp=ev.timestamp,
            attacker=ev.attacker,
            target=ev.target,
            damage=ev.damage,
            modifiers=list(ev.modifiers),
            specials=specials,
            kind=kind,
            spell=spell,
            verb=verb,
        ))
        s = self._get_stats(ev.attacker)
        s.damage += ev.damage
        s.hits += 1
        if ev.damage > s.biggest:
            s.biggest = ev.damage
        if is_crit(ev.modifiers):
            s.crits += 1
        for special in specials:
            s.special_damage[special] = s.special_damage.get(special, 0) + ev.damage
            s.special_hits[special] = s.special_hits.get(special, 0) + 1
        self.last_ts = ev.timestamp

    def record_miss(self, ev):
        # A miss against the target is still combat activity — extend the
        # fight window so a target you can't seem to land on doesn't expire.
        self._get_stats(ev.attacker).misses += 1
        self.last_ts = ev.timestamp

    def finalize(self, end: datetime, fight_complete: bool,
                 fight_id: Optional[int] = None) -> FightResult:
        return FightResult(
            target=self.target,
            start=self.start,
            end=end,
            hits=self.hits,
            stats_by_attacker=self.stats,
            fight_complete=fight_complete,
            fight_id=fight_id,
        )


# ----- Main entry points -----

def analyze_fight(logfile: str,
                  target_name: str,
                  special_mods=DEFAULT_SPECIAL_MODS) -> FightResult:
    """Walk a log file, isolate the named fight, and return per-attacker stats.

    The fight begins on the first damage event landing on the target,
    and ends on the target's death event (or when the log ends, with
    fight_complete=False).

    Args:
      logfile: path to an EQ log file.
      target_name: name of the boss/mob to analyze. Case-insensitive.
      special_mods: tuple of special-attack modifier names to break out.
                    Override to track game-specific mechanics.
    """
    target_lower = target_name.lower()
    builder: Optional[_FightBuilder] = None
    fight_complete = False
    end: Optional[datetime] = None

    for line in tail_file(logfile, read_all=True, follow=False):
        ev = parse_line(line)
        if ev is None:
            continue

        if isinstance(ev, MeleeHit) and ev.target.lower() == target_lower:
            if builder is None:
                builder = _FightBuilder(target_name, ev.timestamp, special_mods)
            builder.record_hit(ev, 'melee')
        elif isinstance(ev, SpellDamage) and ev.target.lower() == target_lower:
            if builder is None:
                builder = _FightBuilder(target_name, ev.timestamp, special_mods)
            builder.record_hit(ev, 'spell')
        elif isinstance(ev, MeleeMiss) and ev.target.lower() == target_lower:
            if builder is None:
                builder = _FightBuilder(target_name, ev.timestamp, special_mods)
            builder.record_miss(ev)
        elif isinstance(ev, DeathMessage) and ev.victim.lower() == target_lower:
            end = ev.timestamp
            fight_complete = True
            break

    if builder is None:
        # No events found for this target.
        return FightResult(target=target_name, start=None, end=None,
                           hits=[], stats_by_attacker={},
                           fight_complete=False)

    if end is None:
        end = builder.last_ts

    return builder.finalize(end, fight_complete)


def detect_fights(logfile: str,
                  gap_seconds: int = 15,
                  min_damage: int = 10_000,
                  min_duration_seconds: int = 0,
                  special_mods=DEFAULT_SPECIAL_MODS) -> List[FightResult]:
    """Backward-compat wrapper around `detect_combat` that returns just
    the fights, discarding heals. New code should call `detect_combat`."""
    fights, _ = detect_combat(logfile,
                              gap_seconds=gap_seconds,
                              min_damage=min_damage,
                              min_duration_seconds=min_duration_seconds,
                              special_mods=special_mods)
    return fights


def detect_combat(logfile: str,
                  gap_seconds: int = 15,
                  min_damage: int = 10_000,
                  min_duration_seconds: int = 0,
                  heals_extend_fights: bool = False,
                  special_mods=DEFAULT_SPECIAL_MODS
                  ) -> Tuple[List[FightResult], List[Heal]]:
    """Walk the log once and return both detected fights and a flat heal
    list. Heals don't open or extend fights at this layer — they're left
    flat so `group_into_encounters` can assign each heal to the encounter
    whose time window contains it.

    A 'fight' is one mob taking damage in a contiguous combat window. We
    open a new fight on the first damage event landing on a target, and
    close it on the target's death event or after `gap_seconds` of no
    activity against that target — whichever comes first. Each target
    has its own fight, so a boss + adds engaged together produce multiple
    overlapping fights (the UI is responsible for grouping them into
    encounters).

    Args:
      logfile: path to an EQ log file.
      gap_seconds: how long with no damage/miss against a target before we
                   call its fight over. Default 15s — short enough that
                   distinct engagements split cleanly. The UI is the place
                   to recombine fights that were really one phase-pausing
                   boss kill.
      min_damage: skip "fights" where total damage is below this threshold.
                  Filters out incidental hits on passing trash.
      min_duration_seconds: skip "fights" shorter than this in seconds.
                  Defaults to 0 (no filter). Useful for hiding one-shot
                  rampage swings on a single trash mob that you didn't
                  actually engage.
      special_mods: tuple of special-attack modifier names to break out.

    Returns:
      List of FightResult sorted by start time, with 1-indexed `fight_id`
      populated. Excludes fights whose target is `You` or a backtick-pet
      and fights below min_damage.
    """
    in_progress: Dict[str, _FightBuilder] = {}
    completed: List[FightResult] = []
    heals: List[Heal] = []

    def _close(target: str, end_ts: datetime, complete: bool):
        b = in_progress.pop(target)
        completed.append(b.finalize(end_ts, complete))

    def _expire_if_stale(target: str, now: datetime):
        b = in_progress.get(target)
        if b is None:
            return
        if (now - b.last_ts).total_seconds() > gap_seconds:
            _close(target, b.last_ts, complete=False)

    def _record_damage(ev, kind: str):
        target = ev.target
        _expire_if_stale(target, ev.timestamp)
        if target not in in_progress:
            in_progress[target] = _FightBuilder(target, ev.timestamp, special_mods)
        in_progress[target].record_hit(ev, kind)

    for line in tail_file(logfile, read_all=True, follow=False):
        ev = parse_line(line)
        if ev is None:
            continue

        if isinstance(ev, MeleeHit):
            _record_damage(ev, 'melee')
        elif isinstance(ev, SpellDamage):
            _record_damage(ev, 'spell')
        elif isinstance(ev, MeleeMiss):
            _expire_if_stale(ev.target, ev.timestamp)
            if ev.target in in_progress:
                in_progress[ev.target].record_miss(ev)
        elif isinstance(ev, DeathMessage):
            if ev.victim in in_progress:
                _close(ev.victim, ev.timestamp, complete=True)
        elif isinstance(ev, HealEvent):
            heals.append(Heal(
                timestamp=ev.timestamp,
                healer=ev.healer,
                target=ev.target,
                amount=ev.amount,
                spell=ev.spell,
                modifiers=list(ev.modifiers),
            ))
            if heals_extend_fights:
                # Treat heals as combat activity: bump every in-progress
                # fight's last_ts so a phase-pause full of heals doesn't
                # let the fight expire. Run staleness expiration first
                # (using the heal timestamp) so genuinely-dead fights
                # don't get revived by a delayed heal tick.
                for tgt in list(in_progress.keys()):
                    _expire_if_stale(tgt, ev.timestamp)
                for builder in in_progress.values():
                    if builder.last_ts < ev.timestamp:
                        builder.last_ts = ev.timestamp

    # Close any still-open fights at end of log.
    for target in list(in_progress.keys()):
        b = in_progress[target]
        _close(target, b.last_ts, complete=False)

    # Drop the logging player and pets (obvious self-damage noise) and
    # anything below the damage threshold. Other-player deaths (target is
    # another player's proper name) can still slip through; the UI is the
    # right place to suppress those once it has a player roster.
    filtered = [
        f for f in completed
        if f.total_damage >= min_damage
        and f.duration_seconds >= min_duration_seconds
        and f.target != 'You'
        and not f.target.endswith('`s pet')
    ]
    filtered.sort(key=lambda f: f.start)
    for i, f in enumerate(filtered, start=1):
        f.fight_id = i
    return filtered, heals


# ----- Parser-coverage debug -----

import re as _re
_DIGITS_RE = _re.compile(r'\d+')


def collect_parser_stats(logfile: str, limit: int = 200) -> dict:
    """Walk the log and report on parser coverage.

    Returns total line counts by event type plus a sorted list of
    UnknownEvent body "shapes" — each line normalized by replacing runs
    of digits with `N`, so a thousand DoT ticks differing only in damage
    numbers collapse into one row. The first verbatim instance is kept
    as the example. Designed for the in-UI debug page; the result is
    JSON-friendly. The log is walked synchronously, so cache the result.
    """
    counts_by_type: Dict[str, int] = {}
    unknown_counts: Dict[str, int] = {}
    unknown_examples: Dict[str, str] = {}
    total = 0
    no_timestamp = 0

    for line in tail_file(logfile, read_all=True, follow=False):
        total += 1
        ev = parse_line(line)
        if ev is None:
            no_timestamp += 1
            continue
        type_name = type(ev).__name__
        counts_by_type[type_name] = counts_by_type.get(type_name, 0) + 1
        if isinstance(ev, UnknownEvent):
            shape = _DIGITS_RE.sub('N', ev.body)
            unknown_counts[shape] = unknown_counts.get(shape, 0) + 1
            if shape not in unknown_examples:
                unknown_examples[shape] = ev.body

    unknowns = [
        {'shape': s, 'count': unknown_counts[s], 'example': unknown_examples[s]}
        for s in unknown_counts
    ]
    unknowns.sort(key=lambda x: x['count'], reverse=True)

    return {
        'total_lines': total,
        'no_timestamp': no_timestamp,
        'by_type': counts_by_type,
        'unknown_groups': unknowns[:limit],
        'unknown_total_groups': len(unknowns),
        'unknown_total_lines': sum(unknown_counts.values()),
    }


# ----- Encounter grouping -----
#
# `detect_fights` splits per target, so a boss + adds engaged together
# produce multiple overlapping fights (and a same-name swarm produces
# fat-then-thin slices, see CONTEXT.md). Encounters glue those back
# together for display: any fights whose time windows overlap (or are
# within `gap_seconds` of each other) become one encounter row in the UI.
#
# We deliberately keep the per-target FightResults around as `members`
# so the detail view can still see the breakdown if it wants.

@dataclass
class Encounter:
    """A group of overlapping fights treated as one logical engagement."""
    encounter_id: int
    members: List[FightResult]
    name: str               # display label — most recent killed target preferred
    fight_complete: bool    # any member killed?
    heals: List['Heal'] = field(default_factory=list)

    @property
    def total_healing(self) -> int:
        return sum(h.amount for h in self.heals)

    @property
    def start(self) -> Optional[datetime]:
        starts = [m.start for m in self.members if m.start is not None]
        return min(starts) if starts else None

    @property
    def end(self) -> Optional[datetime]:
        ends = [m.end for m in self.members if m.end is not None]
        return max(ends) if ends else None

    @property
    def duration_seconds(self) -> float:
        if self.start is None or self.end is None:
            return 0.0
        return (self.end - self.start).total_seconds()

    @property
    def total_damage(self) -> int:
        return sum(m.total_damage for m in self.members)

    @property
    def raid_dps(self) -> float:
        d = self.duration_seconds
        return self.total_damage / d if d > 0 else 0.0

    @property
    def attacker_count(self) -> int:
        attackers: set = set()
        for m in self.members:
            attackers.update(m.stats_by_attacker.keys())
        return len(attackers)

    @property
    def target_count(self) -> int:
        # Count distinct mob names case-insensitively. EQ writes the same
        # mob with leading capital differently in different log contexts
        # ("A subterranean digger" vs "a subterranean digger"); collapsing
        # them here gives a more useful "+N" badge in the session table.
        return len({m.target.lower() for m in self.members})


def _encounter_display_name(members: List[FightResult]) -> str:
    """Pick a label for the encounter: the enemy that took the most damage.

    "Enemy" matches the per-row classification used in the encounter detail
    view: pets and "You" are excluded; everything else qualifies if it took
    more damage than it dealt across this encounter. Falls back to the
    highest-damage member if no member classifies as an enemy (e.g. an
    encounter that's nothing but a player death from environmental damage)."""
    dealt: Dict[str, int] = {}
    for m in members:
        for s in m.stats_by_attacker.values():
            key = s.attacker.lower()
            dealt[key] = dealt.get(key, 0) + s.damage

    def is_enemy(m: FightResult) -> bool:
        t = m.target
        if t.lower() == 'you' or t.endswith('`s pet'):
            return False
        return m.total_damage > dealt.get(t.lower(), 0)

    pool = [m for m in members if is_enemy(m)] or members
    return max(pool, key=lambda m: m.total_damage).target


def group_into_encounters(fights: List[FightResult],
                          gap_seconds: int = 0,
                          heals: Optional[List[Heal]] = None) -> List['Encounter']:
    """Bundle overlapping fights into encounters.

    Two fights are part of the same encounter if their windows overlap (or
    are within `gap_seconds`, default 0 — strict overlap only). Strict
    overlap is the conservative default: it groups simultaneous engagements
    (boss + adds, same-name mob slices) but leaves anything separated by
    even a second of dead air as its own encounter. Bump `gap_seconds` if
    you want phase-transition pauses to merge back together too.

    Returned encounters are 1-indexed by start time. Encounter ids are as
    stable as the underlying fight ids (ie. stable for any prefix of the
    same log).
    """
    if not fights:
        return []

    sorted_fights = sorted(
        [f for f in fights if f.start is not None],
        key=lambda f: f.start,
    )
    if not sorted_fights:
        return []

    groups: List[List[FightResult]] = [[sorted_fights[0]]]
    cur_end = sorted_fights[0].end or sorted_fights[0].start

    for f in sorted_fights[1:]:
        gap = (f.start - cur_end).total_seconds()
        if gap <= gap_seconds:
            groups[-1].append(f)
            f_end = f.end or f.start
            if f_end and (cur_end is None or f_end > cur_end):
                cur_end = f_end
        else:
            groups.append([f])
            cur_end = f.end or f.start

    encounters: List[Encounter] = []
    for i, members in enumerate(groups, start=1):
        encounters.append(Encounter(
            encounter_id=i,
            members=members,
            name=_encounter_display_name(members),
            fight_complete=any(m.fight_complete for m in members),
        ))

    # Assign heals to encounters by timestamp. Each heal lands in the first
    # encounter whose [start, end] window contains its timestamp; heals
    # outside any encounter window (downtime healing, e.g. between pulls)
    # are dropped on the floor for now.
    if heals:
        for h in heals:
            for enc in encounters:
                if enc.start is None or enc.end is None:
                    continue
                if enc.start <= h.timestamp <= enc.end:
                    enc.heals.append(h)
                    break
    return encounters


def merge_encounter(encounter: 'Encounter') -> FightResult:
    """Flatten an encounter's member fights into a single FightResult.

    All hits concatenated, per-attacker stats summed, biggest is the max
    across members, special breakdowns merged. The result plugs straight
    into `bucket_hits()` and the existing fight-detail JSON shape, so the
    encounter detail view reuses every renderer the per-fight view had.
    """
    merged: Dict[str, AttackerStats] = {}
    for m in encounter.members:
        for atk, s in m.stats_by_attacker.items():
            cur = merged.get(atk)
            if cur is None:
                cur = AttackerStats(attacker=atk)
                merged[atk] = cur
            cur.damage += s.damage
            cur.hits += s.hits
            cur.misses += s.misses
            cur.crits += s.crits
            if s.biggest > cur.biggest:
                cur.biggest = s.biggest
            for special, dmg in s.special_damage.items():
                cur.special_damage[special] = cur.special_damage.get(special, 0) + dmg
            for special, n in s.special_hits.items():
                cur.special_hits[special] = cur.special_hits.get(special, 0) + n

    all_hits: List[Hit] = []
    for m in encounter.members:
        all_hits.extend(m.hits)
    all_hits.sort(key=lambda h: h.timestamp)

    return FightResult(
        target=encounter.name,
        start=encounter.start,
        end=encounter.end,
        hits=all_hits,
        stats_by_attacker=merged,
        fight_complete=encounter.fight_complete,
        fight_id=encounter.encounter_id,
    )


# ----- Timeline bucketing -----

@dataclass
class Timeline:
    """Damage bucketed into time windows."""
    bucket_seconds: int
    bucket_starts: List[datetime]
    # per_attacker[attacker_name] -> [damage_in_bucket_0, damage_in_bucket_1, ...]
    per_attacker: Dict[str, List[int]]

    @property
    def n_buckets(self) -> int:
        return len(self.bucket_starts)

    def raid_total_per_bucket(self) -> List[int]:
        """Sum across attackers for each bucket."""
        if not self.per_attacker:
            return [0] * self.n_buckets
        return [
            sum(series[i] for series in self.per_attacker.values())
            for i in range(self.n_buckets)
        ]


def bucket_hits(result: FightResult, bucket_seconds: int = 5) -> Timeline:
    """Bucket the hits in a FightResult into time windows."""
    if result.start is None or result.end is None:
        return Timeline(bucket_seconds=bucket_seconds,
                        bucket_starts=[],
                        per_attacker={})

    duration = result.duration_seconds
    n_buckets = max(1, int(duration / bucket_seconds) + 1)
    bucket_starts = [
        result.start + timedelta(seconds=i * bucket_seconds)
        for i in range(n_buckets)
    ]

    attackers = sorted(result.stats_by_attacker.keys())
    per_attacker = {a: [0] * n_buckets for a in attackers}

    for h in result.hits:
        offset = (h.timestamp - result.start).total_seconds()
        idx = min(int(offset / bucket_seconds), n_buckets - 1)
        per_attacker[h.attacker][idx] += h.damage

    return Timeline(bucket_seconds=bucket_seconds,
                    bucket_starts=bucket_starts,
                    per_attacker=per_attacker)
