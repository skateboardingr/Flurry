// Flurry live overlay.
//
// Polls /api/live/snapshot at OVERLAY_POLL_MS and renders one of three
// states:
//   1. No log loaded → "Load a log in the main window" empty state.
//      Window can be resized + positioned at this stage so the user
//      lays out their on-screen workspace before raid.
//   2. Active fight (live + a fight is in progress) → four big counters
//      for the player (damage out / in, healing out / in) + HP-delta bar.
//   3. Recap (no active fight or live off) → last encounter's name,
//      duration, top damage rows, and clipboard-copy buttons.
//
// State live across renders: only the channel selector (persisted to
// localStorage so the choice sticks across overlay opens).

const OVERLAY_POLL_MS = 250;
// If the server's last_event_ts hasn't advanced for this long, the
// follower is alive but no new combat is landing — UI dims the live dot.
// Computed against the *previous* poll's timestamp client-side; keeps
// us out of wall-clock vs log-clock mismatches.
const STALE_AFTER_TICKS = 16;  // ~4s at 250ms/tick

const NUM = n => n == null ? '—' : n.toLocaleString();
const SHORT = n => {
  if (n == null) return '—';
  const abs = Math.abs(n);
  const sign = n < 0 ? '-' : '';
  if (abs >= 1_000_000) return sign + (abs / 1_000_000).toFixed(1) + 'M';
  if (abs >= 1_000)     return sign + (abs / 1_000).toFixed(0) + 'k';
  return String(n);
};
const FMT_DUR = s => {
  if (s == null) return '—';
  if (s < 60) return Math.round(s) + 's';
  const m = Math.floor(s / 60), r = Math.round(s % 60);
  return `${m}m${String(r).padStart(2, '0')}s`;
};

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// ---- Channel selector state (persisted) ------------------------------

const CHANNELS = [
  {id: 'gsay',     label: '/gsay',     hint: 'Group chat'},
  {id: 'raidsay',  label: '/raidsay',  hint: 'Raid chat'},
  {id: 'guildsay', label: '/guildsay', hint: 'Guild chat'},
  {id: 'say',      label: '/say',      hint: 'Local say'},
  {id: 'tell',     label: '/tell',     hint: 'Tell (private)'},
];
const CHANNEL_KEY = 'flurry.overlay.channel';
function getChannel() {
  return localStorage.getItem(CHANNEL_KEY) || 'gsay';
}
function setChannel(id) {
  localStorage.setItem(CHANNEL_KEY, id);
}

// Pet rollup toggle for the clipboard format. When on, the player's
// pet rows are merged into the player's row (one combined number for
// chat); when off, pets stay as separate rows. Default ON because the
// most common ask is "how much did I do, total" rather than the
// per-entity breakdown.
const COMBINE_PETS_KEY = 'flurry.overlay.combinePets';
function getCombinePets() {
  const v = localStorage.getItem(COMBINE_PETS_KEY);
  return v == null ? true : v === 'true';
}
function setCombinePets(on) {
  localStorage.setItem(COMBINE_PETS_KEY, on ? 'true' : 'false');
}

// Merge the player's pet rows into the player row, recompute dps + pct,
// drop the pet rows, and re-sort by damage desc. No-op if there's no
// player row or no pet rows.
function combinePetRows(rows, durationSec) {
  if (!rows || rows.length === 0) return rows;
  const playerIdx = rows.findIndex(r => r.is_you);
  if (playerIdx < 0) return rows;
  const petDmg = rows
    .filter(r => r.is_your_pet)
    .reduce((s, r) => s + (r.damage || 0), 0);
  if (petDmg === 0) return rows;
  const total = rows.reduce((s, r) => s + (r.damage || 0), 0);
  const dur = Math.max(durationSec || 1, 1);
  const player = {...rows[playerIdx]};
  player.damage = (player.damage || 0) + petDmg;
  player.dps = Math.round(player.damage / dur);
  player.pct = total > 0
    ? Math.round((player.damage / total) * 1000) / 10
    : player.pct;
  return rows
    .filter(r => !r.is_your_pet)
    .map(r => r.is_you ? player : r)
    .sort((a, b) => (b.damage || 0) - (a.damage || 0));
}

// ---- Polling ---------------------------------------------------------

let _lastEventTs = null;
let _ticksSinceEvent = 0;
// In-flight guard for the auto-pin flip: the snapshot poll fires every
// 250ms, so without this we'd send a fresh POST every tick while the
// server's first one was still landing.
let _pinFlipInFlight = false;

async function poll() {
  try {
    const r = await fetch('/api/live/snapshot', {cache: 'no-store'});
    if (!r.ok) {
      // Server hiccup — keep last render, dim the indicator. Will recover.
      setStatus('idle');
      return;
    }
    const d = await r.json();
    // Stale detection: if last_event_ts is unchanged across many ticks,
    // dim the indicator. Active fights with constant DPS still tick
    // last_event_ts forward, so this only fires when the log truly
    // hasn't grown.
    if (d.last_event_ts === _lastEventTs) {
      _ticksSinceEvent++;
    } else {
      _ticksSinceEvent = 0;
      _lastEventTs = d.last_event_ts;
    }
    render(d);
    autoPin(d);
  } catch (e) {
    setStatus('idle');
  }
}

// When the user has pinned the overlay, drive the click-through bit
// off the current view: HUD / counters during an active fight = pass
// clicks through to EQ; recap (or grace-window frozen HUD) = let the
// user interact. The `synthesized` flag distinguishes a real
// in-progress active fight (server.py:_live_snapshot) from the
// inter-fight grace-window frozen HUD — only the former wants
// click-through ON; the synthesized one is post-combat data shown in
// HUD layout, no reason to block clicks on it.
// Skips when not pinned (server is in unpinned state, leave alone).
function autoPin(d) {
  if (!d.overlay_pinned || _pinFlipInFlight) return;
  const wantClickThrough = !!d.active_fight && !d.active_fight.synthesized;
  if (wantClickThrough === d.overlay_click_through) return;
  _pinFlipInFlight = true;
  fetch('/api/overlay/pin', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({click_through: wantClickThrough}),
  }).catch(() => {})
    .finally(() => { _pinFlipInFlight = false; });
}

function setStatus(state) {
  const el = document.getElementById('overlay-status');
  if (!el) return;
  el.classList.remove('live', 'idle', 'stale');
  el.classList.add(state);
  el.title = state === 'live' ? 'Following the active log'
           : state === 'stale' ? 'Live, but no new combat'
           : 'Live mode off or no log loaded';
}

// ---- Render dispatch -------------------------------------------------

// Tracks which view is currently in the DOM and, for recap, the
// identity of the rendered encounter. Lets us skip re-rendering when
// nothing changed — important for the recap because we poll every
// 250ms and a re-render destroys the Copy buttons mid-click. (Active
// view always re-renders since its counters tick every poll.)
let _currentView = null;     // 'empty' | 'active' | 'recap' | 'waiting'
let _currentRecapKey = null;

function render(d) {
  // Title strip (top of the overlay): char name + status. Renders even
  // in the empty-state so the user sees connection state.
  const titleEl = document.getElementById('overlay-title');
  if (d.char_name) {
    titleEl.textContent = d.char_name;
  } else if (d.logfile_basename) {
    titleEl.textContent = d.logfile_basename;
  } else {
    titleEl.textContent = 'Flurry';
  }

  let status;
  if (!d.logfile_basename) {
    status = 'idle';
  } else if (!d.live_enabled || !d.follower_running) {
    status = 'idle';
  } else if (_ticksSinceEvent > STALE_AFTER_TICKS) {
    status = 'stale';
  } else {
    status = 'live';
  }
  setStatus(status);

  const content = document.getElementById('overlay-content');
  if (!d.logfile_basename) {
    if (_currentView !== 'empty') content.innerHTML = renderEmpty();
    _currentView = 'empty';
    _currentRecapKey = null;
  } else if (d.active_fight) {
    content.innerHTML = renderActive(d.active_fight, d.char_name);
    _currentView = 'active';
    _currentRecapKey = null;
  } else if (d.last_encounter) {
    // Identify a completed encounter by start+end — once an encounter
    // is "last", its data is frozen, so we only need to re-render when
    // a different encounter takes its place.
    const le = d.last_encounter;
    const key = `${le.start || ''}|${le.end || ''}`;
    if (_currentView !== 'recap' || _currentRecapKey !== key) {
      content.innerHTML = renderRecap(le, d.char_name);
      wireCopyButtons(le);
      _currentRecapKey = key;
    }
    _currentView = 'recap';
  } else {
    if (_currentView !== 'waiting') content.innerHTML = renderWaiting();
    _currentView = 'waiting';
    _currentRecapKey = null;
  }
}

function renderEmpty() {
  return `
    <div class="overlay-empty">
      <div>Load a log in the main window to start.</div>
      <div class="hint">
        This window can be resized + positioned now. Pin always-on-top
        via PowerToys (<strong>Win+Ctrl+T</strong>) or your OS window
        manager.
      </div>
    </div>`;
}

function renderWaiting() {
  return `
    <div class="overlay-empty">
      <div>Waiting for combat…</div>
      <div class="hint">
        Live follower running. The first encounter's recap will appear
        here once it ends.
      </div>
    </div>`;
}

function renderActive(af, charName) {
  const y = af.you;
  const delta = y.hp_delta_per_sec;
  const deltaCls = delta > 0 ? 'up' : delta < 0 ? 'down' : 'flat';
  // Bar magnitude: |delta| relative to a soft cap so a normal-sized
  // delta doesn't pin the bar at 100%. Cap at 20k Δ/s — adjust if EQ
  // numbers commonly exceed it.
  const cap = 20000;
  const fillPct = Math.min(100, (Math.abs(delta) / cap) * 100);
  const fillCls = delta > 0 ? 'up' : 'down';
  const bar = delta === 0 ? '' :
    `<div class="fill ${fillCls}" style="width:${fillPct}%;
       ${delta > 0 ? 'right:50%' : 'left:50%'}"></div>`;
  return `
    <div class="fight-target">
      ⚔ ${escapeHTML(af.target)}
      <span class="duration">${FMT_DUR(af.duration_seconds)}</span>
    </div>
    <div class="counters">
      <div class="counter dmg-out">
        <div class="label">Damage out</div>
        <div class="value">${SHORT(y.damage_out)}</div>
        <div class="rate">${SHORT(y.dps_out)} dps</div>
      </div>
      <div class="counter dmg-in">
        <div class="label">Damage in</div>
        <div class="value">${SHORT(y.damage_in)}</div>
        <div class="rate">${SHORT(y.dtps_in)} dtps</div>
      </div>
      <div class="counter heal-out">
        <div class="label">Healing out</div>
        <div class="value">${SHORT(y.healing_out)}</div>
        <div class="rate">${SHORT(y.hps_out)} hps</div>
      </div>
      <div class="counter heal-in">
        <div class="label">Healing in</div>
        <div class="value">${SHORT(y.healing_in)}</div>
        <div class="rate">${SHORT(y.hps_in)} hps</div>
      </div>
    </div>
    <div class="hp-delta">
      <span class="label">HP Δ</span>
      <span class="value ${deltaCls}">${delta > 0 ? '+' : ''}${SHORT(delta)} /s</span>
      <div class="bar"><div class="midline"></div>${bar}</div>
    </div>`;
}

function renderRecap(le, charName) {
  const status = le.fight_complete
    ? '<span class="ok">KILLED</span>'
    : '<span class="warn">incomplete</span>';
  const rows = (le.top_damage || []).map((r, i) => {
    // Mark `is_you` (the player) and `is_your_pet` (the player's
    // pets, after pet_owners rewrite) so both highlight as the
    // user's contribution. Pet rows get a small icon prefix.
    const cls = r.is_you ? 'you' : (r.is_your_pet ? 'your-pet' : '');
    const petMark = r.is_your_pet ? '<span class="pet-mark" title="Your pet">🐾</span> ' : '';
    return `
    <div class="top-row ${cls}">
      <span class="pos">${i + 1}.</span>
      <span class="name">${petMark}${escapeHTML(r.name)}</span>
      <span class="dmg">${SHORT(r.damage)}</span>
      <span class="pct">${r.pct.toFixed(1)}%</span>
    </div>`;
  }).join('');

  // Channel selector + two copy buttons. Stored locally so the user's
  // last channel sticks across overlay opens.
  const currentChannel = getChannel();
  const channelOpts = CHANNELS.map(c =>
    `<option value="${c.id}" ${c.id === currentChannel ? 'selected' : ''}
             title="${escapeHTML(c.hint)}">${c.label}</option>`).join('');

  const combineChecked = getCombinePets() ? 'checked' : '';
  return `
    <div class="recap-head">Last encounter</div>
    <div class="recap-name">${escapeHTML(le.name)}</div>
    <div class="recap-meta">
      ${FMT_DUR(le.duration_seconds)} · ${SHORT(le.raid_total_damage)} raid · ${status}
    </div>
    <div class="top-list">${rows}</div>
    <div class="copy-row">
      <button class="copy-btn" id="copy-table"
              title="Copy a single-line parse with rank, name, damage, %, dps.">
        Copy parse
      </button>
      <button class="copy-btn" id="copy-compact"
              title="Copy an ultra-short top-5 parse (just names + damage).">
        Copy short
      </button>
      <label class="combine-toggle"
             title="When on, your pets are rolled into your row in the pasted parse. When off, pets stay as separate entries.">
        <input type="checkbox" id="combine-pets" ${combineChecked}>
        combine pets
      </label>
      <span class="channel-label">paste into</span>
      <select class="channel-select" id="channel-select"
              title="Where you intend to paste the parse. Saved locally — Flurry doesn't actually post for you.">
        ${channelOpts}
      </select>
    </div>`;
}

// ---- Clipboard formatting + copy --------------------------------------

function _rowsForCopy(le) {
  // Apply the user's combine-pets preference, then return a fresh copy
  // of the top-N rows ready for formatting.
  const rows = le.top_damage || [];
  if (getCombinePets()) return combinePetRows(rows, le.duration_seconds);
  return rows.slice();
}

function formatTableParse(le) {
  // Single-line, EQ-chat-friendly. Multi-line text gets collapsed to a
  // single line in EQ chat (each /gsay is one line), so a multi-line
  // table format ends up unreadable. We use ` | ` separators to keep
  // the rank/name/damage chunks visually distinct on one line.
  const dur = FMT_DUR(le.duration_seconds);
  const total = SHORT(le.raid_total_damage);
  const durSecs = Math.max(le.duration_seconds || 1, 1);
  const totalDps = SHORT(Math.round((le.raid_total_damage || 0) / durSecs));
  const head = `${le.name} ${dur} ${total} (${totalDps}dps)`;
  const parts = _rowsForCopy(le).map((r, i) =>
    `${i + 1}.${r.name} ${SHORT(r.damage)} ${r.pct.toFixed(0)}% ${SHORT(r.dps)}dps`);
  return parts.length ? `${head} | ${parts.join(' | ')}` : head;
}

function formatCompactParse(le) {
  // Ultra-short single-line. Top-5 names + damage + dps — fits any
  // channel including /tell. Drops the % column to save room.
  const total = SHORT(le.raid_total_damage);
  const head = `${le.name} (${FMT_DUR(le.duration_seconds)}, ${total}):`;
  const top = _rowsForCopy(le).slice(0, 5)
    .map(r => `${r.name} ${SHORT(r.damage)} ${SHORT(r.dps)}dps`)
    .join(', ');
  return `${head} ${top}`;
}

async function copyToClipboard(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
    if (btn) {
      const orig = btn.textContent;
      btn.textContent = 'Copied!';
      btn.classList.add('copied');
      setTimeout(() => {
        btn.textContent = orig;
        btn.classList.remove('copied');
      }, 2500);
    }
  } catch (e) {
    // Some browsers block clipboard from non-HTTPS origins or non-user
    // gestures. Fall back to a hidden textarea + execCommand.
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch (e2) {}
    document.body.removeChild(ta);
    if (btn) {
      btn.textContent = 'Copied (fallback)';
      btn.classList.add('copied');
      setTimeout(() => {
        btn.classList.remove('copied');
        btn.textContent = btn.id === 'copy-table' ? 'Copy parse' : 'Copy short';
      }, 2500);
    }
  }
}

function wireCopyButtons(le) {
  const tableBtn = document.getElementById('copy-table');
  const compactBtn = document.getElementById('copy-compact');
  const channelSel = document.getElementById('channel-select');
  const combineCb = document.getElementById('combine-pets');
  if (tableBtn) tableBtn.addEventListener('click',
    () => copyToClipboard(formatTableParse(le), tableBtn));
  if (compactBtn) compactBtn.addEventListener('click',
    () => copyToClipboard(formatCompactParse(le), compactBtn));
  if (channelSel) channelSel.addEventListener('change',
    () => setChannel(channelSel.value));
  if (combineCb) combineCb.addEventListener('change',
    () => setCombinePets(combineCb.checked));
}

// ---- Boot ------------------------------------------------------------

poll();
setInterval(poll, OVERLAY_POLL_MS);
