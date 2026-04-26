# Releases

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
