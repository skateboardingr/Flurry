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

// --- Live-mode header indicator + toggle ------------------------------
//
// The header has a small ● dot that reports live state at a glance:
//   green-pulsing → follower running, recent events landing
//   yellow         → follower running but stale (no new events for ~4s)
//   dim grey       → live mode off OR no log loaded
//
// We poll /api/live/snapshot at 1s (slower than the overlay's 250ms)
// to drive the indicator; the indicator only needs to flip occasionally,
// not animate every counter. The overlay opens a separate window with
// its own faster poller for the actual numbers.

let _liveStatusPoll = null;
let _liveLastEventTs = null;
let _liveTicksSinceEvent = 0;
// Tracks the id of the most-recent encounter that was actually
// rendered into the session table. When the snapshot reports a newer
// id AND we're between fights (no active_fight) AND we're on the
// session view, we re-render so new rows pop in without a manual
// Refresh. We skip during active combat — the user is watching the
// overlay then, and re-renders during a pull are distracting flicker.
// Encounters that completed mid-combat batch up and appear all at
// once on the next quiet moment. Sentinel `undefined` distinguishes
// "haven't seen the first poll yet" from "last poll said no encounters."
let _liveRenderedEncounterId = undefined;

function isOnSessionView() {
  const h = location.hash || '';
  return h === '' || h === '#' || h === '#/';
}

function startLiveStatusPoll() {
  if (_liveStatusPoll) return;  // already running
  const tick = async () => {
    try {
      const r = await fetch('/api/live/snapshot', {cache: 'no-store'});
      if (r.ok) {
        const d = await r.json();
        if (d.last_event_ts === _liveLastEventTs) {
          _liveTicksSinceEvent++;
        } else {
          _liveTicksSinceEvent = 0;
          _liveLastEventTs = d.last_event_ts;
        }
        applyLiveIndicator(d, _liveTicksSinceEvent);
        // Auto-refresh session view between fights when a new
        // encounter has formed since we last rendered.
        const newId = d.last_encounter ? d.last_encounter.encounter_id : null;
        if (_liveRenderedEncounterId === undefined) {
          _liveRenderedEncounterId = newId;
        } else if (newId !== _liveRenderedEncounterId
                   && newId !== null
                   && !d.active_fight
                   && isOnSessionView()) {
          _liveRenderedEncounterId = newId;
          renderSession();
        }
      }
    } catch (e) { /* keep polling */ }
  };
  tick();  // first tick immediately
  _liveStatusPoll = setInterval(tick, 1000);
}

function applyLiveIndicator(d, ticksSinceEvent) {
  const btn = document.getElementById('live-btn');
  const dot = btn ? btn.querySelector('.live-dot') : null;
  const label = document.getElementById('live-label');
  if (!btn || !dot || !label) return;
  let state;
  if (!d.logfile_basename) {
    state = 'idle';
    label.textContent = 'Live (no log)';
  } else if (!d.live_enabled || !d.follower_running) {
    state = 'idle';
    label.textContent = 'Live: off';
  } else if (ticksSinceEvent > 4) {
    // ~4s without new events at our 1s poll cadence
    state = 'stale';
    label.textContent = 'Live: idle';
  } else {
    state = 'live';
    label.textContent = 'Live';
  }
  dot.classList.remove('live', 'stale', 'idle');
  dot.classList.add(state);
  btn.classList.toggle('off', state === 'idle' && d.live_enabled === false);
}

async function toggleLive() {
  const btn = document.getElementById('live-btn');
  if (!btn) return;
  // Read current state from the indicator's classes — last poll wrote it.
  const dot = btn.querySelector('.live-dot');
  const wasLive = dot && (dot.classList.contains('live') || dot.classList.contains('stale'));
  try {
    await fetch('/api/live/toggle', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: !wasLive}),
    });
  } catch (e) { /* indicator flips on the next poll regardless */ }
  // Force an immediate poll for snappy feedback.
  if (_liveStatusPoll) {
    _liveTicksSinceEvent = 0;
    fetch('/api/live/snapshot', {cache: 'no-store'})
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) applyLiveIndicator(d, 0); })
      .catch(() => {});
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
            'Click a log file below, hit Browse… for the native picker, or paste a full path',
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

  // Layout (top → bottom):
  //   1. Last N hours (parse-window control — useful before any open)
  //   2. Path input + buttons (Open / Browse… / Upload — primary actions)
  //   3. Parent-dir link (navigation aid for the file table below)
  //   4. File table
  // The current dir's path used to be repeated above the parent link;
  // removed since the input box already shows it.
  app.innerHTML = `
    <div class="panel">
      ${pickerOptionsHTML(sessionParams)}
      ${pickerInputHTML(data.path)}
      <div style="margin-bottom: 12px;">${parentLink}</div>
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
  // Three ways to load a log:
  //   - "Open as log" (or click any file row in the list above):
  //     follows the file IN PLACE. Live tail tracks new writes from EQ.
  //   - "Browse…": opens the native OS file dialog server-side
  //     (tkinter), returns the picked path, then loads via /api/open.
  //     Same live-tracking behavior as Open as log, just without
  //     having to type/navigate the path.
  //   - "Upload static copy": browser file dialog, copies the bytes
  //     to a temp dir, and follows that copy. The copy doesn't grow
  //     as EQ writes — only useful for analyzing a log from another
  //     machine where you don't have a direct path.
  return `
    <div class="picker-input-row">
      <input id="picker-input" placeholder="Or paste a full path…"
             value="${escapeHTML(currentPath)}">
      <button class="btn" id="picker-go"
              title="Navigate to the path in the input box (browse this folder).">Go</button>
      <button class="btn primary" id="picker-open"
              title="Load the path in the input as the active log. Live tail tracks new writes from EQ.">Open as log</button>
      <button class="btn primary" id="picker-browse-native"
              title="Open the native Windows file picker. Loads in live-tracking mode (same as Open as log) — flurry keeps watching the file as EQ writes new events.">Browse…</button>
      <button class="btn" id="picker-upload"
              title="Browser file dialog → copies the file to a temp dir. The copy is STATIC — won't update as EQ writes new events. Only useful when you don't have direct disk access to the file.">Upload static copy…</button>
      <input type="file" id="picker-upload-input" accept=".txt,.log,.*"
             style="display:none">
    </div>
    <div class="picker-help sub">
      <strong>Live tracking</strong>: click <strong>Browse…</strong>
      for the native Windows file picker, click a log file in the list
      above, or paste its full path and hit <strong>Open as log</strong>.
      <strong>Upload static copy</strong> is only for analyzing a log
      file you don't have direct disk access to — the copy doesn't
      grow with EQ.
    </div>`;
}

function wirePickerInput() {
  const input = document.getElementById('picker-input');
  const go = document.getElementById('picker-go');
  const open = document.getElementById('picker-open');
  const browseNative = document.getElementById('picker-browse-native');
  const uploadBtn = document.getElementById('picker-upload');
  const uploadInput = document.getElementById('picker-upload-input');
  if (!input) return;
  go.addEventListener('click', () => renderPicker(input.value));
  open.addEventListener('click', () => openLog(input.value));
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') renderPicker(input.value);
  });
  // Browse… → server-side native file dialog (Tk) → returns the
  // picked path and loads it in live mode. The dialog blocks server-
  // side until the user picks/cancels, so this fetch can take a
  // while; show a transient "Opening…" state on the button.
  if (browseNative) {
    browseNative.addEventListener('click', async () => {
      const orig = browseNative.textContent;
      browseNative.textContent = 'Opening dialog…';
      browseNative.disabled = true;
      try {
        const r = await fetch('/api/browse-native', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({initial_dir: input.value || null}),
        });
        if (!r.ok) {
          const t = await r.text();
          alert('Browse failed: ' + (t || ('HTTP ' + r.status)));
          return;
        }
        const d = await r.json();
        if (d.cancelled || !d.path) return;  // user dismissed
        // Loaded — bounce to session view to render the new log.
        location.hash = '#/';
        route();
      } catch (e) {
        alert('Browse error: ' + e.message);
      } finally {
        browseNative.textContent = orig;
        browseNative.disabled = false;
      }
    });
  }
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
    // Compare button used to live here; it moved into the action bar
    // (next to Merge / Split / Clear) since it's strictly a 2-selection
    // action with no whole-log mode like Session summary has.

    // Live-mode toggle. Defaults ON when a log loads (server-side), so
    // the button shows the current state and lets the user flip it for
    // historical-log review. Indicator dot pulses when live + new
    // events are landing; dims when paused or stale.
    const liveBtn = document.createElement('button');
    liveBtn.className = 'btn live-btn';
    liveBtn.id = 'live-btn';
    liveBtn.title = 'Live tail: when on, the server follows the log file ' +
                    'and the overlay updates in real time. Default on for ' +
                    'active raids; flip off for historical log review.';
    liveBtn.innerHTML = `<span class="live-dot"></span><span id="live-label">Live</span>`;
    liveBtn.addEventListener('click', toggleLive);
    sessActions.appendChild(liveBtn);

    // Pop-out overlay button. Opens /overlay in a separate browser
    // window sized for an on-screen DPS meter. Pinning (always-on-top
    // + click-through) is handled by the Pin/Unpin buttons below,
    // which use Win32 ctypes server-side rather than relying on the
    // user to install AHK / PowerToys.
    const overlayBtn = document.createElement('button');
    overlayBtn.className = 'btn';
    overlayBtn.id = 'overlay-btn';
    overlayBtn.textContent = 'Pop out overlay';
    overlayBtn.title = 'Open the live overlay in its own window. Use ' +
                       'the Pin overlay button to make it always-on-top ' +
                       'and click-through over EQ.';
    overlayBtn.addEventListener('click', () => {
      // Modest default size. User can resize once it's open.
      window.open('/overlay', 'flurry-overlay',
                  'width=320,height=400,resizable=yes,menubar=no,toolbar=no,location=no,status=no');
    });
    sessActions.appendChild(overlayBtn);

    // Pin / Unpin overlay buttons. Pinning applies always-on-top +
    // click-through via Win32 SetWindowLongPtr (server-side). MUST
    // live in the main UI rather than inside the overlay itself —
    // once click-through is on, the overlay can't be clicked anymore,
    // so the unpin control has to be reachable from somewhere else.
    const pinBtn = document.createElement('button');
    pinBtn.className = 'btn';
    pinBtn.id = 'pin-overlay-btn';
    pinBtn.textContent = 'Pin overlay';
    pinBtn.title = 'Make the overlay window always-on-top + click-through ' +
                   '(mouse passes through to EQ underneath). Open the ' +
                   'overlay first via Pop out overlay, then click Pin.';
    pinBtn.addEventListener('click', async () => {
      try {
        const r = await fetch('/api/overlay/pin', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: '{}',
        });
        if (!r.ok) {
          const txt = await r.text();
          alert('Could not pin overlay: ' + (txt || `HTTP ${r.status}`));
        }
      } catch (e) {
        alert('Pin failed: ' + e.message);
      }
    });
    sessActions.appendChild(pinBtn);

    const unpinBtn = document.createElement('button');
    unpinBtn.className = 'btn';
    unpinBtn.id = 'unpin-overlay-btn';
    unpinBtn.textContent = 'Unpin overlay';
    unpinBtn.title = 'Remove always-on-top + click-through from the overlay ' +
                     'so you can interact with it (drag, click Copy, etc.).';
    unpinBtn.addEventListener('click', async () => {
      try {
        await fetch('/api/overlay/unpin', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: '{}',
        });
      } catch (e) { /* idempotent — fine to swallow */ }
    });
    sessActions.appendChild(unpinBtn);
  }

  // Kick off the live-status poller for the header indicator.
  startLiveStatusPoll();

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
    // Compare requires exactly N=2 (in-log diff is strictly two
    // encounters). Compare-across-logs requires exactly N=1 (one
    // primary encounter pairs with one comparison-log encounter
    // selected later). Both render disabled-but-visible at other
    // counts so users see what's available — the title attributes
    // explain why each isn't clickable yet.
    const compareDisabled = n !== 2 ? ' disabled' : '';
    const compareTitle = n === 2
      ? 'Compare these two encounters side-by-side (DPS, damage taken, healing).'
      : `Tick exactly 2 encounters to compare them (currently ${n}).`;
    const crossDisabled = n !== 1 ? ' disabled' : '';
    const crossTitle = n === 1
      ? 'Compare this encounter against one from a different log file. ' +
        'Useful for before/after gear comparisons across raid nights.'
      : `Tick exactly 1 encounter to compare it against another log (currently ${n}).`;
    return `
      <div class="action-bar" id="action-bar">
        <span class="count">${n} selected</span>
        <button class="btn primary" id="act-merge"${mergeDisabled}
                title="Combine the selected encounters into one user-pinned encounter.">Merge</button>
        <button class="btn" id="act-split"
                title="Remove these encounters from any manual groupings, returning them to auto-grouped state.">Split</button>
        <button class="btn ${n === 2 ? 'primary' : ''}" id="act-compare"${compareDisabled}
                title="${compareTitle}">Compare</button>
        <button class="btn ${n === 1 ? 'primary' : ''}" id="act-compare-cross"${crossDisabled}
                title="${crossTitle}">Compare across logs</button>
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
    const compare = document.getElementById('act-compare');
    const crossCompare = document.getElementById('act-compare-cross');
    const clear = document.getElementById('act-clear');
    if (clear) clear.addEventListener('click', () => {
      sessionSelected.clear();
      app.querySelectorAll('input.row-check').forEach(cb => { cb.checked = false; });
      refreshActionBar();
      syncCheckAll();
    });
    if (merge) merge.addEventListener('click', () => postEncounterAction('merge'));
    if (split) split.addEventListener('click', () => postEncounterAction('split'));
    if (compare) compare.addEventListener('click', () => {
      if (sessionSelected.size !== 2) return;
      const ids = Array.from(sessionSelected).join(',');
      location.hash = `#/diff?ids=${ids}`;
    });
    if (crossCompare) crossCompare.addEventListener('click', () => {
      if (sessionSelected.size !== 1) return;
      const primaryId = Array.from(sessionSelected)[0];
      // Send the user to the comparison-log picker. The picker reads
      // primary= off the hash and carries it forward through the second
      // log's encounter selection to the final cross-log diff route.
      location.hash = `#/cross-compare?primary=${primaryId}`;
    });
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

// --- Diff view (two-encounter compare) -------------------------------
//
// Side-by-side comparison of two encounters from the same log. Three
// metric tabs (Damage / Tanking / Healing) each plug different fields
// off the diff payload's per-actor `values` array (length 2). Two
// display modes:
//
//   - Side-by-side: two bars per row (A blue, B green), shared x-scale
//     across all visible rows so absolute magnitudes read true.
//   - Delta: one bar per row centered on zero. Color signals direction
//     of change *with semantic awareness* — for damage and healing
//     bigger is better (positive = green); for damage taken bigger is
//     worse (positive = red). Bars scaled to max abs delta.
//
// State is module-scope so toggling tabs / display / actor visibility
// from within the view doesn't lose state across re-renders. Hidden
// actors persist by lowercased name across mode switches (a hidden
// healer stays hidden when you flip to the damage tab).

const DIFF_MODES = {
  damage:  { label: 'Damage',       value: 'damage',        rate: 'dps',
             rateLabel: 'DPS',  valueLabel: 'damage',
             biggestKey: 'biggest_dmg',   biggestLabel: 'biggest hit',
             betterIfHigher: true },
  tanking: { label: 'Damage taken', value: 'damage_taken',  rate: 'dtps',
             rateLabel: 'DTPS', valueLabel: 'damage taken',
             biggestKey: 'biggest_taken', biggestLabel: 'biggest hit taken',
             betterIfHigher: false },
  healing: { label: 'Healing',      value: 'healing',       rate: 'hps',
             rateLabel: 'HPS',  valueLabel: 'healing',
             biggestKey: 'biggest_heal',  biggestLabel: 'biggest heal',
             betterIfHigher: true },
};

let diffSettings = {
  mode: 'damage',          // 'damage' | 'tanking' | 'healing'
  display: 'side',         // 'side' | 'delta'
  showEnemies: false,
  hidden: new Set(),       // lowercased actor names hidden from the table
};

function diffPctChange(a, b) {
  // Percent change from A to B. Returns null when A==0 (∞ change is
  // not meaningful for the column header — the absolute delta carries
  // the message).
  if (a === 0) return null;
  return ((b - a) / a) * 100;
}

function diffBetterClass(delta, mode) {
  // Map a signed delta to a CSS class so positive/negative get colored
  // by *meaning* not just sign. For tanking, an increase in damage
  // taken is bad (red); for damage and healing it's good (green).
  if (delta === 0) return 'flat';
  const positive = delta > 0;
  const isGood = positive === mode.betterIfHigher;
  return isGood ? 'better' : 'worse';
}

function fmtDelta(delta) {
  if (delta === 0) return '±0';
  const sign = delta > 0 ? '+' : '−';
  return sign + SHORT(Math.abs(delta));
}

function fmtPct(pct) {
  if (pct == null) return '';
  if (pct === 0) return '0%';
  const sign = pct > 0 ? '+' : '−';
  return `${sign}${Math.abs(pct).toFixed(1)}%`;
}

async function renderDiff(spec) {
  if (chartInstance) { chartInstance.destroy(); chartInstance = null; }
  const app = document.getElementById('app');

  // `spec` is one of:
  //   {mode: 'same',  ids: [a, b]}             → /api/diff?ids=A,B
  //   {mode: 'cross', primaryId, secondaryId}  → /api/diff/cross?...
  //   null / malformed                         → friendly fallback
  // Same-log diff was the v0.4.0 entry point; cross-log piggy-backs on
  // the same render path because the payload shape is identical (both
  // return `encounters[2]` + `actors[]`, with `cross_log: true/false`
  // and an optional `log` field on each encounter for the cross case).
  let url, ok = false;
  if (spec && spec.mode === 'same' && Array.isArray(spec.ids) &&
      spec.ids.length === 2) {
    url = `/api/diff?ids=${spec.ids.join(',')}`;
    ok = true;
  } else if (spec && spec.mode === 'cross' &&
             Number.isFinite(spec.primaryId) &&
             Number.isFinite(spec.secondaryId)) {
    url = `/api/diff/cross?primary_id=${spec.primaryId}` +
          `&secondary_id=${spec.secondaryId}`;
    ok = true;
  }
  if (!ok) {
    setHeader('Compare encounters', '', true);
    app.innerHTML = `<a href="#/" class="back">← back</a>` +
      `<div class="panel sub">Compare needs encounter ids in the URL — ` +
      `<code>#/diff?ids=A,B</code> for in-log, ` +
      `<code>#/diff/cross?primary=A&amp;secondary=B</code> for cross-log. ` +
      `Tick rows in the session table and click <strong>Compare</strong> ` +
      `or <strong>Compare across logs</strong>.</div>`;
    return;
  }

  let data;
  try {
    data = await withParseProgress(
      () => fetchJSON(url), app, 'Parsing log…');
  } catch (e) {
    app.innerHTML = `<a href="#/" class="back">← back</a>` +
      `<div class="err">Failed to load diff: ${escapeHTML(e.message)}. ` +
      `If the detection params changed, the encounter ids may have shifted — ` +
      `<a href="#/">go back to the session</a> and reselect.</div>`;
    return;
  }

  const [encA, encB] = data.encounters;
  setHeader('Compare encounters',
            `#${encA.encounter_id} ${encA.name} vs #${encB.encounter_id} ${encB.name}`,
            true);

  const mode = DIFF_MODES[diffSettings.mode] || DIFF_MODES.damage;

  // Header card: per-encounter facts + the headline deltas the user is
  // most likely to read first (duration, total damage / healing, raid
  // DPS). These are mode-specific where it makes sense — total damage
  // is more useful than total healing in damage mode, etc.
  const dDur = encB.duration_seconds - encA.duration_seconds;
  const dTotalDmg = encB.total_damage - encA.total_damage;
  const dTotalDmgPct = diffPctChange(encA.total_damage, encB.total_damage);
  const dRaidDps = encB.raid_dps - encA.raid_dps;
  const dRaidDpsPct = diffPctChange(encA.raid_dps, encB.raid_dps);
  const dTotalHeal = encB.total_healing - encA.total_healing;
  const dTotalHealPct = diffPctChange(encA.total_healing, encB.total_healing);

  function encCard(e, label, accentClass) {
    const status = e.fight_complete ? 'KILLED' : 'incomplete';
    // Cross-log: the payload tags each encounter with its source log
    // basename so the card reads "log: eqlog_X.txt" right under the
    // encounter name. Same-log entries leave `log` null and the row
    // just doesn't render.
    const logRow = e.log
      ? `<div class="diff-enc-log" title="${escapeHTML(e.log)}">
           <span class="sub">log</span> ${escapeHTML(e.log)}
         </div>`
      : '';
    return `
      <div class="diff-enc-card ${accentClass}">
        <div class="diff-enc-label">${label}</div>
        <div class="diff-enc-name">#${e.encounter_id} ${escapeHTML(e.name)}</div>
        ${logRow}
        <div class="diff-enc-meta">
          <span>${escapeHTML(e.start || '—')}</span>
          <span>${FMT_DUR(e.duration_seconds)}</span>
          <span class="${e.fight_complete ? 'ok' : 'warn'}">${status}</span>
        </div>
        <div class="diff-enc-stats">
          <div><span class="sub">Damage</span> ${NUM(e.total_damage)}</div>
          <div><span class="sub">Raid DPS</span> ${NUM(e.raid_dps)}</div>
          <div><span class="sub">Healing</span> ${NUM(e.total_healing)}</div>
        </div>
      </div>`;
  }

  // Top-line deltas — the "did duration / total / raid DPS move?" facts.
  // Color-coded by `betterIfHigher` per metric: faster TTK is good even
  // though duration is a smaller number, so we hand-pick that one (good
  // when negative). Damage & healing follow betterIfHigher=true.
  function diffStat(label, delta, pct, betterIfHigher, fmt = SHORT) {
    if (delta === 0) {
      return `<div class="diff-headline-stat"><span class="sub">${label}</span>
              <span class="flat">±0</span></div>`;
    }
    const sign = delta > 0 ? '+' : '−';
    const cls = (delta > 0) === betterIfHigher ? 'better' : 'worse';
    const pctStr = pct == null ? '' : ` (${fmtPct(pct)})`;
    return `<div class="diff-headline-stat"><span class="sub">${label}</span>
              <span class="${cls}">${sign}${fmt(Math.abs(delta))}${pctStr}</span></div>`;
  }
  // Duration: shorter is better → betterIfHigher = false. Format as
  // seconds rather than the abbreviated SHORT (which would render 8s
  // as "8" and 80s as "80"; the explicit suffix is clearer here).
  const fmtSec = n => Math.round(n) + 's';

  const headerHTML = `
    <a href="#/" class="back">← back</a>
    <div class="panel">
      <div class="diff-enc-grid">
        ${encCard(encA, 'Encounter A', 'a')}
        ${encCard(encB, 'Encounter B', 'b')}
      </div>
      <div class="diff-headline">
        ${diffStat('Δ duration', dDur, diffPctChange(encA.duration_seconds, encB.duration_seconds), false, fmtSec)}
        ${diffStat('Δ total damage', dTotalDmg, dTotalDmgPct, true)}
        ${diffStat('Δ raid DPS', dRaidDps, dRaidDpsPct, true)}
        ${diffStat('Δ total healing', dTotalHeal, dTotalHealPct, true)}
      </div>
    </div>`;

  // Tab strip — same visual language as the session-summary tabs.
  // Numbers in the tab badge are the totals of the active metric across
  // both encounters (a quick "is this metric worth looking at" cue).
  function totalForMode(m) {
    let total = 0;
    for (const a of data.actors) {
      total += (a.values[0][m.value] || 0) + (a.values[1][m.value] || 0);
    }
    return total;
  }
  const tabsHTML = ['damage', 'tanking', 'healing'].map(k => {
    const m = DIFF_MODES[k];
    const total = totalForMode(m);
    const isActive = k === diffSettings.mode;
    return `<button class="tab ${isActive ? 'active' : ''}" data-diff-mode="${k}">
              ${m.label} (${SHORT(total)})
            </button>`;
  }).join('');

  // Filter actors: hide rows the user unchecked, hide rows with zero
  // value on both sides for the active metric (no point showing a tank
  // with zero damage_taken in either encounter), and hide enemies
  // unless the toggle is on.
  const allActors = data.actors;
  const friendlyCount = allActors.filter(a => a.side === 'friendly').length;
  const enemyCount = allActors.filter(a => a.side === 'enemy').length;
  const visibleActors = allActors.filter(a => {
    if (!diffSettings.showEnemies && a.side === 'enemy') return false;
    const sumA = a.values[0][mode.value] || 0;
    const sumB = a.values[1][mode.value] || 0;
    return (sumA + sumB) > 0;
  });

  // Sort by combined value (A + B) so the rows with the most action
  // surface first. Hidden rows still sort in this order — they get
  // pushed to a "hidden" section at the bottom so unhiding is easy.
  visibleActors.sort((a, b) => {
    const sa = (a.values[0][mode.value] || 0) + (a.values[1][mode.value] || 0);
    const sb = (b.values[0][mode.value] || 0) + (b.values[1][mode.value] || 0);
    return sb - sa;
  });

  const shown = visibleActors.filter(a => !diffSettings.hidden.has(a.name.toLowerCase()));

  // Bar scaling. Side-by-side: shared max across both columns and all
  // visible rows so a 1M bar in row 1 and a 100M bar in row 5 read at
  // their true relative heights. Delta: shared max abs across all rows.
  let maxValue = 1, maxDelta = 1;
  for (const a of shown) {
    maxValue = Math.max(maxValue, a.values[0][mode.value] || 0, a.values[1][mode.value] || 0);
    const d = (a.values[1][mode.value] || 0) - (a.values[0][mode.value] || 0);
    maxDelta = Math.max(maxDelta, Math.abs(d));
  }

  function rowHTML(a) {
    const va = a.values[0], vb = a.values[1];
    const vA = va[mode.value] || 0, vB = vb[mode.value] || 0;
    const rA = va[mode.rate] || 0,  rB = vb[mode.rate] || 0;
    const delta = vB - vA;
    const pct = diffPctChange(vA, vB);
    const cls = diffBetterClass(delta, mode);
    const lo = a.name.toLowerCase();
    const isHidden = diffSettings.hidden.has(lo);

    let barsHTML;
    if (diffSettings.display === 'delta') {
      // Delta bar: half-track for negative (left), half-track for
      // positive (right), filled to |delta|/maxDelta. Color by semantic
      // (good/bad) rather than raw sign.
      const pctFill = Math.min(100, (Math.abs(delta) / maxDelta) * 100);
      const side = delta >= 0 ? 'right' : 'left';
      barsHTML = `
        <div class="diff-bar-delta">
          <div class="diff-bar-half left">
            ${side === 'left' ? `<div class="diff-bar-fill ${cls}" style="width:${pctFill}%"></div>` : ''}
          </div>
          <div class="diff-bar-zero"></div>
          <div class="diff-bar-half right">
            ${side === 'right' ? `<div class="diff-bar-fill ${cls}" style="width:${pctFill}%"></div>` : ''}
          </div>
        </div>`;
    } else {
      const wA = Math.min(100, (vA / maxValue) * 100);
      const wB = Math.min(100, (vB / maxValue) * 100);
      barsHTML = `
        <div class="diff-bar-side">
          <div class="diff-bar-row a"><div class="diff-bar-fill" style="width:${wA}%"></div>
            <span class="diff-bar-label">A · ${SHORT(vA)} · ${SHORT(rA)} ${mode.rateLabel}</span>
          </div>
          <div class="diff-bar-row b"><div class="diff-bar-fill" style="width:${wB}%"></div>
            <span class="diff-bar-label">B · ${SHORT(vB)} · ${SHORT(rB)} ${mode.rateLabel}</span>
          </div>
        </div>`;
    }

    return `
      <tr class="diff-row ${isHidden ? 'hidden-row' : ''}" data-actor="${escapeHTML(lo)}">
        <td class="check-cell">
          <label class="check-hit"><input type="checkbox" class="diff-actor-check"
                 data-actor="${escapeHTML(lo)}" ${isHidden ? '' : 'checked'}></label>
        </td>
        <td class="diff-name">
          ${escapeHTML(a.name)}
          ${a.side === 'enemy' ? '<span class="badge enemy">enemy</span>' : ''}
        </td>
        <td class="num diff-val-a">${SHORT(vA)}<br><span class="sub">${SHORT(rA)} ${mode.rateLabel}</span></td>
        <td class="num diff-val-b">${SHORT(vB)}<br><span class="sub">${SHORT(rB)} ${mode.rateLabel}</span></td>
        <td class="num diff-delta ${cls}">${fmtDelta(delta)}<br><span class="sub">${fmtPct(pct)}</span></td>
        <td class="diff-bars">${barsHTML}</td>
      </tr>`;
  }

  const tableHTML = shown.length === 0
    ? `<div class="sub">No actors with ${mode.valueLabel} in either encounter.</div>`
    : `
      <table class="diff-table">
        <thead><tr>
          <th class="check-cell"><label class="check-hit" title="Toggle all rows">
            <input type="checkbox" id="diff-check-all" ${
              shown.every(a => !diffSettings.hidden.has(a.name.toLowerCase())) ? 'checked' : ''
            }></label></th>
          <th>Actor</th>
          <th class="num">A: #${encA.encounter_id}</th>
          <th class="num">B: #${encB.encounter_id}</th>
          <th class="num">Δ</th>
          <th>${diffSettings.display === 'delta' ? 'Δ bar' : 'Side-by-side'}</th>
        </tr></thead>
        <tbody>${shown.map(rowHTML).join('')}</tbody>
      </table>`;

  // Hidden-row strip so the user can re-show actors they unchecked
  // without scrolling the table. Cleared by re-checking from here.
  const hiddenRows = visibleActors.filter(a =>
    diffSettings.hidden.has(a.name.toLowerCase()));
  const hiddenStrip = hiddenRows.length === 0 ? '' : `
    <div class="panel diff-hidden-strip">
      <span class="sub">Hidden:</span>
      ${hiddenRows.map(a => `
        <button class="diff-unhide" data-actor="${escapeHTML(a.name.toLowerCase())}"
                title="Show this actor again">${escapeHTML(a.name)} ✕</button>`).join('')}
      <button class="btn diff-unhide-all">Show all</button>
    </div>`;

  app.innerHTML = headerHTML +
    `<div class="tabs">${tabsHTML}</div>` +
    `<div class="panel">
      <div class="params-row">
        <div class="diff-display-toggle"
             title="Side-by-side shows both encounters' raw values as bars. Delta shows one centered bar per row, colored by whether the change is good or bad for the active metric.">
          ${[
            ['side', 'Side-by-side'],
            ['delta', 'Delta'],
          ].map(([k, label]) => `
            <button class="ss-metric-btn ${diffSettings.display === k ? 'active' : ''}"
                    data-diff-display="${k}">${label}</button>`).join('')}
        </div>
        <label class="check"
               title="Include enemy-side actors (the boss / adds, raid pets that attacked the wrong target). Off by default since friendlies are the usual focus.">
          <input type="checkbox" id="diff-show-enemies"
                 ${diffSettings.showEnemies ? 'checked' : ''}>
          Show enemies
        </label>
        <span class="sub" style="align-self:center; font-size:0.85rem;">
          ${shown.length} shown
          ${diffSettings.hidden.size > 0 ? `· ${diffSettings.hidden.size} hidden` : ''}
          · ${friendlyCount} friendl${friendlyCount === 1 ? 'y' : 'ies'}
          ${enemyCount > 0 ? `· ${enemyCount} enem${enemyCount === 1 ? 'y' : 'ies'}` : ''}
        </span>
      </div>
    </div>` +
    `<div class="panel">${tableHTML}</div>` +
    hiddenStrip;

  // --- Wire interactions ---

  app.querySelectorAll('.tab[data-diff-mode]').forEach(btn => {
    btn.addEventListener('click', () => {
      const m = btn.dataset.diffMode;
      if (m === diffSettings.mode) return;
      diffSettings.mode = m;
      renderDiff(spec);
    });
  });

  app.querySelectorAll('[data-diff-display]').forEach(btn => {
    btn.addEventListener('click', () => {
      const d = btn.dataset.diffDisplay;
      if (d === diffSettings.display) return;
      diffSettings.display = d;
      renderDiff(spec);
    });
  });

  const enemiesEl = document.getElementById('diff-show-enemies');
  if (enemiesEl) {
    enemiesEl.addEventListener('change', e => {
      diffSettings.showEnemies = e.target.checked;
      renderDiff(spec);
    });
  }

  app.querySelectorAll('.diff-actor-check').forEach(cb => {
    cb.addEventListener('change', e => {
      const actor = e.target.dataset.actor;
      if (e.target.checked) diffSettings.hidden.delete(actor);
      else diffSettings.hidden.add(actor);
      renderDiff(spec);
    });
  });

  const checkAll = document.getElementById('diff-check-all');
  if (checkAll) {
    checkAll.addEventListener('change', e => {
      if (e.target.checked) {
        // Re-check only the rows currently in `shown` — checking "all"
        // shouldn't un-hide actors filtered out by the enemy toggle.
        for (const a of shown) diffSettings.hidden.delete(a.name.toLowerCase());
      } else {
        for (const a of shown) diffSettings.hidden.add(a.name.toLowerCase());
      }
      renderDiff(spec);
    });
  }

  app.querySelectorAll('.diff-unhide').forEach(btn => {
    btn.addEventListener('click', () => {
      diffSettings.hidden.delete(btn.dataset.actor);
      renderDiff(spec);
    });
  });

  const unhideAllBtn = app.querySelector('.diff-unhide-all');
  if (unhideAllBtn) {
    unhideAllBtn.addEventListener('click', () => {
      diffSettings.hidden.clear();
      renderDiff(spec);
    });
  }
}

// --- Cross-log compare picker ----------------------------------------
//
// Two-step flow: user ticks 1 encounter in the primary session list and
// clicks **Compare across logs**, landing on `#/cross-compare?primary=N`.
// This view either offers a way to load a second log (file picker /
// drag-drop) or, if a comparison is already loaded, shows that log's
// session table so the user can pick which secondary encounter to diff
// against. Clicking a row navigates to `#/diff/cross?primary=A&secondary=B`
// and the existing renderDiff path takes over.
//
// State: the comparison log's parse lives on the server (_State.comparison_*),
// so this view is mostly a thin renderer over /api/comparison/session.

async function renderCrossCompare(primaryId) {
  if (chartInstance) { chartInstance.destroy(); chartInstance = null; }
  const app = document.getElementById('app');
  app.innerHTML = '<div class="sub">Loading…</div>';

  if (!Number.isFinite(primaryId)) {
    setHeader('Compare across logs', '', true);
    app.innerHTML = `<a href="#/" class="back">← back</a>` +
      `<div class="panel sub">Pick a primary encounter first — tick exactly 1 row ` +
      `in the session table and click <strong>Compare across logs</strong>.</div>`;
    return;
  }

  // Pull primary session + comparison status in parallel so we can
  // render the right state (no comparison vs comparison loaded) on
  // first paint without sequential round-trips.
  let primarySession, comparisonSession;
  try {
    [primarySession, comparisonSession] = await Promise.all([
      fetchJSON('/api/session'),
      fetchJSON('/api/comparison/session'),
    ]);
  } catch (e) {
    app.innerHTML = `<a href="#/" class="back">← back</a>` +
      `<div class="err">Failed to load: ${escapeHTML(e.message)}</div>`;
    return;
  }

  if (primarySession.logfile === null) {
    setHeader('Compare across logs', '', false);
    app.innerHTML = `<a href="#/" class="back">← back</a>` +
      `<div class="panel sub">No primary log loaded. Open one first, then ` +
      `come back to compare it against another.</div>`;
    return;
  }

  const primary = (primarySession.encounters || [])
    .find(e => e.encounter_id === primaryId);
  if (!primary) {
    setHeader('Compare across logs', '', true);
    app.innerHTML = `<a href="#/" class="back">← back</a>` +
      `<div class="panel sub">Couldn't find encounter #${primaryId} in the primary log — ` +
      `the detection params may have changed and ids shifted. ` +
      `<a href="#/">Go back</a> and reselect.</div>`;
    return;
  }

  setHeader('Compare across logs',
            `Primary: #${primary.encounter_id} ${primary.name}`,
            true);

  // Toggle drag-drop routing: while this view is mounted, drops should
  // load the second log (comparison), not replace the primary. The flag
  // is checked by the global drop handler. Cleared on navigation away
  // (the next route() call inside the drop handler picks the correct
  // route for whatever view we land on).
  _crossCompareDropTarget = true;

  const primaryCard = `
    <div class="panel diff-enc-card a"
         style="margin: 0; border-left-width: 3px;">
      <div class="diff-enc-label">Primary encounter</div>
      <div class="diff-enc-name">#${primary.encounter_id} ${escapeHTML(primary.name)}</div>
      <div class="diff-enc-log" title="${escapeHTML(primarySession.logfile_basename)}">
        <span class="sub">log</span> ${escapeHTML(primarySession.logfile_basename)}
      </div>
      <div class="diff-enc-meta">
        <span>${escapeHTML(primary.start || '—')}</span>
        <span>${FMT_DUR(primary.duration_seconds)}</span>
        <span class="${primary.fight_complete ? 'ok' : 'warn'}">${primary.fight_complete ? 'KILLED' : 'incomplete'}</span>
      </div>
      <div class="diff-enc-stats">
        <div><span class="sub">Damage</span> ${NUM(primary.total_damage)}</div>
        <div><span class="sub">Raid DPS</span> ${NUM(primary.raid_dps)}</div>
      </div>
    </div>`;

  const hasComparison = comparisonSession.logfile !== null;

  if (!hasComparison) {
    // Step 1: no comparison loaded yet. Show a load-a-log panel —
    // browse, paste a path, OS file picker, or drag-drop anywhere on
    // the page. Same affordances as the main picker, just routed to
    // /api/comparison/* endpoints.
    app.innerHTML = `
      <a href="#/" class="back">← back</a>
      <div class="cross-compare-grid">
        ${primaryCard}
        <div class="panel">
          <h3 class="cross-step">Step 2 — load a comparison log</h3>
          <p class="sub" style="margin-top:0">
            Pick a second log to compare this encounter against. Drag-drop
            the file anywhere on the page, click <strong>Browse…</strong>,
            or paste a full path below.
          </p>
          <div class="picker-input-row">
            <input id="cc-path-input" placeholder="Paste a full path…">
            <button class="btn primary" id="cc-open-btn">Open</button>
            <button class="btn" id="cc-browse-btn"
                    title="Pick a log file via the OS file dialog.">Browse…</button>
            <input type="file" id="cc-file-input" accept=".txt,.log,.*"
                   style="display:none">
          </div>
        </div>
      </div>`;

    document.getElementById('cc-open-btn').addEventListener('click',
      () => openComparisonByPath(document.getElementById('cc-path-input').value, primaryId));
    document.getElementById('cc-path-input').addEventListener('keydown', e => {
      if (e.key === 'Enter')
        openComparisonByPath(e.target.value, primaryId);
    });
    document.getElementById('cc-browse-btn').addEventListener('click',
      () => document.getElementById('cc-file-input').click());
    document.getElementById('cc-file-input').addEventListener('change', e => {
      if (e.target.files.length > 0)
        uploadComparisonLog(e.target.files[0], primaryId);
    });
    return;
  }

  // Step 2: comparison is loaded. Show its session table — click any
  // row to navigate to the cross-log diff. A "Pick a different log"
  // button clears the comparison and bounces back to step 1.
  const rows = (comparisonSession.encounters || []).map(e => `
    <tr class="fight-row" data-secondary="${e.encounter_id}">
      <td class="num">${e.encounter_id}</td>
      <td>${escapeHTML(e.start || '—')}</td>
      <td class="num">${FMT_DUR(e.duration_seconds)}</td>
      <td class="target">${escapeHTML(e.name)}</td>
      <td class="num">${NUM(e.total_damage)}</td>
      <td class="num">${NUM(e.raid_dps)}</td>
      <td class="status ${e.fight_complete ? 'killed' : 'incomplete'}">
        ${e.fight_complete ? 'Killed' : 'Incomplete'}</td>
    </tr>`).join('');

  const empty = (comparisonSession.encounters || []).length === 0
    ? `<div class="panel sub">No encounters detected in this log under the
       current params. Adjust min damage / since-hours and reload.</div>`
    : '';

  app.innerHTML = `
    <a href="#/" class="back">← back</a>
    <div class="cross-compare-grid">
      ${primaryCard}
      <div class="panel">
        <h3 class="cross-step">Step 2 — pick the comparison encounter</h3>
        <div class="cross-comp-meta">
          <span>Comparison log: <strong>${escapeHTML(comparisonSession.logfile_basename)}</strong></span>
          <span class="sub">${comparisonSession.summary.total_encounters} encounters,
            ${comparisonSession.summary.total_killed} killed</span>
          <button class="btn" id="cc-clear-btn"
                  title="Drop this comparison log and pick a different one.">Pick a different log</button>
        </div>
      </div>
    </div>
    ${empty}
    ${rows ? `
      <div class="panel">
        <table>
          <thead><tr>
            <th class="num">#</th><th>Start</th><th class="num">Dur</th>
            <th>Target</th><th class="num">Damage</th><th class="num">Raid DPS</th>
            <th>Status</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>` : ''}`;

  app.querySelectorAll('tr.fight-row[data-secondary]').forEach(tr => {
    tr.addEventListener('click', () => {
      const secondaryId = tr.dataset.secondary;
      location.hash = `#/diff/cross?primary=${primaryId}&secondary=${secondaryId}`;
    });
  });

  document.getElementById('cc-clear-btn').addEventListener('click', async () => {
    try {
      await fetch('/api/comparison/clear', { method: 'POST' });
      renderCrossCompare(primaryId);
    } catch (e) {
      // Non-fatal — leave the user where they are; they can hit Back.
    }
  });
}

// While renderCrossCompare is mounted, drag-dropped files load as the
// comparison log instead of replacing the primary. The global drop
// handler reads this flag and routes accordingly.
let _crossCompareDropTarget = false;

async function openComparisonByPath(path, primaryId) {
  if (!path || !path.trim()) return;
  const app = document.getElementById('app');
  app.innerHTML = `<div class="sub">Loading comparison log…</div>` +
                  parseProgressHTML(null, 'Parsing comparison log…');
  const stop = startParsePoll(s => {
    if (s.state === 'parsing') {
      app.innerHTML = parseProgressHTML(s, 'Parsing comparison log…');
    }
  });
  try {
    const r = await fetch('/api/comparison/open', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: path.trim()}),
    });
    if (!r.ok) {
      const txt = await r.text();
      throw new Error(txt || `HTTP ${r.status}`);
    }
  } catch (e) {
    if (stop) stop();
    app.innerHTML = `<div class="err">Failed to open: ${escapeHTML(e.message)}</div>` +
                    `<button class="btn" onclick="renderCrossCompare(${primaryId})">Back</button>`;
    return;
  }
  if (stop) stop();
  renderCrossCompare(primaryId);
}

async function uploadComparisonLog(file, primaryId) {
  // Mirror of uploadLog but POSTs to /api/comparison/upload. Reuses the
  // same upload UI affordances (progress bar, parse-status flip).
  const app = document.getElementById('app');
  app.innerHTML = `
    <div class="upload-status">
      <div class="upload-label">Uploading comparison <strong>${escapeHTML(file.name)}</strong> (${fmtSize(file.size)})</div>
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
  let stopPoll = null;
  let phase = 'upload';
  const setLabel = txt => {
    const el = document.querySelector('#app .upload-label');
    if (el) el.innerHTML = txt;
  };
  const flipToParsing = () => {
    if (phase !== 'upload') return;
    phase = 'parse';
    setBar(0);
    setPct('Parsing comparison log…');
    setLabel(`Parsing comparison <strong>${escapeHTML(file.name)}</strong>`);
  };

  try {
    await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/comparison/upload');
      xhr.setRequestHeader('X-Filename', encodeURIComponent(file.name));
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.upload.addEventListener('progress', e => {
        if (phase !== 'upload' || !e.lengthComputable) return;
        const pct = Math.round(e.loaded / e.total * 100);
        setBar(pct);
        setPct(pct + '%');
      });
      xhr.upload.addEventListener('load', flipToParsing);
      xhr.addEventListener('load', () => {
        if (xhr.status >= 200 && xhr.status < 300) resolve();
        else reject(new Error(xhr.responseText || `HTTP ${xhr.status}`));
      });
      xhr.addEventListener('error', () => reject(new Error('Network error during upload')));
      xhr.addEventListener('abort', () => reject(new Error('Upload aborted')));
      xhr.send(file);
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
          setBar(100);
          setPct('Finalizing…');
        } else if (s.state === 'error') {
          setPct('Parse error');
        }
      });
    });
    if (stopPoll) { stopPoll(); stopPoll = null; }
    renderCrossCompare(primaryId);
  } catch (e) {
    if (stopPoll) { stopPoll(); stopPoll = null; }
    app.innerHTML = `<div class="err">Comparison upload failed: ${escapeHTML(e.message)}</div>` +
                    `<button class="btn" onclick="renderCrossCompare(${primaryId})">Back</button>`;
  }
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
  // Modal subtitle. Damage/healing show value + count of underlying
  // events. Delta has no per-event records, so we show the underlying
  // damage and heal totals so the user can sanity-check the chart's
  // amplitude against the inputs at a glance.
  let subLine;
  if (isDelta) {
    const dmgTotal = pair.damage || 0;
    const healTotal = pair.heals_total || 0;
    const net = healTotal - dmgTotal;
    const sign = net > 0 ? '+' : '';
    subLine = `${NUM(dmgTotal)} damage taken · ${NUM(healTotal)} healing received · ${sign}${NUM(net)} net life`;
  } else {
    subLine = `${NUM(primaryTotal)} ${amountLabel} · ${NUM(allHits.length)} ${countLabel}`;
  }
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
          <div class="modal-sub" id="pair-sub">${subLine}</div>
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
    // Skip refresh() and the pointer-event wiring entirely. refresh()
    // recomputes the subtitle and stats from `allHits` — which is empty
    // for delta — and would clobber the informative damage/healing/net
    // subtitle we already wrote. There are no individual events to
    // window or filter in delta either, so nothing useful to wire up.
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
  // Reset cross-compare drop routing on every nav. renderCrossCompare
  // re-sets the flag if we're landing on its view; any other route
  // leaves it false so drops behave normally.
  _crossCompareDropTarget = false;
  // Session-locked layout: only the encounter table scrolls; everything
  // above it (header, summary, params, action bar) stays pinned. Apply
  // the body class only on the session list view — every other view
  // (encounter detail, summary, diff, cross-compare, debug, picker)
  // uses normal page scroll. Toggling here, before dispatch, keeps the
  // lock state in one place rather than scattered across each renderer.
  const isSession = !hash || hash === '#' || hash === '#/' ||
                    (!hash.startsWith('#/encounter/') &&
                     !hash.startsWith('#/picker') &&
                     !hash.startsWith('#/debug') &&
                     !hash.startsWith('#/session-summary') &&
                     !hash.startsWith('#/diff') &&
                     !hash.startsWith('#/cross-compare'));
  document.body.classList.toggle('session-locked', isSession);

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
  } else if (hash.startsWith('#/diff/cross')) {
    // Cross-log diff: ?primary=A&secondary=B (one id per loaded log).
    // Must come BEFORE the same-log #/diff prefix check.
    const primary = (hash.match(/[?&]primary=(\d+)/) || [])[1];
    const secondary = (hash.match(/[?&]secondary=(\d+)/) || [])[1];
    if (primary && secondary) {
      renderDiff({mode: 'cross',
                  primaryId: parseInt(primary, 10),
                  secondaryId: parseInt(secondary, 10)});
    } else {
      renderDiff(null);
    }
  } else if (hash.startsWith('#/diff')) {
    // Same-log two-encounter diff: ?ids=A,B (exactly 2).
    const idsMatch = hash.match(/[?&]ids=([0-9,]+)/);
    let ids = null;
    if (idsMatch) {
      ids = idsMatch[1].split(',')
        .map(s => parseInt(s, 10))
        .filter(n => Number.isFinite(n));
    }
    renderDiff(ids ? {mode: 'same', ids: ids} : null);
  } else if (hash.startsWith('#/cross-compare')) {
    // Comparison-log picker. ?primary=<id> carries the already-selected
    // primary encounter id forward through the second-log encounter
    // pick to the final cross-log diff route.
    const m = hash.match(/[?&]primary=(\d+)/);
    renderCrossCompare(m ? parseInt(m[1], 10) : null);
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
      <div>Drop log file to upload (static copy)</div>
      <div class="sub">eqlog_&lt;character&gt;_&lt;server&gt;.txt</div>
      <div class="sub" style="margin-top:8px; max-width: 420px;">
        Drag-drop creates a snapshot that won't update with new EQ
        events. For live tracking, instead use <strong>Change log</strong>
        and click your eqlog file from the list, or paste its path.
      </div>
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
  const file = e.dataTransfer.files[0];
  // While the user is on the cross-compare picker view, route drops to
  // /api/comparison/upload so dropping a file loads it as the second
  // log instead of replacing the primary. Other views (session list,
  // picker, encounter detail, etc.) keep the existing replace-primary
  // semantics.
  if (_crossCompareDropTarget) {
    const m = location.hash.match(/[?&]primary=(\d+)/);
    const primaryId = m ? parseInt(m[1], 10) : null;
    if (Number.isFinite(primaryId)) {
      await uploadComparisonLog(file, primaryId);
      return;
    }
  }
  await uploadLog(file);
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
