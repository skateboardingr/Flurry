"""
server.py - tiny local web UI for browsing detected fights.

Stdlib only: ThreadingHTTPServer + BaseHTTPRequestHandler. The server
runs the Python analyzer in-process so there's no parser duplication
and no extra deps. The front-end is a single hash-routed page that
fetches JSON from /api/* endpoints.

Pass 1 scope: navigation only. Session table at /, click a fight to
drill into per-attacker stats and a stacked-area timeline. No encounter
grouping, no pet-ownership editing yet — those come in subsequent passes
and persist to a sidecar JSON next to the log.

Run via:
    flurry-ui eqlog_<char>_<server>.txt
"""

import http.server
import json
import os
import re
import socketserver
import statistics
import tempfile
import threading
import urllib.parse
import webbrowser
from datetime import datetime, timedelta
from typing import List, Optional

from .analyzer import (
    FightResult, Encounter, Heal,
    detect_combat, group_into_encounters, apply_pet_owners,
    merge_encounter, bucket_hits, collect_parser_stats,
    DEFAULT_SPECIAL_MODS,
)
from .sidecar import (
    Sidecar, fight_key, load_sidecar, save_sidecar,
)
from .tail import read_last_timestamp


# ----- JSON shape builders -----

def _fight_summary(f: FightResult) -> dict:
    """Compact per-fight row for the session list."""
    return {
        'fight_id': f.fight_id,
        'target': f.target,
        'start': f.start.strftime('%Y-%m-%d %H:%M:%S') if f.start else None,
        'duration_seconds': round(f.duration_seconds, 1),
        'total_damage': f.total_damage,
        'raid_dps': round(f.raid_dps),
        'fight_complete': f.fight_complete,
        'attacker_count': len(f.stats_by_attacker),
    }


def _hit_source(h) -> str:
    """Pick a single 'source' label for a Hit so the UI can group by it.

    Order of precedence:
      1. `pet_origin` — set when `apply_pet_owners` rewrote a pet's
         attacker name to its owner. Surfacing the original raw name
         lets the user see "Onyx Crusher" as a source under Soloson's
         row instead of losing the pet attribution to the merge.
      2. `spell` — set on SpellDamage events (named spell, DS source,
         DoT spell name).
      3. First special — Headshot, Assassinate, Slay Undead, etc. —
         when a special proc'd.
      4. The melee verb, title-cased ('backstabs' -> 'Backstabs').
      5. 'Melee' as a generic fallback.
    """
    if getattr(h, 'pet_origin', None):
        return h.pet_origin
    if h.spell:
        return h.spell
    if h.specials:
        return h.specials[0]
    if h.verb:
        return ' '.join(w.capitalize() for w in h.verb.split())
    return 'Melee'


def _build_healing_block(e: Encounter, bucket_seconds: int,
                         labels: list, sides_by_name: dict) -> dict:
    """Build the healing-side payload for an encounter: per-healer rows
    with `dealt_to` (heals given to each target) and `taken_from` (heals
    received from each healer), plus a stacked-area timeline matching the
    damage view's chart shape. Empty block if no heals were captured.

    `sides_by_name` is the friendly/enemy classification computed for the
    damage view; we reuse it for healing so the same person stays on the
    same side across tabs. Pure-healers (never appear in damage) default
    to friendly.
    """
    if not e.heals or e.start is None:
        return {
            'total_healing': 0,
            'healers': [],
            'biggest_heals': [],
            'timeline': {
                'bucket_seconds': bucket_seconds, 'labels': labels, 'datasets': [],
            },
        }

    duration = e.duration_seconds or 1.0
    n_buckets = len(labels) or 1
    canonical = {}
    matrix = {}  # (healer_lo, target_lo) -> {amount, casts, series}
    per_healer = {}  # healer_lo -> {amount, casts, crits, biggest, series}

    for h in e.heals:
        canonical.setdefault(h.healer.lower(), h.healer)
        canonical.setdefault(h.target.lower(), h.target)
        offset = (h.timestamp - e.start).total_seconds()
        idx = min(max(0, int(offset / bucket_seconds)), n_buckets - 1)
        is_crit = any('critical' in m.lower() for m in h.modifiers)

        cell = matrix.setdefault((h.healer.lower(), h.target.lower()),
                                 {'amount': 0, 'casts': 0,
                                  'series': [0] * n_buckets,
                                  'hits_detail': []})
        cell['amount'] += h.amount
        cell['casts'] += 1
        cell['series'][idx] += h.amount
        cell['hits_detail'].append({
            'offset_s': int(offset),
            'damage': h.amount,            # reuse 'damage' key for UI helper
            'mods': list(h.modifiers),
            'spell': h.spell,
            # If the heal was rewritten from a pet, show the pet's name
            # as the source so the owner's healing breakdown still
            # reveals which pet contributed.
            'source': (getattr(h, 'pet_origin', None)
                       or h.spell or 'Heal'),
        })

        agg = per_healer.setdefault(h.healer.lower(), {
            'amount': 0, 'casts': 0, 'crits': 0, 'biggest': 0,
            'series': [0] * n_buckets,
        })
        agg['amount'] += h.amount
        agg['casts'] += 1
        if is_crit:
            agg['crits'] += 1
        if h.amount > agg['biggest']:
            agg['biggest'] = h.amount
        agg['series'][idx] += h.amount

    total = sum(a['amount'] for a in per_healer.values()) or 1
    healers = []
    for nl, agg in per_healer.items():
        name = canonical[nl]
        side = sides_by_name.get(nl, 'friendly')
        if name.endswith('`s pet'):
            side = 'friendly'
        dealt_to = []
        taken_from = []
        for (atk_lo, tgt_lo), cell in matrix.items():
            if atk_lo == nl:
                dealt_to.append({
                    'target': canonical[tgt_lo],
                    'damage': cell['amount'],   # reuse 'damage' key so the
                    'hits': cell['casts'],      # UI breakdown helper Just Works
                    'series': cell['series'],
                    'hits_detail': cell['hits_detail'],
                })
            if tgt_lo == nl:
                taken_from.append({
                    'attacker': canonical[atk_lo],
                    'damage': cell['amount'],
                    'hits': cell['casts'],
                    'series': cell['series'],
                    'hits_detail': cell['hits_detail'],
                })
        healers.append({
            'attacker': name,             # reuse 'attacker' key for UI helper
            'damage': agg['amount'],      # reuse 'damage' key
            'dps': round(agg['amount'] / duration),  # actually HPS
            'hits': agg['casts'],         # casts shown in the Hits column
            'misses': 0,                  # heals don't have miss-equivalent yet
            'crits': agg['crits'],
            'biggest': agg['biggest'],
            'pct_of_total': round(agg['amount'] / total * 100, 1),
            'side': side,
            'dealt_to': sorted(dealt_to, key=lambda x: x['damage'], reverse=True),
            'taken_from': sorted(taken_from, key=lambda x: x['damage'], reverse=True),
        })
    healers.sort(key=lambda r: r['damage'], reverse=True)

    biggest_heals = sorted(e.heals, key=lambda h: h.amount, reverse=True)[:10]
    biggest_heals_payload = [{
        'offset_s': int((h.timestamp - e.start).total_seconds()),
        'attacker': h.healer,
        'target': h.target,
        'damage': h.amount,
        'specials': [h.spell] if h.spell else [],
    } for h in biggest_heals]

    # Timeline datasets: one per healer, stacked, sorted by total amount.
    sorted_healers = sorted(per_healer.items(),
                            key=lambda kv: sum(kv[1]['series']),
                            reverse=True)
    datasets = [{'label': canonical[nl], 'data': agg['series']}
                for nl, agg in sorted_healers]

    return {
        'total_healing': sum(a['amount'] for a in per_healer.values()),
        'healers': healers,
        'biggest_heals': biggest_heals_payload,
        'timeline': {
            'bucket_seconds': bucket_seconds,
            'labels': labels,
            'datasets': datasets,
        },
    }


def _encounter_summary(e: Encounter, manual_keysets: Optional[List[set]] = None
                       ) -> dict:
    """Compact per-encounter row for the session list. Multi-target
    encounters get a `+N` suffix in the display name so the user knows
    there were other mobs in the engagement.

    `fight_keys` is the list of stable per-fight keys (target+start ISO)
    backing this encounter, which the front-end uses when posting merge/
    split actions. `is_manual` is True when this encounter's exact set of
    fights matches a manual override from the sidecar — the UI shows a
    badge so the user can tell auto-grouped from user-pinned rows."""
    name = e.name
    extras = e.target_count - 1
    if extras > 0:
        name = f'{name} +{extras}'
    fkeys = []
    for m in e.members:
        k = fight_key(m.target, m.start)
        if k is not None:
            fkeys.append(k)
    is_manual = False
    if manual_keysets:
        member_set = set(fkeys)
        is_manual = any(member_set == ks for ks in manual_keysets)
    return {
        'encounter_id': e.encounter_id,
        'name': name,
        'start': e.start.strftime('%Y-%m-%d %H:%M:%S') if e.start else None,
        'duration_seconds': round(e.duration_seconds, 1),
        'total_damage': e.total_damage,
        'total_healing': e.total_healing,
        'raid_dps': round(e.raid_dps),
        'fight_complete': e.fight_complete,
        'attacker_count': e.attacker_count,
        'member_count': len(e.members),
        'fight_keys': fkeys,
        'is_manual': is_manual,
    }


def _fight_detail(f: FightResult, bucket_seconds: int = 5) -> dict:
    """Full per-fight payload: per-attacker stats, specials, timeline,
    biggest hits. Same data the text + HTML reports use, restructured
    for the front-end to render."""
    duration = f.duration_seconds or 1.0
    total = f.total_damage or 1
    attackers = []
    for s in f.attackers_by_damage():
        attackers.append({
            'attacker': s.attacker,
            'damage': s.damage,
            'dps': round(s.damage / duration),
            'hits': s.hits,
            'misses': s.misses,
            'crits': s.crits,
            'biggest': s.biggest,
            'pct_of_total': round(s.damage / total * 100, 1),
        })

    specials = []
    for s in f.attackers_by_damage():
        if not s.special_hits:
            continue
        for special in DEFAULT_SPECIAL_MODS:
            if special in s.special_hits:
                hits = s.special_hits[special]
                dmg = s.special_damage[special]
                specials.append({
                    'attacker': s.attacker,
                    'type': special,
                    'hits': hits,
                    'damage': dmg,
                    'pct_of_attacker': round(dmg / s.damage * 100, 1) if s.damage else 0,
                })

    timeline = bucket_hits(f, bucket_seconds=bucket_seconds)
    labels = [f'+{int((bs - f.start).total_seconds())}s'
              for bs in timeline.bucket_starts] if f.start else []
    # Sort attackers in the timeline by total damage descending so the
    # legend matches the DPS table.
    sorted_atk = sorted(timeline.per_attacker.keys(),
                        key=lambda a: sum(timeline.per_attacker[a]),
                        reverse=True)
    datasets = [{'label': a, 'data': timeline.per_attacker[a]} for a in sorted_atk]

    biggest = sorted(f.hits, key=lambda h: h.damage, reverse=True)[:10]
    biggest_hits = []
    if f.start:
        for h in biggest:
            biggest_hits.append({
                'offset_s': int((h.timestamp - f.start).total_seconds()),
                'attacker': h.attacker,
                'damage': h.damage,
                'specials': h.specials,
            })

    return {
        'fight_id': f.fight_id,
        'target': f.target,
        'start': f.start.strftime('%Y-%m-%d %H:%M:%S') if f.start else None,
        'end': f.end.strftime('%Y-%m-%d %H:%M:%S') if f.end else None,
        'duration_seconds': round(f.duration_seconds, 1),
        'total_damage': f.total_damage,
        'raid_dps': round(f.raid_dps),
        'fight_complete': f.fight_complete,
        'attackers': attackers,
        'specials': specials,
        'timeline': {
            'bucket_seconds': bucket_seconds,
            'labels': labels,
            'datasets': datasets,
        },
        'biggest_hits': biggest_hits,
    }


def _build_session_actor_rollup(
    encounters: List[Encounter],
    encounter_ids_order: List[int],
    per_encounter_actors,
    sides: dict,
) -> List[dict]:
    """Generic per-actor rollup builder used for all three session modes
    (damage, healing, tanking). Same shape across modes — only the source
    of `value` per encounter differs.

    Args:
      encounters: ordered Encounter list (same order as encounter_ids_order).
      encounter_ids_order: encounter ids in chronological order; used to
        align each actor's per-encounter arrays for the heatmap/chart.
      per_encounter_actors: callable(encounter) -> dict[lower_name, dict]
        where each inner dict has {'name': canonical, 'value': int,
        'biggest': int}. 'value' is the absolute value (damage / healing
        / damage_taken) for that actor in that encounter; 'biggest' is
        the largest single event seen.
      sides: lookup of lower_name -> 'friendly'|'enemy' (precomputed at
        session level using the damage classifier so the same actor
        stays on the same side across all three tabs).

    Returns rows with the same shape regardless of mode:
      attacker: str
      side: 'friendly' | 'enemy'
      total: int
      avg_rate / median_rate / p95_rate / best_rate: int (per second)
      biggest: int
      encounters_present: int
      per_encounter_rate: list[int]   (aligned to encounter_ids_order)
      per_encounter_value: list[int]
    """
    by_actor: dict = {}
    for e in encounters:
        duration = e.duration_seconds or 1.0
        for key, info in per_encounter_actors(e).items():
            rec = by_actor.setdefault(key, {
                'name': info['name'],
                'total': 0,
                'biggest': 0,
                'per_encounter_rate': {},
                'per_encounter_value': {},
            })
            rec['total'] += info['value']
            if info['biggest'] > rec['biggest']:
                rec['biggest'] = info['biggest']
            rec['per_encounter_rate'][e.encounter_id] = round(info['value'] / duration)
            rec['per_encounter_value'][e.encounter_id] = info['value']

    out = []
    for key, rec in by_actor.items():
        rates = [v for v in rec['per_encounter_rate'].values() if v > 0]
        if not rates:
            continue
        avg = round(sum(rates) / len(rates))
        median = round(statistics.median(rates))
        # P95 collapses toward best for small N (most logs have <20
        # encounters); use the max in that regime to avoid a misleading
        # "P95 = best" duplicate column.
        if len(rates) >= 20:
            p95 = sorted(rates, reverse=True)[int(len(rates) * 0.05)]
        else:
            p95 = max(rates)
        best = max(rates)
        out.append({
            'attacker': rec['name'],
            'side': sides.get(key, 'friendly'),
            'total': rec['total'],
            'avg_rate': avg,
            'median_rate': median,
            'p95_rate': p95,
            'best_rate': best,
            'biggest': rec['biggest'],
            'encounters_present': len(rates),
            'per_encounter_rate': [rec['per_encounter_rate'].get(eid, 0)
                                   for eid in encounter_ids_order],
            'per_encounter_value': [rec['per_encounter_value'].get(eid, 0)
                                    for eid in encounter_ids_order],
        })
    out.sort(key=lambda x: x['total'], reverse=True)
    return out


def _session_summary_payload(encounters: List[Encounter],
                             killed_only: bool = False,
                             encounter_ids: Optional[set] = None) -> dict:
    """Aggregate per-actor stats across all encounters in the session, in
    three parallel rollups (damage, healing, tanking).

    Builds the data behind the multi-fight session-summary view:
      - Header totals (start/end, duration, encounter and kill counts).
      - `damage_actors`: per-attacker rollup with DPS rates.
      - `healing_actors`: per-healer rollup with HPS rates.
      - `tanking_actors`: per-defender rollup with DTPS rates (damage
        taken per second).
      - `encounters`: per-encounter metadata for chart x-axis labels.

    All three rollups share the same row shape (`total`, `avg_rate`,
    `per_encounter_rate`, etc.) so the front-end can swap modes via a
    single render path. Side classification is computed once at the
    session level using the damage classifier (`received > dealt + healed`)
    and applied to all three rollups so an actor stays on the same side
    across tabs — a healer who only took AoE damage shows friendly in
    the tanking tab same as in the damage tab.

    `killed_only=True` filters to encounters with `fight_complete=True`
    so wipes / partial pulls don't drag down averages — most useful for
    raid-night stats. Default False to match the session table's full
    set; the front-end has its own toggle.

    `encounter_ids`, when provided, restricts the rollup to that subset
    (matched on `Encounter.encounter_id`). Used by the "Session summary
    (N selected)" path. Applied AFTER `killed_only` so explicit
    selection always wins.
    """
    if killed_only:
        encounters = [e for e in encounters if e.fight_complete]
    if encounter_ids is not None:
        encounters = [e for e in encounters if e.encounter_id in encounter_ids]
    if not encounters:
        return {
            'start': None, 'end': None,
            'duration_seconds': 0,
            'encounter_count': 0, 'killed_count': 0,
            'total_damage': 0, 'total_healing': 0, 'total_damage_taken': 0,
            'damage_actors': [],
            'healing_actors': [],
            'tanking_actors': [],
            'encounters': [],
            'killed_only': killed_only,
            'scoped': encounter_ids is not None,
        }

    # Order encounters chronologically so chart x-axis and heatmap
    # columns read left-to-right in time.
    encounters = sorted(encounters, key=lambda e: e.start or datetime.min)

    encounter_meta = [{
        'encounter_id': e.encounter_id,
        'name': _encounter_summary(e)['name'],
        'duration_seconds': round(e.duration_seconds, 1),
        'fight_complete': e.fight_complete,
        'start': e.start.strftime('%Y-%m-%d %H:%M:%S') if e.start else None,
    } for e in encounters]
    encounter_ids_order = [m['encounter_id'] for m in encounter_meta]

    # Pre-merge encounters once; healing/tanking blocks reuse the same
    # merged result the damage block walks.
    merged_by_eid = {e.encounter_id: merge_encounter(e) for e in encounters}

    # ---- Side classification (computed once, applied to all 3 modes) ----
    # Same rule the encounter detail view uses — `received > dealt + healed`
    # at the session level — so an actor's side stays consistent across
    # tabs. Pets always friendly via the backtick suffix.
    canonical: dict = {}
    sums = {}  # lower_name -> {'damage', 'received', 'healed'}
    def _bump(lo, canon, **deltas):
        canonical.setdefault(lo, canon)
        rec = sums.setdefault(lo, {'damage': 0, 'received': 0, 'healed': 0})
        for k, v in deltas.items():
            rec[k] += v
    for e in encounters:
        merged = merged_by_eid[e.encounter_id]
        for atk, s in merged.stats_by_attacker.items():
            _bump(atk.lower(), atk, damage=s.damage)
        for m in e.members:
            _bump(m.target.lower(), m.target, received=m.total_damage)
        for h in e.heals:
            _bump(h.healer.lower(), h.healer, healed=h.amount)
    sides: dict = {}
    for lo, rec in sums.items():
        if canonical[lo].endswith('`s pet'):
            sides[lo] = 'friendly'
        else:
            sides[lo] = ('enemy' if rec['received'] > rec['damage'] + rec['healed']
                         else 'friendly')

    # ---- Damage actors ----
    def damage_actors_for(e):
        out = {}
        for atk, s in merged_by_eid[e.encounter_id].stats_by_attacker.items():
            out[atk.lower()] = {'name': atk, 'value': s.damage, 'biggest': s.biggest}
        return out
    damage_actors = _build_session_actor_rollup(
        encounters, encounter_ids_order, damage_actors_for, sides)

    # ---- Healing actors ----
    def healing_actors_for(e):
        out = {}
        for h in e.heals:
            key = h.healer.lower()
            rec = out.setdefault(key, {'name': h.healer, 'value': 0, 'biggest': 0})
            rec['value'] += h.amount
            if h.amount > rec['biggest']:
                rec['biggest'] = h.amount
        return out
    healing_actors = _build_session_actor_rollup(
        encounters, encounter_ids_order, healing_actors_for, sides)

    # ---- Tanking actors (per-defender) ----
    # Aggregates damage_taken across all attackers per defender. Biggest
    # is the largest single hit taken (max across pairs).
    def tanking_actors_for(e):
        out = {}
        for (_, def_name), d in merged_by_eid[e.encounter_id].defends_by_pair.items():
            if d.damage_taken == 0:
                continue
            key = def_name.lower()
            rec = out.setdefault(key, {'name': def_name, 'value': 0, 'biggest': 0})
            rec['value'] += d.damage_taken
            if d.biggest_taken > rec['biggest']:
                rec['biggest'] = d.biggest_taken
        return out
    tanking_actors = _build_session_actor_rollup(
        encounters, encounter_ids_order, tanking_actors_for, sides)

    # Layer per-encounter healing-received data onto each tanking row so
    # the front-end's chart toggle can swap between damage taken / healing
    # received / life delta without a second round-trip. Heals are
    # attributed to the heal's `target` (the defender being healed).
    # Life delta is computed client-side as heals_received - damage_taken
    # per encounter — no need to ship a third array.
    heals_per_def_enc: dict = {}  # (def_lower, eid) -> total heal amount
    for e in encounters:
        for h in e.heals:
            key = (h.target.lower(), e.encounter_id)
            heals_per_def_enc[key] = heals_per_def_enc.get(key, 0) + h.amount
    enc_durations = {e.encounter_id: (e.duration_seconds or 1.0) for e in encounters}
    for t in tanking_actors:
        def_lo = t['attacker'].lower()
        heals_value = [heals_per_def_enc.get((def_lo, eid), 0)
                       for eid in encounter_ids_order]
        heals_rate = [round(v / enc_durations[eid])
                      for v, eid in zip(heals_value, encounter_ids_order)]
        t['per_encounter_heals_value'] = heals_value
        t['per_encounter_heals_rate'] = heals_rate

    starts = [e.start for e in encounters if e.start]
    ends = [e.end for e in encounters if e.end]
    return {
        'start': min(starts).strftime('%Y-%m-%d %H:%M:%S') if starts else None,
        'end': max(ends).strftime('%Y-%m-%d %H:%M:%S') if ends else None,
        'duration_seconds': sum(e.duration_seconds for e in encounters),
        'encounter_count': len(encounters),
        'killed_count': sum(1 for e in encounters if e.fight_complete),
        'total_damage': sum(e.total_damage for e in encounters),
        'total_healing': sum(e.total_healing for e in encounters),
        'total_damage_taken': sum(t['total'] for t in tanking_actors),
        'damage_actors': damage_actors,
        'healing_actors': healing_actors,
        'tanking_actors': tanking_actors,
        'encounters': encounter_meta,
        'killed_only': killed_only,
        'scoped': encounter_ids is not None,
    }


# ----- Request handler -----

class _State:
    """Module-level state, set by serve() before the server starts.

    Putting it on a class (not on the handler) means we don't have to
    subclass per-instance. The handler reads from here.
    """
    logfile: Optional[str] = None
    gap_seconds: int = 15
    min_damage: int = 10_000
    min_duration_seconds: int = 10
    bucket_seconds: int = 5
    encounter_gap_seconds: int = 10
    heals_extend_fights: bool = False
    # 0 = analyze the whole log; >0 = analyze only the last N hours of
    # log activity (anchored to the log's last timestamp, NOT wall clock,
    # so old logs work too). Default 8 covers a typical raid night while
    # keeping the first parse fast on multi-day logs; users who want
    # older data can bump this up or set it to 0.
    since_hours: int = 8
    fights: Optional[List[FightResult]] = None
    heals: Optional[List[Heal]] = None
    encounters: Optional[List[Encounter]] = None
    parser_stats: Optional[dict] = None
    # Sidecar state — pet owner assignments + manual encounter overrides.
    # Loaded on _set_logfile, written on every edit endpoint.
    sidecar: Optional[Sidecar] = None
    # Parse-progress dict, updated periodically by the parser thread and
    # read lock-free by /api/parse-status. State machine:
    #   'idle'    — no parse in progress; pct meaningless.
    #   'parsing' — currently walking the log; pct in [0, 100].
    #   'done'    — finished cleanly; pct=100. Stays in this state until
    #               the next reset.
    #   'error'   — parse raised; `message` carries the reason.
    parse_progress: dict = {
        'state': 'idle', 'pct': 0.0,
        'bytes_read': 0, 'total_bytes': 0,
        'message': None,
    }
    fights_lock = threading.Lock()


def _set_progress(state: str, *, bytes_read: int = 0, total_bytes: int = 0,
                  message: Optional[str] = None):
    """Replace the progress dict atomically (single attribute assignment
    is GIL-safe). `_State.parse_progress` is read lock-free from the
    status endpoint so we never need to hold the fights lock here."""
    pct = 0.0
    if total_bytes > 0:
        pct = max(0.0, min(100.0, bytes_read / total_bytes * 100.0))
    if state == 'done':
        pct = 100.0
    _State.parse_progress = {
        'state': state, 'pct': round(pct, 1),
        'bytes_read': bytes_read, 'total_bytes': total_bytes,
        'message': message,
    }


def _resolve_since_locked() -> Optional[datetime]:
    """Translate `since_hours` into an absolute cutoff datetime by reading
    the log's last timestamp. Anchoring to log-end (not wall clock) means
    `since_hours=4` works on a log that ended yesterday, returning the
    last 4h of recorded activity rather than nothing.

    Caller must hold `_State.fights_lock`. Returns None when no slicing
    should happen (since_hours == 0, log empty, or no parseable
    timestamps in the tail)."""
    if _State.since_hours <= 0 or _State.logfile is None:
        return None
    last = read_last_timestamp(_State.logfile)
    if last is None:
        return None
    return last - timedelta(hours=_State.since_hours)


def _ensure_combat_cached():
    """Walk the log once to populate fights + heals if not already cached.
    Caller must hold `_State.fights_lock`.

    `_State.fights` and `_State.heals` are deliberately the RAW outputs
    of `detect_combat` (no pet-owner rewrite). Rewriting happens later
    in `_get_encounters_locked` so the raw attacker names stay visible
    for the pet-owner edit modal. The cost is one extra pass per
    sidecar edit; we trade a little CPU for a much simpler edit flow.

    During the parse we update `_State.parse_progress` periodically so a
    concurrent /api/parse-status request can show a live progress bar.
    Sidecar edits don't trigger a re-parse (they only invalidate the
    encounter cache), so the progress bar is only relevant on the first
    load + reload + param change paths."""
    if _State.fights is not None and _State.heals is not None:
        return
    since = _resolve_since_locked()
    total = os.path.getsize(_State.logfile) if os.path.isfile(_State.logfile) else 0
    _set_progress('parsing', bytes_read=0, total_bytes=total)

    def _on_progress(bytes_read: int, total_bytes: int):
        _set_progress('parsing', bytes_read=bytes_read,
                      total_bytes=total_bytes)

    try:
        fights, heals = detect_combat(
            _State.logfile,
            gap_seconds=_State.gap_seconds,
            min_damage=_State.min_damage,
            min_duration_seconds=_State.min_duration_seconds,
            heals_extend_fights=_State.heals_extend_fights,
            since=since,
            progress_cb=_on_progress)
    except Exception as e:
        _set_progress('error', total_bytes=total, message=f'{type(e).__name__}: {e}')
        raise
    _State.fights = fights
    _State.heals = heals
    _set_progress('done', bytes_read=total, total_bytes=total)


def _get_fights() -> List[FightResult]:
    """Lazy-cache the detected fights so we don't re-parse on every request.
    The cache is per-process; switching logs invalidates it."""
    with _State.fights_lock:
        if _State.logfile is None:
            return []
        _ensure_combat_cached()
        return _State.fights


def _get_encounters_locked() -> List[Encounter]:
    """Lock-held variant of `_get_encounters`. Same lazy cache semantics
    as `_get_fights`. Pet-owner rewrites and manual encounter overrides
    are applied here so users can edit either without invalidating the
    expensive log-parse cache. Caller must hold `_State.fights_lock`."""
    if _State.logfile is None:
        return []
    _ensure_combat_cached()
    if _State.encounters is None:
        sidecar = _State.sidecar or Sidecar.empty()
        if sidecar.pet_owners:
            fights, heals = apply_pet_owners(
                _State.fights, _State.heals, sidecar.pet_owners)
        else:
            fights, heals = _State.fights, _State.heals
        _State.encounters = group_into_encounters(
            fights,
            gap_seconds=_State.encounter_gap_seconds,
            heals=heals,
            manual_groups=sidecar.manual_groups_for_grouper())
    return _State.encounters


def _get_encounters() -> List[Encounter]:
    with _State.fights_lock:
        return _get_encounters_locked()


def _set_logfile(path: str):
    """Switch the active log. Validates, resets caches, and loads the
    sidecar (`<logfile>.flurry.json`) if one exists. A missing or
    unreadable sidecar yields an empty one — the file isn't created
    until the user makes their first edit."""
    abs_path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(f'log file not found: {abs_path}')
    with _State.fights_lock:
        _State.logfile = abs_path
        _State.fights = None
        _State.heals = None
        _State.encounters = None
        _State.parser_stats = None
        _State.sidecar = load_sidecar(abs_path)
    _set_progress('idle')


def _invalidate_caches_locked(*, drop_combat: bool = False):
    """Clear derived caches. Caller must hold `_State.fights_lock`.

    `drop_combat=True` also drops the per-fight cache, forcing a re-walk
    of the log on the next request — needed when pet ownership changes
    so the rewritten attacker names propagate. Encounter-only edits
    (manual groupings) drop just the encounters cache; the fights/heals
    are unaffected."""
    if drop_combat:
        _State.fights = None
        _State.heals = None
        _State.parser_stats = None
        # The next consumer will trigger a fresh parse; reset progress so
        # the UI can show the new run from 0%.
        _set_progress('idle')
    _State.encounters = None


def _persist_sidecar_locked():
    """Atomic save of the active sidecar. Caller must hold the lock."""
    if _State.logfile is None or _State.sidecar is None:
        return
    save_sidecar(_State.logfile, _State.sidecar)


# Filenames are sanitized to a safe subset before being written to the
# uploads dir, since they come straight from a user header.
_SAFE_FILENAME_RE = re.compile(r'[^A-Za-z0-9._-]+')


def _save_uploaded_log(filename: str, content_length: int, stream) -> str:
    """Stream a request body to disk and return the saved path.

    Used by the drag-drop endpoint. The browser only gives us file content,
    not the original disk path, so we save it to a flurry-specific subdir
    of the OS temp dir and treat that as the active log. Streaming the body
    in chunks keeps memory usage bounded for large EQ logs.
    """
    name = os.path.basename(filename) or 'uploaded.txt'
    name = _SAFE_FILENAME_RE.sub('_', name)
    if not name:
        name = 'uploaded.txt'
    upload_dir = os.path.join(tempfile.gettempdir(), 'flurry-uploads')
    os.makedirs(upload_dir, exist_ok=True)
    dest = os.path.join(upload_dir, name)
    remaining = content_length
    with open(dest, 'wb') as out:
        while remaining > 0:
            chunk = stream.read(min(1 << 16, remaining))
            if not chunk:
                break
            out.write(chunk)
            remaining -= len(chunk)
    return dest


def _list_dir(path: str) -> dict:
    """List subdirs and EQ-log-style files at `path`. Used by the file
    picker. Filters files to those matching `eqlog_*.txt` (case-insensitive)
    so the picker doesn't drown the user in unrelated files. Subdirs are
    always shown so they can navigate anywhere."""
    abs_path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(abs_path):
        raise FileNotFoundError(f'not a directory: {abs_path}')

    dirs: List[str] = []
    files: List[dict] = []
    try:
        entries = os.listdir(abs_path)
    except PermissionError:
        entries = []

    for name in entries:
        full = os.path.join(abs_path, name)
        try:
            if os.path.isdir(full):
                # Skip hidden dirs (.git, .venv, etc.) to keep listings clean.
                if not name.startswith('.'):
                    dirs.append(name)
            elif os.path.isfile(full):
                low = name.lower()
                if low.startswith('eqlog_') and low.endswith('.txt'):
                    files.append({
                        'name': name,
                        'path': full,
                        'size': os.path.getsize(full),
                        'mtime': os.path.getmtime(full),
                    })
        except OSError:
            # Broken symlink, permission denied on stat, etc. Skip silently.
            continue

    dirs.sort(key=str.lower)
    files.sort(key=lambda f: f['mtime'], reverse=True)  # newest first

    parent = os.path.dirname(abs_path)
    if parent == abs_path:
        # Filesystem root (e.g. "C:\" on Windows or "/" on POSIX).
        parent = None

    return {
        'path': abs_path,
        'parent': parent,
        'dirs': dirs,
        'files': files,
    }


def _default_browse_path() -> str:
    """Where to point the picker when it opens with no path specified."""
    if _State.logfile:
        return os.path.dirname(_State.logfile)
    return os.path.expanduser('~')


class FlurryHandler(http.server.BaseHTTPRequestHandler):

    # Quiet the default request logging so the terminal stays usable.
    def log_message(self, *args, **kwargs):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            if path in ('/', '/index.html'):
                self._serve_html(_INDEX_HTML)
            elif path == '/api/session':
                self._serve_json(self._session_payload())
            elif path == '/api/browse':
                requested = qs.get('path', [None])[0] or _default_browse_path()
                self._serve_json(_list_dir(requested))
            elif path.startswith('/api/fight/'):
                fid_str = path.rsplit('/', 1)[1]
                if not fid_str.isdigit():
                    self.send_error(400, 'fight id must be an integer')
                    return
                payload = self._fight_payload(int(fid_str))
                if payload is None:
                    self.send_error(404, f'no fight with id {fid_str}')
                    return
                self._serve_json(payload)
            elif path == '/api/session-summary':
                # Multi-fight rollup. Query params:
                #   ?killed_only=1 — restrict to fight_complete=True
                #     encounters (raid wipes / aborted pulls don't drag
                #     down the avg/median DPS for that night).
                #   ?encounter_ids=1,2,3 — scope the rollup to a
                #     user-selected subset (driven by the session
                #     table's checkboxes). Empty / absent → whole log.
                killed_only = qs.get('killed_only', ['0'])[0] in ('1', 'true', 'True')
                ids_raw = qs.get('encounter_ids', [''])[0]
                encounter_ids = None
                if ids_raw:
                    try:
                        encounter_ids = {int(s) for s in ids_raw.split(',') if s.strip()}
                    except ValueError:
                        self.send_error(400, 'encounter_ids must be a comma-separated list of integers')
                        return
                    if not encounter_ids:
                        encounter_ids = None
                encounters = _get_encounters()
                self._serve_json(_session_summary_payload(
                    encounters, killed_only=killed_only,
                    encounter_ids=encounter_ids))
            elif path == '/api/parse-status':
                # Lock-free read of the progress dict — single attribute
                # access, GIL-safe. Polled rapidly by the upload UI to
                # animate a progress bar while a long parse runs in
                # another request handler thread.
                self._serve_json(_State.parse_progress)
            elif path == '/api/debug':
                # Walk the log and report parser coverage. Cached because
                # the walk is the same cost as detect_combat.
                with _State.fights_lock:
                    if _State.logfile is None:
                        self.send_error(400, 'no log loaded')
                        return
                    if _State.parser_stats is None:
                        _State.parser_stats = collect_parser_stats(_State.logfile)
                    payload = _State.parser_stats
                self._serve_json(payload)
            elif path.startswith('/api/encounter/'):
                eid_str = path.rsplit('/', 1)[1]
                if not eid_str.isdigit():
                    self.send_error(400, 'encounter id must be an integer')
                    return
                payload = self._encounter_payload(int(eid_str))
                if payload is None:
                    self.send_error(404, f'no encounter with id {eid_str}')
                    return
                self._serve_json(payload)
            else:
                self.send_error(404)
        except FileNotFoundError as e:
            self.send_error(404, str(e))
        except Exception as e:
            # Don't crash the server on a single bad request.
            self.send_error(500, f'{type(e).__name__}: {e}')

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            length = int(self.headers.get('Content-Length', '0'))

            # /api/upload streams the raw request body to disk (logs can be
            # large), so it bypasses the JSON-decode path the other routes
            # share. Pull the original filename from a header.
            if path == '/api/upload':
                raw_name = self.headers.get('X-Filename', 'uploaded.txt')
                filename = urllib.parse.unquote(raw_name)
                if length <= 0:
                    self.send_error(400, 'empty upload')
                    return
                saved = _save_uploaded_log(filename, length, self.rfile)
                _set_logfile(saved)
                self._serve_json(self._session_payload())
                return

            body = self.rfile.read(length).decode('utf-8') if length else ''
            data = json.loads(body) if body else {}

            if path == '/api/open':
                requested = data.get('path')
                if not requested:
                    self.send_error(400, 'missing "path" in body')
                    return
                _set_logfile(requested)
                self._serve_json(self._session_payload())
            elif path == '/api/params':
                # Update detection / grouping knobs and invalidate caches.
                # Allowed without a log loaded so the picker can pre-set
                # values like `since_hours` before the first parse — the
                # cache invalidation is a no-op when there's nothing
                # cached yet, and the values stick on `_State` for the
                # eventual parse to pick up.
                int_keys = ('gap_seconds', 'min_damage',
                            'min_duration_seconds',
                            'encounter_gap_seconds', 'bucket_seconds',
                            'since_hours')
                bool_keys = ('heals_extend_fights',)
                updates = {}
                for key in int_keys:
                    if key not in data:
                        continue
                    try:
                        v = int(data[key])
                    except (TypeError, ValueError):
                        self.send_error(400, f'{key} must be an integer')
                        return
                    if v < 0:
                        self.send_error(400, f'{key} must be non-negative')
                        return
                    updates[key] = v
                for key in bool_keys:
                    if key in data:
                        updates[key] = bool(data[key])
                with _State.fights_lock:
                    for k, v in updates.items():
                        setattr(_State, k, v)
                    _invalidate_caches_locked(drop_combat=True)
                self._serve_json(self._session_payload())
            elif path == '/api/reload':
                # Re-parse the current log (picks up any new fights appended
                # since last load). No-op if no log is loaded. Sidecar is
                # preserved so user edits survive a reload.
                with _State.fights_lock:
                    if _State.logfile is None:
                        self.send_error(400, 'no log loaded')
                        return
                    _invalidate_caches_locked(drop_combat=True)
                self._serve_json(self._session_payload())
            elif path == '/api/pet-owners':
                # Set or clear pet-owner assignments. Two body shapes:
                #   {"actor": "<actor>", "owner": "<owner>" | null}
                #   {"updates": [{"actor": "...", "owner": "..." | null}, ...]}
                # Owner null/empty clears that mapping. Batch shape lets
                # the modal commit all dropdown changes in one POST so we
                # only invalidate the encounter cache once for a multi-
                # row edit. The rewrite happens at encounter-build time,
                # so we never drop the (more expensive) fights cache.
                if _State.logfile is None:
                    self.send_error(400, 'no log loaded')
                    return
                if 'updates' in data:
                    items = data.get('updates') or []
                    if not isinstance(items, list):
                        self.send_error(400, '"updates" must be a list')
                        return
                else:
                    items = [{'actor': data.get('actor'),
                              'owner': data.get('owner')}]
                # Validate all items up-front so a bad entry mid-batch
                # doesn't half-apply changes — sidecar mutations should
                # be all-or-nothing from the user's perspective.
                cleaned = []
                for u in items:
                    if not isinstance(u, dict):
                        self.send_error(400, 'each update must be an object')
                        return
                    actor = u.get('actor')
                    owner = u.get('owner')
                    if not actor or not isinstance(actor, str):
                        self.send_error(400, 'missing "actor" in update')
                        return
                    if owner is not None and not isinstance(owner, str):
                        self.send_error(400, '"owner" must be a string or null')
                        return
                    cleaned.append((actor, owner))
                with _State.fights_lock:
                    if _State.sidecar is None:
                        _State.sidecar = Sidecar.empty()
                    for actor, owner in cleaned:
                        _State.sidecar.set_pet_owner(actor, owner)
                    _persist_sidecar_locked()
                    _invalidate_caches_locked(drop_combat=False)
                self._serve_json(self._session_payload())
            elif path == '/api/encounters':
                # Manual encounter override. Body:
                #   {"action": "merge", "encounter_ids": [...], "name": null?}
                #   {"action": "split", "encounter_ids": [...]}
                # Encounter ids are resolved to stable fight keys against
                # the *current* encounter list (held under the lock), so
                # the wire format using ids is fine even though ids are
                # unstable across param changes.
                if _State.logfile is None:
                    self.send_error(400, 'no log loaded')
                    return
                action = data.get('action')
                if action not in ('merge', 'split'):
                    self.send_error(400, '"action" must be "merge" or "split"')
                    return
                ids = data.get('encounter_ids') or []
                if not isinstance(ids, list) or not all(isinstance(x, int) for x in ids):
                    self.send_error(400, '"encounter_ids" must be a list of ints')
                    return
                with _State.fights_lock:
                    if _State.sidecar is None:
                        _State.sidecar = Sidecar.empty()
                    encounters = _get_encounters_locked()
                    by_id = {e.encounter_id: e for e in encounters}
                    selected_keys = []
                    for eid in ids:
                        e = by_id.get(eid)
                        if e is None:
                            self.send_error(400, f'no encounter with id {eid}')
                            return
                        for m in e.members:
                            k = fight_key(m.target, m.start)
                            if k is not None:
                                selected_keys.append(k)
                    # Dedupe while preserving order so the stored list
                    # reads naturally if anyone opens the sidecar by hand.
                    seen = set()
                    deduped = []
                    for k in selected_keys:
                        if k not in seen:
                            seen.add(k)
                            deduped.append(k)
                    if action == 'merge':
                        if len(deduped) < 2:
                            self.send_error(400,
                                'merge needs at least 2 fights across the selected encounters')
                            return
                        name = data.get('name')
                        if name is not None and not isinstance(name, str):
                            self.send_error(400, '"name" must be a string or null')
                            return
                        _State.sidecar.merge_encounter(deduped, name=name)
                    else:  # split
                        _State.sidecar.remove_keys_from_manual(deduped)
                    _persist_sidecar_locked()
                    # Pet-owner caches are unaffected by encounter edits;
                    # only the encounter cache needs to drop.
                    _invalidate_caches_locked(drop_combat=False)
                self._serve_json(self._session_payload())
            else:
                self.send_error(404)
        except FileNotFoundError as e:
            self.send_error(404, str(e))
        except json.JSONDecodeError as e:
            self.send_error(400, f'invalid JSON: {e}')
        except Exception as e:
            self.send_error(500, f'{type(e).__name__}: {e}')

    def _serve_html(self, html: str):
        body = html.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_json(self, data):
        body = json.dumps(data).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _session_payload(self):
        params = {
            'gap_seconds': _State.gap_seconds,
            'min_damage': _State.min_damage,
            'min_duration_seconds': _State.min_duration_seconds,
            'encounter_gap_seconds': _State.encounter_gap_seconds,
            'bucket_seconds': _State.bucket_seconds,
            'heals_extend_fights': _State.heals_extend_fights,
            'since_hours': _State.since_hours,
        }
        if _State.logfile is None:
            # No log loaded — UI shows the file picker.
            return {
                'logfile': None,
                'logfile_basename': None,
                'params': params,
                'encounters': [],
                'summary': None,
                'pet_owners': {},
                'manual_encounters': 0,
            }
        encounters = _get_encounters()
        # Pre-compute the manual-encounter keysets once so the per-row
        # `is_manual` flag is O(members) per row rather than O(members^2).
        sidecar = _State.sidecar or Sidecar.empty()
        manual_keysets = [set(m.fight_keys) for m in sidecar.manual_encounters]
        return {
            'logfile': _State.logfile,
            'logfile_basename': os.path.basename(_State.logfile),
            'params': params,
            'encounters': [_encounter_summary(e, manual_keysets)
                           for e in encounters],
            'summary': {
                'total_encounters': len(encounters),
                'total_killed': sum(1 for e in encounters if e.fight_complete),
                'total_damage': sum(e.total_damage for e in encounters),
            },
            'pet_owners': dict(sidecar.pet_owners),
            'manual_encounters': len(sidecar.manual_encounters),
        }

    def _fight_payload(self, fight_id: int):
        fights = _get_fights()
        f = next((x for x in fights if x.fight_id == fight_id), None)
        if f is None:
            return None
        return _fight_detail(f, bucket_seconds=_State.bucket_seconds)

    def _encounter_payload(self, encounter_id: int):
        encounters = _get_encounters()
        e = next((x for x in encounters if x.encounter_id == encounter_id), None)
        if e is None:
            return None
        # Reuse `_fight_detail` against a merged FightResult so the encounter
        # detail view gets the same JSON shape (per-attacker table, specials,
        # timeline, biggest hits) for free. We only add a `members` block on
        # top so the front-end can render the constituent fights list.
        merged = merge_encounter(e)
        payload = _fight_detail(merged, bucket_seconds=_State.bucket_seconds)
        payload['encounter_id'] = e.encounter_id
        payload['name'] = _encounter_summary(e)['name']
        payload['member_count'] = len(e.members)
        payload['target_count'] = e.target_count
        payload['members'] = [
            {
                'fight_id': m.fight_id,
                'target': m.target,
                'damage': m.total_damage,
                'duration_seconds': round(m.duration_seconds, 1),
                'fight_complete': m.fight_complete,
                'attacker_count': len(m.stats_by_attacker),
            }
            for m in e.members
        ]
        # Drop corpses from the per-attacker rows. `<X>'s corpse` is what
        # death-DS / death-touch procs get attributed to in EQ — real
        # damage but not actionable as a "who's doing what" attribution.
        # Filter here rather than in detect_combat so the encounter total
        # damage and per-fight totals still include those hits if anyone
        # cares to query them later.
        def _is_corpse(name):
            return name.endswith("`s corpse") or name.endswith("'s corpse")

        payload['attackers'] = [a for a in payload['attackers']
                                if not _is_corpse(a['attacker'])]
        # Recompute pct_of_total off the filtered total so percentages
        # still sum to ~100% in the UI.
        non_corpse_total = sum(a['damage'] for a in payload['attackers']) or 1
        for a in payload['attackers']:
            a['pct_of_total'] = round(a['damage'] / non_corpse_total * 100, 1)

        # Classify each attacker as friendly or enemy by comparing how
        # much damage they DEALT vs how much they TOOK in this encounter:
        #   - Friendlies (PCs, pets) deal lots of damage and take some.
        #   - Enemies (the boss/adds being killed) take lots of damage and
        #     deal some back to the raid.
        # `received > dealt` is the first-pass signal. A second pass
        # below catches enemies (e.g. environmental AoE mobs like a Lava
        # Vortex) that deal damage but are never targeted back: their
        # damage lands almost entirely on friendlies, so that's the tell.
        #
        # We also fold in HEALING dispensed: a pure healer who only took
        # AoE damage and never struck back (typical for cleric/druid
        # mains in heavy raid content) would otherwise look like an
        # enemy by the dealt-vs-received rule. Counting their healing
        # output on the "active" side of the comparison fixes that.
        # Enemies that heal themselves are unaffected: their incoming
        # damage swamps any self-heal so received > dealt + healed
        # still holds.
        damage_received = {}
        for m in e.members:
            key = m.target.lower()
            damage_received[key] = damage_received.get(key, 0) + m.total_damage
        healing_dispensed = {}
        for h in e.heals:
            key = h.healer.lower()
            healing_dispensed[key] = healing_dispensed.get(key, 0) + h.amount
        for a in payload['attackers']:
            name = a['attacker']
            if name.endswith('`s pet'):
                a['side'] = 'friendly'
                continue
            received = damage_received.get(name.lower(), 0)
            dealt = a['damage']
            healed = healing_dispensed.get(name.lower(), 0)
            a['side'] = 'enemy' if received > dealt + healed else 'friendly'

        # Per-attacker damage breakdown: who hit whom for how much, plus a
        # bucketed timeline series per pair AND the raw per-hit detail
        # (offset, damage, modifiers) so the UI can both render a chart
        # and drill into the hits inside a clicked bucket.
        bucket_seconds = _State.bucket_seconds
        n_buckets = len(payload['timeline']['labels']) or 1
        canonical = {}  # lowercased name -> first-seen canonical casing
        matrix = {}     # (attacker_lower, target_lower) -> cell
        for m in e.members:
            for h in m.hits:
                if _is_corpse(h.attacker) or _is_corpse(h.target):
                    continue
                canonical.setdefault(h.attacker.lower(), h.attacker)
                canonical.setdefault(h.target.lower(), h.target)
                key = (h.attacker.lower(), h.target.lower())
                cell = matrix.setdefault(key, {
                    'damage': 0, 'hits': 0, 'series': [0] * n_buckets,
                    'hits_detail': [],
                })
                cell['damage'] += h.damage
                cell['hits'] += 1
                offset = 0
                if e.start is not None:
                    offset = (h.timestamp - e.start).total_seconds()
                    idx = min(max(0, int(offset / bucket_seconds)), n_buckets - 1)
                    cell['series'][idx] += h.damage
                cell['hits_detail'].append({
                    'offset_s': int(offset),
                    'damage': h.damage,
                    'mods': list(h.modifiers),
                    'kind': h.kind,
                    'spell': h.spell,
                    'source': _hit_source(h),
                })
        for a in payload['attackers']:
            nl = a['attacker'].lower()
            dealt_to = []
            taken_from = []
            for (atk_lo, tgt_lo), cell in matrix.items():
                if atk_lo == nl:
                    dealt_to.append({
                        'target': canonical[tgt_lo],
                        'damage': cell['damage'],
                        'hits': cell['hits'],
                        'series': cell['series'],
                        'hits_detail': cell['hits_detail'],
                    })
                if tgt_lo == nl:
                    taken_from.append({
                        'attacker': canonical[atk_lo],
                        'damage': cell['damage'],
                        'hits': cell['hits'],
                        'series': cell['series'],
                        'hits_detail': cell['hits_detail'],
                    })
            a['dealt_to'] = sorted(dealt_to, key=lambda x: x['damage'], reverse=True)
            a['taken_from'] = sorted(taken_from, key=lambda x: x['damage'], reverse=True)

        # Pass-2 classifier refinement: an attacker initially called
        # friendly but whose damage primarily lands on (still-friendly)
        # names is almost certainly an enemy that just doesn't show up as
        # a target itself. Catches cases like Lava Vortex AoE'ing the
        # raid without anyone hitting it back.
        sides = {a['attacker'].lower(): a['side'] for a in payload['attackers']}
        for a in payload['attackers']:
            if a['side'] == 'enemy':
                continue
            # An actor who healed in this encounter is decisively
            # friendly — don't let stray damage flip them. Covers
            # healers whose only "damage" is a damage-shield proc on
            # the friendly tank when they were spell-buffed.
            if healing_dispensed.get(a['attacker'].lower(), 0) > 0:
                continue
            if a['attacker'].endswith('`s pet'):
                continue
            to_friendlies = 0
            to_enemies = 0
            for d in a['dealt_to']:
                tgt_side = sides.get(d['target'].lower(), 'friendly')
                if tgt_side == 'enemy':
                    to_enemies += d['damage']
                else:
                    to_friendlies += d['damage']
            if to_friendlies > to_enemies:
                a['side'] = 'enemy'

        # Build the healing-side block. Reuse damage-side classification so
        # the same person stays on the same side across the Damage / Healing
        # tabs.
        sides_by_name = {a['attacker'].lower(): a['side']
                         for a in payload['attackers']}
        payload['healing'] = _build_healing_block(
            e, bucket_seconds, payload['timeline']['labels'], sides_by_name)

        # Build the tanking-side block from merged.defends_by_pair.
        # Friendly-focused: a defender shows up in the tanking tab if
        # they took damage and aren't classified as enemy. Defenders not
        # in `sides_by_name` (e.g. pure-tank classes who only blocked and
        # never landed a swing) get a fallback friendly classification —
        # better to include them than silently drop the tank from their
        # own tab. Enemies' damage taken is already on the Damage tab's
        # Enemies section.
        # Per-defender heal series + per-heal records, for the modal's
        # damage / healing / life-delta toggle on the All row. Bucketed
        # off the same encounter timeline as the damage matrix so the
        # life-delta line can be computed client-side as heals - damage.
        defender_heals: dict = {}  # def_lower -> {'series': [...], 'hits_detail': [...], 'total': N}
        for h in e.heals:
            def_lo = h.target.lower()
            entry = defender_heals.setdefault(def_lo, {
                'series': [0] * n_buckets,
                'hits_detail': [],
                'total': 0,
            })
            entry['total'] += h.amount
            offset = 0
            if e.start is not None:
                offset = (h.timestamp - e.start).total_seconds()
                idx = min(max(0, int(offset / bucket_seconds)), n_buckets - 1)
                entry['series'][idx] += h.amount
            entry['hits_detail'].append({
                'offset_s': int(offset),
                'damage': h.amount,           # reuse `damage` field so the
                'mods': list(h.modifiers),    # modal's source breakdown
                'kind': 'heal',               # works without translation
                'spell': h.spell,
                'source': h.healer,
            })

        defenders_map: dict = {}
        for (atk_name, def_name), d in merged.defends_by_pair.items():
            if _is_corpse(atk_name) or _is_corpse(def_name):
                continue
            side = sides_by_name.get(def_name.lower(), 'friendly')
            if side == 'enemy':
                continue
            rec = defenders_map.setdefault(def_name.lower(), {
                'defender': def_name,
                'damage_taken': 0,
                'hits_landed': 0,
                'biggest_taken': 0,
                'avoided': {},        # outcome -> count, aggregated across attackers
                'breakdown': [],      # per-attacker rows
            })
            rec['damage_taken'] += d.damage_taken
            rec['hits_landed'] += d.hits_landed
            if d.biggest_taken > rec['biggest_taken']:
                rec['biggest_taken'] = d.biggest_taken
            for outcome, n in d.avoided.items():
                rec['avoided'][outcome] = rec['avoided'].get(outcome, 0) + n
            # Pull the per-(attacker, defender) bucketed series and per-hit
            # detail from the damage matrix so the breakdown row can pop
            # the same DTPS-over-time modal the Damage tab uses. A pair
            # whose only events were avoidances has an empty series; the
            # modal handles that gracefully.
            cell = matrix.get((atk_name.lower(), def_name.lower()))
            series = list(cell['series']) if cell else [0] * n_buckets
            hits_detail = list(cell['hits_detail']) if cell else []
            rec['breakdown'].append({
                'attacker': atk_name,
                'damage_taken': d.damage_taken,
                'hits_landed': d.hits_landed,
                'biggest_taken': d.biggest_taken,
                'avoided': dict(d.avoided),
                'series': series,
                'hits_detail': hits_detail,
            })
        defenders = []
        for def_lo, rec in defenders_map.items():
            rec['breakdown'].sort(key=lambda r: (r['damage_taken'], r['hits_landed']),
                                  reverse=True)
            # Attach per-defender heals (used by the modal's All-row toggle
            # to render healing-received and life-delta series). Empty
            # arrays for defenders with no heals received.
            heal_entry = defender_heals.get(def_lo)
            if heal_entry:
                rec['heals_series'] = heal_entry['series']
                rec['heals_detail'] = heal_entry['hits_detail']
                rec['heals_total'] = heal_entry['total']
            else:
                rec['heals_series'] = [0] * n_buckets
                rec['heals_detail'] = []
                rec['heals_total'] = 0
            defenders.append(rec)
        defenders.sort(key=lambda r: (r['damage_taken'], r['hits_landed']),
                       reverse=True)
        payload['defenders'] = defenders

        # Pet-owner state — the front-end shows it in the per-attacker
        # edit modal so the user can see (and clear) existing assignments.
        # `raw_attackers` is the list of original (un-rewritten) attacker
        # names visible across this encounter's RAW fights, sorted by
        # damage desc. The modal uses this as its candidate list — the
        # rewritten `attackers` array above hides the original names so
        # we need a separate channel. Each raw attacker carries a `side`
        # so the owner dropdown can filter to plausible candidates
        # (friendly pet → friendly owners, enemy pet → enemy owners).
        sidecar = _State.sidecar or Sidecar.empty()
        payload['pet_owners'] = dict(sidecar.pet_owners)
        member_keys = {fight_key(m.target, m.start) for m in e.members}
        raw_totals: dict = {}
        for rf in (_State.fights or []):
            if fight_key(rf.target, rf.start) in member_keys:
                for atk_name, s in rf.stats_by_attacker.items():
                    raw_totals[atk_name] = raw_totals.get(atk_name, 0) + s.damage
        # `damage_received` is keyed by lowercased TARGET name and was
        # built above; targets aren't rewritten so it works for raw
        # actors too. Compare dealt vs received the same way the
        # rewritten attackers do — this is the "basic" classifier
        # without the pass-2 refinement, which is fine here because
        # the dropdown is just suggesting candidates, not making
        # downstream decisions.
        raw_attackers = []
        for name, dmg in raw_totals.items():
            if _is_corpse(name):
                continue
            if name.endswith('`s pet'):
                side = 'friendly'
            else:
                received = damage_received.get(name.lower(), 0)
                side = 'enemy' if received > dmg else 'friendly'
            raw_attackers.append({'attacker': name, 'damage': dmg, 'side': side})
        raw_attackers.sort(key=lambda x: x['damage'], reverse=True)
        payload['raw_attackers'] = raw_attackers
        return payload


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threaded so the browser's parallel requests don't queue. Daemon
    threads so Ctrl-C doesn't hang on in-flight connections."""
    daemon_threads = True
    allow_reuse_address = True


# ----- Entry point -----

def serve(logfile: Optional[str] = None,
          port: int = 8765,
          gap_seconds: int = 15,
          min_damage: int = 10_000,
          min_duration_seconds: int = 10,
          bucket_seconds: int = 5,
          encounter_gap_seconds: int = 10,
          heals_extend_fights: bool = False,
          since_hours: int = 8,
          open_browser: bool = True):
    """Start the local UI server. Blocks until Ctrl-C.

    If `logfile` is None the UI launches into the file picker. Either way
    the user can switch logs from inside the UI without restarting.
    """
    _State.logfile = None
    _State.fights = None
    _State.heals = None
    _State.encounters = None
    _State.parser_stats = None
    _State.sidecar = None
    _State.gap_seconds = gap_seconds
    _State.min_damage = min_damage
    _State.min_duration_seconds = min_duration_seconds
    _State.bucket_seconds = bucket_seconds
    _State.encounter_gap_seconds = encounter_gap_seconds
    _State.heals_extend_fights = heals_extend_fights
    _State.since_hours = since_hours
    _set_progress('idle')
    if logfile is not None:
        _set_logfile(logfile)

    server = _ThreadingServer(('127.0.0.1', port), FlurryHandler)
    url = f'http://127.0.0.1:{port}/'
    print(f'Flurry UI: {url}')
    if _State.logfile:
        print(f'  log:    {_State.logfile}')
    else:
        print('  log:    (none — pick one in the UI)')
    print(f'  params: gap={gap_seconds}s  min_damage={min_damage:,}  '
          f'min_dur={min_duration_seconds}s  '
          f'encounter_gap={encounter_gap_seconds}s')
    print('Ctrl-C to stop.')

    if open_browser:
        # Open in a background thread so we don't race the server startup.
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopping.')
    finally:
        server.server_close()


# ----- HTML template -----
#
# Single-page hash-routed UI. Two views: session list (#/) and fight
# detail (#/fight/<id>). Plain vanilla JS, Chart.js from CDN for the
# timeline chart (matching the existing report.py choice — vendoring
# Chart.js is a future improvement).

_INDEX_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flurry</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0f1419;
    --panel: #1a2030;
    --row: #1e2536;
    --row-hover: #263048;
    --border: #2a3142;
    --text: #e5e7eb;
    --text-dim: #94a3b8;
    --text-bright: #f8fafc;
    --accent: #60a5fa;
    --good: #34d399;
    --warn: #fbbf24;
    --bad: #f87171;
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--bg); color: var(--text);
    margin: 0; padding: 24px;
    max-width: 1200px; margin: 0 auto;
  }
  header { margin-bottom: 20px; }
  h1 { font-size: 1.5rem; margin: 0 0 4px; color: var(--text-bright); }
  h1 .brand { color: var(--accent); font-weight: 700; }
  h1 .sep { color: var(--border); margin: 0 8px; }
  .sub { color: var(--text-dim); font-size: 0.9rem; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .back { font-size: 0.9rem; margin-bottom: 12px; display: inline-block; }

  table { width: 100%; border-collapse: collapse; font-size: 0.92rem; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }
  th { color: var(--text-dim); font-weight: 600; font-size: 0.8rem;
       text-transform: uppercase; letter-spacing: 0.04em; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  tr.fight-row { cursor: pointer; }
  tr.fight-row:hover td { background: var(--row-hover); }
  td.target { font-weight: 600; color: var(--text-bright); }
  td.status { font-size: 0.85rem; }
  .status.killed { color: var(--good); }
  .status.incomplete { color: var(--warn); }
  th.sortable { cursor: pointer; user-select: none; }
  th.sortable:hover { color: var(--text-bright); }
  .sort-arrow { color: var(--accent); margin-left: 4px; }

  /* Tunable-parameters panel */
  .params-help { margin-bottom: 12px; font-size: 0.8rem;
                 color: var(--text-dim); line-height: 1.45; }
  .params-help strong { color: var(--text-bright); font-weight: 600; }
  .params-row { display: flex; gap: 14px; align-items: flex-end; flex-wrap: wrap; }
  .params-row label { display: flex; flex-direction: column; gap: 4px;
                      font-size: 0.75rem; color: var(--text-dim);
                      text-transform: uppercase; letter-spacing: 0.04em; }
  .params-row input[type="number"] {
                      background: var(--row); color: var(--text);
                      border: 1px solid var(--border); padding: 6px 10px;
                      border-radius: 4px; font-size: 0.9rem; width: 110px;
                      font-variant-numeric: tabular-nums; font-family: inherit; }
  .params-row input:focus { outline: none; border-color: var(--accent); }
  .params-row .err-msg { color: var(--bad); font-size: 0.85rem;
                         align-self: center; margin-left: 8px; }
  .params-row label.check { flex-direction: row; align-items: center;
                            gap: 6px; padding-bottom: 8px;
                            text-transform: none; letter-spacing: 0;
                            font-size: 0.85rem; color: var(--text); }
  .params-row label.check input { accent-color: var(--accent); }

  /* Expandable per-attacker rows */
  tr.attacker-row { cursor: pointer; }
  tr.attacker-row:hover td { background: var(--row-hover); }
  tr.attacker-row .expand { display: inline-block; color: var(--text-dim);
                            transition: transform 0.15s; margin-right: 6px;
                            font-size: 0.7rem; vertical-align: middle;
                            width: 0.8rem; }
  tr.attacker-row.expanded .expand { transform: rotate(90deg); }
  tr.attacker-detail > td { padding: 14px 20px; background: var(--bg);
                            border-bottom: 1px solid var(--border); }
  .breakdown { display: grid;
               grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
               gap: 24px; }
  .breakdown h4 { color: var(--text-dim); font-size: 0.72rem;
                  text-transform: uppercase; letter-spacing: 0.06em;
                  margin: 0 0 8px; }
  .breakdown table { width: 100%; }
  .breakdown table th, .breakdown table td {
    padding: 4px 8px; font-size: 0.85rem; }
  .breakdown .empty { color: var(--text-dim); font-size: 0.85rem;
                      padding: 4px 0; }
  tr.pair-row { cursor: pointer; }
  tr.pair-row:hover td { background: var(--row-hover); }
  tr.pair-row td:first-child { color: var(--accent); }
  /* Synthetic "All" row at the top of each breakdown table — sums the
     other rows. Subtle accent stripe so it reads as a rollup, not just
     another target. */
  tr.pair-row-all td { background: rgba(96, 165, 250, 0.08);
                       border-top: 1px solid var(--border);
                       border-bottom: 1px solid var(--border); }
  tr.pair-row-all:hover td { background: rgba(96, 165, 250, 0.16); }
  tr.pair-row-all td:first-child { color: var(--text-bright); }

  /* Tanking table — denser cells than the damage tables because 12
     columns get cramped at the default padding. */
  .tanking-table th, .tanking-table td,
  .tanking-breakdown th, .tanking-breakdown td {
    padding: 6px 10px; font-size: 0.85rem;
  }
  .tanking-table th.num, .tanking-breakdown th.num { font-size: 0.75rem; }

  /* Pair-detail modal */
  .modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.65);
                    display: flex; align-items: center; justify-content: center;
                    z-index: 1000; }
  .modal { background: var(--panel); border: 1px solid var(--border);
           border-radius: 8px; padding: 24px 24px 28px;
           max-width: 900px; width: 92%; max-height: 85vh;
           overflow: auto; position: relative; }
  .modal h3 { margin: 0 0 4px; color: var(--text-bright); font-size: 1.05rem; }
  .modal .modal-sub { color: var(--text-dim); font-size: 0.85rem;
                      margin-bottom: 16px; }
  .modal-close { position: absolute; top: 8px; right: 12px;
                 background: transparent; border: none; color: var(--text-dim);
                 font-size: 1.6rem; line-height: 1; cursor: pointer;
                 padding: 4px 10px; font-family: inherit; }
  .modal-close:hover { color: var(--text-bright); }

  /* Pair-modal layout: chart + stats + hits on the left, source
     breakdown on the right. Stack the columns when narrow. */
  .modal.pair-modal { max-width: 1100px; }
  /* Delta mode hides the source-breakdown column; let the chart take
     the full width. */
  .modal.pair-modal.no-source-panel .pair-body {
    grid-template-columns: 1fr;
  }
  /* Header row with title + (optional) damage/healing/delta toggle.
     padding-right on the row itself reserves space for the absolutely-
     positioned modal-close (×) button so the toggle group doesn't slide
     under it on narrower viewports. */
  .pair-modal-head { display: flex; align-items: flex-start;
                     justify-content: space-between; gap: 16px;
                     margin-bottom: 4px; padding-right: 36px; }
  .pair-metric-toggle { display: inline-flex; gap: 0;
                        border: 1px solid var(--border); border-radius: 6px;
                        overflow: hidden; flex-shrink: 0; }
  .pair-body { display: grid; grid-template-columns: 1fr 260px;
               gap: 20px; margin-top: 14px; align-items: start; }
  @media (max-width: 800px) { .pair-body { grid-template-columns: 1fr; } }
  .pair-stats { display: flex; flex-wrap: wrap; gap: 18px;
                margin-top: 14px; padding: 10px 14px;
                background: var(--row); border-radius: 6px;
                font-size: 0.85rem; color: var(--text-dim); }
  .pair-stats strong { color: var(--text); font-weight: 600; }
  .pair-hits-help { margin-top: 12px; font-size: 0.8rem; }
  /* Anchor the Clear button to the bottom-left corner of the chart
     canvas — visually below the y-axis 0 tick and left of the first
     x-axis label. */
  .pair-chart-wrap { position: relative; }
  .pair-clear-btn { position: absolute; left: 0; bottom: 0;
                    padding: 2px 10px; font-size: 0.75rem;
                    background: transparent; color: var(--text-dim);
                    border: 1px solid var(--border); border-radius: 4px;
                    cursor: pointer; font-family: inherit; z-index: 2; }
  .pair-clear-btn:hover { color: var(--text-bright);
                          border-color: var(--text-dim); }
  .pair-hits-heading { color: var(--text-bright); font-size: 0.9rem;
                       margin: 16px 0 8px; }
  .pair-hits-table { width: 100%; font-size: 0.85rem; }
  .pair-hits-table th, .pair-hits-table td { padding: 4px 8px;
                                              border-bottom: 1px solid var(--border); }
  .pair-hits-table th { color: var(--text-dim); font-weight: 600;
                        font-size: 0.75rem; text-transform: uppercase;
                        letter-spacing: 0.04em; text-align: left; }
  .pair-hits-table td.num, .pair-hits-table th.num { text-align: right; }
  /* Inner per-source detail table revealed when a source row is expanded.
     Slightly tighter than the outer table to read as a sub-list. */
  .pair-hits-detail-table { width: 100%; font-size: 0.8rem;
                            border-collapse: collapse; }
  .pair-hits-detail-table th, .pair-hits-detail-table td { padding: 3px 8px;
                                                            border-bottom: 1px solid var(--border); }
  .pair-hits-detail-table th { color: var(--text-dim); font-weight: 600;
                                font-size: 0.7rem; text-transform: uppercase;
                                letter-spacing: 0.04em; text-align: left; }
  .pair-hits-detail-table td.num, .pair-hits-detail-table th.num { text-align: right; }

  .source-breakdown { width: 100%; font-size: 0.85rem;
                      border-collapse: collapse; }
  .source-breakdown caption { color: var(--text-dim); font-size: 0.72rem;
                              text-transform: uppercase; letter-spacing: 0.06em;
                              text-align: left; padding: 0 0 8px; }
  .source-breakdown th { color: var(--text-dim); font-weight: 600;
                         font-size: 0.7rem; text-transform: uppercase;
                         letter-spacing: 0.04em; padding: 4px 8px;
                         text-align: left; }
  .source-breakdown td { padding: 4px 8px;
                         border-bottom: 1px solid var(--border); }
  .source-breakdown td.num, .source-breakdown th.num { text-align: right; }
  .source-row { cursor: pointer; }
  .source-row:hover td { background: var(--row-hover); }
  .source-row.active td { background: var(--row); color: var(--accent);
                          font-weight: 600; }

  /* Manual-encounter pin badge in the session table */
  .pin-badge { display: inline-block; margin-left: 6px;
               padding: 1px 6px; border-radius: 3px;
               font-size: 0.65rem; font-weight: 700; letter-spacing: 0.04em;
               text-transform: uppercase;
               background: rgba(96, 165, 250, 0.18); color: var(--accent); }

  /* Selection action bar above the session table. Only rendered when at
     least one encounter row is selected; the buttons themselves disable
     based on minimum-count rules (merge needs 2+, etc.). */
  .action-bar { display: flex; align-items: center; gap: 8px; padding: 10px 14px;
                margin-bottom: 12px; background: var(--row);
                border: 1px solid var(--border); border-radius: 6px;
                font-size: 0.9rem; }
  .action-bar .count { color: var(--text-bright); font-weight: 600;
                       margin-right: auto; }
  .action-bar .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .action-bar .help { color: var(--text-dim); font-size: 0.8rem; }

  /* Per-row checkbox column. The label fills the whole cell so anywhere
     in the cell area is a click target, not just the tiny native widget.
     Padding 0 on the cell + display:flex on the label expands the hit
     box to ~28px × full row height. */
  .check-cell { width: 36px; padding: 0; }
  .check-cell label.check-hit { display: flex; align-items: center;
                                justify-content: center;
                                width: 100%; height: 100%;
                                padding: 8px 10px; cursor: pointer;
                                margin: 0; }
  .check-cell input[type="checkbox"] { accent-color: var(--accent);
                                       cursor: pointer; pointer-events: none; }

  /* Pet-owner edit modal */
  .modal.pets-modal { max-width: 720px; }
  /* Header-action row hosts the [Save] [×] pair top-right. The close
     button keeps its existing static styling (font-size, color); we
     just override its absolute positioning so it lays out as a sibling
     of Save inside the flex container. */
  .pets-modal-actions { position: absolute; top: 8px; right: 12px;
                        display: flex; gap: 8px; align-items: center; }
  .pets-modal-actions .modal-close { position: static; top: auto;
                                     right: auto; }
  .pets-help { color: var(--text-dim); font-size: 0.85rem;
               line-height: 1.5; margin-bottom: 16px; }
  .pets-help code { background: var(--row); padding: 1px 6px;
                    border-radius: 3px; font-size: 0.85em; }
  .pets-table { width: 100%; font-size: 0.9rem;
                border-collapse: collapse; }
  .pets-table th { color: var(--text-dim); font-weight: 600;
                   font-size: 0.72rem; text-transform: uppercase;
                   letter-spacing: 0.04em; padding: 6px 8px;
                   text-align: left; border-bottom: 1px solid var(--border); }
  .pets-table td { padding: 8px; border-bottom: 1px solid var(--border); }
  .pets-table td.num { text-align: right;
                       font-variant-numeric: tabular-nums; }
  .pets-table .actor { color: var(--text-bright); }
  .pets-table .owner-input { background: var(--row); color: var(--text);
                             border: 1px solid var(--border);
                             padding: 4px 8px; border-radius: 4px;
                             font-family: inherit; font-size: 0.9rem;
                             width: 100%; }
  .pets-table .owner-input:focus { outline: none;
                                   border-color: var(--accent); }
  .pets-table .side-tag { display: inline-block; margin-left: 6px;
                          padding: 1px 6px; border-radius: 3px;
                          font-size: 0.65rem; font-weight: 700;
                          letter-spacing: 0.04em; text-transform: uppercase;
                          vertical-align: middle; }
  .pets-table .side-tag.friendly { background: rgba(52, 211, 153, 0.18);
                                   color: var(--good); }
  .pets-table .side-tag.enemy { background: rgba(248, 113, 113, 0.18);
                                color: var(--bad); }
  .pets-table .row-actions { white-space: nowrap; text-align: right; }
  .pets-table .row-actions .btn { font-size: 0.8rem;
                                  padding: 4px 10px; margin-left: 4px; }
  .pets-current-list { margin-top: 16px; font-size: 0.85rem; }
  .pets-current-list .sub { color: var(--text-dim); }

  /* Session-summary view: charts stacked on the left, rollup table on
     the right. Stack everything into a single column on narrow screens
     so the heatmap stays usable. `align-items: start` so each column
     sizes to its own content — without it, the grid stretches both
     columns to the row height (set by whichever side is taller),
     leaving the panel BG ending mid-table on the shorter side. */
  .ss-grid { display: grid;
             grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr);
             gap: 16px; margin-bottom: 20px; align-items: start; }
  .ss-charts { display: flex; flex-direction: column; gap: 16px;
               min-width: 0; }
  .ss-table-col { display: flex; flex-direction: column; gap: 16px;
                  min-width: 0; }
  @media (max-width: 1100px) { .ss-grid { grid-template-columns: 1fr; } }
  .ss-section-h { color: var(--text-bright); font-size: 1rem;
                  margin: 0 0 12px; font-weight: 600; }
  .ss-section-h .sub { color: var(--text-dim); font-weight: 400;
                       font-size: 0.85rem; margin-left: 6px; }

  /* Tanking sub-metric toggle (damage / healing / delta), shown beside
     the chart heading when the Tanking tab is active. */
  .ss-chart-head { display: flex; align-items: baseline;
                   justify-content: space-between; gap: 12px;
                   margin-bottom: 12px; flex-wrap: wrap; }
  .ss-chart-head .ss-section-h { margin: 0; }
  .ss-metric-toggle { display: inline-flex; gap: 0;
                      border: 1px solid var(--border); border-radius: 6px;
                      overflow: hidden; }
  .ss-metric-btn { background: transparent; border: 0;
                   border-right: 1px solid var(--border);
                   color: var(--text-dim); font-family: inherit;
                   font-size: 0.8rem; padding: 4px 12px; cursor: pointer; }
  .ss-metric-btn:last-child { border-right: 0; }
  .ss-metric-btn:hover { background: var(--row-hover);
                         color: var(--text-bright); }
  .ss-metric-btn.active { background: var(--accent); color: #fff; }

  /* Rollup table — narrower text, fewer fences so it fits in the right
     column without wrapping the numeric columns. The 8-column table
     can have a min-content width wider than the right grid column on
     narrow viewports / with long attacker names; the wrapper provides
     horizontal scroll instead of bleeding past the panel BG. */
  .ss-table-wrap { overflow-x: auto; }
  table.ss-table { width: 100%; border-collapse: separate;
                   border-spacing: 0; font-size: 0.85rem; }
  table.ss-table th, table.ss-table td { padding: 6px 8px;
                                          border-bottom: 1px solid var(--border); }
  table.ss-table th { color: var(--text-dim); font-weight: 600;
                      font-size: 0.72rem; text-transform: uppercase;
                      letter-spacing: 0.04em; text-align: left; }
  table.ss-table td.num, table.ss-table th.num { text-align: right;
                                                  font-variant-numeric: tabular-nums; }
  table.ss-table td.target { color: var(--text-bright); font-weight: 600; }
  /* Lock the Attacker column when the wrapper scrolls horizontally so
     the player name stays in view as the user reads across to the
     stats columns. Background matches the panel so scrolled content
     passes behind cleanly; the right-edge shadow distinguishes the
     pinned column from the rest. border-collapse must be `separate`
     for sticky borders to render properly — collapsed borders detach
     from sticky cells and ghost in place during scroll. */
  table.ss-table th:first-child,
  table.ss-table td:first-child { position: sticky; left: 0;
                                  background: var(--panel); z-index: 1;
                                  box-shadow: 1px 0 0 var(--border); }

  /* Heatmap — cells color-shaded by DPS magnitude. Sticky row/column
     headers so a wide grid stays navigable. Horizontal scroll wrapper
     keeps the rest of the layout from blowing up on raids with lots of
     encounters. */
  .ss-heatmap-wrap { overflow-x: auto; max-height: 60vh; }
  table.ss-heatmap { border-collapse: collapse; font-size: 0.75rem;
                     font-variant-numeric: tabular-nums; }
  table.ss-heatmap th, table.ss-heatmap td { padding: 4px 6px;
                                              border: 1px solid var(--border);
                                              text-align: center;
                                              white-space: nowrap; }
  th.ss-heatmap-col-h { color: var(--text-dim); font-size: 0.7rem;
                        background: var(--panel); position: sticky;
                        top: 0; z-index: 1; }
  th.ss-heatmap-row-h { color: var(--text-bright); font-size: 0.75rem;
                        text-align: left; padding-right: 10px;
                        background: var(--panel); position: sticky;
                        left: 0; z-index: 1; max-width: 160px;
                        overflow: hidden; text-overflow: ellipsis; }
  td.ss-heatmap-cell { color: #0f1419; font-weight: 600; cursor: pointer; }
  td.ss-heatmap-cell:hover { outline: 2px solid var(--accent);
                             outline-offset: -2px; }
  td.ss-heatmap-empty { color: var(--border); cursor: pointer;
                        background: transparent; }
  td.ss-heatmap-empty:hover { background: var(--row-hover); }

  /* Damage/Healing tabs above encounter detail content */
  .tabs { display: flex; gap: 4px; margin: 8px 0 16px;
          border-bottom: 1px solid var(--border); }
  .tabs .tab { background: transparent; border: none; color: var(--text-dim);
               padding: 10px 18px; cursor: pointer; font-family: inherit;
               font-size: 0.9rem; border-bottom: 2px solid transparent;
               margin-bottom: -1px; transition: color 0.1s, border-color 0.1s; }
  .tabs .tab:hover { color: var(--text); }
  .tabs .tab.active { color: var(--text-bright); border-bottom-color: var(--accent); }

  /* Drop-anywhere overlay shown while a file is dragged over the page */
  /* Upload progress UI shown while bytes stream to /api/upload and the
     server parses the log. The bar fills 0–100% during the byte transfer,
     then the label flips to "Parsing log…" while we wait on the response. */
  .upload-status { max-width: 480px; margin: 24px auto; padding: 20px;
                   background: var(--panel); border: 1px solid var(--border);
                   border-radius: 8px; }
  .upload-label { color: var(--text); margin-bottom: 12px; font-size: 0.95rem; }
  .upload-label strong { color: var(--text-bright); }
  .progress-track { width: 100%; height: 8px; background: var(--row);
                    border-radius: 4px; overflow: hidden; }
  .progress-fill { height: 100%; background: var(--accent);
                   transition: width 0.1s linear; }
  .upload-pct { margin-top: 8px; font-size: 0.8rem; }

  .drop-overlay { position: fixed; inset: 16px; pointer-events: none;
                  border: 3px dashed var(--accent); border-radius: 12px;
                  background: rgba(96, 165, 250, 0.08);
                  display: flex; align-items: center; justify-content: center;
                  font-size: 1.4rem; color: var(--text-bright);
                  z-index: 999; }
  .drop-overlay .hint { background: var(--panel); padding: 16px 24px;
                        border-radius: 8px; border: 1px solid var(--border);
                        text-align: center; }
  .drop-overlay .hint .sub { font-size: 0.85rem; color: var(--text-dim);
                             margin-top: 4px; }

  .panel {
    background: var(--panel); border-radius: 8px; padding: 20px;
    margin-bottom: 20px;
  }
  .summary-grid {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px;
  }
  .summary-grid .stat {
    background: var(--row); border-radius: 6px; padding: 12px 16px;
  }
  .summary-grid .stat .label {
    color: var(--text-dim); font-size: 0.75rem;
    text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px;
  }
  .summary-grid .stat .value {
    color: var(--text-bright); font-size: 1.3rem; font-weight: 600;
    font-variant-numeric: tabular-nums;
  }

  h2 { color: var(--text-bright); font-size: 1.1rem; margin: 24px 0 12px; }
  .specials { color: var(--warn); font-size: 0.85rem; }
  .err { color: var(--bad); padding: 12px; }

  /* Compact summary line above the chart */
  .chart-wrap { background: var(--panel); border-radius: 8px;
                padding: 20px; margin-bottom: 20px; }

  /* Header layout: title left, actions right */
  .header-row { display: flex; justify-content: space-between;
                align-items: flex-end; gap: 12px; flex-wrap: wrap; }
  .header-row .actions { display: flex; gap: 8px; }
  .btn {
    background: var(--row); color: var(--text); border: 1px solid var(--border);
    padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 0.85rem;
    font-family: inherit;
  }
  .btn:hover { background: var(--row-hover); border-color: var(--accent); }
  .btn.primary { background: var(--accent); color: #0f1419; border-color: var(--accent); font-weight: 600; }
  .btn.primary:hover { filter: brightness(1.1); }

  /* File picker */
  .picker-options { display: flex; align-items: center; gap: 14px;
                    margin-bottom: 12px; padding: 10px 14px;
                    background: var(--row);
                    border: 1px solid var(--border); border-radius: 6px; }
  .picker-options label { display: flex; flex-direction: column; gap: 4px;
                          font-size: 0.75rem; color: var(--text-dim);
                          text-transform: uppercase; letter-spacing: 0.04em; }
  .picker-options input[type="number"] {
                          background: var(--bg); color: var(--text);
                          border: 1px solid var(--border); padding: 6px 10px;
                          border-radius: 4px; font-size: 0.9rem; width: 100px;
                          font-variant-numeric: tabular-nums; font-family: inherit; }
  .picker-options input:focus { outline: none; border-color: var(--accent); }
  .picker-options-help { font-size: 0.8rem; flex: 1; line-height: 1.45; }
  .picker-path {
    font-family: ui-monospace, 'SF Mono', Consolas, monospace;
    font-size: 0.85rem; color: var(--text-dim); padding: 6px 10px;
    background: var(--row); border-radius: 4px; margin-bottom: 10px;
    word-break: break-all;
  }
  .picker-input-row {
    display: flex; gap: 8px; margin-bottom: 16px;
  }
  .picker-input-row input {
    flex: 1; background: var(--row); color: var(--text);
    border: 1px solid var(--border); padding: 8px 12px; border-radius: 4px;
    font-family: ui-monospace, 'SF Mono', Consolas, monospace; font-size: 0.9rem;
  }
  .picker-input-row input:focus { outline: none; border-color: var(--accent); }
  .type-tag {
    display: inline-block; padding: 1px 6px; border-radius: 3px;
    font-size: 0.7rem; font-weight: 700; letter-spacing: 0.04em;
    text-transform: uppercase; margin-right: 8px;
  }
  .type-tag.dir { background: var(--border); color: var(--text-dim); }
  .type-tag.log { background: var(--accent); color: #0f1419; }
  .picker-empty { color: var(--text-dim); padding: 20px; text-align: center; }
</style>
</head>
<body>

<header>
  <div class="header-row">
    <div>
      <h1>
        <span class="brand">Flurry</span>
        <span class="sep">·</span>
        <span id="title">…</span>
      </h1>
      <div class="sub" id="sub">Loading…</div>
    </div>
    <div class="actions" id="actions"></div>
  </div>
</header>

<main id="app"><div class="sub">Loading…</div></main>

<script>
const NUM = n => n == null ? '—' : n.toLocaleString();
const SHORT = n => {
  if (n == null) return '—';
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
  if (n >= 1_000) return (n / 1_000).toFixed(0) + 'k';
  return String(n);
};
const FMT_DUR = s => {
  if (s == null) return '—';
  if (s < 60) return Math.round(s) + 's';
  const m = Math.floor(s / 60), r = Math.round(s % 60);
  return `${m}m${String(r).padStart(2, '0')}s`;
};

const COLORS = [
  '#60a5fa', '#34d399', '#fbbf24', '#f87171',
  '#a78bfa', '#22d3ee', '#fb923c', '#facc15', '#94a3b8'
];

let chartInstance = null;
let sessionSort = { key: 'encounter_id', dir: 'desc' };
// Selected encounter ids in the session table. Persists across sort
// re-renders (the user can sort while keeping their selection) but is
// cleared on a fresh `renderSession` since ids may have shifted.
let sessionSelected = new Set();

async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

// --- Parse-progress polling ------------------------------------------
//
// `/api/parse-status` reports the current parse_progress dict. While a
// long log walk is happening in another request handler thread, callers
// poll this and reflect bytes_read/total_bytes onto a progress UI. The
// poll stops as soon as the calling action (the request whose handler
// triggered the parse) resolves — we don't try to detect 'done' from
// the status alone because the cache might be filled by a still-in-
// flight request whose response hasn't propagated yet.

function fmtMB(bytes) {
  if (bytes == null || bytes <= 0) return '0 MB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function parseProgressHTML(s, headline = 'Parsing log…') {
  const pct = (s && s.state === 'parsing') ? s.pct : 0;
  const sizeNote = (s && s.total_bytes > 0)
    ? `<strong>${fmtMB(s.bytes_read)}</strong> / ${fmtMB(s.total_bytes)}`
    : '';
  const pctNote = (s && s.state === 'parsing') ? `${pct.toFixed(1)}%` : '';
  return `
    <div class="upload-status">
      <div class="upload-label">${headline} ${sizeNote}</div>
      <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
      <div class="upload-pct sub">${pctNote}</div>
    </div>`;
}

// Start a poller that calls onTick(status) every interval. Returns a
// stop() function. First tick fires immediately (the await in the
// initial fetch yields control, so the caller's own fetch can still
// race the status fetch).
function startParsePoll(onTick, intervalMs = 250) {
  let stopped = false;
  let timer = null;
  async function tick() {
    if (stopped) return;
    try {
      const r = await fetch('/api/parse-status');
      if (r.ok) {
        const s = await r.json();
        if (!stopped) onTick(s);
      }
    } catch (e) { /* network blip — keep polling */ }
    if (!stopped) timer = setTimeout(tick, intervalMs);
  }
  tick();
  return () => { stopped = true; if (timer) clearTimeout(timer); };
}

// Show parse-progress UI in `app` while `promiseFactory()` runs. The
// progress overlay is only swapped in if the server reports
// state==='parsing' — a warm cache shows nothing, no flicker.
async function withParseProgress(promiseFactory, app, headline) {
  const placeholder = `<div class="sub">Loading…</div>`;
  app.innerHTML = placeholder;
  let showingProgress = false;
  const stop = startParsePoll(s => {
    if (s.state === 'parsing') {
      app.innerHTML = parseProgressHTML(s, headline);
      showingProgress = true;
    }
  });
  try {
    return await promiseFactory();
  } finally {
    stop();
    // If we did show the parse UI, leave it there for the caller's
    // post-fetch swap. If we didn't, the placeholder is unchanged.
    void showingProgress;
  }
}

function setHeader(title, sub, hasLog) {
  document.getElementById('title').textContent = title;
  document.getElementById('sub').textContent = sub;
  // Action button: only show "Change log" when a log is loaded.
  const actions = document.getElementById('actions');
  actions.innerHTML = '';
  if (hasLog) {
    const refresh = document.createElement('button');
    refresh.className = 'btn';
    refresh.textContent = 'Refresh';
    refresh.title = 'Re-read the log file (picks up new fights)';
    refresh.addEventListener('click', refreshLog);
    actions.appendChild(refresh);

    const change = document.createElement('button');
    change.className = 'btn';
    change.textContent = 'Change log';
    change.addEventListener('click', () => { location.hash = '#/picker'; });
    actions.appendChild(change);
  }
}

async function refreshLog() {
  const app = document.getElementById('app');
  app.innerHTML = '<div class="sub">Reloading log…</div>';
  try {
    const r = await fetch('/api/reload', { method: 'POST' });
    if (!r.ok) {
      const txt = await r.text();
      app.innerHTML = `<div class="err">Failed to reload: ${escapeHTML(txt)}</div>`;
      return;
    }
    // Re-render whatever view we were on. Stays on a fight detail page if
    // that's where the user clicked Refresh — fight_ids are stable for
    // already-detected fights when the log is appended to.
    route();
  } catch (e) {
    app.innerHTML = `<div class="err">Failed to reload: ${e.message}</div>`;
  }
}

function fmtSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + ' KB';
  if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB';
  return (bytes / 1024 / 1024 / 1024).toFixed(2) + ' GB';
}

function fmtMtime(unixSeconds) {
  const d = new Date(unixSeconds * 1000);
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ` +
         `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// --- File picker view -------------------------------------------------

async function renderPicker(path) {
  if (chartInstance) { chartInstance.destroy(); chartInstance = null; }
  const app = document.getElementById('app');
  app.innerHTML = '<div class="sub">Loading…</div>';
  setHeader('Open log',
            'Browse, paste a path, or drag a log file anywhere on the page',
            false);

  // Fetch current params alongside the dir listing so the "Last N hours"
  // input can prefill — the picker is the right place to set the slice
  // BEFORE the initial parse, not after it.
  const url = path ? `/api/browse?path=${encodeURIComponent(path)}` : '/api/browse';
  let data, sessionParams;
  try {
    [data, sessionParams] = await Promise.all([
      fetchJSON(url),
      fetchJSON('/api/session').then(s => s.params || {}).catch(() => ({})),
    ]);
  } catch (e) {
    app.innerHTML = `<div class="err">Could not list ${escapeHTML(path || '')}: ${e.message}</div>` +
                    pickerOptionsHTML({since_hours: 0}) +
                    pickerInputHTML('');
    wirePickerInput();
    wirePickerOptions();
    return;
  }

  const parentLink = data.parent
    ? `<a href="#" data-go="${escapeHTML(data.parent)}">↑ ${escapeHTML(data.parent)}</a>`
    : '<span class="sub">(filesystem root)</span>';

  const dirRows = data.dirs.map(name => `
    <tr class="fight-row" data-go="${escapeHTML(joinPath(data.path, name))}">
      <td><span class="type-tag dir">DIR</span>${escapeHTML(name)}</td>
      <td></td><td></td>
    </tr>`).join('');

  const fileRows = data.files.map(f => `
    <tr class="fight-row" data-open="${escapeHTML(f.path)}">
      <td><span class="type-tag log">LOG</span>${escapeHTML(f.name)}</td>
      <td class="num">${fmtSize(f.size)}</td>
      <td class="num">${fmtMtime(f.mtime)}</td>
    </tr>`).join('');

  const empty = (data.dirs.length === 0 && data.files.length === 0)
    ? '<div class="picker-empty">Nothing matching <code>eqlog_*.txt</code> here. ' +
      'Navigate up or paste a path above.</div>'
    : '';

  app.innerHTML = `
    <div class="panel">
      <div class="picker-path">${escapeHTML(data.path)}</div>
      <div style="margin-bottom: 12px;">${parentLink}</div>
      ${pickerOptionsHTML(sessionParams)}
      ${pickerInputHTML(data.path)}
      ${(dirRows || fileRows) ? `
        <table>
          <thead><tr>
            <th>Name</th><th class="num">Size</th><th class="num">Modified</th>
          </tr></thead>
          <tbody>${dirRows}${fileRows}</tbody>
        </table>` : ''}
      ${empty}
    </div>`;

  // Wire dir-navigation links.
  app.querySelectorAll('[data-go]').forEach(el => {
    el.addEventListener('click', e => {
      e.preventDefault();
      renderPicker(el.dataset.go);
    });
  });
  // Wire file-open clicks.
  app.querySelectorAll('[data-open]').forEach(el => {
    el.addEventListener('click', () => openLog(el.dataset.open));
  });
  wirePickerInput();
  wirePickerOptions();
}

function pickerOptionsHTML(params) {
  // Pre-parse knobs the user can set before opening a log. Only
  // since_hours for now — others (gap, min damage, etc.) are easier to
  // tune iteratively from the params panel after a first load.
  const sinceHours = (params && typeof params.since_hours === 'number')
    ? params.since_hours : 0;
  return `
    <div class="picker-options">
      <label>Last N hours
        <input type="number" id="picker-since-hours" min="0" step="1"
               value="${sinceHours}"
               title="Analyze only the last N hours of log activity, anchored to the log's last timestamp. 0 = whole log. Big speedup on multi-day logs.">
      </label>
      <span class="sub picker-options-help">
        Set this before opening a long log to skip parsing the prefix —
        a 24h window on a multi-day log can be 10× faster.
      </span>
    </div>`;
}

function wirePickerOptions() {
  const sinceInput = document.getElementById('picker-since-hours');
  if (!sinceInput) return;
  // POST to /api/params on commit (Enter, blur, or step click). We use
  // 'change' rather than 'input' so we don't spam the server on every
  // keystroke — and so the value is final by the time we navigate.
  sinceInput.addEventListener('change', async () => {
    const v = parseInt(sinceInput.value, 10);
    if (Number.isNaN(v) || v < 0) {
      sinceInput.value = 0;
      return;
    }
    try {
      await fetch('/api/params', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({since_hours: v}),
      });
    } catch (e) { /* leave value, error surfaces on next action */ }
  });
}

function pickerInputHTML(currentPath) {
  return `
    <div class="picker-input-row">
      <input id="picker-input" placeholder="Or paste a full path…"
             value="${escapeHTML(currentPath)}">
      <button class="btn" id="picker-go">Go</button>
      <button class="btn primary" id="picker-open">Open as log</button>
      <button class="btn" id="picker-upload"
              title="Pick a log file via the OS file dialog. The file is copied to a temp dir (browsers don't expose disk paths to JS).">Upload…</button>
      <input type="file" id="picker-upload-input" accept=".txt,.log,.*"
             style="display:none">
    </div>`;
}

function wirePickerInput() {
  const input = document.getElementById('picker-input');
  const go = document.getElementById('picker-go');
  const open = document.getElementById('picker-open');
  const uploadBtn = document.getElementById('picker-upload');
  const uploadInput = document.getElementById('picker-upload-input');
  if (!input) return;
  go.addEventListener('click', () => renderPicker(input.value));
  open.addEventListener('click', () => openLog(input.value));
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') renderPicker(input.value);
  });
  uploadBtn.addEventListener('click', () => uploadInput.click());
  uploadInput.addEventListener('change', () => {
    if (uploadInput.files.length > 0) uploadLog(uploadInput.files[0]);
  });
}

function joinPath(parent, name) {
  // Pick a separator that matches the OS based on what the server returned.
  const sep = parent.includes('\\') ? '\\' : '/';
  if (parent.endsWith(sep)) return parent + name;
  return parent + sep + name;
}

async function openLog(path) {
  if (!path) return;
  const app = document.getElementById('app');
  // /api/open triggers the first parse synchronously inside the request
  // handler (its response includes the encounter list). Wrap the fetch
  // with parse-progress polling so the bar shows over the picker UI
  // while the parse is running on the server-side handler thread.
  try {
    const r = await withParseProgress(
      () => fetch('/api/open', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path}),
      }),
      app, 'Loading log…');
    if (!r.ok) {
      const txt = await r.text();
      app.innerHTML = `<div class="err">Failed to open: ${escapeHTML(txt)}</div>`;
      return;
    }
    location.hash = '#/';
    // hashchange may not fire if we were already on '#/'; re-route explicitly.
    route();
  } catch (e) {
    app.innerHTML = `<div class="err">Failed to open: ${e.message}</div>`;
  }
}

function compareFights(a, b, key, dir) {
  let av = a[key], bv = b[key];
  if (av == null) av = '';
  if (bv == null) bv = '';
  let cmp;
  if (typeof av === 'number' && typeof bv === 'number') {
    cmp = av - bv;
  } else if (typeof av === 'boolean' || typeof bv === 'boolean') {
    cmp = (av === bv) ? 0 : (av ? 1 : -1);
  } else {
    cmp = String(av).localeCompare(String(bv));
  }
  return dir === 'desc' ? -cmp : cmp;
}

// --- Session view -----------------------------------------------------

async function renderSession() {
  if (chartInstance) { chartInstance.destroy(); chartInstance = null; }
  const app = document.getElementById('app');

  let data;
  try {
    data = await withParseProgress(
      () => fetchJSON('/api/session'), app, 'Parsing log…');
  } catch (e) {
    app.innerHTML = `<div class="err">Failed to load session: ${e.message}</div>`;
    return;
  }

  // No log loaded → bounce straight to the picker.
  if (data.logfile === null) {
    location.hash = '#/picker';
    return;
  }

  const sinceLabel = data.params.since_hours > 0
    ? `last ${data.params.since_hours}h`
    : 'whole log';
  setHeader(data.logfile_basename,
            `${sinceLabel} · min between fights ${data.params.gap_seconds}s · min between encounters ${data.params.encounter_gap_seconds}s · min damage ${NUM(data.params.min_damage)}`,
            true);
  // Add a "Session summary" button to the session-view header. setHeader
  // already laid out Refresh + Change log; we append after them so the
  // primary nav stays leftmost. The button's label and target hash
  // adapt to the current selection — refreshSummaryBtn (defined below)
  // is called from refreshActionBar() whenever sessionSelected changes
  // so the user always sees what scope they're about to load.
  const sessActions = document.getElementById('actions');
  if (sessActions) {
    const summaryBtn = document.createElement('button');
    summaryBtn.className = 'btn';
    summaryBtn.id = 'summary-btn';
    summaryBtn.addEventListener('click', () => {
      // Re-read sessionSelected at click time so we always honor the
      // latest selection, even if refreshSummaryBtn somehow lagged.
      if (sessionSelected.size > 0) {
        const ids = Array.from(sessionSelected).join(',');
        location.hash = `#/session-summary?ids=${ids}`;
      } else {
        location.hash = '#/session-summary';
      }
    });
    sessActions.appendChild(summaryBtn);
  }

  const s = data.summary;
  const summaryHTML = `
    <div class="panel">
      <div class="summary-grid">
        <div class="stat"><div class="label">Encounters</div>
          <div class="value">${s.total_encounters}</div></div>
        <div class="stat"><div class="label">Killed</div>
          <div class="value">${s.total_killed}</div></div>
        <div class="stat"><div class="label">Total damage</div>
          <div class="value">${NUM(s.total_damage)}</div></div>
        <div class="stat"><div class="label">Log</div>
          <div class="value" style="font-size:0.95rem;word-break:break-all">${data.logfile_basename}</div></div>
      </div>
    </div>`;

  const paramsHTML = `
    <div class="panel">
      <div class="params-help sub">
        A <strong>fight</strong> is one slice of combat against a single mob name —
        a boss and its adds become separate fights. An <strong>encounter</strong>
        bundles overlapping or adjacent fights back into one logical engagement
        (boss + adds = one encounter).
      </div>
      <div class="params-row">
        <label>Min time between fights (s)
          <input type="number" id="param-gap" min="0" step="1"
                 value="${data.params.gap_seconds}"
                 title="Combat separated by at least this many seconds of inactivity becomes two fights (default 15). Lower splits aggressively; higher keeps lulls inside one fight.">
        </label>
        <label>Min damage
          <input type="number" id="param-min-damage" min="0" step="1000"
                 value="${data.params.min_damage}"
                 title="Drop fights below this total damage threshold">
        </label>
        <label>Min duration (s)
          <input type="number" id="param-min-duration" min="0" step="1"
                 value="${data.params.min_duration_seconds}"
                 title="Drop fights shorter than this many seconds">
        </label>
        <label>Min time between encounters (s)
          <input type="number" id="param-encounter-gap" min="0" step="1"
                 value="${data.params.encounter_gap_seconds}"
                 title="Adjacent fights separated by less than this much downtime stay in the same encounter (default 10, 0 = strict overlap only).">
        </label>
        <label>Last N hours
          <input type="number" id="param-since-hours" min="0" step="1"
                 value="${data.params.since_hours}"
                 title="Analyze only the last N hours of log activity, anchored to the log's last timestamp. 0 = whole log (default). Big speedup on multi-day logs.">
        </label>
        <label class="check"
               title="When on, heal events count as combat activity and keep in-progress fights alive across no-damage gaps. Heals outside any fight still don't open new ones.">
          <input type="checkbox" id="param-heals-extend"
                 ${data.params.heals_extend_fights ? 'checked' : ''}>
          Heals extend fights
        </label>
        <button class="btn primary" id="param-apply">Apply</button>
        <span id="param-msg" class="err-msg"></span>
      </div>
    </div>`;

  if (data.encounters.length === 0) {
    app.innerHTML = summaryHTML + paramsHTML +
      `<div class="panel sub">No encounters detected. Try lowering Min damage above.</div>`;
    wireParamsPanel();
    return;
  }

  // Sortable columns. `defaultDir` is the direction applied on the first
  // click of the column; clicking the active column flips it.
  const COLS = [
    { key: 'encounter_id',     label: '#',         num: true,  defaultDir: 'desc' },
    { key: 'start',            label: 'Start',     num: false, defaultDir: 'desc' },
    { key: 'duration_seconds', label: 'Dur',       num: true,  defaultDir: 'desc' },
    { key: 'name',             label: 'Target',    num: false, defaultDir: 'asc'  },
    { key: 'total_damage',     label: 'Damage',    num: true,  defaultDir: 'desc' },
    { key: 'raid_dps',         label: 'Raid DPS',  num: true,  defaultDir: 'desc' },
    { key: 'attacker_count',   label: 'Attackers', num: true,  defaultDir: 'desc' },
    { key: 'fight_complete',   label: 'Status',    num: false, defaultDir: 'desc' },
  ];

  // Drop any selections that no longer correspond to a current encounter
  // id. New session payloads (param change, log switch, manual edit) can
  // shift ids around, so stale selections shouldn't trigger merge/split
  // against unrelated encounters.
  const validIds = new Set(data.encounters.map(e => e.encounter_id));
  for (const id of Array.from(sessionSelected)) {
    if (!validIds.has(id)) sessionSelected.delete(id);
  }

  function renderActionBar() {
    const n = sessionSelected.size;
    if (n === 0) return '';
    const mergeDisabled = n < 2 ? ' disabled' : '';
    return `
      <div class="action-bar" id="action-bar">
        <span class="count">${n} selected</span>
        <button class="btn primary" id="act-merge"${mergeDisabled}
                title="Combine the selected encounters into one user-pinned encounter.">Merge</button>
        <button class="btn" id="act-split"
                title="Remove these encounters from any manual groupings, returning them to auto-grouped state.">Split</button>
        <button class="btn" id="act-clear"
                title="Clear the selection.">Clear</button>
      </div>`;
  }

  function refreshActionBar() {
    const slot = document.getElementById('action-bar-slot');
    if (slot) slot.innerHTML = renderActionBar();
    wireActionBar();
    refreshSummaryBtn();
  }

  // Sync the header's Session summary button label/title to the
  // current selection. With nothing checked: "Session summary" + whole
  // log. With N checked: "Session summary (N selected)" + scoped to
  // those — same button, different scope.
  function refreshSummaryBtn() {
    const btn = document.getElementById('summary-btn');
    if (!btn) return;
    const n = sessionSelected.size;
    if (n > 0) {
      btn.textContent = `Session summary (${n} selected)`;
      btn.title = `Per-attacker rollup scoped to the ${n} selected encounter${n === 1 ? '' : 's'}. Clear selection to summarize the whole log.`;
      btn.classList.add('primary');
    } else {
      btn.textContent = 'Session summary';
      btn.title = 'Per-attacker rollup across every encounter — total/avg/median/p95 DPS, plus a trend chart and attacker × encounter heatmap. Tick rows to scope to a subset.';
      btn.classList.remove('primary');
    }
  }

  function renderTablePanel() {
    const sorted = data.encounters.slice().sort((a, b) =>
      compareFights(a, b, sessionSort.key, sessionSort.dir));

    const headerHTML =
      `<th class="check-cell">
         <label class="check-hit" title="Toggle all encounters">
           <input type="checkbox" id="check-all">
         </label>
       </th>` +
      COLS.map(c => {
        const arrow = sessionSort.key === c.key
          ? `<span class="sort-arrow">${sessionSort.dir === 'desc' ? '▼' : '▲'}</span>`
          : '';
        return `<th class="sortable${c.num ? ' num' : ''}" data-key="${c.key}">${c.label}${arrow}</th>`;
      }).join('');

    const rows = sorted.map(e => {
      const checked = sessionSelected.has(e.encounter_id) ? ' checked' : '';
      const pin = e.is_manual ? '<span class="pin-badge" title="User-pinned encounter">★ pinned</span>' : '';
      return `
      <tr class="fight-row" data-id="${e.encounter_id}">
        <td class="check-cell">
          <label class="check-hit">
            <input type="checkbox" class="row-check"
                   data-id="${e.encounter_id}"${checked}>
          </label>
        </td>
        <td class="num">${e.encounter_id}</td>
        <td>${e.start}</td>
        <td class="num">${FMT_DUR(e.duration_seconds)}</td>
        <td class="target">${escapeHTML(e.name)}${pin}</td>
        <td class="num">${NUM(e.total_damage)}</td>
        <td class="num">${NUM(e.raid_dps)}</td>
        <td class="num">${e.attacker_count}</td>
        <td class="status ${e.fight_complete ? 'killed' : 'incomplete'}">
          ${e.fight_complete ? 'Killed' : 'Incomplete'}</td>
      </tr>`;
    }).join('');

    return `
      <div class="panel" id="fight-table-panel">
        <table>
          <thead><tr>${headerHTML}</tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  function wireTablePanel() {
    app.querySelectorAll('th.sortable').forEach(th => {
      th.addEventListener('click', () => {
        const key = th.dataset.key;
        if (sessionSort.key === key) {
          sessionSort.dir = sessionSort.dir === 'desc' ? 'asc' : 'desc';
        } else {
          const col = COLS.find(c => c.key === key);
          sessionSort = { key, dir: col.defaultDir };
        }
        const panel = document.getElementById('fight-table-panel');
        if (panel) {
          panel.outerHTML = renderTablePanel();
          wireTablePanel();
        }
      });
    });

    // Stop propagation on the cell-spanning label so clicks anywhere
    // in the check-cell toggle the checkbox without also triggering
    // row-click navigation. The label's `for`-less wrapping of the
    // input makes the input toggle automatically; pointer-events on
    // the input are disabled in CSS so all clicks land on the label.
    app.querySelectorAll('td.check-cell').forEach(td => {
      td.addEventListener('click', ev => ev.stopPropagation());
    });
    app.querySelectorAll('input.row-check').forEach(cb => {
      cb.addEventListener('change', () => {
        const id = parseInt(cb.dataset.id, 10);
        if (cb.checked) sessionSelected.add(id);
        else sessionSelected.delete(id);
        refreshActionBar();
        syncCheckAll();
      });
    });

    // Header checkbox: select-all/none of the *currently visible* rows.
    const checkAll = document.getElementById('check-all');
    if (checkAll) {
      syncCheckAll();
      checkAll.addEventListener('change', () => {
        app.querySelectorAll('input.row-check').forEach(cb => {
          const id = parseInt(cb.dataset.id, 10);
          cb.checked = checkAll.checked;
          if (checkAll.checked) sessionSelected.add(id);
          else sessionSelected.delete(id);
        });
        refreshActionBar();
      });
    }

    // Row click navigates — but only when the click didn't originate on
    // the checkbox cell, which has its own stopPropagation handler.
    app.querySelectorAll('tr.fight-row').forEach(tr => {
      tr.addEventListener('click', () => {
        location.hash = `#/encounter/${tr.dataset.id}`;
      });
    });
  }

  function syncCheckAll() {
    const checkAll = document.getElementById('check-all');
    if (!checkAll) return;
    const rowChecks = app.querySelectorAll('input.row-check');
    if (rowChecks.length === 0) {
      checkAll.checked = false;
      checkAll.indeterminate = false;
      return;
    }
    const checked = Array.from(rowChecks).filter(cb => cb.checked).length;
    checkAll.checked = checked === rowChecks.length;
    checkAll.indeterminate = checked > 0 && checked < rowChecks.length;
  }

  function wireActionBar() {
    const merge = document.getElementById('act-merge');
    const split = document.getElementById('act-split');
    const clear = document.getElementById('act-clear');
    if (clear) clear.addEventListener('click', () => {
      sessionSelected.clear();
      app.querySelectorAll('input.row-check').forEach(cb => { cb.checked = false; });
      refreshActionBar();
      syncCheckAll();
    });
    if (merge) merge.addEventListener('click', () => postEncounterAction('merge'));
    if (split) split.addEventListener('click', () => postEncounterAction('split'));
  }

  app.innerHTML = summaryHTML + paramsHTML +
    `<div id="action-bar-slot">${renderActionBar()}</div>` +
    renderTablePanel();
  wireParamsPanel();
  wireTablePanel();
  wireActionBar();
  refreshSummaryBtn();
}

async function postEncounterAction(action) {
  const ids = Array.from(sessionSelected);
  if (ids.length === 0) return;
  if (action === 'merge' && ids.length < 2) return;
  const merge = document.getElementById('act-merge');
  const split = document.getElementById('act-split');
  // Disable both buttons while the request is in flight so a double-click
  // doesn't double-submit. The full re-render at the end resets state.
  if (merge) merge.disabled = true;
  if (split) split.disabled = true;
  try {
    const r = await fetch('/api/encounters', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, encounter_ids: ids}),
    });
    if (!r.ok) {
      const txt = await r.text();
      alert(`${action} failed: ${txt}`);
      return;
    }
    // Encounter ids shift after merge/split, so wipe the selection and
    // do a full reroute instead of a partial in-place update.
    sessionSelected.clear();
    location.hash = '#/';
    route();
  } catch (e) {
    alert(`${action} failed: ${e.message}`);
  } finally {
    if (merge) merge.disabled = false;
    if (split) split.disabled = false;
  }
}

async function wireParamsPanel() {
  // Idempotent: called from each renderSession() pass.
  const apply = document.getElementById('param-apply');
  if (!apply) return;
  apply.addEventListener('click', async () => {
    const msg = document.getElementById('param-msg');
    msg.textContent = '';
    const ints = {
      gap_seconds: parseInt(document.getElementById('param-gap').value, 10),
      min_damage: parseInt(document.getElementById('param-min-damage').value, 10),
      min_duration_seconds: parseInt(document.getElementById('param-min-duration').value, 10),
      encounter_gap_seconds: parseInt(document.getElementById('param-encounter-gap').value, 10),
      since_hours: parseInt(document.getElementById('param-since-hours').value, 10),
    };
    if (Object.values(ints).some(v => Number.isNaN(v) || v < 0)) {
      msg.textContent = 'Values must be non-negative integers.';
      return;
    }
    const body = {
      ...ints,
      heals_extend_fights: document.getElementById('param-heals-extend').checked,
    };
    apply.disabled = true;
    apply.textContent = 'Applying…';
    try {
      const r = await fetch('/api/params', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const txt = await r.text();
        msg.textContent = `Failed: ${txt}`;
        return;
      }
      // Reset to session list — encounter ids may have shifted under the
      // new params, so a stale #/encounter/<id> URL would land on the
      // wrong encounter or 404.
      location.hash = '#/';
      route();
    } catch (e) {
      msg.textContent = `Failed: ${e.message}`;
    } finally {
      apply.disabled = false;
      apply.textContent = 'Apply';
    }
  });
}

// --- Parser-coverage debug view --------------------------------------

async function renderDebug() {
  if (chartInstance) { chartInstance.destroy(); chartInstance = null; }
  const app = document.getElementById('app');

  let data;
  try {
    data = await withParseProgress(
      () => fetchJSON('/api/debug'), app, 'Walking log…');
  } catch (e) {
    app.innerHTML = `<div class="err">Failed to load debug stats: ${e.message}</div>`;
    return;
  }

  setHeader('Parser coverage',
            `${NUM(data.total_lines)} lines · ${NUM(data.unknown_total_lines)} unparsed`,
            true);

  // By-type counts as a small panel of stats. Sort descending so the most
  // common event types lead.
  const typeRows = Object.entries(data.by_type)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `
      <tr>
        <td class="target">${escapeHTML(k)}</td>
        <td class="num">${NUM(v)}</td>
      </tr>`).join('');

  const typeHTML = `
    <h2>By event type</h2>
    <div class="panel">
      <table>
        <thead><tr>
          <th>Type</th><th class="num">Count</th>
        </tr></thead>
        <tbody>${typeRows}
          <tr>
            <td class="sub">(no timestamp — skipped)</td>
            <td class="num">${NUM(data.no_timestamp)}</td>
          </tr>
        </tbody>
      </table>
    </div>`;

  // Unknown groups. The "shape" is the body with digits replaced by N so
  // similar lines (e.g. DoT ticks differing only by damage) collapse.
  // We show the verbatim example because that's what's actually useful
  // when writing a new regex.
  const unknownHTML = data.unknown_groups.length === 0
    ? '<div class="panel sub">No unknown lines — every timestamped body matched some pattern.</div>'
    : `
    <h2>Unknown line shapes (${NUM(data.unknown_total_groups)} distinct, top ${data.unknown_groups.length})</h2>
    <div class="panel">
      <table>
        <thead><tr>
          <th class="num">Count</th><th>Example</th>
        </tr></thead>
        <tbody>${data.unknown_groups.map(g => `
          <tr>
            <td class="num">${NUM(g.count)}</td>
            <td><code style="white-space:pre-wrap">${escapeHTML(g.example)}</code></td>
          </tr>`).join('')}
        </tbody>
      </table>
    </div>`;

  app.innerHTML = `<a href="#/" class="back">← back</a>` + typeHTML + unknownHTML;
}

// --- Encounter detail view --------------------------------------------

async function renderEncounter(id) {
  if (chartInstance) { chartInstance.destroy(); chartInstance = null; }
  const app = document.getElementById('app');

  let f;
  try {
    f = await withParseProgress(
      () => fetchJSON(`/api/encounter/${id}`), app, 'Parsing log…');
  } catch (e) {
    app.innerHTML = `<div class="err">Failed to load encounter ${id}: ${e.message}</div>`;
    return;
  }

  setHeader(`#${f.encounter_id} · ${f.name || f.target}`,
            `${f.start} → ${f.end} · ${FMT_DUR(f.duration_seconds)} · ` +
            (f.fight_complete ? 'Killed' : 'Incomplete') +
            (f.member_count > 1 ? ` · ${f.member_count} fights merged` : ''),
            true);
  // Add an extra "Pet owners" button to the header actions. setHeader has
  // already populated Refresh/Change log; we append after them so the
  // primary nav stays leftmost.
  const headerActions = document.getElementById('actions');
  if (headerActions) {
    const petsBtn = document.createElement('button');
    petsBtn.className = 'btn';
    petsBtn.textContent = 'Pet owners';
    const ownerCount = Object.keys(f.pet_owners || {}).length;
    if (ownerCount > 0) petsBtn.textContent += ` (${ownerCount})`;
    petsBtn.title = 'Assign owners to actors that don\'t carry the backtick-pet suffix in the log.';
    petsBtn.addEventListener('click', () => showPetOwnersModal(f));
    headerActions.appendChild(petsBtn);
  }

  const summaryHTML = `
    <a href="#/" class="back">← back</a>
    <div class="panel">
      <div class="summary-grid">
        <div class="stat"><div class="label">Total damage</div>
          <div class="value">${NUM(f.total_damage)}</div></div>
        <div class="stat"><div class="label">Raid DPS</div>
          <div class="value">${NUM(f.raid_dps)}</div></div>
        <div class="stat"><div class="label">Duration</div>
          <div class="value">${FMT_DUR(f.duration_seconds)}</div></div>
        <div class="stat"><div class="label">Attackers</div>
          <div class="value">${f.attackers.length}</div></div>
      </div>
    </div>`;

  // Members panel: shown only when the encounter is more than one fight.
  // Lets the user see which mob slices were merged into this row.
  const membersHTML = (f.members && f.members.length > 1) ? `
    <h2>Member fights</h2>
    <div class="panel">
      <table>
        <thead><tr>
          <th class="num">Fight</th><th>Target</th>
          <th class="num">Damage</th><th class="num">Dur</th>
          <th class="num">Attackers</th><th>Status</th>
        </tr></thead>
        <tbody>${f.members.map(m => `
          <tr>
            <td class="num">${m.fight_id}</td>
            <td class="target">${escapeHTML(m.target)}</td>
            <td class="num">${NUM(m.damage)}</td>
            <td class="num">${FMT_DUR(m.duration_seconds)}</td>
            <td class="num">${m.attacker_count}</td>
            <td class="status ${m.fight_complete ? 'killed' : 'incomplete'}">
              ${m.fight_complete ? 'Killed' : 'Incomplete'}</td>
          </tr>`).join('')}
        </tbody>
      </table>
    </div>` : '';

  // Default missing `side` to friendly so /api/fight/<id> responses (which
  // don't classify) still render cleanly.
  const dmgFriendlies = f.attackers.filter(a => (a.side || 'friendly') === 'friendly');
  const dmgEnemies = f.attackers.filter(a => a.side === 'enemy');

  const healingData = f.healing || {healers: [], biggest_heals: [], timeline: {labels: [], datasets: [], bucket_seconds: 5}, total_healing: 0};
  const healFriendlies = healingData.healers.filter(a => (a.side || 'friendly') === 'friendly');
  const healEnemies = healingData.healers.filter(a => a.side === 'enemy');

  // Each attacker row is followed by a hidden detail row containing a
  // dealt-to / taken-from drilldown. Pair rows inside the breakdown are
  // clickable — they pop a modal chart for that (attacker, target) pair.
  // Pair series live in `_pairData` so the click handler can look them up
  // without having to embed JSON in DOM attributes.
  let _detailId = 0;
  let _pairId = 0;
  const _pairData = window._pairData = {};

  // `kind` config drives column labels, headings, and units for both
  // damage and healing tabs so the same row/table/breakdown helpers work
  // for both. The healing payload deliberately uses the same JSON keys
  // ('attacker', 'damage', 'hits') as damage so the helpers don't need
  // any further translation.
  const KIND_DAMAGE = {
    who: 'Attacker', amount: 'Damage', rate: 'DPS', count: 'Hits',
    showMisses: true, biggestLabel: 'Biggest', unit: 'DPS',
    pairLeftLabelTarget: 'Target', pairLeftLabelSource: 'Source',
    pairAmountLabel: 'Damage', pairCountLabel: 'Hits',
    dealtHeading: 'Damage dealt to', takenHeading: 'Damage taken from',
  };
  const KIND_HEALING = {
    who: 'Healer', amount: 'Healing', rate: 'HPS', count: 'Casts',
    showMisses: false, biggestLabel: 'Biggest', unit: 'HPS',
    pairLeftLabelTarget: 'Target', pairLeftLabelSource: 'Source',
    pairAmountLabel: 'Healing', pairCountLabel: 'Casts',
    dealtHeading: 'Healing dealt to', takenHeading: 'Healing taken from',
  };
  // Used by the Tanking tab. Series for tanking pairs are bucketed off
  // the damage timeline (same labels), so the modal's pair.unit-based
  // label lookup falls through to f.timeline correctly.
  const KIND_TANKING = {
    who: 'Defender', amount: 'Damage', rate: 'DTPS', count: 'Hits',
    showMisses: false, biggestLabel: 'Biggest', unit: 'DTPS',
    pairLeftLabelTarget: 'Defender', pairLeftLabelSource: 'Attacker',
    pairAmountLabel: 'Damage', pairCountLabel: 'Hits',
    dealtHeading: 'Damage taken from', takenHeading: 'Damage dealt to',
  };

  const registerPair = (attacker, target, row, kind) => {
    const id = `pair-${++_pairId}`;
    _pairData[id] = {
      attacker, target,
      series: row.series || [],
      hits_detail: row.hits_detail || [],
      damage: row.damage,
      hits: row.hits,
      unit: kind.unit,
      amountLabel: kind.pairAmountLabel,
      countLabel: kind.pairCountLabel,
      // Optional tanking-only extras: when present, the pair modal
      // shows a damage / healing / delta toggle and switches the chart
      // series accordingly. Only set on the All-row of a tanking
      // defender — per-attacker rows have no defender-scoped heal data.
      heals_series: row.heals_series || null,
      heals_detail: row.heals_detail || null,
      heals_total: row.heals_total || 0,
    };
    return id;
  };

  const breakdownTable = (heading, rows, leftCol, attackerName, kind) => {
    if (!rows || rows.length === 0) {
      return `<div><h4>${heading}</h4><div class="empty">— none —</div></div>`;
    }
    // Synthesize an "All" row that sums every breakdown row, so the
    // user can pop a chart of the attacker's total dealt-to-everyone or
    // total taken-from-everyone in this encounter without picking a
    // single target/source. Series is element-wise summed across rows
    // (different lengths shouldn't happen — every row is bucketed off
    // the same encounter timeline — but be defensive). hits_detail is
    // concatenated; the modal's by-source grouping already handles a
    // mixed pile of hits from many pairs.
    let allDamage = 0, allHits = 0;
    let allSeries = null;
    const allHitsDetail = [];
    for (const r of rows) {
      allDamage += r.damage || 0;
      allHits += r.hits || 0;
      if (Array.isArray(r.series)) {
        if (allSeries === null) {
          allSeries = r.series.slice();
        } else {
          const len = Math.max(allSeries.length, r.series.length);
          for (let i = 0; i < len; i++) {
            allSeries[i] = (allSeries[i] || 0) + (r.series[i] || 0);
          }
        }
      }
      if (Array.isArray(r.hits_detail)) {
        for (const h of r.hits_detail) allHitsDetail.push(h);
      }
    }
    const allRow = {
      [leftCol]: 'All',
      damage: allDamage,
      hits: allHits,
      series: allSeries || [],
      hits_detail: allHitsDetail,
    };
    const allAtk = leftCol === 'target' ? attackerName : 'All';
    const allTgt = leftCol === 'target' ? 'All' : attackerName;
    const allPairId = registerPair(allAtk, allTgt, allRow, kind);
    const allRowHTML = `
        <tr class="pair-row pair-row-all" data-pair-id="${allPairId}">
          <td><strong>All</strong></td>
          <td class="num"><strong>${NUM(allDamage)}</strong></td>
          <td class="num"><strong>${allHits}</strong></td>
        </tr>`;

    const body = rows.map(r => {
      const atk = leftCol === 'target' ? attackerName : r.attacker;
      const tgt = leftCol === 'target' ? r.target : attackerName;
      const id = registerPair(atk, tgt, r, kind);
      return `
        <tr class="pair-row" data-pair-id="${id}">
          <td>${escapeHTML(r[leftCol])}</td>
          <td class="num">${NUM(r.damage)}</td>
          <td class="num">${r.hits}</td>
        </tr>`;
    }).join('');
    const leftLabel = leftCol === 'target'
      ? kind.pairLeftLabelTarget : kind.pairLeftLabelSource;
    return `
      <div>
        <h4>${heading}</h4>
        <table>
          <thead><tr>
            <th>${leftLabel}</th>
            <th class="num">${kind.pairAmountLabel}</th>
            <th class="num">${kind.pairCountLabel}</th>
          </tr></thead>
          <tbody>${allRowHTML}${body}</tbody>
        </table>
      </div>`;
  };

  const attackerRowPair = (a, kind) => {
    const id = `atk-detail-${++_detailId}`;
    const colspan = kind.showMisses ? 8 : 7;
    return `
      <tr class="attacker-row" data-toggle="${id}">
        <td class="target"><span class="expand">▶</span>${escapeHTML(a.attacker)}</td>
        <td class="num">${NUM(a.damage)}</td>
        <td class="num">${NUM(a.dps)}</td>
        <td class="num">${a.hits}</td>
        ${kind.showMisses ? `<td class="num">${a.misses}</td>` : ''}
        <td class="num">${a.crits}</td>
        <td class="num">${NUM(a.biggest)}</td>
        <td class="num">${a.pct_of_total.toFixed(1)}%</td>
      </tr>
      <tr class="attacker-detail" id="${id}" style="display:none">
        <td colspan="${colspan}">
          <div class="breakdown">
            ${breakdownTable(kind.dealtHeading, a.dealt_to, 'target', a.attacker, kind)}
            ${breakdownTable(kind.takenHeading, a.taken_from, 'attacker', a.attacker, kind)}
          </div>
        </td>
      </tr>`;
  };

  const attackerTableHTML = (heading, rows, kind) => `
    <h2>${heading}</h2>
    <div class="panel">
      <table>
        <thead><tr>
          <th>${kind.who}</th>
          <th class="num">${kind.amount}</th>
          <th class="num">${kind.rate}</th>
          <th class="num">${kind.count}</th>
          ${kind.showMisses ? '<th class="num">Miss</th>' : ''}
          <th class="num">Crit</th>
          <th class="num">${kind.biggestLabel}</th>
          <th class="num">%</th>
        </tr></thead>
        <tbody>${rows.map(r => attackerRowPair(r, kind)).join('')}</tbody>
      </table>
    </div>`;

  const dmgFriendlyHTML = dmgFriendlies.length
    ? attackerTableHTML(`Friendlies (${dmgFriendlies.length})`, dmgFriendlies, KIND_DAMAGE)
    : '';
  // Enemies section only appears when there's enemy damage to show.
  // Most well-formed encounters have nothing here (damage shields get
  // re-attributed to the player who owns the DS).
  const dmgEnemyHTML = dmgEnemies.length
    ? attackerTableHTML(`Enemies (${dmgEnemies.length})`, dmgEnemies, KIND_DAMAGE)
    : '';
  const dpsTableHTML = dmgFriendlyHTML + dmgEnemyHTML;

  const healFriendlyHTML = healFriendlies.length
    ? attackerTableHTML(`Healers (${healFriendlies.length})`, healFriendlies, KIND_HEALING)
    : '';
  const healEnemyHTML = healEnemies.length
    ? attackerTableHTML(`Enemy healers (${healEnemies.length})`, healEnemies, KIND_HEALING)
    : '';
  const healTablesHTML = healFriendlyHTML + healEnemyHTML;

  const specialsHTML = f.specials.length === 0 ? '' : `
    <h2>Special attacks</h2>
    <div class="panel">
      <table>
        <thead><tr>
          <th>Attacker</th><th>Type</th><th class="num">Hits</th>
          <th class="num">Damage</th><th class="num">% of attacker</th>
        </tr></thead>
        <tbody>${f.specials.map(s => `
          <tr>
            <td class="target">${escapeHTML(s.attacker)}</td>
            <td class="specials">${s.type}</td>
            <td class="num">${s.hits}</td>
            <td class="num">${NUM(s.damage)}</td>
            <td class="num">${s.pct_of_attacker.toFixed(1)}%</td>
          </tr>`).join('')}
        </tbody>
      </table>
    </div>`;

  const dmgChartHTML = `
    <h2>Timeline (${f.timeline.bucket_seconds}s buckets, DPS)</h2>
    <div class="chart-wrap"><canvas id="dmg-chart" height="120"></canvas></div>`;

  const dmgBiggestHTML = f.biggest_hits.length === 0 ? '' : `
    <h2>Biggest hits</h2>
    <div class="panel">
      <table>
        <thead><tr>
          <th class="num">Time</th><th>Attacker</th>
          <th class="num">Damage</th><th>Special</th>
        </tr></thead>
        <tbody>${f.biggest_hits.map(h => `
          <tr>
            <td class="num">+${h.offset_s}s</td>
            <td class="target">${escapeHTML(h.attacker)}</td>
            <td class="num">${NUM(h.damage)}</td>
            <td class="specials">${h.specials.join(', ') || '—'}</td>
          </tr>`).join('')}
        </tbody>
      </table>
    </div>`;

  // Healing tab content. Empty-encounter case shows a friendly message
  // instead of a stack of empty panels.
  const hasHealing = healingData.healers.length > 0;
  const healChartHTML = !hasHealing ? '' : `
    <h2>Timeline (${healingData.timeline.bucket_seconds}s buckets, HPS)</h2>
    <div class="chart-wrap"><canvas id="heal-chart" height="120"></canvas></div>`;
  const healBiggestHTML = healingData.biggest_heals.length === 0 ? '' : `
    <h2>Biggest heals</h2>
    <div class="panel">
      <table>
        <thead><tr>
          <th class="num">Time</th><th>Healer</th><th>Target</th>
          <th class="num">Healing</th><th>Spell</th>
        </tr></thead>
        <tbody>${healingData.biggest_heals.map(h => `
          <tr>
            <td class="num">+${h.offset_s}s</td>
            <td class="target">${escapeHTML(h.attacker)}</td>
            <td>${escapeHTML(h.target || '')}</td>
            <td class="num">${NUM(h.damage)}</td>
            <td class="specials">${h.specials.join(', ') || '—'}</td>
          </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
  const healingTabHTML = !hasHealing
    ? '<div class="panel sub">No healing recorded in this encounter.</div>'
    : (healTablesHTML + healChartHTML + healBiggestHTML);

  // ---- Tanking tab ----
  // Friendly-focused view of damage taken, ordered by damage_taken desc,
  // with a per-outcome avoidance breakdown (parry/block/dodge/rune/invuln/
  // miss/riposte) the existing Damage tab doesn't surface. Each row
  // expands into a per-attacker breakdown using the same columns.
  const tanking = f.defenders || [];
  const hasTanking = tanking.length > 0;
  const tankingTotalDamage = tanking.reduce(
    (sum, d) => sum + (d.damage_taken || 0), 0);
  const AVOID_KEYS = ['parry','block','dodge','rune','invulnerable','miss','riposte'];
  const sumAvoid = (av) => AVOID_KEYS.reduce((s, k) => s + ((av || {})[k] || 0), 0);
  const avoidPct = (avoided, hits) => {
    const swings = hits + avoided;
    return swings > 0 ? Math.round(avoided / swings * 1000) / 10 : 0;
  };
  // Each tanking breakdown row registers as a pair so clicking it opens
  // the same DTPS-over-time modal that Damage and Healing tabs use. We
  // pass the breakdown row's series + hits_detail (pulled server-side
  // from the damage matrix) and let registerPair attach unit/labels.
  const tankBreakdownRow = (br, defenderName) => {
    const avoided = sumAvoid(br.avoided);
    const swings = br.hits_landed + avoided;
    const pairRow = {
      damage: br.damage_taken, hits: br.hits_landed,
      series: br.series || [], hits_detail: br.hits_detail || [],
    };
    const pairId = registerPair(br.attacker, defenderName, pairRow, KIND_TANKING);
    return `<tr class="pair-row" data-pair-id="${pairId}">
      <td>${escapeHTML(br.attacker)}</td>
      <td class="num">${NUM(br.damage_taken)}</td>
      <td class="num">${NUM(swings)}</td>
      <td class="num">${avoidPct(avoided, br.hits_landed)}%</td>
      <td class="num">${br.avoided.parry || 0}</td>
      <td class="num">${br.avoided.block || 0}</td>
      <td class="num">${br.avoided.dodge || 0}</td>
      <td class="num">${br.avoided.rune || 0}</td>
      <td class="num">${br.avoided.invulnerable || 0}</td>
      <td class="num">${br.avoided.miss || 0}</td>
      <td class="num">${br.avoided.riposte || 0}</td>
      <td class="num">${NUM(br.biggest_taken)}</td>
    </tr>`;
  };
  const tankRowHTML = (d, idx) => {
    const avoided = sumAvoid(d.avoided);
    const swings = d.hits_landed + avoided;
    const detailId = `tank-detail-${idx}`;
    // Synthesize an "All" row at the top of the breakdown that aggregates
    // every attacker's series and hits_detail, so a click pops a modal
    // showing total damage taken by this defender across all sources.
    // Mirrors the breakdownTable helper's All-row pattern on Damage/Healing.
    let allSeries = null;
    const allHitsDetail = [];
    for (const br of d.breakdown) {
      if (Array.isArray(br.series)) {
        if (allSeries === null) {
          allSeries = br.series.slice();
        } else {
          const len = Math.max(allSeries.length, br.series.length);
          for (let i = 0; i < len; i++) {
            allSeries[i] = (allSeries[i] || 0) + (br.series[i] || 0);
          }
        }
      }
      if (Array.isArray(br.hits_detail)) {
        for (const h of br.hits_detail) allHitsDetail.push(h);
      }
    }
    // Attach the defender's heals series + per-heal detail so the modal
    // can toggle between damage taken / healing received / life delta.
    // Per-attacker breakdown rows don't get this (heals aren't keyed by
    // attacker), so the toggle only appears on the All row.
    const allPairRow = {
      damage: d.damage_taken, hits: d.hits_landed,
      series: allSeries || [], hits_detail: allHitsDetail,
      heals_series: d.heals_series || [],
      heals_detail: d.heals_detail || [],
      heals_total: d.heals_total || 0,
    };
    const allPairId = registerPair('All', d.defender, allPairRow, KIND_TANKING);
    const allBreakdownRow = `
      <tr class="pair-row pair-row-all" data-pair-id="${allPairId}">
        <td><strong>All</strong></td>
        <td class="num"><strong>${NUM(d.damage_taken)}</strong></td>
        <td class="num"><strong>${NUM(swings)}</strong></td>
        <td class="num"><strong>${avoidPct(avoided, d.hits_landed)}%</strong></td>
        <td class="num"><strong>${d.avoided.parry || 0}</strong></td>
        <td class="num"><strong>${d.avoided.block || 0}</strong></td>
        <td class="num"><strong>${d.avoided.dodge || 0}</strong></td>
        <td class="num"><strong>${d.avoided.rune || 0}</strong></td>
        <td class="num"><strong>${d.avoided.invulnerable || 0}</strong></td>
        <td class="num"><strong>${d.avoided.miss || 0}</strong></td>
        <td class="num"><strong>${d.avoided.riposte || 0}</strong></td>
        <td class="num"><strong>${NUM(d.biggest_taken)}</strong></td>
      </tr>`;
    return `
    <tr class="attacker-row" data-toggle="${detailId}">
      <td><span class="expand">▶</span>${escapeHTML(d.defender)}</td>
      <td class="num">${NUM(d.damage_taken)}</td>
      <td class="num">${NUM(swings)}</td>
      <td class="num">${avoidPct(avoided, d.hits_landed)}%</td>
      <td class="num">${d.avoided.parry || 0}</td>
      <td class="num">${d.avoided.block || 0}</td>
      <td class="num">${d.avoided.dodge || 0}</td>
      <td class="num">${d.avoided.rune || 0}</td>
      <td class="num">${d.avoided.invulnerable || 0}</td>
      <td class="num">${d.avoided.miss || 0}</td>
      <td class="num">${d.avoided.riposte || 0}</td>
      <td class="num">${NUM(d.biggest_taken)}</td>
    </tr>
    <tr class="attacker-detail" id="${detailId}" style="display:none">
      <td colspan="12">
        <table class="tanking-breakdown">
          <thead><tr>
            <th>Attacker</th>
            <th class="num">Damage</th>
            <th class="num">Swings</th>
            <th class="num">Avoid %</th>
            <th class="num">Parry</th>
            <th class="num">Block</th>
            <th class="num">Dodge</th>
            <th class="num">Rune</th>
            <th class="num">Invuln</th>
            <th class="num">Miss</th>
            <th class="num">Rip</th>
            <th class="num">Biggest</th>
          </tr></thead>
          <tbody>${allBreakdownRow}${d.breakdown.map(br => tankBreakdownRow(br, d.defender)).join('')}</tbody>
        </table>
      </td>
    </tr>`;
  };
  const tankingTabHTML = !hasTanking
    ? '<div class="panel sub">No incoming damage tracked in this encounter.</div>'
    : `<h2>Tanks (${tanking.length})</h2>
       <div class="panel">
         <table class="tanking-table">
           <thead><tr>
             <th>Defender</th>
             <th class="num">Dmg Taken</th>
             <th class="num">Swings</th>
             <th class="num">Avoid %</th>
             <th class="num">Parry</th>
             <th class="num">Block</th>
             <th class="num">Dodge</th>
             <th class="num">Rune</th>
             <th class="num">Invuln</th>
             <th class="num">Miss</th>
             <th class="num">Rip</th>
             <th class="num">Biggest</th>
           </tr></thead>
           <tbody>${tanking.map((d, i) => tankRowHTML(d, i)).join('')}</tbody>
         </table>
       </div>`;

  const tabsHTML = `
    <div class="tabs">
      <button class="tab active" data-tab="damage">Damage (${NUM(f.total_damage)})</button>
      <button class="tab" data-tab="healing">Healing (${NUM(healingData.total_healing)})</button>
      <button class="tab" data-tab="tanking">Tanking (${NUM(tankingTotalDamage)})</button>
    </div>`;

  // Members panel sits OUTSIDE the tab content because it's encounter-
  // level info (which mob slices got merged into this row) — useful in
  // both Damage and Healing tabs, and pushed to the bottom so it doesn't
  // crowd the per-attacker tables that are usually what you want first.
  app.innerHTML = summaryHTML + tabsHTML +
    `<div id="tab-damage">${dpsTableHTML}${specialsHTML}${dmgChartHTML}${dmgBiggestHTML}</div>` +
    `<div id="tab-healing" style="display:none">${healingTabHTML}</div>` +
    `<div id="tab-tanking" style="display:none">${tankingTabHTML}</div>` +
    membersHTML;

  // Toggle the per-attacker drilldown row when the parent row is clicked.
  // Selectors run across both tabs because IDs in `#tab-healing` are also
  // wired up here even though that section is hidden initially.
  app.querySelectorAll('tr.attacker-row').forEach(tr => {
    tr.addEventListener('click', () => {
      const detail = document.getElementById(tr.dataset.toggle);
      if (!detail) return;
      const collapsed = detail.style.display === 'none';
      detail.style.display = collapsed ? '' : 'none';
      tr.classList.toggle('expanded', collapsed);
    });
  });

  // Click a pair row inside a breakdown to pop the per-pair timeline chart.
  app.querySelectorAll('tr.pair-row').forEach(tr => {
    tr.addEventListener('click', ev => {
      ev.stopPropagation();
      const pair = _pairData[tr.dataset.pairId];
      if (!pair) return;
      // Damage and healing pairs share a registry but each row knows its
      // unit/labels (set by `registerPair`); the modal just renders them.
      const labels = pair.unit === 'HPS' ? healingData.timeline.labels
                                          : f.timeline.labels;
      const bs = pair.unit === 'HPS' ? healingData.timeline.bucket_seconds
                                      : f.timeline.bucket_seconds;
      showPairChart(pair, labels, bs);
    });
  });

  // Tab switching. The healing chart is built lazily on first switch so
  // Chart.js doesn't try to size a canvas inside `display:none`.
  let healingChartBuilt = false;
  app.querySelectorAll('.tabs .tab').forEach(btn => {
    btn.addEventListener('click', () => {
      const which = btn.dataset.tab;
      app.querySelectorAll('.tabs .tab').forEach(b =>
        b.classList.toggle('active', b === btn));
      document.getElementById('tab-damage').style.display =
        which === 'damage' ? '' : 'none';
      document.getElementById('tab-healing').style.display =
        which === 'healing' ? '' : 'none';
      document.getElementById('tab-tanking').style.display =
        which === 'tanking' ? '' : 'none';
      if (which === 'healing' && hasHealing && !healingChartBuilt) {
        buildStackedChart('heal-chart', healingData.timeline, 'HPS');
        healingChartBuilt = true;
      }
    });
  });

  // Build the visible damage chart immediately.
  buildStackedChart('dmg-chart', f.timeline, 'DPS');
}

// --- Session summary view -------------------------------------------
//
// Multi-fight rollup. Three pieces of UI in a single layout:
//   - Trend chart (top-left): line chart, top-N attackers, x = encounter,
//     y = DPS in that encounter. Reveals consistency vs spike-y players.
//   - Heatmap (bottom-left): attacker × encounter grid, cell color
//     intensity = DPS magnitude. Reveals "who showed up for what."
//   - Rollup table (right column): one row per attacker with total
//     damage, avg/median/p95/best DPS, encounters present. The
//     authoritative numerical view; the charts are visual aids.
//
// State (sessionSummarySettings) is module-scope so toggling killed-only
// or the min-DPS filter doesn't lose state on navigation back.

// Per-mode config for the session-summary view. The three tabs (damage,
// healing, tanking) all run through the same render path; this table is
// the only place mode differences live.
const SS_MODES = {
  damage: {
    label: 'Damage',
    actorsField: 'damage_actors',
    totalField: 'total_damage',
    rateLabel: 'DPS',
    valueLabel: 'damage',
    actorLabel: 'Attacker',
    actorPlural: 'attackers',
    totalSuffix: 'damage',
  },
  healing: {
    label: 'Healing',
    actorsField: 'healing_actors',
    totalField: 'total_healing',
    rateLabel: 'HPS',
    valueLabel: 'healing',
    actorLabel: 'Healer',
    actorPlural: 'healers',
    totalSuffix: 'healing',
  },
  tanking: {
    label: 'Tanking',
    actorsField: 'tanking_actors',
    totalField: 'total_damage_taken',
    rateLabel: 'DTPS',
    valueLabel: 'damage taken',
    actorLabel: 'Defender',
    actorPlural: 'defenders',
    totalSuffix: 'damage taken',
  },
};

let sessionSummarySettings = {
  killedOnly: true,   // raid wipes drag down averages — default-filter them
  minRate: 0,         // hide rows below this avg rate (the table tails get
                      // long otherwise) — units depend on active mode
  trendTopN: 10,      // chart legend cap; rest collapse into "Other"
  mode: 'damage',     // 'damage' | 'healing' | 'tanking'
  tankingMetric: 'damage', // tanking sub-toggle: 'damage' | 'healing' | 'delta'
};
let sessionSummaryChart = null;

// Tanking sub-toggle for the chart + heatmap. Each metric pulls a
// different per-encounter array off the actor row (server provides
// damage and healing arrays; delta is computed client-side).
const TANK_METRICS = {
  damage: {
    label: 'Damage taken', shortLabel: 'Damage',
    rateLabel: 'DTPS',
    rateOf: a => a.per_encounter_rate,
  },
  healing: {
    label: 'Healing received', shortLabel: 'Healing',
    rateLabel: 'HPS in',
    rateOf: a => a.per_encounter_heals_rate || a.per_encounter_rate.map(() => 0),
  },
  delta: {
    label: 'Life delta', shortLabel: 'Δ Life',
    rateLabel: 'ΔHP/s',
    // Healing minus damage taken per encounter — positive = net heal,
    // negative = net loss. Plotted as a single area dipping below zero.
    rateOf: a => {
      const dmg = a.per_encounter_rate || [];
      const heal = a.per_encounter_heals_rate || dmg.map(() => 0);
      return dmg.map((d, i) => (heal[i] || 0) - d);
    },
  },
};

async function renderSessionSummary(scopedIds) {
  if (chartInstance) { chartInstance.destroy(); chartInstance = null; }
  if (sessionSummaryChart) { sessionSummaryChart.destroy(); sessionSummaryChart = null; }
  const app = document.getElementById('app');

  // When scoped to a user-selected subset, force killed_only OFF so
  // the explicit selection is honored as-is. Wipes the user picked on
  // purpose (e.g. to compare burn-phase DPS) shouldn't be filtered.
  const scoped = Array.isArray(scopedIds) && scopedIds.length > 0;
  const killedOnlyForRequest = scoped ? false : sessionSummarySettings.killedOnly;

  let data;
  try {
    let url = '/api/session-summary?killed_only=' +
              (killedOnlyForRequest ? '1' : '0');
    if (scoped) url += '&encounter_ids=' + scopedIds.join(',');
    data = await withParseProgress(
      () => fetchJSON(url), app, 'Parsing log…');
  } catch (e) {
    app.innerHTML = `<div class="err">Failed to load session summary: ${e.message}</div>`;
    return;
  }

  const scopeLabel = scoped ? ` · scoped to ${scopedIds.length} selected` : '';
  setHeader('Session summary',
            `${data.encounter_count} encounters · ${data.killed_count} killed · ` +
            `${NUM(data.total_damage)} damage · ${FMT_DUR(data.duration_seconds)} of combat` +
            scopeLabel,
            true);

  // Active mode + helpers. Each mode sources from a different actors
  // array on the payload (damage_actors / healing_actors / tanking_actors)
  // but they all share the same row shape so the render path is uniform.
  const mode = SS_MODES[sessionSummarySettings.mode] || SS_MODES.damage;
  const allActors = data[mode.actorsField] || [];
  const friendlies = allActors.filter(a => a.side === 'friendly')
                              .filter(a => a.avg_rate >= sessionSummarySettings.minRate);
  const enemies = allActors.filter(a => a.side === 'enemy');

  // Tanking sub-metric (damage taken / healing received / life delta).
  // Only meaningful when mode === 'tanking'; the helper produces a
  // per-encounter rate array per actor that the chart and heatmap use.
  const isTanking = sessionSummarySettings.mode === 'tanking';
  const tankMetric = TANK_METRICS[sessionSummarySettings.tankingMetric] || TANK_METRICS.damage;
  const rateLabelForChart = isTanking ? tankMetric.rateLabel : mode.rateLabel;
  const rateForActor = a => isTanking ? tankMetric.rateOf(a) : a.per_encounter_rate;

  if (data.encounter_count === 0) {
    let hint;
    if (scoped) {
      hint = `The selected encounter${scopedIds.length === 1 ? '' : 's'} ` +
             `couldn't be matched — likely the detection params changed and ` +
             `the ids shifted. <a href="#/session-summary">Show whole log</a>.`;
    } else if (sessionSummarySettings.killedOnly) {
      hint = 'Try un-checking "Killed only" — there may be incomplete fights worth seeing.';
    } else {
      hint = 'Pick a log with combat data first.';
    }
    app.innerHTML = `<a href="#/" class="back">← back</a>` +
      `<div class="panel sub">No encounters in scope. ${hint}</div>`;
    return;
  }

  // Tab badge totals come from the payload's totals so they reflect the
  // currently-loaded slice (killed_only / scoped) without having to sum
  // each actors array client-side.
  const tabsHTML = ['damage', 'healing', 'tanking'].map(k => {
    const m = SS_MODES[k];
    const total = data[m.totalField] || 0;
    const isActive = k === sessionSummarySettings.mode;
    return `<button class="tab ${isActive ? 'active' : ''}" data-ss-mode="${k}">
      ${m.label} (${NUM(total)})
    </button>`;
  }).join('');

  app.innerHTML = `
    <a href="#/" class="back">← back</a>
    <div class="panel">
      <div class="summary-grid">
        <div class="stat"><div class="label">Encounters</div>
          <div class="value">${data.encounter_count}</div></div>
        <div class="stat"><div class="label">Killed</div>
          <div class="value">${data.killed_count}</div></div>
        <div class="stat"><div class="label">Total damage</div>
          <div class="value">${NUM(data.total_damage)}</div></div>
        <div class="stat"><div class="label">Combat time</div>
          <div class="value">${FMT_DUR(data.duration_seconds)}</div></div>
      </div>
    </div>
    <div class="tabs">${tabsHTML}</div>
    <div class="panel">
      <div class="params-row">
        <label class="check"
               title="${scoped ? 'Disabled while scoped to a selection — explicit picks always show as-is.' : 'Restrict the rollup to encounters that were killed (fight_complete=true). Wipes and aborted pulls would otherwise drag down averages.'}">
          <input type="checkbox" id="ss-killed-only"
                 ${killedOnlyForRequest ? 'checked' : ''}
                 ${scoped ? 'disabled' : ''}>
          Killed encounters only
        </label>
        <label>Min avg ${mode.rateLabel}
          <input type="number" id="ss-min-rate" min="0" step="100"
                 value="${sessionSummarySettings.minRate}"
                 title="Hide rollup rows whose avg ${mode.rateLabel} is below this. Useful for hiding low-impact actors that pad the table.">
        </label>
        <span class="sub" style="align-self:center; font-size:0.85rem;">
          Showing <strong>${friendlies.length}</strong> friendly ${mode.actorPlural}
          ${enemies.length > 0 ? `· ${enemies.length} enemies tracked separately` : ''}
        </span>
        ${scoped ? `<a href="#/session-summary" class="btn"
                       style="margin-left:auto; align-self:center; text-decoration:none;"
                       title="Drop the selection scope and show the whole-log rollup.">Show whole log</a>` : ''}
      </div>
    </div>
    <div class="ss-grid">
      <div class="ss-charts">
        <div class="panel">
          <div class="ss-chart-head">
            <h3 class="ss-section-h">${rateLabelForChart} by encounter <span class="sub">— top ${Math.min(sessionSummarySettings.trendTopN, friendlies.length)}</span></h3>
            ${isTanking ? `
              <div class="ss-metric-toggle">
                ${['damage', 'healing', 'delta'].map(k => {
                  const m = TANK_METRICS[k];
                  const isActive = k === sessionSummarySettings.tankingMetric;
                  return `<button class="ss-metric-btn ${isActive ? 'active' : ''}"
                                  data-tank-metric="${k}"
                                  title="${m.label}">${m.shortLabel}</button>`;
                }).join('')}
              </div>` : ''}
          </div>
          <div class="chart-wrap" style="background: transparent; padding: 0; margin: 0;">
            <canvas id="ss-trend-chart" height="200"></canvas>
          </div>
        </div>
        <div class="panel">
          <h3 class="ss-section-h">${mode.actorLabel} × encounter heatmap
            <span class="sub">— click a cell to drill in</span></h3>
          ${ssHeatmapHTML(friendlies, data.encounters, mode, isTanking ? tankMetric : null)}
        </div>
      </div>
      <div class="ss-table-col">
        <div class="panel">
          <h3 class="ss-section-h">Per-${mode.actorLabel.toLowerCase()} rollup</h3>
          ${ssTableHTML(friendlies, mode)}
        </div>
      </div>
    </div>`;

  // Wire toggles. killedOnly hits the server (it changes which
  // encounters are aggregated); the others re-render client-side off
  // the cached payload. We always call renderSessionSummary which
  // re-fetches — simpler than caching the payload across renders, and
  // the killed-only path already needs the fetch.
  document.getElementById('ss-killed-only').addEventListener('change', e => {
    sessionSummarySettings.killedOnly = e.target.checked;
    renderSessionSummary(scopedIds);
  });
  document.getElementById('ss-min-rate').addEventListener('change', e => {
    const v = parseInt(e.target.value, 10);
    sessionSummarySettings.minRate = (Number.isNaN(v) || v < 0) ? 0 : v;
    renderSessionSummary(scopedIds);
  });
  // Tab buttons swap mode and re-render. Min-rate filter resets to 0
  // because the units differ — a "Min DPS = 1000" threshold doesn't map
  // sensibly to "Min HPS = 1000" or "Min DTPS = 1000".
  app.querySelectorAll('.tabs .tab[data-ss-mode]').forEach(btn => {
    btn.addEventListener('click', () => {
      const m = btn.dataset.ssMode;
      if (m === sessionSummarySettings.mode) return;
      sessionSummarySettings.mode = m;
      sessionSummarySettings.minRate = 0;
      renderSessionSummary(scopedIds);
    });
  });
  // Tanking sub-toggle (damage / healing / delta). Re-renders the chart
  // and heatmap; the rollup table stays on damage-taken stats since the
  // per-actor aggregates (avg/median/p95) are damage-rooted.
  app.querySelectorAll('.ss-metric-btn[data-tank-metric]').forEach(btn => {
    btn.addEventListener('click', () => {
      const m = btn.dataset.tankMetric;
      if (m === sessionSummarySettings.tankingMetric) return;
      sessionSummarySettings.tankingMetric = m;
      renderSessionSummary(scopedIds);
    });
  });

  // Wire heatmap cell clicks. data-encounter-id navigates to that
  // encounter's detail view — same pattern as the session table.
  document.querySelectorAll('.ss-heatmap td[data-encounter-id]').forEach(td => {
    td.addEventListener('click', () => {
      location.hash = `#/encounter/${td.dataset.encounterId}`;
    });
  });

  // Build the trend chart. Top-N actors get their own line; the rest
  // are summed into an "Other" line so the legend stays readable.
  // For tanking, the line data comes from the active sub-metric
  // (damage / healing / delta); for damage and healing tabs it's the
  // single per_encounter_rate array.
  const topN = sessionSummarySettings.trendTopN;
  const top = friendlies.slice(0, topN);
  const rest = friendlies.slice(topN);
  const labels = data.encounters.map(m => `#${m.encounter_id}`);
  // Delta mode is the only one that can go negative, so it renders as
  // a filled area (positive above zero, negative below). Other modes
  // stay as line-only so multiple actors don't visually compete.
  const isDelta = isTanking && sessionSummarySettings.tankingMetric === 'delta';
  const datasets = top.map((a, i) => ({
    label: a.attacker,
    data: rateForActor(a),
    backgroundColor: COLORS[i % COLORS.length] + (isDelta ? '55' : '33'),
    borderColor: COLORS[i % COLORS.length],
    borderWidth: 1.5,
    fill: isDelta ? 'origin' : false,
    pointRadius: 2, tension: 0.2,
    spanGaps: false,
  }));
  if (rest.length > 0) {
    const otherSeries = data.encounters.map((_, idx) =>
      rest.reduce((s, a) => s + (rateForActor(a)[idx] || 0), 0));
    datasets.push({
      label: `Other (${rest.length})`, data: otherSeries,
      backgroundColor: COLORS[8] + (isDelta ? '55' : '33'),
      borderColor: COLORS[8],
      borderWidth: 1,
      fill: isDelta ? 'origin' : false,
      pointRadius: 2, tension: 0.2,
      borderDash: [4, 4], spanGaps: false,
    });
  }

  const trendCanvas = document.getElementById('ss-trend-chart');
  if (trendCanvas) {
    sessionSummaryChart = new Chart(trendCanvas, {
      type: 'line',
      data: { labels, datasets },
      options: {
        responsive: true,
        interaction: { mode: 'index', intersect: false },
        scales: {
          x: { ticks: { color: '#94a3b8' }, grid: { color: '#2a3142' } },
          y: { beginAtZero: !isDelta,
               ticks: { color: '#94a3b8',
                        callback: v => SHORT(v) + ' ' + rateLabelForChart },
               grid: { color: '#2a3142' } },
        },
        plugins: {
          legend: { labels: { color: '#e5e7eb', boxWidth: 12 } },
          tooltip: {
            callbacks: {
              title: items => {
                const idx = items[0].dataIndex;
                const enc = data.encounters[idx];
                const status = enc.fight_complete ? 'killed' : 'incomplete';
                return `#${enc.encounter_id}: ${enc.name} (${status})`;
              },
              label: ctx => `${ctx.dataset.label}: ${SHORT(ctx.parsed.y)} ${rateLabelForChart}`,
            },
          },
        },
      },
    });
  }
}

function ssTableHTML(rows, mode) {
  if (rows.length === 0) {
    return `<div class="sub">Nothing to show — try lowering the Min avg ${mode.rateLabel} filter.</div>`;
  }
  // Wrap in an overflow-x scroller — the 8-column table has a
  // min-content width that exceeds the right grid column on narrower
  // viewports / longer attacker names. Without the wrapper the table
  // bleeds past the panel BG.
  const totalAll = rows.reduce((s, r) => s + r.total, 0) || 1;
  return `
    <div class="ss-table-wrap">
    <table class="ss-table">
      <thead><tr>
        <th>${mode.actorLabel}</th>
        <th class="num">Total</th>
        <th class="num">% raid</th>
        <th class="num">Avg ${mode.rateLabel}</th>
        <th class="num">Median</th>
        <th class="num">P95</th>
        <th class="num">Best</th>
        <th class="num" title="Encounters where this ${mode.actorLabel.toLowerCase()} had nonzero ${mode.valueLabel}">Pres.</th>
      </tr></thead>
      <tbody>${rows.map(a => {
        const pct = (a.total / totalAll * 100).toFixed(1);
        return `
          <tr>
            <td class="target">${escapeHTML(a.attacker)}</td>
            <td class="num">${NUM(a.total)}</td>
            <td class="num">${pct}%</td>
            <td class="num">${NUM(a.avg_rate)}</td>
            <td class="num">${NUM(a.median_rate)}</td>
            <td class="num">${NUM(a.p95_rate)}</td>
            <td class="num">${NUM(a.best_rate)}</td>
            <td class="num">${a.encounters_present}</td>
          </tr>`;
      }).join('')}</tbody>
    </table>
    </div>`;
}

function ssHeatmapHTML(rows, encounters, mode, tankMetric) {
  if (rows.length === 0 || encounters.length === 0) {
    return '<div class="sub">No data.</div>';
  }
  // Pull the right per-encounter array per row. For tanking + delta, the
  // values can be negative (net loss); we color positive blue and
  // negative red with intensity scaled to max abs value.
  const rateOf = tankMetric ? tankMetric.rateOf : (a => a.per_encounter_rate);
  const rateLabel = tankMetric ? tankMetric.rateLabel : mode.rateLabel;
  const isDelta = tankMetric && tankMetric.label === 'Life delta';

  let maxAbs = 0;
  const rowRates = rows.map(a => rateOf(a));
  for (const arr of rowRates) {
    for (const v of arr) {
      const av = Math.abs(v);
      if (av > maxAbs) maxAbs = av;
    }
  }
  if (maxAbs === 0) maxAbs = 1;

  const headerCells = encounters.map(m => {
    const status = m.fight_complete ? 'killed' : 'incomplete';
    const tip = `#${m.encounter_id}: ${m.name} — ${status}`;
    return `<th class="ss-heatmap-col-h" title="${escapeHTML(tip)}">${m.encounter_id}</th>`;
  }).join('');

  const bodyRows = rows.map((a, rowIdx) => {
    const cells = rowRates[rowIdx].map((rate, idx) => {
      const enc = encounters[idx];
      if (rate === 0) {
        return `<td class="ss-heatmap-empty"
                    data-encounter-id="${enc.encounter_id}"
                    title="${escapeHTML(a.attacker)} — absent from #${enc.encounter_id}: ${escapeHTML(enc.name)}">·</td>`;
      }
      // Intensity in [0.15, 1.0] so even small values get a visible
      // fill; pure 0..1 makes 5%-of-max cells nearly invisible.
      const alpha = 0.15 + 0.85 * (Math.abs(rate) / maxAbs);
      // Blue for positive (or non-delta), red for negative-delta.
      const rgb = (isDelta && rate < 0) ? '248, 113, 113' : '96, 165, 250';
      return `<td class="ss-heatmap-cell"
                  style="background: rgba(${rgb}, ${alpha.toFixed(3)})"
                  data-encounter-id="${enc.encounter_id}"
                  title="${escapeHTML(a.attacker)} — ${SHORT(rate)} ${rateLabel} in #${enc.encounter_id}: ${escapeHTML(enc.name)}">${SHORT(rate)}</td>`;
    }).join('');
    return `<tr>
      <th class="ss-heatmap-row-h" title="${escapeHTML(a.attacker)}">${escapeHTML(a.attacker)}</th>
      ${cells}
    </tr>`;
  }).join('');

  return `
    <div class="ss-heatmap-wrap">
      <table class="ss-heatmap">
        <thead><tr><th></th>${headerCells}</tr></thead>
        <tbody>${bodyRows}</tbody>
      </table>
    </div>`;
}

function buildStackedChart(canvasId, timeline, unit) {
  // Convert per-bucket totals to per-second rate (DPS or HPS) so the
  // y-axis is meaningful regardless of bucket size. Top 8 series get
  // their own datasets; the rest are bucketed into "Other".
  const bs = timeline.bucket_seconds;
  const datasets = (timeline.datasets || []).slice(0, 8).map((d, i) => ({
    label: d.label,
    data: d.data.map(v => v / bs),
    backgroundColor: COLORS[i % COLORS.length] + 'cc',
    borderColor: COLORS[i % COLORS.length],
    borderWidth: 1, fill: true, pointRadius: 0, tension: 0.3,
  }));
  if (timeline.datasets && timeline.datasets.length > 8) {
    const rest = timeline.datasets.slice(8);
    const n = timeline.labels.length;
    const other = Array.from({length: n}, (_, i) =>
      rest.reduce((s, d) => s + (d.data[i] || 0), 0) / bs);
    datasets.push({
      label: 'Other', data: other,
      backgroundColor: COLORS[8] + 'cc', borderColor: COLORS[8],
      borderWidth: 1, fill: true, pointRadius: 0, tension: 0.3,
    });
  }
  const canvas = document.getElementById(canvasId);
  if (!canvas) return null;
  // The encounter timeline chart used to live in `chartInstance`; we keep
  // a single global ref so navigating away tears down whatever we built.
  if (canvasId === 'dmg-chart') {
    if (chartInstance) chartInstance.destroy();
  }
  const inst = new Chart(canvas, {
    type: 'line',
    data: { labels: timeline.labels, datasets },
    options: {
      responsive: true,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { stacked: true, ticks: { color: '#94a3b8' }, grid: { color: '#2a3142' } },
        y: { stacked: true,
             ticks: { color: '#94a3b8',
                      callback: v => SHORT(v) + ' ' + unit },
             grid: { color: '#2a3142' } },
      },
      plugins: {
        legend: { labels: { color: '#e5e7eb' } },
        tooltip: {
          callbacks: {
            label: ctx => `${ctx.dataset.label}: ${SHORT(ctx.parsed.y)} ${unit}`,
          },
        },
      },
    },
  });
  if (canvasId === 'dmg-chart') chartInstance = inst;
  return inst;
}

let pairChartInstance = null;

function showPairChart(pair, labels, bucketSeconds, metricArg) {
  // Tear down any previous modal/chart first so reopening is idempotent.
  const existing = document.getElementById('pair-modal');
  if (existing) existing.remove();
  if (pairChartInstance) { pairChartInstance.destroy(); pairChartInstance = null; }

  // Tanking pairs can carry an extra heals_series/heals_detail attached
  // server-side. When present, the modal shows a damage/healing/delta
  // toggle and `metric` selects which dataset drives the chart and the
  // by-source breakdown. Damage and healing reuse the full windowing/
  // source-filter machinery; delta is a simpler chart-only view since
  // it's a derived series with no individual events.
  const supportsToggle = !!pair.heals_series;
  const metric = supportsToggle ? (metricArg || 'damage') : 'damage';
  const isDelta = metric === 'delta';

  let allHits, primarySeries, primaryTotal, unit, amountLabel, countLabel;
  if (metric === 'healing') {
    allHits = pair.heals_detail || [];
    primarySeries = pair.heals_series || [];
    primaryTotal = pair.heals_total || 0;
    unit = 'HPS in';
    amountLabel = 'healing';
    countLabel = 'heals';
  } else if (metric === 'delta') {
    // Delta = heals - damage per bucket. No individual events to
    // window/filter, so source breakdown and the click-to-window UX
    // are disabled in this mode (allHits stays empty).
    allHits = [];
    const dmg = pair.series || [];
    const heal = pair.heals_series || [];
    const len = Math.max(dmg.length, heal.length);
    primarySeries = Array.from({length: len}, (_, i) =>
      (heal[i] || 0) - (dmg[i] || 0));
    primaryTotal = (pair.heals_total || 0) - (pair.damage || 0);
    unit = 'ΔHP/s';
    amountLabel = 'net life';
    countLabel = 'buckets';
  } else {
    allHits = pair.hits_detail || [];
    primarySeries = pair.series || [];
    primaryTotal = pair.damage || 0;
    unit = pair.unit || 'DPS';
    amountLabel = (pair.amountLabel || 'damage').toLowerCase();
    countLabel = (pair.countLabel || 'hits').toLowerCase();
  }
  const nBuckets = labels.length;

  // Group hits by source for the right-column breakdown. Sources are
  // sorted by damage desc so the biggest contributor is at the top.
  const groups = {};
  for (const h of allHits) {
    const s = h.source || 'Melee';
    if (!groups[s]) groups[s] = { source: s, damage: 0, hits: 0 };
    groups[s].damage += h.damage;
    groups[s].hits += 1;
  }
  const sources = Object.values(groups).sort((a, b) => b.damage - a.damage);
  const totalDamage = allHits.reduce((s, h) => s + h.damage, 0);

  const sourceRows = `
    <tr class="source-row active" data-source="__all__">
      <td>All</td>
      <td class="num">${NUM(totalDamage)}</td>
      <td class="num">${allHits.length}</td>
    </tr>` + sources.map(s => `
    <tr class="source-row" data-source="${escapeHTML(s.source)}">
      <td>${escapeHTML(s.source)}</td>
      <td class="num">${NUM(s.damage)}</td>
      <td class="num">${s.hits}</td>
    </tr>`).join('');

  // Hide the source-breakdown column for delta — there are no
  // individual events to credit. Damage and healing keep their full
  // by-source table even when the toggle is present.
  const showSourcePanel = !isDelta;
  const subTotal = primaryTotal;
  const subCount = allHits.length;
  const toggleHTML = !supportsToggle ? '' : `
    <div class="pair-metric-toggle">
      ${['damage', 'healing', 'delta'].map(k => {
        const m = TANK_METRICS[k];
        const isActive = k === metric;
        return `<button class="ss-metric-btn ${isActive ? 'active' : ''}"
                        data-pair-metric="${k}"
                        title="${m.label}">${m.shortLabel}</button>`;
      }).join('')}
    </div>`;

  const modal = document.createElement('div');
  modal.id = 'pair-modal';
  modal.className = 'modal-backdrop';
  modal.innerHTML = `
    <div class="modal pair-modal ${showSourcePanel ? '' : 'no-source-panel'}">
      <button class="modal-close" aria-label="Close">×</button>
      <div class="pair-modal-head">
        <div>
          <h3>${escapeHTML(pair.attacker)} → ${escapeHTML(pair.target)}</h3>
          <div class="modal-sub" id="pair-sub">${NUM(subTotal)} ${amountLabel} · ${subCount} ${countLabel}</div>
        </div>
        ${toggleHTML}
      </div>
      <div class="pair-body">
        <div class="pair-left">
          <div class="pair-chart-wrap">
            <canvas id="pair-chart" height="120"></canvas>
            <button type="button" id="pair-clear" class="pair-clear-btn" style="display:none">Clear</button>
          </div>
          <div class="pair-stats" id="pair-stats"></div>
          ${isDelta ? '' : `
          <div class="pair-hits-help sub">
            Click the chart to set a 5s window, then drag the yellow edges to widen it (5s steps).
          </div>
          <div id="pair-hits-list"></div>`}
        </div>
        ${!showSourcePanel ? '' : `
        <div class="pair-right">
          <table class="source-breakdown">
            <caption>By source — click to filter</caption>
            <thead><tr>
              <th>Source</th>
              <th class="num">${pair.amountLabel || 'Damage'}</th>
              <th class="num">Hits</th>
            </tr></thead>
            <tbody>${sourceRows}</tbody>
          </table>
        </div>`}
      </div>
    </div>`;
  document.body.appendChild(modal);

  const close = () => {
    if (pairChartInstance) { pairChartInstance.destroy(); pairChartInstance = null; }
    modal.remove();
  };
  modal.addEventListener('click', e => { if (e.target === modal) close(); });
  modal.querySelector('.modal-close').addEventListener('click', close);
  const clearBtn = modal.querySelector('#pair-clear');
  if (clearBtn) {
    clearBtn.addEventListener('click', ev => {
      ev.stopPropagation();
      clearWindow();
    });
  }
  // Tanking-only metric toggle. Re-opens the modal with the new metric;
  // simpler than swapping data on the existing chart since the windowing
  // closure captures the current metric's data.
  modal.querySelectorAll('.pair-metric-toggle [data-pair-metric]').forEach(btn => {
    btn.addEventListener('click', ev => {
      ev.stopPropagation();
      const m = btn.dataset.pairMetric;
      if (m !== metric) showPairChart(pair, labels, bucketSeconds, m);
    });
  });

  // Filter state — null means "all". Captured via closure so re-opening
  // a different pair starts fresh.
  let selectedSource = null;
  // Selection-window state: inclusive bucket indices for the highlighted
  // range. windowStart === null means "no selection". The window starts at
  // a single bucket on first click and the user can drag either edge in
  // 5-second (one-bucket) steps to widen it; minimum width is 1 bucket.
  let windowStart = null;
  let windowEnd = null;
  let dragMode = null;  // 'left' | 'right' | null
  const EDGE_TOL = 8;   // px tolerance for grabbing a window edge

  const getFilteredHits = () => selectedSource === null
    ? allHits
    : allHits.filter(h => (h.source || 'Melee') === selectedSource);

  // Per-bucket pixel geometry. Recomputed on each call so resizes and
  // animation frames stay in sync — getPixelForValue depends on the
  // current chart size. Each bucket spans [gridline N, gridline N+1) so
  // window edges land exactly on Chart.js's vertical gridlines and snap
  // visually to the same divisions the user sees on the chart.
  function bucketWidthPx() {
    if (!pairChartInstance) return 0;
    const xs = pairChartInstance.scales.x;
    if (nBuckets > 1) return xs.getPixelForValue(1) - xs.getPixelForValue(0);
    return pairChartInstance.chartArea.width;
  }
  // For setting a 1-bucket window from a click — pick the bucket the
  // click falls inside (gridline N is the start of bucket N).
  function pxToBucket(px) {
    if (!pairChartInstance) return 0;
    const xs = pairChartInstance.scales.x;
    const w = bucketWidthPx() || 1;
    return Math.max(0, Math.min(nBuckets - 1,
      Math.floor((px - xs.getPixelForValue(0)) / w)));
  }
  // For dragging an edge — snap to the *nearest* gridline (0..nBuckets).
  function pxToGridline(px) {
    if (!pairChartInstance) return 0;
    const xs = pairChartInstance.scales.x;
    const w = bucketWidthPx() || 1;
    return Math.max(0, Math.min(nBuckets,
      Math.round((px - xs.getPixelForValue(0)) / w)));
  }
  function windowEdgePixels() {
    if (windowStart === null || !pairChartInstance) return null;
    const xs = pairChartInstance.scales.x;
    const w = bucketWidthPx();
    return {
      left: xs.getPixelForValue(windowStart),
      right: xs.getPixelForValue(windowEnd) + w,
    };
  }

  function syncClearButton() {
    const btn = document.getElementById('pair-clear');
    if (btn) btn.style.display = windowStart === null ? 'none' : '';
  }
  function updateStatsForSelection() {
    const filtered = getFilteredHits();
    let statsHits, statsSeries, range = null;
    if (windowStart === null) {
      statsHits = filtered;
      statsSeries = buildSeries(filtered);
    } else {
      const startS = windowStart * bucketSeconds;
      const endS = (windowEnd + 1) * bucketSeconds;
      statsHits = filtered.filter(h => h.offset_s >= startS && h.offset_s < endS);
      statsSeries = buildSeries(statsHits);
      range = { startS, endS };
    }
    const statsEl = document.getElementById('pair-stats');
    if (statsEl) {
      statsEl.innerHTML = computePairStatsHTML(
        statsHits, statsSeries, bucketSeconds, unit, range);
    }
  }
  function updateHitsList() {
    if (windowStart === null) {
      document.getElementById('pair-hits-list').innerHTML = '';
    } else {
      showHitsForRange(getFilteredHits(), windowStart, windowEnd,
                       bucketSeconds, pair.amountLabel || 'Damage');
    }
    updateStatsForSelection();
    syncClearButton();
  }
  function clearWindow() {
    windowStart = null;
    windowEnd = null;
    if (pairChartInstance) pairChartInstance.update('none');
    updateHitsList();
  }

  function buildSeries(hits) {
    const arr = new Array(nBuckets).fill(0);
    for (const h of hits) {
      const idx = Math.min(Math.max(0, Math.floor(h.offset_s / bucketSeconds)),
                           nBuckets - 1);
      arr[idx] += h.damage;
    }
    return arr;
  }

  function refresh() {
    const filtered = getFilteredHits();
    const series = buildSeries(filtered);
    const rateSeries = series.map(v => v / bucketSeconds);

    if (pairChartInstance) {
      pairChartInstance.data.datasets[0].data = rateSeries;
      pairChartInstance.update('none');
    }

    document.getElementById('pair-stats').innerHTML =
      computePairStatsHTML(filtered, series, bucketSeconds, unit);

    const subEl = document.getElementById('pair-sub');
    if (subEl) {
      const dmg = filtered.reduce((s, h) => s + h.damage, 0);
      const filterTag = selectedSource === null ? '' :
        ` · filter: <strong>${escapeHTML(selectedSource)}</strong>`;
      subEl.innerHTML = `${NUM(dmg)} ${amountLabel} · ${filtered.length} ${countLabel}${filterTag}`;
    }

    modal.querySelectorAll('.source-row').forEach(tr => {
      const isActive = (selectedSource === null && tr.dataset.source === '__all__') ||
                       tr.dataset.source === selectedSource;
      tr.classList.toggle('active', isActive);
    });

    // Drop the selection window on filter change — the previously-
    // selected range may have nothing left in it under the new filter.
    windowStart = null;
    windowEnd = null;
    document.getElementById('pair-hits-list').innerHTML = '';
    if (pairChartInstance) pairChartInstance.update('none');
    syncClearButton();
  }

  modal.querySelectorAll('.source-row').forEach(tr => {
    tr.addEventListener('click', ev => {
      ev.stopPropagation();
      const src = tr.dataset.source === '__all__' ? null : tr.dataset.source;
      selectedSource = src;
      refresh();
    });
  });

  // Chart-local plugin that draws the selection window: a translucent
  // fill spanning the selected bucket range plus two yellow vertical
  // edges with grip handles. Runs after the dataset draw so it sits on
  // top of the line/area.
  const windowOverlayPlugin = {
    id: 'windowOverlay',
    afterDatasetsDraw(chart) {
      if (windowStart === null) return;
      const ed = windowEdgePixels();
      if (!ed) return;
      const area = chart.chartArea;
      const ctx = chart.ctx;
      const left = Math.round(ed.left);
      const right = Math.round(ed.right);
      ctx.save();
      ctx.fillStyle = 'rgba(250, 204, 21, 0.10)';
      ctx.fillRect(left, area.top, Math.max(1, right - left), area.bottom - area.top);
      ctx.strokeStyle = '#facc15';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(left, area.top);  ctx.lineTo(left, area.bottom);
      ctx.moveTo(right, area.top); ctx.lineTo(right, area.bottom);
      ctx.stroke();
      // Grip handles — small filled rectangles centered vertically so the
      // edges look obviously draggable.
      ctx.fillStyle = '#facc15';
      const midY = (area.top + area.bottom) / 2;
      ctx.fillRect(left - 3, midY - 10, 6, 20);
      ctx.fillRect(right - 3, midY - 10, 6, 20);
      ctx.restore();
    },
  };

  // Build the chart with the unfiltered data, then call refresh() so the
  // stats panel and other pieces render through the same code path.
  // For delta, primarySeries is already the per-bucket delta (heals -
  // damage); we just convert to a per-second rate. For damage/healing,
  // we build from hits so the windowing/source-filter path works.
  const initialSeries = isDelta
    ? primarySeries.map(v => v / bucketSeconds)
    : buildSeries(allHits).map(v => v / bucketSeconds);
  pairChartInstance = new Chart(document.getElementById('pair-chart'), {
    type: 'line',
    plugins: [windowOverlayPlugin],
    data: {
      labels: labels,
      datasets: [{
        label: `${pair.attacker} → ${pair.target}`,
        data: initialSeries,
        backgroundColor: COLORS[0] + 'cc',
        borderColor: COLORS[0],
        borderWidth: 1.5,
        // Delta fills from origin so positives sit above zero and
        // negatives below; damage/healing fill to the bottom as before.
        fill: isDelta ? 'origin' : true,
        pointRadius: 0,
        tension: 0.3,
      }],
    },
    options: {
      responsive: true,
      scales: {
        x: { ticks: { color: '#94a3b8' }, grid: { color: '#2a3142' } },
        y: { beginAtZero: !isDelta,
             ticks: { color: '#94a3b8',
                      callback: v => SHORT(v) + ' ' + unit },
             grid: { color: '#2a3142' } },
      },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: ctx => `${SHORT(ctx.parsed.y)} ${unit}` } },
      },
    },
  });

  // Pointer events for setting and dragging the selection window. Pointer
  // capture means a drag started near an edge keeps tracking even if the
  // cursor leaves the canvas, so the user doesn't lose the gesture by
  // overshooting. Skipped for delta mode — there are no individual
  // events to drill into, so windowing has nothing to show.
  const canvas = pairChartInstance.canvas;
  if (isDelta) {
    refresh();
    return;
  }
  canvas.addEventListener('pointerdown', ev => {
    const rect = canvas.getBoundingClientRect();
    const x = ev.clientX - rect.left;
    const ed = windowEdgePixels();
    if (ed && Math.abs(x - ed.left) <= EDGE_TOL) {
      dragMode = 'left';
    } else if (ed && Math.abs(x - ed.right) <= EDGE_TOL) {
      dragMode = 'right';
    }
    if (dragMode) {
      canvas.setPointerCapture(ev.pointerId);
      ev.preventDefault();
    } else {
      // Plain click: reset to a 1-bucket window at the clicked spot.
      const idx = pxToBucket(x);
      windowStart = idx;
      windowEnd = idx;
      pairChartInstance.update('none');
      updateHitsList();
    }
  });
  canvas.addEventListener('pointermove', ev => {
    const rect = canvas.getBoundingClientRect();
    const x = ev.clientX - rect.left;
    if (dragMode) {
      // Snap to the nearest gridline. The right edge sits on gridline
      // (windowEnd + 1), so drag-right uses gridline - 1 to recover the
      // bucket index. Min window of 1 bucket is enforced by the clamps.
      const g = pxToGridline(x);
      if (dragMode === 'left') {
        windowStart = Math.min(g, windowEnd);
      } else {
        windowEnd = Math.max(g - 1, windowStart);
      }
      pairChartInstance.update('none');
      updateHitsList();
      return;
    }
    // Hover hint: show the resize cursor when over an edge.
    const ed = windowEdgePixels();
    canvas.style.cursor = (ed && (Math.abs(x - ed.left) <= EDGE_TOL ||
                                  Math.abs(x - ed.right) <= EDGE_TOL))
      ? 'ew-resize' : '';
  });
  const endDrag = ev => {
    if (!dragMode) return;
    if (canvas.hasPointerCapture(ev.pointerId)) {
      canvas.releasePointerCapture(ev.pointerId);
    }
    dragMode = null;
    updateHitsList();
  };
  canvas.addEventListener('pointerup', endDrag);
  canvas.addEventListener('pointercancel', endDrag);

  refresh();
}

function showPetOwnersModal(encounter) {
  // List the encounter's RAW attacker names alongside any existing pet-
  // owner mapping. Each row has an inline owner input + Save/Clear; the
  // user can assign a new owner, change an existing one, or clear back
  // to no mapping. Saves are per-row to keep the wire format simple.
  const existing = document.getElementById('pets-modal');
  if (existing) existing.remove();

  const petOwners = Object.assign({}, encounter.pet_owners || {});
  const rawAttackers = encounter.raw_attackers || [];
  // Map raw attacker names by lowercase for the input prefill (the
  // sidecar matches case-insensitively but stores the casing the user
  // first saved). Pre-bin candidate owners by side so each row's
  // dropdown only shows plausible owners — friendly pet → friendly
  // owners, enemy pet → enemy owners.
  const ownerByActorLo = {};
  for (const k of Object.keys(petOwners)) {
    ownerByActorLo[k.toLowerCase()] = petOwners[k];
  }
  const candidatesBySide = {friendly: [], enemy: []};
  for (const a of rawAttackers) {
    if (a.attacker.endsWith('`s pet') || a.attacker.endsWith("'s pet")) continue;
    const side = a.side === 'enemy' ? 'enemy' : 'friendly';
    if (candidatesBySide[side].indexOf(a.attacker) === -1) {
      candidatesBySide[side].push(a.attacker);
    }
  }

  // Sort raw attackers: those with a current mapping first, then by
  // damage desc. Makes the "what's currently set" answer obvious.
  const sortedRaw = rawAttackers.slice().sort((a, b) => {
    const am = ownerByActorLo[a.attacker.toLowerCase()] ? 1 : 0;
    const bm = ownerByActorLo[b.attacker.toLowerCase()] ? 1 : 0;
    if (am !== bm) return bm - am;
    return b.damage - a.damage;
  });

  const rowHTML = (a) => {
    const cur = ownerByActorLo[a.attacker.toLowerCase()] || '';
    const safeActor = escapeHTML(a.attacker);
    const side = a.side === 'enemy' ? 'enemy' : 'friendly';
    // The actor itself shouldn't be a candidate owner (would create a
    // self-loop). If the current saved owner isn't among the candidates
    // (e.g. an enemy chosen as a friendly's owner because the user knew
    // something the side classifier didn't), include it as a one-off so
    // the row still shows the correct value.
    const candidates = candidatesBySide[side]
      .filter(n => n.toLowerCase() !== a.attacker.toLowerCase());
    if (cur && candidates.findIndex(n => n.toLowerCase() === cur.toLowerCase()) === -1) {
      candidates.unshift(cur);
    }
    const sideTag = side === 'enemy'
      ? ' <span class="side-tag enemy">enemy</span>'
      : ' <span class="side-tag friendly">friendly</span>';
    const opts = `<option value="">(no owner)</option>` +
      candidates.map(n => {
        const sel = n.toLowerCase() === cur.toLowerCase() ? ' selected' : '';
        return `<option value="${escapeHTML(n)}"${sel}>${escapeHTML(n)}</option>`;
      }).join('');
    return `
      <tr data-actor="${safeActor}">
        <td class="actor">${safeActor}${sideTag}</td>
        <td class="num">${NUM(a.damage)}</td>
        <td><select class="owner-input">${opts}</select></td>
        <td class="row-actions">
          <button class="btn owner-clear">Clear</button>
        </td>
      </tr>`;
  };

  // Any owners assigned to actors NOT in this encounter are listed below
  // the table so the user can still see and clear them. Without this,
  // an assignment made on one encounter would be invisible from any
  // other encounter that doesn't include the same actor.
  const presentLo = new Set(rawAttackers.map(a => a.attacker.toLowerCase()));
  const otherOwners = Object.entries(petOwners)
    .filter(([actor]) => !presentLo.has(actor.toLowerCase()));
  const otherHTML = otherOwners.length === 0 ? '' : `
    <div class="pets-current-list">
      <h4>Other assignments (not in this encounter)</h4>
      <table class="pets-table">
        <thead><tr>
          <th>Actor</th><th>Owner</th><th class="row-actions"></th>
        </tr></thead>
        <tbody>
          ${otherOwners.map(([actor, owner]) => `
            <tr data-actor="${escapeHTML(actor)}">
              <td class="actor">${escapeHTML(actor)}</td>
              <td>${escapeHTML(owner)}</td>
              <td class="row-actions">
                <button class="btn owner-clear-other">Clear</button>
              </td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;

  const tableHTML = sortedRaw.length === 0
    ? '<div class="sub">No attackers in this encounter.</div>'
    : `
      <table class="pets-table">
        <thead><tr>
          <th>Actor</th>
          <th class="num">Damage in encounter</th>
          <th>Owner</th>
          <th class="row-actions"></th>
        </tr></thead>
        <tbody>${sortedRaw.map(rowHTML).join('')}</tbody>
      </table>`;

  const modal = document.createElement('div');
  modal.id = 'pets-modal';
  modal.className = 'modal-backdrop';
  modal.innerHTML = `
    <div class="modal pets-modal">
      <div class="pets-modal-actions">
        <button class="btn primary pets-save">Save</button>
        <button class="modal-close" aria-label="Close">×</button>
      </div>
      <h3>Pet owners</h3>
      <div class="pets-help">
        Assign an owner to actors that show up under their own name in the
        log (e.g. <code>Onyx Crusher</code> for a mage water pet). Their
        damage gets re-attributed to <code>&lt;owner&gt;\`s pet</code>.
        The owner dropdown is filtered to actors on the same side
        (friendly pet → friendly owners). Backtick-named pets are
        already handled automatically — assign them only if you want
        to override.
        <br>
        Pick owners from the dropdowns, then click <strong>Save</strong>
        to commit all changes at once. <strong>Clear</strong> on a row
        just resets that row to "(no owner)" — nothing is saved until
        you click Save. Close (×) discards everything.
      </div>
      ${tableHTML}
      ${otherHTML}
    </div>`;
  document.body.appendChild(modal);

  const close = () => modal.remove();
  modal.addEventListener('click', e => { if (e.target === modal) close(); });
  modal.querySelector('.modal-close').addEventListener('click', close);

  // Track Other-table rows the user has queued for clearing. They have
  // no dropdown to inspect, so we accumulate explicit intents here and
  // commit them as part of the batch on Save.
  const pendingOtherClears = new Set();

  // Main-table Clear: reset the dropdown to "(no owner)". Doesn't post
  // anything — the actual write happens when the user clicks Save.
  modal.querySelectorAll('.pets-table .owner-clear').forEach(btn => {
    btn.addEventListener('click', () => {
      const tr = btn.closest('tr');
      const select = tr.querySelector('select.owner-input');
      if (select) select.value = '';
    });
  });

  // Other-table Clear: queue the actor for a clear and dim the row so
  // the user sees the change is staged but not yet saved.
  modal.querySelectorAll('.pets-table .owner-clear-other').forEach(btn => {
    btn.addEventListener('click', () => {
      const tr = btn.closest('tr');
      const actor = tr.dataset.actor;
      pendingOtherClears.add(actor);
      tr.style.opacity = '0.4';
      tr.style.textDecoration = 'line-through';
      btn.disabled = true;
      btn.textContent = 'Cleared';
    });
  });

  // Save: collect all dropdown values that differ from their original,
  // plus any queued Other-table clears, and POST as a single batch so
  // the server invalidates the encounter cache once.
  const saveBtn = modal.querySelector('.pets-save');
  saveBtn.addEventListener('click', async () => {
    const updates = [];
    modal.querySelectorAll('tr[data-actor]').forEach(tr => {
      const select = tr.querySelector('select.owner-input');
      if (!select) return;  // Other-table row — handled below
      const actor = tr.dataset.actor;
      const cur = (select.value || '').trim();
      const orig = ownerByActorLo[actor.toLowerCase()] || '';
      if (cur.toLowerCase() !== orig.toLowerCase()) {
        updates.push({actor, owner: cur || null});
      }
    });
    for (const actor of pendingOtherClears) {
      updates.push({actor, owner: null});
    }
    if (updates.length === 0) {
      close();
      return;
    }
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';
    try {
      const r = await fetch('/api/pet-owners', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({updates}),
      });
      if (!r.ok) throw new Error(await r.text());
      close();
      route();
    } catch (e) {
      alert(`Save failed: ${e.message}`);
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save';
    }
  });
}

function computePairStatsHTML(hits, series, bucketSeconds, unit, selectionRange) {
  if (hits.length === 0) {
    return '<span class="sub">No data under the current filter.</span>';
  }
  const peak = Math.max(...series);
  const peakIdx = series.indexOf(peak);
  const active = series.filter(v => v > 0);
  const avgActive = active.length > 0
    ? active.reduce((a, b) => a + b, 0) / active.length : 0;
  const biggest = hits.reduce((m, h) => Math.max(m, h.damage), 0);
  const crits = hits.filter(h =>
    (h.mods || []).some(m => /critical|crippling/i.test(m))).length;
  const critRate = Math.round(crits / hits.length * 100);
  // When a window is set, replace the "Active: N/M buckets" stat with
  // the explicit time range — it's more informative than a bucket count
  // for a user-selected range, and matches the heading on the per-hit
  // table below.
  const rangeStat = selectionRange
    ? `<span><strong>Selected:</strong> +${selectionRange.startS}s – +${selectionRange.endS}s</span>`
    : `<span><strong>Active:</strong> ${active.length}/${series.length} buckets</span>`;
  return `
    <span><strong>Peak:</strong> ${SHORT(Math.round(peak / bucketSeconds))} ${unit}
          @ +${peakIdx * bucketSeconds}s</span>
    <span><strong>Avg active:</strong> ${SHORT(Math.round(avgActive / bucketSeconds))} ${unit}</span>
    ${rangeStat}
    <span><strong>Biggest:</strong> ${NUM(biggest)}</span>
    <span><strong>Crit:</strong> ${critRate}%</span>`;
}

function showHitsForRange(hits, startIdx, endIdx, bucketSeconds, amountLabel) {
  const start = startIdx * bucketSeconds;
  const end = (endIdx + 1) * bucketSeconds;
  const inRange = hits.filter(h => h.offset_s >= start && h.offset_s < end);
  const container = document.getElementById('pair-hits-list');
  if (!container) return;
  if (inRange.length === 0) {
    container.innerHTML = `<div class="sub" style="margin-top:12px">No hits in +${start}s to +${end}s under this filter.</div>`;
    return;
  }
  const total = inRange.reduce((s, h) => s + h.damage, 0);

  // Group hits by source so a high-hit-count window collapses to a few
  // expandable rows instead of a thousand-row dump. Each source row
  // expands inline to show its individual hits.
  const groups = {};
  for (const h of inRange) {
    const src = h.source || 'Melee';
    if (!groups[src]) groups[src] = { source: src, damage: 0, hits: [] };
    groups[src].damage += h.damage;
    groups[src].hits.push(h);
  }
  const sourceList = Object.values(groups)
    .sort((a, b) => b.damage - a.damage);

  const sourceRows = sourceList.map((g, i) => {
    const rowId = `src-detail-${i}`;
    const sortedHits = g.hits.slice().sort((a, b) => b.damage - a.damage);
    const detailRows = sortedHits.map(h => `
      <tr>
        <td class="num">+${h.offset_s}s</td>
        <td class="num">${NUM(h.damage)}</td>
        <td class="sub">${escapeHTML(h.kind || '')}</td>
        <td class="sub">${(h.mods || []).map(escapeHTML).join(', ') || '—'}</td>
      </tr>`).join('');
    return `
      <tr class="attacker-row" data-toggle="${rowId}">
        <td><span class="expand">▶</span>${escapeHTML(g.source)}</td>
        <td class="num">${NUM(g.damage)}</td>
        <td class="num">${g.hits.length}</td>
      </tr>
      <tr class="attacker-detail" id="${rowId}" style="display:none">
        <td colspan="3">
          <table class="pair-hits-detail-table">
            <thead><tr>
              <th class="num">+s</th>
              <th class="num">${amountLabel}</th>
              <th>Kind</th>
              <th>Modifiers</th>
            </tr></thead>
            <tbody>${detailRows}</tbody>
          </table>
        </td>
      </tr>`;
  }).join('');

  container.innerHTML = `
    <h4 class="pair-hits-heading">+${start}s to +${end}s · ${inRange.length} hits · ${NUM(total)} total</h4>
    <table class="pair-hits-table">
      <thead><tr>
        <th>Source</th>
        <th class="num">${amountLabel}</th>
        <th class="num">Hits</th>
      </tr></thead>
      <tbody>${sourceRows}</tbody>
    </table>`;

  // Wire up the per-source expand/collapse toggles. Scoped to this
  // container so we don't accidentally bind handlers to attacker rows
  // elsewhere on the page.
  container.querySelectorAll('tr.attacker-row').forEach(tr => {
    tr.addEventListener('click', () => {
      const detail = document.getElementById(tr.dataset.toggle);
      if (!detail) return;
      const collapsed = detail.style.display === 'none';
      detail.style.display = collapsed ? '' : 'none';
      tr.classList.toggle('expanded', collapsed);
    });
  });
}

// --- Router -----------------------------------------------------------

function route() {
  const hash = location.hash;
  const encMatch = hash.match(/^#\/encounter\/(\d+)/);
  if (encMatch) {
    renderEncounter(parseInt(encMatch[1], 10));
  } else if (hash.startsWith('#/picker')) {
    renderPicker(null);
  } else if (hash.startsWith('#/debug')) {
    renderDebug();
  } else if (hash.startsWith('#/session-summary')) {
    // Optional `?ids=1,2,3` scopes the summary to that subset of
    // encounters (driven by the session-table checkboxes). Whole-log
    // mode when absent.
    const idsMatch = hash.match(/[?&]ids=([0-9,]+)/);
    let ids = null;
    if (idsMatch) {
      ids = idsMatch[1].split(',')
        .map(s => parseInt(s, 10))
        .filter(n => Number.isFinite(n));
      if (ids.length === 0) ids = null;
    }
    renderSessionSummary(ids);
  } else {
    renderSession();
  }
}

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

window.addEventListener('hashchange', route);
window.addEventListener('load', route);

// --- Drag-and-drop log loading ----------------------------------------
//
// Browsers don't expose a dropped file's disk path (security), so we
// stream the file content to /api/upload and the server saves it under
// the OS temp dir. The dragCounter pattern handles dragenter/dragleave
// flicker as the cursor moves between child elements.

let _dragCounter = 0;

function _showDropOverlay() {
  if (document.getElementById('drop-overlay')) return;
  const div = document.createElement('div');
  div.id = 'drop-overlay';
  div.className = 'drop-overlay';
  div.innerHTML = `
    <div class="hint">
      <div>Drop log file to load</div>
      <div class="sub">eqlog_&lt;character&gt;_&lt;server&gt;.txt</div>
    </div>`;
  document.body.appendChild(div);
}

function _hideDropOverlay() {
  const div = document.getElementById('drop-overlay');
  if (div) div.remove();
}

window.addEventListener('dragenter', e => {
  if (!e.dataTransfer || !Array.from(e.dataTransfer.types || []).includes('Files')) return;
  e.preventDefault();
  _dragCounter++;
  if (_dragCounter === 1) _showDropOverlay();
});
window.addEventListener('dragleave', e => {
  if (!e.dataTransfer) return;
  e.preventDefault();
  _dragCounter = Math.max(0, _dragCounter - 1);
  if (_dragCounter === 0) _hideDropOverlay();
});
window.addEventListener('dragover', e => {
  if (!e.dataTransfer || !Array.from(e.dataTransfer.types || []).includes('Files')) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'copy';
});
window.addEventListener('drop', async e => {
  if (!e.dataTransfer || e.dataTransfer.files.length === 0) return;
  e.preventDefault();
  _dragCounter = 0;
  _hideDropOverlay();
  await uploadLog(e.dataTransfer.files[0]);
});

async function uploadLog(file) {
  const app = document.getElementById('app');
  // Three-stage status: upload bytes (with %), then "Parsing…" while
  // the server walks the log, then navigate. We use XHR rather than
  // fetch() because fetch doesn't expose upload progress events.
  app.innerHTML = `
    <div class="upload-status">
      <div class="upload-label">Uploading <strong>${escapeHTML(file.name)}</strong> (${fmtSize(file.size)})</div>
      <div class="progress-track"><div class="progress-fill" id="upload-bar" style="width:0%"></div></div>
      <div class="upload-pct sub" id="upload-pct">0%</div>
    </div>`;

  const setPct = txt => {
    const el = document.getElementById('upload-pct');
    if (el) el.textContent = txt;
  };
  const setBar = pct => {
    const el = document.getElementById('upload-bar');
    if (el) el.style.width = pct + '%';
  };

  // Parse-status poller. Started early (right after the XHR is sent) so
  // the UI flip from Uploading → Parsing is driven by the server-side
  // parse_progress state rather than xhr.upload.load — that event is
  // unreliable across browsers when the server holds the connection
  // open through a slow parse, leaving the UI stuck at "Uploading 100%"
  // until the server finally responds.
  let stopPoll = null;
  let phase = 'upload';   // 'upload' -> 'parse' (one-way transition)
  const setLabel = txt => {
    const el = document.querySelector('#app .upload-label');
    if (el) el.innerHTML = txt;
  };
  const flipToParsing = () => {
    if (phase !== 'upload') return;
    phase = 'parse';
    setBar(0);
    setPct('Parsing log…');
    setLabel(`Parsing <strong>${escapeHTML(file.name)}</strong>`);
  };

  try {
    await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/upload');
      xhr.setRequestHeader('X-Filename', encodeURIComponent(file.name));
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');

      xhr.upload.addEventListener('progress', e => {
        if (phase !== 'upload' || !e.lengthComputable) return;
        const pct = Math.round(e.loaded / e.total * 100);
        setBar(pct);
        setPct(pct + '%');
      });
      // Belt-and-suspenders: if the upload-side load event does fire,
      // flip immediately rather than waiting for the parse poll.
      xhr.upload.addEventListener('load', flipToParsing);
      xhr.addEventListener('load', () => {
        if (xhr.status >= 200 && xhr.status < 300) resolve();
        else reject(new Error(xhr.responseText || `HTTP ${xhr.status}`));
      });
      xhr.addEventListener('error', () => reject(new Error('Network error during upload')));
      xhr.addEventListener('abort', () => reject(new Error('Upload aborted')));
      xhr.send(file);

      // Start polling parse-status now. The poller flips the UI as soon
      // as the server reports 'parsing' state — this is the reliable
      // signal that the upload bytes have landed and parsing began.
      stopPoll = startParsePoll(s => {
        if (s.state === 'parsing') {
          flipToParsing();
          if (phase === 'parse') {
            setBar(s.pct);
            const note = (s.total_bytes > 0)
              ? `${fmtMB(s.bytes_read)} / ${fmtMB(s.total_bytes)} · ${s.pct.toFixed(1)}%`
              : `${s.pct.toFixed(1)}%`;
            setPct(note);
          }
        } else if (s.state === 'done' && phase === 'parse') {
          // Parse finished but the response hasn't landed yet (server
          // is still serializing/sending JSON). Show 100% so the bar
          // doesn't sit at the last polled value.
          setBar(100);
          setPct('Finalizing…');
        } else if (s.state === 'error') {
          setPct('Parse error');
        }
      });
    });

    if (stopPoll) { stopPoll(); stopPoll = null; }
    location.hash = '#/';
    route();
  } catch (e) {
    if (stopPoll) { stopPoll(); stopPoll = null; }
    app.innerHTML = `<div class="err">Upload failed: ${escapeHTML(e.message)}</div>`;
  }
}
</script>
</body>
</html>
'''
