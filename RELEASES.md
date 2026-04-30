# Releases

## v0.6.2 — Recap top 10

- **Recap top damage** bumped from 8 to 10 rows. The Copy parse
  output includes all 10; Copy short stays at top 5.
- **Default overlay window height** bumped from 380 to 400 to fit
  the two extra rows on first open. Browsers remember the named
  window's last size, so close the existing overlay window before
  Pop out overlay to see the new default.

## v0.6.1 — Overlay copy polish

Three fixes for the overlay's clipboard buttons after raid-night
testing exposed friction:

- **Copy short** now includes per-actor DPS, not just damage.
  Format is `name dmg dps, name dmg dps, ...` — still channel-
  friendly, with enough detail to be useful at a glance.
- **Recap re-render skip.** The overlay polls every 250ms and was
  rebuilding the recap HTML on every poll, which destroyed and
  recreated the Copy buttons mid-click. A `mousedown` on one
  generation of the button could land its `mouseup` on a fresh
  replacement that never received the down — and the click event
  was lost. The recap is now keyed by encounter start+end timestamps
  and only re-rendered when a new encounter takes its place.
- **"Copied!" confirmation duration** bumped from 1200ms → 2500ms
  so the feedback is visible long enough to read.

## v0.6.0 — Live tail mode + player overlay

Flurry now follows your log file in real time. With a log loaded and
live mode on (default), a background follower thread tails the file
every 250ms, parses appended events, and feeds them into the same
detector the static parse populates. The session view, encounter
detail, debug counters — everything in the main UI — keeps showing
the latest state without a manual refresh after each fight.

A new **player overlay** rides on top of this: a compact, always-on-
top window with damage out / in and healing out / in counters during
a fight, an HP-Δ strip, and a recap of the last encounter when you
zone out. With one click in the main UI it goes click-through and
sits over EQ as a HUD.

### Live follower

`_CombatDetector` is a new analyzer class that owns the fight-detection
state machine — what `detect_combat` used to do walking through events,
the detector does one event at a time via `feed_event`. The follower
thread holds the log file open, polls 250ms for new bytes, parses
complete lines, and feeds them in. Stale fights expire by **wall
clock** on every tick so in-progress fights close after `gap_seconds`
of real-world idle even when the log itself goes quiet.

The detector survives across detection-param changes; log reloads
follow normal cache invalidation. `/api/live/snapshot` returns the
in-progress active fight + the last completed encounter on demand,
and is read by both the main-UI live indicator and the overlay.

### Player overlay

A separate `/overlay` page (its own HTML / CSS / JS, served from
`flurry/static/overlay.*`) polls `/api/live/snapshot` at 250ms and
renders one of three views:

- **Empty state** — "Load a log in the main window" while you size
  and position the window for raid.
- **Active fight** — four counters (damage out, damage in, healing
  out, healing in) with per-second rates, a horizontal HP-Δ bar
  (red when damage > heals, green when heals > damage), and the
  current target name + duration. Counters aggregate across
  in-progress fights so dmg-out from the boss fight, dmg-in from
  the YOU fight, and pet damage all roll up to one set of numbers.
- **Recap** — last encounter's name, duration, top damage rows,
  and Copy buttons for clipboard parses.

The active-fight target is picked by **highest cumulative damage**
across the in-progress set, with a `received > dealt + healed`
classifier filter to drop friendly PCs from the candidate list.
The detector creates a fight per defender, so friendly tanks
getting hit by mobs would otherwise show up as candidate targets
and the displayed name would flicker between friendlies and
enemies.

### Pin overlay (always-on-top + click-through)

A **Pin overlay** button in the main UI applies `WS_EX_TOPMOST` +
`WS_EX_LAYERED` + `WS_EX_TRANSPARENT` to the overlay's browser
window via Win32 ctypes (`SetWindowLongPtrW`), making it
always-on-top and click-through over EQ. Pin/Unpin live in the
main UI rather than inside the overlay because once click-through
is on, the overlay itself can't be clicked.

The overlay **auto-toggles click-through** based on what's
currently rendered: during an active fight the click-through bit
is on (HUD mode, mouse passes through to EQ); during the recap
it's off (Copy buttons are clickable). Snapshot payload carries
the current pin state and the overlay POSTs the bit on view
transitions.

### Clipboard parses

Recap view has Copy buttons for the last encounter:

- **Copy parse** — one-line `name dmg %% dps` rows separated by
  ` | ` (EQ chat collapses multi-line, so a multi-line table is
  unreadable in-game).
- **Copy short** — top-5 names + raw damage, fits any channel
  including `/tell`.
- **Combine pets** toggle (default on) rolls your pet rows into
  your row before formatting so the clipboard reads "you did N
  total" rather than splitting your damage across multiple lines.
- **Channel selector** persists in localStorage; defaults to
  `/gsay`.

### Mage / charmed pet rollup

`apply_pet_owners` (introduced for necro + beastlord pets via the
`<owner>'s pet` form) now rewrites mage and charmed pet names that
EQ doesn't already attribute. The pet-owner editor on the encounter
detail page populates the sidecar; the live snapshot applies the
mapping every tick so the overlay's "you + your pets" counters
include mage pets without needing per-edit cache busts.

### Native file picker

A **Browse…** button in the main UI opens the OS file picker via
server-side `tkinter.filedialog.askopenfilename` so users can pick
a log via the standard Windows dialog AND get live tracking on the
original file. The drag-drop path (which copies to a temp dir and
follows a static copy) still exists for users who prefer it.

### Live-mode polish

- **Persistent file handle** in the follower. Re-opening the log
  every tick was hitting a Windows directory-cache-lag effect that
  made the overlay update on a 5–7s cadence. A long-lived handle
  reads freshly-appended bytes the moment they land.
- **Wall-clock stale expiration** so a phantom in-progress fight
  closes after `gap_seconds` of real-world idle, not just when the
  log clock advances past the gap.
- **`tests/sim_live_log.py`** — appends fake combat lines to a
  temp log at a configurable rate so live-tail behavior can be
  validated without an actual EQ session.

### Out of scope for v0.6.0

- **Configurable overlay layout** — the four-counter arrangement
  is hardcoded.
- **HP-Δ history chart** in the overlay — only the current rate
  is shown, not a trace.
- **Multi-character overlay** (one window per follower) — the
  overlay follows whichever log the main UI has loaded.

## v0.5.0 — Cross-log encounter diff

The diff view from v0.4.0 grows up: instead of comparing two encounters
inside a single log, you can now load a **second log** alongside the
primary and diff one encounter from each. Built for the actually-useful
"before/after gear change between raid nights" case — different log
files, same boss, side-by-side numbers.

### Compare across logs

Tick exactly **one** row in the session list and the action bar gains
a **Compare across logs** button (next to the existing in-log Compare,
which still requires N=2). Click it to land on the new comparison-log
picker at `#/cross-compare?primary=<id>`:

- **Primary encounter card** at the top of the page so you remember
  which one you're comparing against (name, log filename, killed/
  incomplete status, raid DPS).
- **Load a comparison log** below: drag-drop the second log file
  anywhere on the page, click **Browse…** for the OS file picker, or
  paste a full path. Drag-drop on this view is auto-routed to the
  comparison endpoint instead of replacing the primary.
- Once parsed, the page flips to **Step 2 — pick the comparison
  encounter** and shows that log's session table. Click any row to
  open the cross-log diff.
- A **Pick a different log** button drops the comparison and bounces
  you back to the load-a-log step.

### Cross-log diff view

The existing `renderDiff()` view is reused — the payload shape is the
same as in-log diff, with a `cross_log: true` flag and a `log` field on
each encounter card. Cards in cross-log mode now show a **log:
filename.txt** subtitle right under the encounter name so it's
immediately obvious which log each side came from. Side classification
(friendly / enemy) is still computed across the union, so an actor on
both raid nights stays on the same side in both columns.

### Server: parallel comparison-log state

`_State` grows a parallel set of `comparison_*` slots — `comparison_logfile`,
`comparison_fights`, `comparison_heals`, `comparison_encounters`,
`comparison_sidecar` — that mirror the primary lifecycle but never feed
into the editing flows (sidecar mutations, manual encounter merges, etc.
all stay primary-only). The comparison parses with the **same
detection params** as the primary so encounters detected in both logs
are directly comparable. Swapping the primary log automatically clears
the comparison — a stale "compare against this other log" loses
meaning when the primary changes.

New endpoints:

- `POST /api/comparison/open` — load a second log from a disk path.
- `POST /api/comparison/upload` — drag-drop the second log; mirrors
  `/api/upload` shape.
- `POST /api/comparison/clear` — drop the comparison.
- `GET /api/comparison/session` — encounter list of the comparison
  log (same shape as `/api/session`, sans editing fields).
- `GET /api/diff/cross?primary_id=A&secondary_id=B` — the cross-log
  diff payload.

`_diff_payload` was refactored to take resolved `Encounter` objects
plus optional log labels rather than encounter id lists, so both
same-log and cross-log paths share one builder. Same-log callers
leave the labels as `None` and the `log` field on each encounter card
stays absent.

### Cross-log only (for now)

Out of scope for v0.5.0:

- **Cross-log encounter pairing UX** ("this looks like the same boss" —
  auto-suggesting which secondary encounter to compare against).
- **Standalone "Replace or Compare?" prompt** when a primary is loaded
  and the user drag-drops a file *outside* the cross-compare view.
  Currently those drops still replace the primary, matching v0.4.0
  behavior. The cross-compare view itself routes drops correctly.
- **N>2 logs** — the parallel-state approach intentionally caps at
  exactly two loaded logs. A future refactor to a `dict[path, Parsed]`
  could lift the cap if a use case for it shows up.

---

## v0.4.0 — Encounter diff + front-end split

The headline addition is **encounter diffing** — pick two encounters
from the same log and compare them side-by-side. Built to answer the
"did this gear change actually do anything?" question that was driving
the roadmap. Same-log only this release; cross-log diff is the next
slice once the server learns to hold more than one log at a time.

### Encounter diff

Tick exactly two rows in the session list and the action bar gains a
**Compare** button (next to Merge / Split / Clear). Click it to open
the diff view, which shows:

- **Two encounter cards** at the top — name, start time, duration,
  killed/incomplete status, total damage, raid DPS, total healing.
- **Headline delta strip** — Δ duration, Δ total damage, Δ raid DPS,
  Δ total healing — colored by *meaning*: faster duration is green
  (improvement) even though the delta is negative; total damage up is
  green; damage taken up is red; etc.
- **Three metric tabs** — **Damage** / **Damage taken** / **Healing**.
  Each rolls up the union of actors across both encounters with a
  per-encounter value and a delta.
- **Display toggle** — **Side-by-side** (two stacked bars per row,
  encounter A blue / encounter B green, shared x-scale across all
  visible rows so absolute magnitudes read true) or **Delta** (one
  centered-on-zero bar per row, colored by meaning, scaled to max abs
  delta).
- **Per-actor checkboxes** to hide noise, with a hidden-row strip at
  the bottom for one-click un-hide. State persists across tab and
  display switches so flipping between Damage and Healing keeps your
  filtered view.

Side classification (friendly vs enemy) is computed across the union
of both encounters using the same `received > dealt + healed` rule the
encounter detail and session summary use, so an actor stays on the
same side in both columns even if one encounter alone would flip them.

### Scroll-locked session view

The session list now uses a viewport-bounded layout: the page header,
summary stats, params panel, and action bar stay pinned while only
the encounter table scrolls. The table's column headers are sticky
inside that scroll region so they stay visible as you page through
long sessions. Other views (encounter detail, session summary, diff,
debug) keep normal page scroll — the lock is scoped to the session
view via a body class toggled by the router.

### Compare lives in the action bar

The Compare button used to live next to Session summary in the page
header, disabled-but-visible when nothing was selected. It moved into
the action bar alongside Merge / Split / Clear since it's strictly a
two-selection action with no whole-log mode like Session summary has
(Session summary works at any selection count and stays in the
header). Net: the header stays clean when nothing is ticked, and
Compare appears the moment you start acting on selection.

### Front-end extracted to `flurry/static/`

The HTML/CSS/JS for the web UI used to be one ~4,000-line triple-string
constant inside `server.py`. It's now `flurry/static/styles.css` and
`flurry/static/app.js`, served by a `/static/<name>` route that uses
`importlib.resources.files('flurry') / 'static'` to resolve files —
which works the same way in source-tree, `pip install`, and inside
the PyInstaller `--onefile` bundle (the build ships the dir via
`--add-data flurry/static`).

Net effect for users: nothing visible. For contributors: `app.js` and
`styles.css` get proper editor support (syntax highlighting, separate
linting, sensible grep), and edits to either show up on a plain
browser refresh — no Python restart needed (server replies with
`Cache-Control: no-cache`). `server.py` dropped from 5,750 → ~1,800
lines of clean Python.

---

## v0.3.1 — Tanking modal fix

Patch on top of v0.3.0. The **Δ Life** mode of the encounter-level
tanking pair modal showed `0 net life · 0 buckets` regardless of the
actual numbers. Cause: the modal's `refresh()` reset the subtitle and
stats from the per-event hits list, which is empty by design in delta
mode (delta is a derived series with no individual events). The
informative subtitle the modal had just written got clobbered the
moment it opened.

Fix: the subtitle is now delta-aware (`X damage taken · Y healing
received · ±Z net life`) and `refresh()` is skipped entirely for delta
since there's nothing to filter or window into. The chart math was
always correct — only the subtitle was misleading.

---

## v0.3.0 — Tanking views

The big addition is full **tank-side** visibility — what hit you, how
much got through, and what was avoided — surfaced both per-encounter
and across the whole session. Required substantial parser work to lift
avoidance/resist data out of `UnknownEvent`, then layered on UI.

### Parser: avoidance, resists, and edge cases

The melee miss path now classifies every avoided swing into one of
seven outcomes: `miss`, `riposte`, `parry`, `block`, `dodge`, `rune`,
`invulnerable`. The new `MeleeMiss.outcome` field carries the label,
classified from the line's tail clause (e.g. `but Y's magical skin
absorbs the blow!` → `rune`, `but YOU are INVULNERABLE!` →
`invulnerable`). First-person and third-person variants both parse,
including the `blocks with her shield!` long form.

A new `SpellResist` event covers `<target> resisted your <spell>!`
(only the first-person form exists in EQ logs since the log filters to
your perspective). Lines like `You were hit by non-melee for N damage.`
now flow through to damage-taken views as `(unattributed)` instead of
disappearing. A speculative `(falling)` source picks up `You take N
points of falling damage.` so a tank thrown off a platform mid-fight
gets labeled rather than collapsing into the generic non-melee bucket.

Same pass closed adjacent parser gaps that were dropping real combat
data: added `rend`/`stab` to the melee verb lists, allowed hyphens in
NAME (`Cazic-Thule`, `Terris-Thule`), introduced a `BODY_NAME` variant
for lowercase-article mob names (`a shadowstone grabber hit X for N
points by Spell.`), and tolerated EQ's stray double-spaces between
melee tokens with `[ ]+`. Combat-line coverage validated at 99%+
across six real logs.

### Encounter Tanking tab

A third tab joins Damage / Healing on every encounter detail page.
Friendly defenders sort by damage taken; each row expands into a
per-attacker breakdown with parry / block / dodge / rune / invuln /
miss / riposte counts, an avoid % column, and the biggest hit taken.
A synthesized **All** row at the top of each breakdown sums every
attacker's hits.

Click any breakdown row (or the All row) to pop a DTPS-over-time
modal — same chart machinery the Damage tab uses, just keyed by
`(attacker, defender)` instead of `(attacker, target)`. The full
windowing UI (click to set a 5s window, drag the yellow edges) and
the by-source filter both work.

### Session-summary Damage / Healing / Tanking tabs

The session summary view now mirrors the encounter detail's three-tab
structure. The **Tanking** tab shows defenders sorted by damage taken,
a DTPS-by-encounter line chart, a defender × encounter heatmap, and a
per-defender rollup table — all the visual building blocks you had
for damage and healing, now applied to incoming damage. Side
classification is computed once at the session level so an actor
stays on the same side across all three tabs.

### Damage / Healing / Life-delta toggle

Three modes selectable on every tanking graph surface:

- **Damage taken** — incoming DTPS (the default).
- **Healing received** — heals landing on the defender per second.
- **Life delta** — `healing - damage` per bucket. Positive = net heal,
  negative = net loss. On the line chart it renders as a filled area
  whose fill goes above zero for net-positive buckets and below for
  net-negative; on the heatmap, blue cells are net-up and red cells
  are net-down.

The toggle appears on the session-summary chart + heatmap and inside
the encounter-level pair modal (on the All row only — heals aren't
keyed by attacker, so the toggle doesn't make sense for per-attacker
rows). In delta mode the modal hides the by-source breakdown since
delta is a derived series with no individual events.

### Upload UI fix

Drag-dropping a log used to stick at "Uploading 100%" while the server
parsed, never transitioning to the parse-progress phase. Root cause:
`xhr.upload.load` isn't reliably fired when the server holds the HTTP
connection open through a slow parse. The UI now flips to "Parsing X
of Y · Z%" the moment the server's `parse_progress` state turns to
`parsing`, driven by the existing parse-status poll.

---

## v0.2.0 — Session summary

The big addition is a **multi-fight rollup view** that turns a whole
night of pulls into a single picture.

### Session summary

A new **Session summary** button on the session view opens a per-attacker
rollup across every encounter in the log:

- **Per-attacker table** — total damage, % of raid, avg/median/p95/best
  DPS, biggest hit, and how many encounters they appeared in. Sorted by
  total damage.
- **DPS-by-encounter chart** — top-N attackers as a line chart, the
  rest collapsed into an "Other" line so the legend stays readable. See
  who's consistent vs spike-y at a glance.
- **Attacker × encounter heatmap** — every friendly attacker as a row,
  every encounter as a column, cell color scaled to DPS. Click any cell
  to drill straight into that encounter. Empty cells (player absent)
  render as a faint dot, distinct from "showed up but did low damage."
- **Filters** — *Killed encounters only* (default on) drops wipes and
  aborted pulls so they don't drag down averages. *Min avg DPS* hides
  low-impact rows from the table.

### Selection-scoped summaries

Tick rows on the session list, then **Session summary** scopes the
rollup to just those encounters. The button label flips to
**Session summary (N selected)** so it's obvious what scope you're
about to load. Useful for:

- Boss-only views (skip the trash pulls)
- Comparing two attempts of the same fight
- Summarizing a single phase of a raid night

A **Show whole log** link appears in the scoped view to drop the scope
without going back.

### "All" row in damage / healing breakdowns

Expand any attacker row on an encounter detail and the per-target
breakdown tables (*Damage dealt to*, *Damage taken from*, plus the
healing equivalents) now have an **All** row at the top. It sums every
breakdown row, and clicking it pops the same modal chart as the
per-target rows — but aggregated across everyone, with a by-source
breakdown that mixes hits from all targets.

### UI polish

- Per-attacker rollup table is wider and has a sticky **Attacker**
  column when you scroll horizontally — the name stays in view as you
  read across to the stats.
- Fixed a layout bug where the rollup panel background ended mid-table
  on certain viewports.
- The session-summary view stacks vertically below 1100px so the
  heatmap stays usable on narrower screens.

### Build

- The Windows build script now retries when Defender / Explorer
  transiently locks the `dist/` directory mid-cleanup, instead of
  failing with `PermissionError`.

---

## v0.1.0 — Initial release

First public version. Pure-Python parser for EverQuest combat logs,
with a local web UI, per-attacker DPS breakdowns, encounter
auto-grouping, pet-owner overrides, healing views, time-window slicing
of large logs, and a single-file `flurry.exe` build for Windows users
without Python.
