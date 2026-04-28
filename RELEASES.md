# Releases

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
