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

import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from .events import (
    MeleeHit, MeleeMiss, SpellDamage, DeathMessage, HealEvent, UnknownEvent,
)
from .parser import parse_line
from .tail import tail_file, find_offset_for_timestamp


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
    # When `apply_pet_owners` rewrites a pet's attacker name to its
    # owner, this carries the original raw actor name so the UI can
    # surface the pet as a damage source within the owner's row. None
    # for hits that weren't rewritten.
    pet_origin: Optional[str] = None


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
    # Same role as Hit.pet_origin — set when a pet's heal is rewritten to
    # its owner's name so the UI can credit the pet as the source.
    pet_origin: Optional[str] = None


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


# Outcome labels used in DefenseStats.avoided. Match MeleeMiss.outcome
# values so we can populate from a parsed event without translation.
DEFENSE_OUTCOMES = ('miss', 'riposte', 'parry', 'block', 'dodge', 'rune', 'invulnerable')


@dataclass
class DefenseStats:
    """Per-(attacker, defender) accumulator from the defender's perspective.

    Counts every swing one attacker took at one defender during a fight,
    breaking down the misses by `outcome` so the tank UI can show how the
    avoidance shook out (parry vs block vs dodge vs rune vs invulnerable
    vs plain miss vs riposte). `damage_taken` and `biggest_taken` record
    landed damage; `avoided` is keyed by the same outcome label MeleeMiss
    carries.

    Built alongside AttackerStats inside `_FightBuilder` — every hit and
    every miss bumps both the attacker side (AttackerStats) and the
    defender side (this), so no separate event walk is needed.
    """
    attacker: str
    defender: str
    damage_taken: int = 0
    hits_landed: int = 0
    biggest_taken: int = 0
    avoided: Dict[str, int] = field(default_factory=dict)

    @property
    def total_avoided(self) -> int:
        return sum(self.avoided.values())

    @property
    def total_swings(self) -> int:
        return self.hits_landed + self.total_avoided


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
    # Per-(attacker, defender) accumulator from the defender's perspective.
    # Populated in lockstep with stats_by_attacker so the tank-view UI can
    # show how each attacker's swings landed or were avoided. Keyed by the
    # raw (attacker, defender) tuple — pet-owner rewrites remap the
    # attacker side via apply_pet_owners.
    defends_by_pair: Dict[Tuple[str, str], DefenseStats] = field(default_factory=dict)

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
        self.defends: Dict[Tuple[str, str], DefenseStats] = {}

    def _get_stats(self, attacker: str) -> AttackerStats:
        s = self.stats.get(attacker)
        if s is None:
            s = AttackerStats(attacker=attacker)
            self.stats[attacker] = s
        return s

    def _get_defense(self, attacker: str, defender: str) -> DefenseStats:
        key = (attacker, defender)
        d = self.defends.get(key)
        if d is None:
            d = DefenseStats(attacker=attacker, defender=defender)
            self.defends[key] = d
        return d

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
        d = self._get_defense(ev.attacker, ev.target)
        d.damage_taken += ev.damage
        d.hits_landed += 1
        if ev.damage > d.biggest_taken:
            d.biggest_taken = ev.damage
        self.last_ts = ev.timestamp

    def record_miss(self, ev):
        # A miss against the target is still combat activity — extend the
        # fight window so a target you can't seem to land on doesn't expire.
        self._get_stats(ev.attacker).misses += 1
        outcome = getattr(ev, 'outcome', 'miss')
        d = self._get_defense(ev.attacker, ev.target)
        d.avoided[outcome] = d.avoided.get(outcome, 0) + 1
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
            defends_by_pair=self.defends,
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
                  since: Optional[datetime] = None,
                  progress_cb: Optional[Callable[[int, int], None]] = None,
                  special_mods=DEFAULT_SPECIAL_MODS) -> List[FightResult]:
    """Backward-compat wrapper around `detect_combat` that returns just
    the fights, discarding heals. New code should call `detect_combat`."""
    fights, _ = detect_combat(logfile,
                              gap_seconds=gap_seconds,
                              min_damage=min_damage,
                              min_duration_seconds=min_duration_seconds,
                              since=since,
                              progress_cb=progress_cb,
                              special_mods=special_mods)
    return fights


class _CombatDetector:
    """Stateful, incremental version of the detect_combat inner loop.

    Holds in-progress fights across multiple `feed_event()` calls so a
    follower thread can append parsed events as the log grows. The
    static `detect_combat` is now a thin wrapper that pumps file-walked
    events through this detector and finalizes — same external behavior.

    Live mode flow:
      d = _CombatDetector(gap_seconds=15, ...)
      for ev in initial_walk: d.feed_event(ev)
      d.expire_stale()                        # close any orphans
      # ... follower thread runs:
      while live:
        for ev in newly_appended: d.feed_event(ev)
        d.expire_stale()                      # housekeeping
      # ... endpoints serve:
      fights, heals = d.snapshot(include_in_progress=True)

    Snapshots can be taken at any time. `include_in_progress=True` rolls
    still-open fights into the returned list with `fight_complete=False`
    so the live UI can render them as the active fight.
    """

    def __init__(self,
                 gap_seconds: int = 15,
                 min_damage: int = 10_000,
                 min_duration_seconds: int = 0,
                 heals_extend_fights: bool = False,
                 special_mods=DEFAULT_SPECIAL_MODS):
        self.gap_seconds = gap_seconds
        self.min_damage = min_damage
        self.min_duration_seconds = min_duration_seconds
        self.heals_extend_fights = heals_extend_fights
        self.special_mods = special_mods
        self.in_progress: Dict[str, _FightBuilder] = {}
        self.completed: List[FightResult] = []
        self.heals: List[Heal] = []
        # Latest event timestamp seen. Used by snapshot() to know how
        # current the data is, and by callers running periodic
        # expire_stale() between events.
        self.last_event_ts: Optional[datetime] = None

    def _close(self, target: str, end_ts: datetime, complete: bool):
        b = self.in_progress.pop(target)
        self.completed.append(b.finalize(end_ts, complete))

    def _expire_if_stale(self, target: str, now: datetime):
        b = self.in_progress.get(target)
        if b is None:
            return
        if (now - b.last_ts).total_seconds() > self.gap_seconds:
            self._close(target, b.last_ts, complete=False)

    def _record_damage(self, ev, kind: str):
        target = ev.target
        self._expire_if_stale(target, ev.timestamp)
        if target not in self.in_progress:
            self.in_progress[target] = _FightBuilder(
                target, ev.timestamp, self.special_mods)
        self.in_progress[target].record_hit(ev, kind)

    def feed_event(self, ev) -> None:
        """Process one parsed event. Mirrors the dispatch in
        detect_combat's inner loop."""
        self.last_event_ts = ev.timestamp
        if isinstance(ev, MeleeHit):
            self._record_damage(ev, 'melee')
        elif isinstance(ev, SpellDamage):
            self._record_damage(ev, 'spell')
        elif isinstance(ev, MeleeMiss):
            self._expire_if_stale(ev.target, ev.timestamp)
            if ev.target not in self.in_progress:
                # Open the fight on a miss too — without this, a target
                # being completely avoided (rune / parry / miss every
                # swing, no damage either way) silently fails to
                # register as in-progress, and the live overlay never
                # shows it as an active fight. Static parses are
                # unaffected: a miss-only fight has total_damage=0 and
                # is dropped by the min_damage filter in snapshot().
                self.in_progress[ev.target] = _FightBuilder(
                    ev.target, ev.timestamp, self.special_mods)
            self.in_progress[ev.target].record_miss(ev)
        elif isinstance(ev, DeathMessage):
            if ev.victim in self.in_progress:
                self._close(ev.victim, ev.timestamp, complete=True)
            else:
                # Late death: expire_stale may have already closed the
                # fight before the death event landed. Common with tight
                # gap_seconds, DoT kills, or live-mode log buffering.
                # Walk back through recently-completed fights and flip
                # the most recent matching slice to complete; bail out
                # of the walk as soon as a fight is too old to be the
                # one we're looking for.
                grace = self.gap_seconds + 10
                for f in reversed(self.completed):
                    if f.end is None:
                        continue
                    if (ev.timestamp - f.end).total_seconds() > grace:
                        break
                    if f.target == ev.victim and not f.fight_complete:
                        f.fight_complete = True
                        break
        elif isinstance(ev, HealEvent):
            self.heals.append(Heal(
                timestamp=ev.timestamp,
                healer=ev.healer,
                target=ev.target,
                amount=ev.amount,
                spell=ev.spell,
                modifiers=list(ev.modifiers),
            ))
            if self.heals_extend_fights:
                # Treat heals as combat activity: bump every in-progress
                # fight's last_ts so a phase-pause full of heals doesn't
                # let the fight expire. Run staleness expiration first
                # (using the heal timestamp) so genuinely-dead fights
                # don't get revived by a delayed heal tick.
                for tgt in list(self.in_progress.keys()):
                    self._expire_if_stale(tgt, ev.timestamp)
                for builder in self.in_progress.values():
                    if builder.last_ts < ev.timestamp:
                        builder.last_ts = ev.timestamp

    def expire_stale(self, now: Optional[datetime] = None) -> None:
        """Close any in-progress fights whose last_ts is older than
        gap_seconds relative to `now`. Called between events to give the
        UI a way to know "this fight ended even though no death message
        landed" — typical for trash mobs you walked away from. Defaults
        to using last_event_ts if no `now` is supplied."""
        anchor = now or self.last_event_ts
        if anchor is None:
            return
        for target in list(self.in_progress.keys()):
            self._expire_if_stale(target, anchor)

    def finalize_all(self) -> None:
        """Force-close every in-progress fight at its own last_ts. Used
        by detect_combat at end-of-walk to flush fights still open when
        the log ran out — equivalent to the old end-of-loop close pass."""
        for target in list(self.in_progress.keys()):
            b = self.in_progress[target]
            self._close(target, b.last_ts, complete=False)

    def snapshot(self, include_in_progress: bool = False
                 ) -> Tuple[List[FightResult], List[Heal]]:
        """Return the current detector state as (fights, heals).

        `include_in_progress=True` adds still-open fights to the output
        with fight_complete=False — used by live-mode callers who want
        to see the active fight's stats. End-of-walk callers pass False
        and rely on finalize_all() to have closed everything first.

        Filtering and 1-indexed fight_id assignment match detect_combat
        exactly so the static and live paths produce comparable data.
        """
        fights = list(self.completed)
        if include_in_progress:
            for b in self.in_progress.values():
                fights.append(b.finalize(b.last_ts, fight_complete=False))
        filtered = [
            f for f in fights
            if f.total_damage >= self.min_damage
            and f.duration_seconds >= self.min_duration_seconds
            and f.target != 'You'
            and not f.target.endswith('`s pet')
        ]
        filtered.sort(key=lambda f: f.start)
        for i, f in enumerate(filtered, start=1):
            f.fight_id = i
        return filtered, list(self.heals)


def detect_combat(logfile: str,
                  gap_seconds: int = 15,
                  min_damage: int = 10_000,
                  min_duration_seconds: int = 0,
                  heals_extend_fights: bool = False,
                  since: Optional[datetime] = None,
                  progress_cb: Optional[Callable[[int, int], None]] = None,
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
      since: optional cutoff datetime — events with `timestamp < since`
                  are skipped. Combined with `find_offset_for_timestamp`
                  this lets the caller analyze only the tail of a long
                  log without paying the cost of parsing the prefix.
      progress_cb: optional callable `(bytes_read, total_bytes)` invoked
                  periodically (every ~256KB of file read) so the caller
                  can update a UI progress bar. Errors swallowed inside
                  `tail_file`. `total_bytes` reflects the raw file size
                  not the slice size — matters for UI math when `since`
                  is set and we start mid-file.
      special_mods: tuple of special-attack modifier names to break out.

    Returns:
      List of FightResult sorted by start time, with 1-indexed `fight_id`
      populated. Excludes fights whose target is `You` or a backtick-pet
      and fights below min_damage.
    """
    detector = _CombatDetector(
        gap_seconds=gap_seconds,
        min_damage=min_damage,
        min_duration_seconds=min_duration_seconds,
        heals_extend_fights=heals_extend_fights,
        special_mods=special_mods,
    )
    end_offset = walk_into_detector(
        logfile, detector,
        since=since, progress_cb=progress_cb,
    )
    # Close any still-open fights at end of log so this static-walk
    # caller sees a fully-finalized result.
    detector.finalize_all()
    fights, heals = detector.snapshot(include_in_progress=False)
    # `end_offset` isn't part of the public detect_combat contract; we
    # only return it to live-mode callers via walk_into_detector.
    del end_offset
    return fights, heals


def walk_into_detector(logfile: str,
                       detector: '_CombatDetector',
                       since: Optional[datetime] = None,
                       start_offset: Optional[int] = None,
                       progress_cb: Optional[Callable[[int, int], None]] = None,
                       ) -> int:
    """Walk the log file and feed each parsed event into `detector`.

    Returns the byte offset reached at end of walk so live-mode callers
    can pick up the follower from there (no double-parsing). The detector
    is NOT finalized here — callers control whether to expire stale
    fights, finalize all, or leave in-progress fights open for the
    follower to extend.

    Args:
      logfile: path to an EQ log file.
      detector: a `_CombatDetector` to feed events into. Caller-owned.
      since: optional cutoff datetime; events before are skipped. Resolved
            to a byte offset via `find_offset_for_timestamp` so the
            file-prefix walk is skipped on long logs.
      start_offset: optional explicit byte offset to start at, bypassing
            the `since` resolution. Used by the live follower to resume
            from the position the initial walk left off.
      progress_cb: optional `(bytes_read, total_bytes)` callback, both
            relative to the slice walked.

    Caller pattern (live mode):
        d = _CombatDetector(...)
        end = walk_into_detector(path, d, since=cutoff)
        # ... background thread continues from `end`:
        end = walk_into_detector(path, d, start_offset=end)
    """
    # Resolve `since` to a starting byte offset unless one was given.
    # Skipping the prefix of a huge log is the bulk of the speedup; the
    # inline `since` filter below is a backstop for any old lines that
    # sneak through (e.g. an off-by-one near the cutoff).
    if start_offset is None:
        start_offset = 0
        if since is not None:
            try:
                start_offset = find_offset_for_timestamp(logfile, since)
            except OSError:
                start_offset = 0

    file_size = os.path.getsize(logfile) if os.path.isfile(logfile) else 0
    # Report progress relative to the slice we actually walk so a slice
    # starting two-thirds into the file shows 0% → 100% across the work
    # we're doing, not the work we skipped.
    slice_size = max(0, file_size - start_offset)
    inner_progress = None
    last_pos = [start_offset]
    if progress_cb is not None:
        def inner_progress(abs_pos: int):
            last_pos[0] = abs_pos
            try:
                progress_cb(max(0, abs_pos - start_offset), slice_size)
            except Exception:
                pass
    else:
        def inner_progress(abs_pos: int):
            last_pos[0] = abs_pos

    for line in tail_file(logfile, read_all=True, follow=False,
                          start_offset=start_offset,
                          progress_cb=inner_progress):
        ev = parse_line(line)
        if ev is None:
            continue
        if since is not None and ev.timestamp < since:
            continue
        detector.feed_event(ev)
    # Return the most recent byte position reported by tail_file's
    # progress callback, or fall back to file size if no callback fired
    # (very small files, no chunk boundaries crossed).
    return max(last_pos[0], file_size)


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


def _fight_key(f: FightResult) -> Optional[str]:
    """Stable composite key for a fight: lowercased target + ISO start.
    Mirrors `flurry.sidecar.fight_key` so manual-encounter overrides round-
    trip cleanly. Two fights with the same target+start are treated as the
    same fight; only happens if the log itself is duplicated, which we
    accept."""
    if f.start is None:
        return None
    return f'{f.target.lower()}|{f.start.isoformat()}'


def group_into_encounters(fights: List[FightResult],
                          gap_seconds: int = 0,
                          heals: Optional[List[Heal]] = None,
                          manual_groups: Optional[List[dict]] = None
                          ) -> List['Encounter']:
    """Bundle overlapping fights into encounters.

    Two fights are part of the same encounter if their windows overlap (or
    are within `gap_seconds`, default 0 — strict overlap only). Strict
    overlap is the conservative default: it groups simultaneous engagements
    (boss + adds, same-name mob slices) but leaves anything separated by
    even a second of dead air as its own encounter. Bump `gap_seconds` if
    you want phase-transition pauses to merge back together too.

    `manual_groups` is the user override channel (sidecar persistence).
    Each entry is `{'fight_keys': [<target|iso_ts>, ...], 'name': <opt>}`;
    fights matching a manual group bypass auto-grouping and form their own
    encounter regardless of timing. `name` overrides the auto-derived
    display name. Empty / single-key / unknown-key groups are ignored —
    nothing the user does to a stale sidecar can shadow real fights.

    Returned encounters are 1-indexed by start time. Encounter ids are
    stable for the same log + same params + same sidecar; they shift when
    any of those change.
    """
    if not fights:
        return []

    sorted_fights = sorted(
        [f for f in fights if f.start is not None],
        key=lambda f: f.start,
    )
    if not sorted_fights:
        return []

    # Resolve manual groups to actual FightResult sets. We iterate by
    # fight (not by sidecar key) so a sidecar referencing a fight that
    # disappeared under new params is silently ignored — the missing keys
    # just don't match anything. `manual_idx` maps a fight's id() to the
    # index of the manual group it belongs to, if any.
    manual_groups = manual_groups or []
    manual_idx: Dict[int, int] = {}
    manual_names: Dict[int, Optional[str]] = {}
    for gi, group in enumerate(manual_groups):
        keys = set(group.get('fight_keys') or [])
        if len(keys) < 2:
            continue
        manual_names[gi] = group.get('name') or None
        for f in sorted_fights:
            k = _fight_key(f)
            if k in keys and id(f) not in manual_idx:
                manual_idx[id(f)] = gi

    # Auto-group only the fights that aren't manually claimed.
    auto_fights = [f for f in sorted_fights if id(f) not in manual_idx]
    auto_buckets: List[List[FightResult]] = []
    if auto_fights:
        auto_buckets.append([auto_fights[0]])
        cur_end = auto_fights[0].end or auto_fights[0].start
        for f in auto_fights[1:]:
            gap = (f.start - cur_end).total_seconds()
            if gap <= gap_seconds:
                auto_buckets[-1].append(f)
                f_end = f.end or f.start
                if f_end and (cur_end is None or f_end > cur_end):
                    cur_end = f_end
            else:
                auto_buckets.append([f])
                cur_end = f.end or f.start

    # Bucket manually-claimed fights back together. We use a dict so a
    # group with fights from non-adjacent positions in `sorted_fights`
    # still ends up as one bucket.
    manual_buckets: Dict[int, List[FightResult]] = {}
    for f in sorted_fights:
        gi = manual_idx.get(id(f))
        if gi is not None:
            manual_buckets.setdefault(gi, []).append(f)

    # Combine auto and manual groups, then sort by group start time so
    # encounter ids reflect chronological order regardless of source.
    all_groups: List[Tuple[List[FightResult], Optional[str]]] = []
    for members in auto_buckets:
        all_groups.append((members, None))
    for gi, members in manual_buckets.items():
        all_groups.append((members, manual_names.get(gi)))
    all_groups.sort(key=lambda gn: min(f.start for f in gn[0]))

    encounters: List[Encounter] = []
    for i, (members, name_override) in enumerate(all_groups, start=1):
        encounters.append(Encounter(
            encounter_id=i,
            members=members,
            name=name_override or _encounter_display_name(members),
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

    # Merge defender-perspective stats across member fights. Two members
    # of the same encounter sometimes overlap when a boss + add are pulled
    # together; the same (attacker, defender) pair can appear in both, so
    # we sum into one DefenseStats per pair.
    merged_defends: Dict[Tuple[str, str], DefenseStats] = {}
    for m in encounter.members:
        for key, d in m.defends_by_pair.items():
            cur = merged_defends.get(key)
            if cur is None:
                cur = DefenseStats(attacker=d.attacker, defender=d.defender)
                merged_defends[key] = cur
            cur.damage_taken += d.damage_taken
            cur.hits_landed += d.hits_landed
            if d.biggest_taken > cur.biggest_taken:
                cur.biggest_taken = d.biggest_taken
            for outcome, n in d.avoided.items():
                cur.avoided[outcome] = cur.avoided.get(outcome, 0) + n

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
        defends_by_pair=merged_defends,
    )


# ----- Pet ownership rewrite -----
#
# `apply_pet_owners` is a post-process pass that rewrites attacker/healer
# names on hits and heals so unnamed pets ("Onyx Crusher") or other
# entities the user has assigned an owner show up under `<owner>'s pet`
# in the per-attacker tables. We do this after detect_combat instead of
# inside the parser so:
#   - the parser stays a pure regex layer with no user-state coupling, and
#   - removing/changing a pet assignment is a re-render rather than a
#     re-parse — cheap to iterate on in the UI.
#
# Backtick-pet names from the log itself (`Soloson\`s pet`) are already
# handled by the parser's NAME pattern; this layer is for cases where EQ
# writes the pet under its own proper name with no owner cue (mage water
# pets, charmed mobs, etc.).

def apply_pet_owners(fights: List[FightResult],
                     heals: List[Heal],
                     pet_owners: Dict[str, str]
                     ) -> Tuple[List[FightResult], List[Heal]]:
    """Return rewritten fights/heals where attackers in `pet_owners` are
    merged into their owner's row. Original inputs are not mutated.

    The rewrite changes `attacker` (or `healer`) to the owner's name and
    sets `pet_origin` on each affected event to the original raw actor
    name. The owner's own hits are unchanged. Per-attacker stats are
    re-aggregated under the owner so two raw actors mapped to the same
    owner sum cleanly, and the pet's damage ends up summed with the
    owner's own.

    The UI uses `pet_origin` to surface the pet as a "Source" row inside
    the owner's pair-modal breakdown, so the user can still see what
    fraction of the owner's row came from each pet without losing the
    rolled-up DPS view.

    Lookups are case-insensitive on the actor name; the relabel preserves
    the owner casing the user typed. If `pet_owners` is empty the inputs
    are returned unchanged (cheap no-op for the common case)."""
    if not pet_owners:
        return fights, heals

    lookup = {k.lower(): v for k, v in pet_owners.items() if k and v}
    if not lookup:
        return fights, heals

    def _rewrite_fight(f: FightResult) -> FightResult:
        # Skip the rewrite entirely if no attacker in this fight is
        # affected — keeps the per-fight pass cheap when the sidecar
        # only renames a few names across a long log.
        relevant = any(atk.lower() in lookup for atk in f.stats_by_attacker)
        if not relevant:
            return f
        new_hits = []
        for h in f.hits:
            owner = lookup.get(h.attacker.lower())
            if owner is not None:
                new_hits.append(replace(h, attacker=owner,
                                        pet_origin=h.attacker))
            else:
                new_hits.append(h)
        # Re-aggregate per-attacker stats from the rewritten hits so a
        # pet's damage rolls up under its owner. The owner's own hits
        # also pass through this loop and accumulate into the same
        # AttackerStats by name match, so owner + pet end up summed.
        new_stats: Dict[str, AttackerStats] = {}
        for h in new_hits:
            s = new_stats.get(h.attacker)
            if s is None:
                s = AttackerStats(attacker=h.attacker)
                new_stats[h.attacker] = s
            s.damage += h.damage
            s.hits += 1
            if h.damage > s.biggest:
                s.biggest = h.damage
            if is_crit(h.modifiers):
                s.crits += 1
            for special in h.specials:
                s.special_damage[special] = s.special_damage.get(special, 0) + h.damage
                s.special_hits[special] = s.special_hits.get(special, 0) + 1
        # Layer in miss counts from the original stats. Misses don't
        # produce Hit objects so they wouldn't otherwise be rebuilt; the
        # original AttackerStats is the source of truth for them.
        for old_name, old in f.stats_by_attacker.items():
            if old.misses == 0:
                continue
            new_name = lookup.get(old_name.lower(), old_name)
            s = new_stats.get(new_name)
            if s is None:
                s = AttackerStats(attacker=new_name)
                new_stats[new_name] = s
            s.misses += old.misses
        # Rewrite defender-perspective stats: only the attacker side is
        # remapped (a pet's swings against a defender become the owner's
        # swings against that defender). Two raw actors mapping to the
        # same owner sum cleanly into one DefenseStats per pair.
        new_defends: Dict[Tuple[str, str], DefenseStats] = {}
        for (old_atk, defender), d in f.defends_by_pair.items():
            new_atk = lookup.get(old_atk.lower(), old_atk)
            key = (new_atk, defender)
            cur = new_defends.get(key)
            if cur is None:
                cur = DefenseStats(attacker=new_atk, defender=defender)
                new_defends[key] = cur
            cur.damage_taken += d.damage_taken
            cur.hits_landed += d.hits_landed
            if d.biggest_taken > cur.biggest_taken:
                cur.biggest_taken = d.biggest_taken
            for outcome, n in d.avoided.items():
                cur.avoided[outcome] = cur.avoided.get(outcome, 0) + n
        return FightResult(
            target=f.target,
            start=f.start,
            end=f.end,
            hits=new_hits,
            stats_by_attacker=new_stats,
            fight_complete=f.fight_complete,
            fight_id=f.fight_id,
            defends_by_pair=new_defends,
        )

    def _rewrite_heal(h: Heal) -> Heal:
        owner = lookup.get(h.healer.lower())
        if owner is not None:
            return replace(h, healer=owner, pet_origin=h.healer)
        return h

    return ([_rewrite_fight(f) for f in fights],
            [_rewrite_heal(h) for h in heals])


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
