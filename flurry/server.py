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
import time
import urllib.parse
import webbrowser
from datetime import datetime, timedelta
from typing import List, Optional

import sys

from .analyzer import (
    FightResult, Encounter, Heal,
    detect_combat, group_into_encounters, apply_pet_owners,
    merge_encounter, bucket_hits, collect_parser_stats,
    walk_into_detector, _CombatDetector,
    DEFAULT_SPECIAL_MODS,
)


# ----- Win32 overlay-pinning helpers (always-on-top + click-through) -----
#
# The overlay window can be made always-on-top AND click-through so
# mouse events pass through to EQ underneath. Browsers don't expose
# these capabilities to JavaScript, so we apply them via Win32 from
# the server side: find the browser window by title, toggle its
# WS_EX_TOPMOST / WS_EX_TRANSPARENT / WS_EX_LAYERED flags via
# SetWindowLongPtrW.
#
# Pin/unpin buttons live in the MAIN UI, not in the overlay itself —
# because once click-through is on, the overlay can't be clicked
# anymore. Toggling off has to come from the always-clickable main UI.
_user32 = None
if sys.platform == 'win32':
    try:
        import ctypes as _ctypes
        from ctypes import wintypes as _wintypes

        _user32 = _ctypes.windll.user32

        _GWL_EXSTYLE = -20
        _WS_EX_TOPMOST = 0x00000008
        _WS_EX_TRANSPARENT = 0x00000020
        _WS_EX_LAYERED = 0x00080000
        _LWA_ALPHA = 0x00000002
        _HWND_TOPMOST = _wintypes.HWND(-1)
        _HWND_NOTOPMOST = _wintypes.HWND(-2)
        _SWP_NOMOVE = 0x0002
        _SWP_NOSIZE = 0x0001
        _SWP_NOACTIVATE = 0x0010

        _EnumWindowsProc = _ctypes.WINFUNCTYPE(
            _wintypes.BOOL, _wintypes.HWND, _wintypes.LPARAM)

        _EnumWindows = _user32.EnumWindows
        _EnumWindows.argtypes = [_EnumWindowsProc, _wintypes.LPARAM]
        _EnumWindows.restype = _wintypes.BOOL

        _GetWindowTextLengthW = _user32.GetWindowTextLengthW
        _GetWindowTextLengthW.argtypes = [_wintypes.HWND]
        _GetWindowTextLengthW.restype = _ctypes.c_int

        _GetWindowTextW = _user32.GetWindowTextW
        _GetWindowTextW.argtypes = [
            _wintypes.HWND, _wintypes.LPWSTR, _ctypes.c_int]
        _GetWindowTextW.restype = _ctypes.c_int

        _IsWindowVisible = _user32.IsWindowVisible
        _IsWindowVisible.argtypes = [_wintypes.HWND]
        _IsWindowVisible.restype = _wintypes.BOOL

        _GetWindowLongPtrW = _user32.GetWindowLongPtrW
        _GetWindowLongPtrW.argtypes = [_wintypes.HWND, _ctypes.c_int]
        _GetWindowLongPtrW.restype = _ctypes.c_ssize_t

        _SetWindowLongPtrW = _user32.SetWindowLongPtrW
        _SetWindowLongPtrW.argtypes = [
            _wintypes.HWND, _ctypes.c_int, _ctypes.c_ssize_t]
        _SetWindowLongPtrW.restype = _ctypes.c_ssize_t

        _SetLayeredWindowAttributes = _user32.SetLayeredWindowAttributes
        _SetLayeredWindowAttributes.argtypes = [
            _wintypes.HWND, _wintypes.COLORREF,
            _wintypes.BYTE, _wintypes.DWORD]
        _SetLayeredWindowAttributes.restype = _wintypes.BOOL

        _SetWindowPos = _user32.SetWindowPos
        _SetWindowPos.argtypes = [
            _wintypes.HWND, _wintypes.HWND,
            _ctypes.c_int, _ctypes.c_int,
            _ctypes.c_int, _ctypes.c_int, _wintypes.UINT]
        _SetWindowPos.restype = _wintypes.BOOL
    except Exception:
        _user32 = None


def _find_overlay_window():
    """Find the browser window currently showing the flurry overlay
    (`/overlay`). Browsers append their app name to the page title:
      - Firefox: 'Flurry — Live — Mozilla Firefox'
      - Edge:    'Flurry — Live - Microsoft Edge'
      - Chrome:  'Flurry — Live - Google Chrome'
    so we substring-match on 'Flurry — Live' (and the hyphen variant
    in case any browser transcribes the em-dash). Returns the first
    visible match's HWND, or None."""
    if _user32 is None:
        return None
    found: List = []

    @_EnumWindowsProc
    def callback(hwnd, lparam):
        if not _IsWindowVisible(hwnd):
            return True
        length = _GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = _ctypes.create_unicode_buffer(length + 1)
        _GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if 'Flurry — Live' in title or 'Flurry - Live' in title:
            found.append(hwnd)
        return True  # continue enumeration

    _EnumWindows(callback, 0)
    return found[0] if found else None


def _pin_overlay_window(alpha: int = 255) -> bool:
    """Apply always-on-top + click-through to the overlay browser
    window. `alpha` is 0-255 (255 = fully opaque). Returns True if the
    window was found and styles were applied."""
    if _user32 is None:
        return False
    hwnd = _find_overlay_window()
    if hwnd is None:
        return False
    # Add WS_EX_LAYERED (required for click-through to work) and
    # WS_EX_TRANSPARENT (mouse events pass through to whatever's
    # underneath — typically the EQ window).
    current = _GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
    new_style = current | _WS_EX_LAYERED | _WS_EX_TRANSPARENT
    _SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, new_style)
    # Required after setting WS_EX_LAYERED — without an alpha attr the
    # window can paint as fully transparent (invisible). 255 = opaque.
    _SetLayeredWindowAttributes(
        hwnd, 0, max(0, min(255, alpha)), _LWA_ALPHA)
    # Always-on-top via HWND_TOPMOST. SWP_NOMOVE/NOSIZE preserve the
    # user's position/size; SWP_NOACTIVATE doesn't steal focus.
    _SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
                  _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)
    return True


def _unpin_overlay_window() -> bool:
    """Remove always-on-top + click-through from the overlay window
    (revert to a normal browser window). Returns True if the window
    was found."""
    if _user32 is None:
        return False
    hwnd = _find_overlay_window()
    if hwnd is None:
        return False
    current = _GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
    new_style = current & ~(_WS_EX_LAYERED | _WS_EX_TRANSPARENT)
    _SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, new_style)
    _SetWindowPos(hwnd, _HWND_NOTOPMOST, 0, 0, 0, 0,
                  _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)
    return True


# ----- Native file dialog (server-side OS picker) -----
#
# Browsers don't expose a picked file's disk path to JS — for security
# reasons, `<input type="file">` only gives the front-end the file's
# CONTENT and basename. That's why the existing "Upload" path copies
# the bytes to a temp dir and follows the static copy.
#
# To give users the standard "Browse via OS file dialog AND track the
# original file live" flow, we open the dialog server-side via Tk and
# get the path that way. tkinter is in Python's stdlib, comes along
# in the PyInstaller bundle automatically, and renders the same
# Windows file picker any other app does.
try:
    import tkinter as _tk
    from tkinter import filedialog as _tk_fd
    _TK_AVAILABLE = True
except ImportError:
    _TK_AVAILABLE = False

# Serialize dialog opens so multiple concurrent requests don't fight
# over Tk state. In practice the user opens at most one dialog at a
# time but defensive coding here is cheap.
_native_dialog_lock = threading.Lock()


def _native_file_picker(initial_dir: Optional[str] = None) -> Optional[str]:
    """Open the OS native file-picker dialog (Tk-driven) and return
    the user's selected path, or None if the dialog was cancelled or
    Tk is unavailable. Synchronous — blocks the calling thread until
    the user picks a file or dismisses the dialog."""
    if not _TK_AVAILABLE:
        return None
    with _native_dialog_lock:
        root = _tk.Tk()
        try:
            # Hide the empty Tk window — we only want the dialog.
            root.withdraw()
            # Force the dialog above other windows (the browser is
            # often the active window when this is invoked).
            root.attributes('-topmost', True)
            picked = _tk_fd.askopenfilename(
                parent=root,
                initialdir=initial_dir,
                title='Open EQ log',
                filetypes=[
                    ('EQ log files', 'eqlog_*.txt'),
                    ('Text files', '*.txt'),
                    ('All files', '*.*'),
                ],
            )
        finally:
            try:
                root.destroy()
            except Exception:
                pass
    # askopenfilename returns '' on cancel.
    return picked if picked else None


from .sidecar import (
    Sidecar, fight_key, load_sidecar, save_sidecar,
)
from .tail import read_last_timestamp


# ----- Static asset resolver -----
#
# The HTML/CSS/JS for the front-end live as files under flurry/static/
# (they used to be one giant string constant inside this module — see
# CONTEXT.md "Where things live"). Two runtime layouts to support:
#
#   1. Source tree / pip install: flurry/static/ sits next to this file,
#      so importlib.resources locates it via the package.
#   2. PyInstaller --onefile bundle: the bootloader extracts the bundle
#      to sys._MEIPASS at startup; build_exe.py ships flurry/static via
#      --add-data so the same package-relative path resolves.
#
# importlib.resources.files() handles both transparently.

_STATIC_CONTENT_TYPES = {
    '.css':  'text/css; charset=utf-8',
    '.js':   'application/javascript; charset=utf-8',
    '.html': 'text/html; charset=utf-8',
    '.svg':  'image/svg+xml',
    '.png':  'image/png',
    '.ico':  'image/x-icon',
}


def _static_path(filename: str) -> Optional[str]:
    """Resolve a name under flurry/static/ to an on-disk path. Returns
    None if the file doesn't exist or the request escapes the static
    dir (basic path-traversal guard — only bare filenames allowed)."""
    safe = os.path.basename(filename)
    if not safe or safe != filename:
        return None
    try:
        from importlib.resources import files
        path = files('flurry') / 'static' / safe
        if not path.is_file():
            return None
        return str(path)
    except (FileNotFoundError, ModuleNotFoundError):
        return None


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


def _diff_payload(enc_a: Encounter, enc_b: Encounter,
                  log_a: Optional[str] = None,
                  log_b: Optional[str] = None) -> dict:
    """Two-encounter side-by-side diff.

    Builds a parallel per-actor payload over the union of attackers/
    defenders/healers in both encounters. The three metric tabs (damage,
    damage taken, healing) all share a common row shape so the front-end
    swaps modes through one render path.

    Same-log mode: callers resolve both encounters from the same
    `_get_encounters()` list and leave `log_a`/`log_b` as None.
    Cross-log mode: callers pull `enc_a` from the primary log's
    encounters, `enc_b` from the comparison log's, and pass each log's
    basename so the front-end can label the encounter cards.
    """
    pair = [enc_a, enc_b]
    merged = [merge_encounter(e) for e in pair]

    # Side classification — same `received > dealt + healed` rule the
    # encounter detail and session summary use, but evaluated across the
    # union of the two encounters so an actor stays on the same side in
    # both columns even if one encounter alone would flip them.
    canonical: dict = {}
    sums: dict = {}

    def _bump(lo, canon, **deltas):
        canonical.setdefault(lo, canon)
        rec = sums.setdefault(lo, {'damage': 0, 'received': 0, 'healed': 0})
        for k, v in deltas.items():
            rec[k] += v

    for i, e in enumerate(pair):
        for atk, s in merged[i].stats_by_attacker.items():
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

    # Per-encounter, per-actor metric pull. Damage from the merged
    # FightResult.attackers, damage_taken from defends_by_pair (rolled up
    # across all attackers per defender), healing from Encounter.heals.
    def actors_for(e: Encounter, m) -> dict:
        out: dict = {}

        def _row(name: str) -> dict:
            return out.setdefault(name.lower(), {
                'name': name,
                'damage': 0, 'damage_taken': 0, 'healing': 0,
                'biggest_dmg': 0, 'biggest_taken': 0, 'biggest_heal': 0,
            })

        for atk, s in m.stats_by_attacker.items():
            r = _row(atk)
            r['damage'] += s.damage
            if s.biggest > r['biggest_dmg']:
                r['biggest_dmg'] = s.biggest
        for (_, def_name), d in m.defends_by_pair.items():
            if d.damage_taken == 0:
                continue
            r = _row(def_name)
            r['damage_taken'] += d.damage_taken
            if d.biggest_taken > r['biggest_taken']:
                r['biggest_taken'] = d.biggest_taken
        for h in e.heals:
            r = _row(h.healer)
            r['healing'] += h.amount
            if h.amount > r['biggest_heal']:
                r['biggest_heal'] = h.amount
        return out

    per_enc = [actors_for(pair[i], merged[i]) for i in range(2)]

    # Union of names across both encounters. Each row carries a length-2
    # `values` array — index 0 = encounter A, index 1 = encounter B. An
    # actor missing from one side gets a zero-row there so the front-end
    # can still render it in the diff.
    all_keys = set(per_enc[0].keys()) | set(per_enc[1].keys())
    actors = []
    durations = [pair[i].duration_seconds or 1.0 for i in range(2)]
    for key in all_keys:
        row_a = per_enc[0].get(key)
        row_b = per_enc[1].get(key)
        canon_name = (row_a or row_b)['name']
        values = []
        for i, row in enumerate((row_a, row_b)):
            if row is None:
                values.append({
                    'damage': 0, 'dps': 0,
                    'damage_taken': 0, 'dtps': 0,
                    'healing': 0, 'hps': 0,
                    'biggest_dmg': 0, 'biggest_taken': 0, 'biggest_heal': 0,
                    'present': False,
                })
            else:
                d = durations[i]
                values.append({
                    'damage': row['damage'],
                    'dps': round(row['damage'] / d),
                    'damage_taken': row['damage_taken'],
                    'dtps': round(row['damage_taken'] / d),
                    'healing': row['healing'],
                    'hps': round(row['healing'] / d),
                    'biggest_dmg': row['biggest_dmg'],
                    'biggest_taken': row['biggest_taken'],
                    'biggest_heal': row['biggest_heal'],
                    'present': True,
                })
        actors.append({
            'name': canon_name,
            'side': sides.get(key, 'friendly'),
            'values': values,
        })

    log_labels = [log_a, log_b]
    encounter_meta = [{
        'encounter_id': e.encounter_id,
        'name': _encounter_summary(e)['name'],
        'start': e.start.strftime('%Y-%m-%d %H:%M:%S') if e.start else None,
        'duration_seconds': round(e.duration_seconds, 1),
        'fight_complete': e.fight_complete,
        'total_damage': e.total_damage,
        'total_healing': e.total_healing,
        'raid_dps': round(e.raid_dps),
        'log': log_labels[i],
    } for i, e in enumerate(pair)]

    # `cross_log` flips the front-end into cross-log presentation (log
    # filename badge on each encounter card, "Drop comparison log" hint
    # in the header). True when either label is provided — same-log
    # callers leave both None and the field stays False.
    cross_log = bool(log_a or log_b)

    return {
        'encounters': encounter_meta,
        'actors': actors,
        'cross_log': cross_log,
    }


# ----- Live snapshot helpers -----
#
# Player identity in EQ logs has four context-dependent forms:
#   - 'You'      — first-person attacker ("You slash X")
#   - 'YOU'      — defender, all caps ("X hits YOU")
#   - 'you'      — heal target ("Emberlight healed you")
#   - 'yourself' — self-heal target ("You healed yourself")
# `char_name` from the logfile basename (`eqlog_<char>_<server>.txt`)
# is the cosmetic third-person name; it doesn't appear in first-person
# attribution. The `_is_you` helper normalizes all four.

_EQLOG_NAME_RE = re.compile(r'^eqlog_([A-Za-z]+)_', re.IGNORECASE)
_PLAYER_ALIASES = {'you', 'yourself'}


def _parse_char_name(logfile_path: Optional[str]) -> Optional[str]:
    if not logfile_path:
        return None
    m = _EQLOG_NAME_RE.match(os.path.basename(logfile_path))
    return m.group(1) if m else None


def _is_you(name: str) -> bool:
    return name.lower() in _PLAYER_ALIASES


def _is_your_pet(name: str, char_name: Optional[str]) -> bool:
    """Match attackers belonging to the logging player. Assumes
    `apply_pet_owners()` has been run on the fights upstream — after
    that pass, all of the player's pets (necro / beastlord backtick
    form like 'Hacral`s pet' AND mage / charmed pets remapped via the
    sidecar's pet_owners) carry the unified `<owner>`s pet` name. So
    this just compares the name's prefix to the char name parsed from
    the logfile basename.

    Limitation: EQ logs don't include instance IDs, so two pets with
    the same name in the same group (rare but possible) collapse onto
    one attribution. Same applies if a charmed mob shares its name
    with an enemy mob in the same encounter. Both are accepted as
    known limitations of the log format."""
    if not char_name:
        return False
    name_lower = name.lower()
    suffix = '`s pet'
    if not name_lower.endswith(suffix):
        return False
    return name_lower[:-len(suffix)] == char_name.lower()


def _player_metrics(fight: FightResult, heals_in_window: List[Heal],
                    char_name: Optional[str] = None) -> dict:
    """Aggregate the four overlay counters (damage out/in, healing out/in)
    for the logging player from a single fight + the heals scoped to
    that fight's time window. Rate fields are per-second.

    `damage_out` rolls in the player's pets — necro/beastlord pets are
    auto-named `<char>`s pet` by EQ; mage/charmed pets need a sidecar
    pet_owners entry (set via the encounter detail's Pet owners button)
    so `apply_pet_owners` rewrites their attacker name to the same
    canonical form. `damage_in` / healing stay player-only — pet
    health tracking would be useful but isn't what raid leaders look
    at on the player's overlay.
    """
    duration = max(fight.duration_seconds or 1.0, 1.0)

    def _is_player_or_pet(atk):
        return _is_you(atk) or _is_your_pet(atk, char_name)

    damage_out = sum(s.damage for atk, s in fight.stats_by_attacker.items()
                     if _is_player_or_pet(atk))
    damage_in = sum(d.damage_taken for (_, defender), d
                    in fight.defends_by_pair.items()
                    if _is_you(defender))
    healing_out = sum(h.amount for h in heals_in_window if _is_you(h.healer))
    healing_in = sum(h.amount for h in heals_in_window if _is_you(h.target))
    return {
        'damage_out':  damage_out,
        'dps_out':     round(damage_out / duration),
        'damage_in':   damage_in,
        'dtps_in':     round(damage_in / duration),
        'healing_out': healing_out,
        'hps_out':     round(healing_out / duration),
        'healing_in':  healing_in,
        'hps_in':      round(healing_in / duration),
        # HP delta = healing received minus damage taken, per second.
        # Positive = net heal, negative = net loss. Drives the green/red
        # indicator in the overlay.
        'hp_delta_per_sec': round((healing_in - damage_in) / duration),
    }


def _top_damage(fight: FightResult, n: int = 8,
                char_name: Optional[str] = None) -> List[dict]:
    """Top-N damage dealers in the fight, sorted desc. Used by the
    overlay's recap state and by the clipboard copy.

    Each row carries `is_you` and `is_your_pet` flags so the front-end
    can style the player's own contributions distinctly (player +
    their pets get highlighted; everyone else's pet rolls up to its
    owner via apply_pet_owners upstream)."""
    total = fight.total_damage or 1
    duration = max(fight.duration_seconds or 1.0, 1.0)
    rows = []
    for s in sorted(fight.stats_by_attacker.values(),
                    key=lambda x: x.damage, reverse=True):
        if s.damage <= 0:
            continue
        # Skip the boss/adds (enemies). Cheap heuristic that works for
        # the typical raid log: the target is exactly one of the
        # attackers we want to exclude. Friendlies vs enemies is the
        # encounter-detail rule but it requires aggregate state we
        # don't want to recompute on every fast poll; the target-name
        # exclusion catches the vast majority.
        if s.attacker.lower() == fight.target.lower():
            continue
        rows.append({
            'name':       s.attacker,
            'damage':     s.damage,
            'dps':        round(s.damage / duration),
            'pct':        round(s.damage / total * 100, 1),
            'is_you':     _is_you(s.attacker),
            'is_your_pet': _is_your_pet(s.attacker, char_name),
        })
        if len(rows) >= n:
            break
    return rows


def _top_healing(heals: List[Heal], duration: float, n: int = 5) -> List[dict]:
    duration = max(duration or 1.0, 1.0)
    by_healer: dict = {}
    for h in heals:
        rec = by_healer.setdefault(h.healer, {'name': h.healer,
                                              'healing': 0,
                                              'is_you': _is_you(h.healer)})
        rec['healing'] += h.amount
    rows = sorted(by_healer.values(), key=lambda x: x['healing'],
                  reverse=True)[:n]
    for r in rows:
        r['hps'] = round(r['healing'] / duration)
    return rows


def _live_last_encounter(encounters: List[Encounter],
                         all_heals: List[Heal],
                         char_name: Optional[str] = None) -> Optional[dict]:
    """Recap of the most recent completed encounter. Used in the
    overlay's no-active-fight state. Falls back to the latest fight if
    grouping isn't available (shouldn't happen in normal flow)."""
    if not encounters:
        return None
    # Most-recent encounter by end time (some encounters can run long
    # while a quick trash kill encounter ends after — sort by `end`).
    e = max(encounters, key=lambda x: x.end or datetime.min)
    flat = merge_encounter(e)
    # Heals scoped to encounter window. Encounter.heals is already
    # populated by group_into_encounters but the overlay's player
    # metrics need the same shape as the active-fight path.
    heals_window = list(e.heals)
    return {
        'encounter_id':       e.encounter_id,
        'name':               _encounter_summary(e)['name'],
        'start':              e.start.strftime('%Y-%m-%d %H:%M:%S') if e.start else None,
        'end':                e.end.strftime('%Y-%m-%d %H:%M:%S') if e.end else None,
        'duration_seconds':   round(e.duration_seconds, 1),
        'fight_complete':     e.fight_complete,
        'raid_total_damage':  e.total_damage,
        'you':                _player_metrics(flat, heals_window,
                                              char_name=char_name),
        'top_damage':         _top_damage(flat, n=8, char_name=char_name),
        'top_healing':        _top_healing(heals_window,
                                           e.duration_seconds, n=5),
    }


def _live_snapshot_payload() -> dict:
    """Build the live-overlay payload. Acquires the lock briefly to
    snapshot detector state, then releases before doing the (purely
    Python) computation work — keeps the follower thread unblocked."""
    char_name = _parse_char_name(_State.logfile)
    follower = _State.live_follower
    follower_running = follower is not None and follower.is_running()
    base = {
        'live_enabled':     _State.live_enabled,
        'follower_running': follower_running,
        'logfile_basename': (os.path.basename(_State.logfile)
                             if _State.logfile else None),
        'char_name':        char_name,
        'last_event_ts':    None,
        'active_fight':     None,
        'last_encounter':   None,
    }
    if _State.logfile is None or _State.detector is None:
        return base

    # Snapshot the detector state under the lock. We finalize the
    # in-progress fight via _live_active_fight; we group the completed
    # fights via group_into_encounters for the recap. Both are read-only
    # ops on the detector but we hold the lock to ensure consistency.
    with _State.fights_lock:
        detector = _State.detector
        if detector is None:
            return base
        last_event = detector.last_event_ts
        # Snapshot copies. detector.heals and detector.completed are
        # lists; copy them so we can compute outside the lock if we
        # later want to (currently we hold the lock for simplicity).
        all_heals = list(detector.heals)
        completed = list(detector.completed)
        in_progress_copy = dict(detector.in_progress)

        # Read the sidecar's pet_owners map up front — used to roll
        # mage / charmed pets onto their owner across both the active
        # fight and the recap. The encounter detail's "Pet owners"
        # editor populates this; for necro/beastlord pets that EQ
        # already names `<owner>`s pet`, no entry is needed (the
        # parser already attributes correctly).
        sidecar_for_pets = _State.sidecar or Sidecar.empty()
        pet_owners_map = sidecar_for_pets.pet_owners or {}

        # Active fight: pick the most-recent ENEMY fight (target isn't
        # the player or a pet) — the analyzer creates a separate fight
        # per defender, so when a boss hits the player there's both a
        # boss fight (target='a sim grabber') AND a YOU fight (target=
        # 'YOU'). The user wants to see the boss as the engagement.
        # Suppressed when live mode is off (user is reviewing static
        # history, not actively fighting).
        active = None
        if in_progress_copy and _State.live_enabled:
            # Snapshot ALL in-progress builders to FightResults first so
            # apply_pet_owners can rewrite them uniformly. After this,
            # all of the player's pets (necro/beastlord backtick form
            # AND mage/charmed pets via the sidecar) carry the unified
            # `<owner>`s pet` name, so _is_your_pet just checks the
            # name prefix.
            in_progress_snapshots = [
                b.finalize(b.last_ts, fight_complete=False)
                for b in in_progress_copy.values()
            ]
            if pet_owners_map:
                in_progress_snapshots, _ = apply_pet_owners(
                    in_progress_snapshots, [], pet_owners_map)

            enemy_fights = [
                f for f in in_progress_snapshots
                if not _is_you(f.target)
                and not f.target.endswith('`s pet')
            ]
            if enemy_fights:
                # Pick the most-recent enemy fight as the displayed
                # target. (Multi-mob aware grouping was an earlier
                # planned improvement; for now show the most recently
                # active enemy.)
                primary = max(enemy_fights, key=lambda f: f.end or f.start)
                window_start = min(f.start for f in in_progress_snapshots)
                window_end = max((f.end or f.start) for f in in_progress_snapshots)
                window_dur = max((window_end - window_start).total_seconds(), 1.0)
                heals_window = [h for h in all_heals
                                if window_start <= h.timestamp <= window_end]
                # Player + their pets aggregated across all in-progress
                # fights (so dmg-out on the boss fight + dmg-in on the
                # YOU fight + pet damage on the boss fight all roll up
                # to one set of headline counters).
                def _player_or_pet(atk):
                    return _is_you(atk) or _is_your_pet(atk, char_name)
                damage_out = 0
                damage_in = 0
                for f in in_progress_snapshots:
                    damage_out += sum(s.damage
                                      for atk, s in f.stats_by_attacker.items()
                                      if _player_or_pet(atk))
                    damage_in += sum(d.damage_taken
                                     for (_, defender), d
                                     in f.defends_by_pair.items()
                                     if _is_you(defender))
                healing_out = sum(h.amount for h in heals_window if _is_you(h.healer))
                healing_in = sum(h.amount for h in heals_window if _is_you(h.target))
                you_metrics = {
                    'damage_out':  damage_out,
                    'dps_out':     round(damage_out / window_dur),
                    'damage_in':   damage_in,
                    'dtps_in':     round(damage_in / window_dur),
                    'healing_out': healing_out,
                    'hps_out':     round(healing_out / window_dur),
                    'healing_in':  healing_in,
                    'hps_in':      round(healing_in / window_dur),
                    'hp_delta_per_sec': round((healing_in - damage_in) / window_dur),
                }
                active = {
                    'target':             primary.target,
                    'start':              primary.start.strftime('%Y-%m-%d %H:%M:%S'),
                    'duration_seconds':   round(primary.duration_seconds, 1),
                    'raid_total_damage':  primary.total_damage,
                    'you':                you_metrics,
                    'top_damage':         _top_damage(primary, n=8,
                                                      char_name=char_name),
                    'top_healing':        _top_healing(heals_window,
                                                       window_dur, n=5),
                }

        # Last encounter for recap. Always re-derive from the detector's
        # completed fights — the cached `_State.encounters` was built at
        # initial-parse time and doesn't include fights the follower has
        # closed since. Re-grouping is cheap (linear in completed
        # fights). Apply pet owners + manual groups the same way the
        # main UI does so the recap names match.
        sidecar = _State.sidecar or Sidecar.empty()
        if sidecar.pet_owners:
            fights2, heals2 = apply_pet_owners(
                completed, all_heals, sidecar.pet_owners)
        else:
            fights2, heals2 = completed, all_heals
        encounters = group_into_encounters(
            fights2,
            gap_seconds=_State.encounter_gap_seconds,
            heals=heals2,
            manual_groups=sidecar.manual_groups_for_grouper())
        last_enc = _live_last_encounter(encounters, all_heals,
                                        char_name=char_name)

        # Debug counters to diagnose "live tail not working" reports —
        # these surface in /api/live/snapshot so the user can compare
        # follower position to file size and see if events are landing.
        try:
            file_size = (os.path.getsize(_State.logfile)
                         if _State.logfile and os.path.isfile(_State.logfile)
                         else 0)
        except OSError:
            file_size = 0
        debug = {
            # Full path flurry is following. Drag-drop copies the file
            # to %TEMP%\flurry-uploads\... and follows the COPY (which
            # doesn't grow as EQ writes); Browse / paste-path follows
            # the original. If this isn't the path EQ is writing to,
            # that's the entire reason live tail looks frozen.
            'logfile_path':       _State.logfile,
            'file_size_bytes':    file_size,
            'follower_position':  _State.live_position,
            'bytes_behind':       max(0, file_size - _State.live_position),
            'in_progress_fights': len(in_progress_copy),
            'completed_fights':   len(completed),
            'total_heal_events':  len(all_heals),
        }

    base['last_event_ts'] = last_event.strftime('%Y-%m-%d %H:%M:%S') if last_event else None
    base['active_fight'] = active
    base['last_encounter'] = last_enc
    base['debug'] = debug
    return base


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
    # ----- Comparison log (cross-log diff) -----
    # A second log loaded alongside the primary, used solely for cross-log
    # diffing. Parsed with the same detection params as primary so the two
    # are comparable. Read-only from the cross-log diff path — no encounter
    # editing / merging / sidecar mutation flows through these slots; only
    # the primary log gets that. Cleared when the primary log changes
    # (the comparison context loses meaning when the primary swaps).
    comparison_logfile: Optional[str] = None
    comparison_fights: Optional[List[FightResult]] = None
    comparison_heals: Optional[List[Heal]] = None
    comparison_encounters: Optional[List[Encounter]] = None
    comparison_sidecar: Optional[Sidecar] = None
    # ----- Live mode -----
    # When `live_enabled` is True and a log is loaded, the follower thread
    # holds the file open at `live_position` and polls every `poll_ms` for
    # new bytes. New events get parsed and fed into the long-lived
    # `detector`, which is what the live-snapshot endpoint reads. Default
    # ON when a log loads — typically you're loading a log that's still
    # being written to during a raid; users reviewing historical logs can
    # toggle off via the header. The detector survives across param
    # changes (since detection params change the slicing of the same
    # event stream, not the events themselves — though we currently do a
    # full re-walk on param change for simplicity).
    live_enabled: bool = True
    live_poll_ms: int = 250
    detector: Optional['_CombatDetector'] = None
    live_position: int = 0  # byte offset in logfile the follower has reached
    live_follower: Optional['_LiveFollower'] = None
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

    Builds a long-lived `_CombatDetector` on `_State.detector` so live
    mode's follower thread can continue feeding events into the same
    accumulator after the initial walk. `_State.fights` and
    `_State.heals` are snapshotted from the detector here and stay
    static until the next invalidation — Phase 1 doesn't auto-refresh
    them between snapshots; the live overlay reads the detector
    directly via /api/live/snapshot.

    `_State.fights` and `_State.heals` are deliberately the RAW outputs
    of the detector (no pet-owner rewrite). Rewriting happens later in
    `_get_encounters_locked` so the raw attacker names stay visible for
    the pet-owner edit modal. The cost is one extra pass per sidecar
    edit; we trade a little CPU for a much simpler edit flow.

    During the parse we update `_State.parse_progress` periodically so
    a concurrent /api/parse-status request can show a live progress
    bar. Sidecar edits don't trigger a re-parse (they only invalidate
    the encounter cache), so the progress bar is only relevant on the
    first load + reload + param change paths."""
    if _State.fights is not None and _State.heals is not None:
        return
    since = _resolve_since_locked()
    total = os.path.getsize(_State.logfile) if os.path.isfile(_State.logfile) else 0
    _set_progress('parsing', bytes_read=0, total_bytes=total)

    def _on_progress(bytes_read: int, total_bytes: int):
        _set_progress('parsing', bytes_read=bytes_read,
                      total_bytes=total_bytes)

    detector = _CombatDetector(
        gap_seconds=_State.gap_seconds,
        min_damage=_State.min_damage,
        min_duration_seconds=_State.min_duration_seconds,
        heals_extend_fights=_State.heals_extend_fights,
    )
    try:
        end_pos = walk_into_detector(
            _State.logfile, detector,
            since=since, progress_cb=_on_progress,
        )
    except Exception as e:
        _set_progress('error', total_bytes=total, message=f'{type(e).__name__}: {e}')
        raise
    # IMPORTANT: don't finalize_all() here — that would close every
    # in-progress fight unconditionally, which is wrong when the live
    # follower is about to extend the detector. We DO run expire_stale()
    # so fights that have actually been quiet for >gap_seconds (typical
    # for static / historical logs) close cleanly. Genuinely-active
    # fights (within gap_seconds of the last event) stay open for the
    # follower to extend with new events.
    detector.expire_stale()
    fights, heals = detector.snapshot(include_in_progress=False)
    _State.detector = detector
    _State.live_position = end_pos
    _State.fights = fights
    _State.heals = heals
    _set_progress('done', bytes_read=total, total_bytes=total)
    # Kick off the live follower if live mode is enabled. The follower
    # picks up at `live_position` (end of the initial walk) and keeps
    # the detector alive across appended events. Caller already holds
    # the lock; _start_live_follower_locked is a no-op if a follower is
    # already running for this log.
    _start_live_follower_locked()


# ----- Live follower thread -----
#
# A background thread that holds the active log open at a known byte
# position and polls every ~250ms for newly-appended bytes. Anything new
# is parsed and fed into `_State.detector`, extending fights and heals
# in place. The live-snapshot endpoint reads the detector at request
# time so the polling overlay sees the latest state.
#
# Lifecycle: started by `_set_logfile` when `_State.live_enabled` is
# True (after the initial parse populates the detector); stopped on log
# swap, log clear, or live-mode toggle off. Lock-coordinated with the
# rest of the server via `_State.fights_lock` — every batch of new
# events is fed into the detector under the lock.

class _LiveFollower:
    """Background thread that tails the active log and feeds new events
    into the shared `_CombatDetector`.

    Reads via plain `open(path, 'rb')` each tick. (An earlier version
    chased a phantom Windows file-cache bug that turned out to be
    misdiagnosis: the user was loading via /api/upload, which copies
    to a temp dir and follows a static copy. Once /api/open / native
    Browse landed, the simple Python read path proved correct.)"""

    def __init__(self, logfile: str, poll_interval_s: float = 0.25):
        self.logfile = logfile
        self.poll_interval_s = poll_interval_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name='flurry-live-follower', daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 1.0):
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)
        self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        # Loop: read new bytes from the current position, parse complete
        # lines, feed events into the detector under the lock. Sleep
        # `poll_interval_s` between passes. Errors are logged and the
        # loop continues — a transient read failure shouldn't kill
        # live mode.
        from .parser import parse_line
        while not self._stop.is_set():
            try:
                self._tick(parse_line)
            except Exception:
                # Quietly skip bad ticks. A persistent error shows up as
                # the snapshot timestamp going stale, which the UI
                # surfaces as the indicator dimming.
                pass
            # Wait with wake-up support so stop() returns quickly.
            self._stop.wait(self.poll_interval_s)

    def _tick(self, parse_line):
        # Read newly-appended bytes from `live_position` to EOF. Open
        # afresh each tick so a log rotation (file replaced) doesn't
        # strand us on a stale handle.
        if not os.path.isfile(self.logfile):
            return
        size = os.path.getsize(self.logfile)
        with _State.fights_lock:
            # Per-tick guards: if a different logfile is now active or
            # we've been replaced as the current follower, this thread
            # is an orphan from a swap — exit without touching state.
            # Together with the daemon-thread / stop-flag semantics,
            # this lets `_stop_live_follower_locked` be non-blocking
            # (just set the flag and clear the slot; we'll notice).
            if (_State.logfile != self.logfile
                    or _State.live_follower is not self):
                self._stop.set()
                return
            position = _State.live_position
            detector = _State.detector
            if detector is None:
                return
            has_new_bytes = position < size
        # Read outside the lock (I/O can be slow); decode + parse
        # without touching shared state. Falls through to the
        # wall-clock expire_stale at the end even if there are no new
        # bytes, so phantom in-progress fights still close after
        # gap_seconds of real-world idle.
        chunk = b''
        if has_new_bytes:
            try:
                with open(self.logfile, 'rb') as f:
                    f.seek(position)
                    chunk = f.read(size - position)
            except OSError:
                chunk = b''
        if chunk:
            try:
                text = chunk.decode('utf-8', errors='replace')
            except UnicodeDecodeError:
                text = chunk.decode('latin-1', errors='replace')
            # EQ logs are line-buffered so chunks should end on \n; if
            # the writer is mid-line, leave the trailing partial line
            # for the next tick.
            last_nl = text.rfind('\n')
            if last_nl >= 0:
                complete_text = text[:last_nl + 1]
                consumed_bytes = len(complete_text.encode('utf-8'))
                with _State.fights_lock:
                    # Re-check guards + detector existence — anything
                    # could have changed during the I/O window.
                    if (_State.logfile != self.logfile
                            or _State.live_follower is not self):
                        self._stop.set()
                        return
                    detector = _State.detector
                    if detector is None:
                        return
                    for line in complete_text.splitlines():
                        if not line:
                            continue
                        ev = parse_line(line)
                        if ev is not None:
                            detector.feed_event(ev)
                    # Advance position by what we actually consumed
                    # (may be less than the chunk we read if the chunk
                    # ended mid-line).
                    _State.live_position = position + consumed_bytes
        # Always run expire_stale at end-of-tick with WALL CLOCK as
        # the anchor — NOT detector.last_event_ts. Without this, when
        # the log goes idle (player out of combat / EQ paused / zone
        # quiet), stale in-progress fights never close because the
        # log-time anchor doesn't advance, and the overlay shows a
        # phantom "active fight" indefinitely. The static-walk path
        # is unaffected — finalize_all() handles end-of-walk separately.
        with _State.fights_lock:
            if (_State.logfile == self.logfile
                    and _State.live_follower is self
                    and _State.detector is not None):
                _State.detector.expire_stale(now=datetime.now())


def _start_live_follower_locked():
    """Start the live follower for the active log if live is enabled
    and a follower isn't already running. Caller must hold the lock."""
    if not _State.live_enabled or _State.logfile is None:
        return
    if _State.live_follower is not None and _State.live_follower.is_running():
        return
    _State.live_follower = _LiveFollower(
        _State.logfile,
        poll_interval_s=max(0.05, _State.live_poll_ms / 1000.0))
    _State.live_follower.start()


def _stop_live_follower_locked():
    """Signal the live follower to stop and clear the slot. Non-blocking
    by design — the daemon thread exits within ~poll_interval_s on its
    next wake; the per-tick guards in _tick() ensure it can't mutate
    state in the meantime. Caller must hold the lock."""
    follower = _State.live_follower
    _State.live_follower = None
    if follower is not None:
        follower._stop.set()


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
    until the user makes their first edit.

    Also drops any comparison log — when the primary swaps, "compare
    against this other log" loses its meaning, and silently keeping a
    stale comparison around would be confusing in the diff UI."""
    abs_path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(f'log file not found: {abs_path}')
    with _State.fights_lock:
        # Stop any existing live follower before swapping the logfile
        # underneath it — the per-tick guards would catch a race, but
        # signaling stop here lets the orphan exit on its next wake
        # rather than churning until it notices.
        _stop_live_follower_locked()
        _State.logfile = abs_path
        _State.fights = None
        _State.heals = None
        _State.encounters = None
        _State.parser_stats = None
        _State.detector = None
        _State.live_position = 0
        _State.sidecar = load_sidecar(abs_path)
        _clear_comparison_locked()
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
        # The detector is rebuilt by the next _ensure_combat_cached(),
        # so drop it here too. Stop the follower so it doesn't try to
        # extend a stale detector while the re-walk runs; it'll get
        # restarted at the end of the request handler that triggered
        # the invalidation.
        _State.detector = None
        _State.live_position = 0
        _stop_live_follower_locked()
        # The next consumer will trigger a fresh parse; reset progress so
        # the UI can show the new run from 0%.
        _set_progress('idle')
    _State.encounters = None


def _persist_sidecar_locked():
    """Atomic save of the active sidecar. Caller must hold the lock."""
    if _State.logfile is None or _State.sidecar is None:
        return
    save_sidecar(_State.logfile, _State.sidecar)


# ----- Comparison-log helpers (cross-log diff) -----
#
# Mirror the primary-log lifecycle (set / parse / get-encounters / clear)
# against the `comparison_*` slots. Detection params come from the same
# `_State` fields the primary uses so encounters detected in both logs
# are directly comparable. Pet owners apply per-log via each log's own
# sidecar; manual encounter groupings likewise.
#
# These never mutate `_State.parse_progress` — comparison loads share the
# primary's progress dict so the existing UI progress bar keeps working
# without the front-end having to know about a parallel parse stream.


def _resolve_comparison_since_locked() -> Optional[datetime]:
    """`since_hours` cutoff anchored to the comparison log's last
    timestamp (NOT the primary's). Each log gets its own slice. Caller
    must hold `_State.fights_lock`."""
    if _State.since_hours <= 0 or _State.comparison_logfile is None:
        return None
    last = read_last_timestamp(_State.comparison_logfile)
    if last is None:
        return None
    return last - timedelta(hours=_State.since_hours)


def _ensure_comparison_combat_cached():
    """Walk the comparison log and populate fights+heals if not cached.
    Caller must hold `_State.fights_lock`. Same shape as
    `_ensure_combat_cached` but operates on the comparison slots."""
    if (_State.comparison_fights is not None
            and _State.comparison_heals is not None):
        return
    since = _resolve_comparison_since_locked()
    total = (os.path.getsize(_State.comparison_logfile)
             if os.path.isfile(_State.comparison_logfile) else 0)
    _set_progress('parsing', bytes_read=0, total_bytes=total)

    def _on_progress(bytes_read: int, total_bytes: int):
        _set_progress('parsing', bytes_read=bytes_read,
                      total_bytes=total_bytes)

    try:
        fights, heals = detect_combat(
            _State.comparison_logfile,
            gap_seconds=_State.gap_seconds,
            min_damage=_State.min_damage,
            min_duration_seconds=_State.min_duration_seconds,
            heals_extend_fights=_State.heals_extend_fights,
            since=since,
            progress_cb=_on_progress)
    except Exception as e:
        _set_progress('error', total_bytes=total,
                      message=f'{type(e).__name__}: {e}')
        raise
    _State.comparison_fights = fights
    _State.comparison_heals = heals
    _set_progress('done', bytes_read=total, total_bytes=total)


def _get_comparison_encounters_locked() -> List[Encounter]:
    """Build comparison encounters with the comparison log's sidecar
    applied (pet owners + manual groups). Caller must hold the lock."""
    if _State.comparison_logfile is None:
        return []
    _ensure_comparison_combat_cached()
    if _State.comparison_encounters is None:
        sidecar = _State.comparison_sidecar or Sidecar.empty()
        if sidecar.pet_owners:
            fights, heals = apply_pet_owners(
                _State.comparison_fights, _State.comparison_heals,
                sidecar.pet_owners)
        else:
            fights, heals = (_State.comparison_fights,
                             _State.comparison_heals)
        _State.comparison_encounters = group_into_encounters(
            fights,
            gap_seconds=_State.encounter_gap_seconds,
            heals=heals,
            manual_groups=sidecar.manual_groups_for_grouper())
    return _State.comparison_encounters


def _get_comparison_encounters() -> List[Encounter]:
    with _State.fights_lock:
        return _get_comparison_encounters_locked()


def _set_comparison_logfile(path: str):
    """Load a second log for cross-log diff. Validates, resets the
    comparison caches, and loads the comparison log's sidecar so its
    pet-owner assignments apply when grouping its encounters."""
    abs_path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(f'log file not found: {abs_path}')
    with _State.fights_lock:
        _State.comparison_logfile = abs_path
        _State.comparison_fights = None
        _State.comparison_heals = None
        _State.comparison_encounters = None
        _State.comparison_sidecar = load_sidecar(abs_path)
    _set_progress('idle')


def _clear_comparison_locked():
    """Drop the comparison log and all its derived caches. Caller must
    hold `_State.fights_lock`."""
    _State.comparison_logfile = None
    _State.comparison_fights = None
    _State.comparison_heals = None
    _State.comparison_encounters = None
    _State.comparison_sidecar = None


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
            elif path == '/overlay':
                # Live-overlay window — separate page from the SPA so it
                # can be opened in its own browser window with a compact,
                # borderless layout. It polls /api/live/snapshot and
                # works even with no log loaded (shows the "load a log
                # in the main window" empty state) so users can size +
                # position the window before raid.
                real = _static_path('overlay.html')
                if real is None:
                    self.send_error(500, 'overlay.html not bundled')
                    return
                self._serve_static(real)
            elif path.startswith('/static/'):
                # Front-end assets (CSS, JS, future images) live as files
                # under flurry/static/ rather than embedded in this module.
                # _static_path enforces the path-traversal guard.
                name = path[len('/static/'):]
                real = _static_path(name)
                if real is None:
                    self.send_error(404)
                    return
                self._serve_static(real)
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
            elif path == '/api/diff':
                # Two-encounter side-by-side diff (same log). Both ids
                # resolve against the active session's encounters.
                # Query: ?ids=A,B (exactly 2).
                ids_raw = qs.get('ids', [''])[0]
                try:
                    ids = [int(s) for s in ids_raw.split(',') if s.strip()]
                except ValueError:
                    self.send_error(400, 'ids must be comma-separated integers')
                    return
                if len(ids) != 2:
                    self.send_error(400, 'diff needs exactly 2 encounter ids')
                    return
                encounters = _get_encounters()
                by_id = {e.encounter_id: e for e in encounters}
                a, b = by_id.get(ids[0]), by_id.get(ids[1])
                if a is None or b is None:
                    self.send_error(404, 'one or both encounters not found '
                                         '(ids may have shifted under new params)')
                    return
                self._serve_json(_diff_payload(a, b))
            elif path == '/api/diff/cross':
                # Cross-log diff. `primary_id` resolves against the
                # active log's encounters, `secondary_id` against the
                # comparison log's. Each log's basename is included on
                # the encounter cards so the user can tell them apart.
                if _State.comparison_logfile is None:
                    # send_error puts the message in the HTTP status line
                    # which is latin-1 only — keep it ASCII (no em-dash).
                    self.send_error(400, 'no comparison log loaded; '
                                         'POST /api/comparison/open or '
                                         '/api/comparison/upload first')
                    return
                try:
                    primary_id = int(qs.get('primary_id', [''])[0])
                    secondary_id = int(qs.get('secondary_id', [''])[0])
                except ValueError:
                    self.send_error(400, 'primary_id and secondary_id '
                                         'must be integers')
                    return
                primary = _get_encounters()
                secondary = _get_comparison_encounters()
                a = next((e for e in primary
                          if e.encounter_id == primary_id), None)
                b = next((e for e in secondary
                          if e.encounter_id == secondary_id), None)
                if a is None or b is None:
                    self.send_error(404, 'one or both encounters not found '
                                         '(ids may have shifted under new params)')
                    return
                self._serve_json(_diff_payload(
                    a, b,
                    log_a=os.path.basename(_State.logfile),
                    log_b=os.path.basename(_State.comparison_logfile)))
            elif path == '/api/comparison/session':
                # Mirror of /api/session for the comparison log so the
                # second-log encounter picker can render a session table
                # before the user picks which encounter to diff against.
                self._serve_json(self._comparison_session_payload())
            elif path == '/api/live/snapshot':
                # Compact live-overlay payload: char name, active fight
                # (if any) with the player's four counters + top-N
                # damage/healing rows, plus the last-encounter recap
                # for the no-active-fight state. Polled at ~250ms.
                self._serve_json(_live_snapshot_payload())
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

            # /api/comparison/upload mirrors /api/upload but routes the
            # saved file to the comparison-log slots. Used by the
            # drag-drop "Compare" path when a primary log is already
            # loaded — the drop-side picker streams here instead of
            # replacing the primary.
            if path == '/api/comparison/upload':
                raw_name = self.headers.get('X-Filename', 'uploaded.txt')
                filename = urllib.parse.unquote(raw_name)
                if length <= 0:
                    self.send_error(400, 'empty upload')
                    return
                if _State.logfile is None:
                    self.send_error(400, 'load a primary log first')
                    return
                saved = _save_uploaded_log(filename, length, self.rfile)
                _set_comparison_logfile(saved)
                self._serve_json(self._comparison_session_payload())
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
            elif path == '/api/browse-native':
                # Server-side native file dialog. Browsers can't give
                # JS a picked file's disk path, but a Tk file dialog
                # opened from the Python server runs in the user's
                # session and returns the path directly. Loads via
                # _set_logfile (live tracking), unlike /api/upload
                # which copies to a temp dir.
                if not _TK_AVAILABLE:
                    self.send_error(400, 'native file dialog not available '
                                         '(tkinter import failed)')
                    return
                initial_dir = data.get('initial_dir') if data else None
                try:
                    picked = _native_file_picker(initial_dir=initial_dir)
                except Exception as e:
                    self.send_error(500, f'native dialog failed: '
                                         f'{type(e).__name__}: {e}')
                    return
                if not picked:
                    # User cancelled. 200 with no path so the front-end
                    # treats it as a no-op.
                    self._serve_json({'path': None, 'cancelled': True})
                    return
                _set_logfile(picked)
                self._serve_json({
                    'path': picked,
                    'cancelled': False,
                    'session': self._session_payload(),
                })
            elif path == '/api/comparison/open':
                # Load a second log for cross-log diff. Body: {"path":...}.
                # Requires a primary log already loaded — comparison
                # without primary has no diff target.
                if _State.logfile is None:
                    self.send_error(400, 'load a primary log first')
                    return
                requested = data.get('path')
                if not requested:
                    self.send_error(400, 'missing "path" in body')
                    return
                _set_comparison_logfile(requested)
                self._serve_json(self._comparison_session_payload())
            elif path == '/api/comparison/clear':
                # Drop the comparison log. No body needed. Idempotent.
                with _State.fights_lock:
                    _clear_comparison_locked()
                self._serve_json({'ok': True})
            elif path == '/api/overlay/pin':
                # Apply always-on-top + click-through to the overlay
                # browser window via Win32. Has to be triggered from
                # the main UI (not the overlay itself) — once
                # click-through is on, the overlay can't be clicked.
                # Optional `alpha` (0-255, default 255 = opaque) lets
                # the user dim the overlay slightly for some
                # see-through effect. Mostly cosmetic; primary
                # purpose is the always-on-top + click-through.
                if _user32 is None:
                    self.send_error(400, 'overlay pinning is Windows-only')
                    return
                try:
                    alpha = int(data.get('alpha', 255))
                except (TypeError, ValueError):
                    self.send_error(400, 'alpha must be an integer 0-255')
                    return
                ok = _pin_overlay_window(alpha=alpha)
                if not ok:
                    self.send_error(404, 'overlay window not found; '
                                         'open it via Pop out overlay first')
                    return
                self._serve_json({
                    'pinned': True,
                    'alpha': max(0, min(255, alpha)),
                })
            elif path == '/api/overlay/unpin':
                # Revert always-on-top + click-through. Idempotent —
                # works whether the window is currently pinned or not.
                if _user32 is None:
                    self.send_error(400, 'overlay pinning is Windows-only')
                    return
                ok = _unpin_overlay_window()
                self._serve_json({'pinned': False, 'found': ok})
            elif path == '/api/live/toggle':
                # Turn the live follower on or off. Body: {"enabled": bool}.
                # When toggling on with a log loaded, immediately starts
                # the follower at the current end-of-file position so we
                # don't double-parse history. When toggling off, signals
                # the follower to exit on its next poll cycle.
                want = bool(data.get('enabled', not _State.live_enabled))
                with _State.fights_lock:
                    _State.live_enabled = want
                    if want:
                        # Pick up from end-of-file if the detector is
                        # already populated; otherwise the follower
                        # will start once _ensure_combat_cached() runs.
                        if (_State.detector is not None
                                and _State.logfile is not None
                                and os.path.isfile(_State.logfile)):
                            _State.live_position = os.path.getsize(_State.logfile)
                        _start_live_follower_locked()
                    else:
                        _stop_live_follower_locked()
                self._serve_json({
                    'live_enabled': _State.live_enabled,
                    'follower_running': (_State.live_follower is not None
                                         and _State.live_follower.is_running()),
                })
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

    def _serve_static(self, file_path: str):
        """Stream a file from flurry/static/ as the response body.
        Cache-Control: no-cache so edits to app.js / styles.css show up
        on a plain refresh during development — at this scale revalidation
        is free, and a stale cached copy of the JS would confuse anyone
        debugging UI issues."""
        ext = os.path.splitext(file_path)[1].lower()
        ct = _STATIC_CONTENT_TYPES.get(ext, 'application/octet-stream')
        with open(file_path, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', ct)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
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
                'comparison': None,
            }
        encounters = _get_encounters()
        # Pre-compute the manual-encounter keysets once so the per-row
        # `is_manual` flag is O(members) per row rather than O(members^2).
        sidecar = _State.sidecar or Sidecar.empty()
        manual_keysets = [set(m.fight_keys) for m in sidecar.manual_encounters]
        # Compact comparison-log status for the session-view header /
        # action bar: just enough for the front-end to render a
        # "Comparison: <filename>" label and offer a Clear button. The
        # full encounter list comes from /api/comparison/session.
        comparison_meta = None
        if _State.comparison_logfile is not None:
            comparison_meta = {
                'logfile_basename': os.path.basename(_State.comparison_logfile),
            }
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
            'comparison': comparison_meta,
        }

    def _comparison_session_payload(self):
        """Mirror of `_session_payload` for the comparison log. Same
        encounter-row shape so the front-end can reuse the session-table
        renderer for the second-log encounter picker, but no params /
        sidecar fields (read-only from the diff path)."""
        if _State.comparison_logfile is None:
            return {
                'logfile': None,
                'logfile_basename': None,
                'encounters': [],
                'summary': None,
            }
        encounters = _get_comparison_encounters()
        sidecar = _State.comparison_sidecar or Sidecar.empty()
        manual_keysets = [set(m.fight_keys) for m in sidecar.manual_encounters]
        return {
            'logfile': _State.comparison_logfile,
            'logfile_basename': os.path.basename(_State.comparison_logfile),
            'encounters': [_encounter_summary(e, manual_keysets)
                           for e in encounters],
            'summary': {
                'total_encounters': len(encounters),
                'total_killed': sum(1 for e in encounters if e.fight_complete),
                'total_damage': sum(e.total_damage for e in encounters),
            },
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
<link rel="stylesheet" href="/static/styles.css">
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

<script src="/static/app.js"></script>
</body>
</html>
'''
