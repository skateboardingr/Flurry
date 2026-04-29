# Flurry

**EverQuest combat log analyzer.** Parse your eqlog files, break down fights
by attacker, and visualize damage timelines.

```
                            0s    5s   10s   15s   20s   25s   30s
  ----------------------------------------------------------------
  Soloson                 4.5M 10.7M 16.0M 10.1M 10.4M  8.2M 10.3M
  You                     2.9M  9.7M  4.2M  3.7M  2.7M  2.7M  4.5M
  Keidara                 4.6M  9.9M 14.7M 14.9M  9.3M  8.5M    .
  Robbinwuud              176k 10.3M  1.3M  6.4M  1.3M  1.3M  2.3M
  RAID                   12.1M 40.7M 36.3M 35.2M 23.7M 20.6M 17.1M
  DPS                     2.4M  8.1M  7.3M  7.0M  4.7M  4.1M  3.4M
```

Pure Python, no runtime dependencies. Works on live retail, P99, Quarm,
TAKP — anywhere EQ writes its standard log format.

## What it parses

- First-person and third-person melee (`You slash X`, `Soloson slashes X`)
- Pet damage (`Soloson\`s pet` — note the backtick, an EQ quirk)
- Spells with named damage types (`You hit X for N points of cold damage by Strike of Ice`)
- DoT ticks (`X has taken N damage from <spell> by <source>`)
- Damage shields and weapon procs (`X is pierced by Y's thorns`)
- Heals — direct and HoT, with self-heal pronoun normalization
- All seven melee avoidance outcomes — `miss`, `riposte`, `parry`,
  `block` (incl. `blocks with her shield!`), `dodge`, `rune`
  (`magical skin absorbs the blow!`), `invulnerable` (divine aura)
- Spell resists (`X resisted your Spell!`) as their own event type
- Both death-message formats (`has been slain by` / `was slain by`)
- Special attacks: Headshot, Assassinate, Slay Undead, Decapitate,
  Double Bow Shot, Flurry, Twincast, Rampage
- Edge cases: hyphenated names (`Cazic-Thule`, `Terris-Thule`),
  lowercase mob articles at body start (`a shadowstone grabber hit X`),
  EQ's stray double-spaces between melee tokens, and falling damage

## Install

Flurry is a standalone app. There are three ways to run it; pick the one
that matches your situation.

### 1. Download `flurry.exe` (Windows, no Python needed)

Grab the latest `flurry.exe` from the releases page. Double-click it. A
console window opens, the URL prints, and your browser opens to the UI.
Close the console to stop.

> **Heads-up about Windows SmartScreen.** The first time you run
> `flurry.exe` you'll likely see *"Windows protected your PC — unrecognized
> app from an unknown publisher"*. That's the standard warning Windows
> shows for any unsigned binary it hasn't seen before — not a malware
> detection. Click **More info → Run anyway**. Windows will remember
> that exact build's hash and not prompt again on this machine; new
> releases (different hash) will prompt once. Source is on GitHub if
> you'd rather build it yourself — see option 3 below.

### 2. Run from source (Python installed)

```bash
git clone https://github.com/skateboardingr/Flurry.git
cd Flurry
python -m flurry              # launch UI
```

Or double-click `flurry.bat` (Windows) / `./flurry.sh` (POSIX). You can
also run `python -m flurry [logfile]` to skip the file picker.

### 3. Build the .exe yourself

```bash
pip install -e ".[build]"     # pulls in PyInstaller
python build_exe.py
```

Produces `dist/flurry.exe` (~9 MB, single-file, self-contained).

### Library use (optional)

```bash
pip install -e .
```

`pip install` doesn't put anything on PATH — it just makes
`from flurry import …` available. Useful if you want to drive the
parser/analyzer from another Python project.

## Usage

### Per-attacker damage breakdown

```bash
python -m flurry dps eqlog_Hacral_firiona.txt "Shei Vinitras"
```

Output:

```
=== Fight: Shei Vinitras ===
  Start:    2026-04-25 12:21:17
  End:      2026-04-25 12:22:48
  Duration: 91.0s

  Total damage dealt: 322,252,497
  Raid DPS:           3,541,236

  Attacker                     Damage        DPS   Hits  Miss  Crit     Biggest     %
  -------------------- -------------- ---------- ------ ----- ----- ----------- -----
  Soloson                 130,025,461  1,428,851    408    66   280   1,250,520 40.3%
  You                      66,577,052    731,616    405    30   339   1,613,471 20.7%
  Keidara                  61,942,657    680,689     54     4    52   7,276,235 19.2%
  Robbinwuud               60,460,191    664,398    128    13   127   8,435,147 18.8%
  Zeldahn                   3,207,092     35,243      9     0     6     967,999  1.0%
  Hacral`s pet                 38,945        428     52    32     0       1,504  0.0%
  Soloson`s pet                 1,099         12     17     3     0         115  0.0%

  Special attacks:
    Attacker             Type                Hits         Damage % of their dmg
    -------------------- ------------------ ----- -------------- --------------
    Soloson              Flurry                42     24,998,050          19.2%
    You                  Flurry                36      8,889,350          13.4%
    Keidara              Headshot               8     47,882,984          77.3%
    Keidara              Double Bow Shot       42     13,568,703          21.9%
    Robbinwuud           Headshot               5     29,881,730          49.4%
    Robbinwuud           Double Bow Shot      120     29,961,415          49.6%
```

### Browse fights interactively (web UI)

```bash
python -m flurry                                # launches with a file picker
python -m flurry eqlog_Hacral_firiona.txt       # jumps straight to the session view
```

Or run `flurry.exe` (downloaded release) / double-click `flurry.bat`
(source).

Opens a browser tab pointing at `localhost:8765`. The session view lists
every detected fight (auto-grouped into encounters when fights overlap),
sortable by any column. Click a row to open the encounter detail page
with three tabs:

- **Damage** — per-attacker DPS table split into Friendlies / Enemies,
  expandable into per-target damage breakdowns. Click any breakdown
  row to pop a modal with a per-second chart, a click-to-window timeline
  for drilling into a 5s slice, and a by-source filter (Slashes,
  Backstabs, Strike of Ice, etc.).
- **Healing** — same shape but for heals: per-healer HPS, per-(healer,
  target) breakdowns, biggest-heals list, stacked-area HPS timeline.
- **Tanking** — friendly defenders sorted by damage taken, with parry /
  block / dodge / rune / invuln / miss / riposte counts and avoid %.
  Each row expands into a per-attacker breakdown. Click any row (or the
  bold **All** row at the top) to pop a DTPS-over-time modal with a
  **Damage / Healing / Δ Life** toggle — Δ Life shows healing minus
  damage per bucket, dipping below zero when net negative.

A **Session summary** button on the session view opens a multi-fight
rollup also split by Damage / Healing / Tanking tabs. Each tab shows a
trend chart, an actor × encounter heatmap (click any cell to drill into
that encounter), and a per-actor table with avg/median/p95/best rates.
Tanking gets the same Damage / Healing / Δ Life sub-toggle on its chart
and heatmap, with diverging blue/red colors for delta.

Tick exactly two rows in the session list and the action bar gains a
**Compare** button. Click it to open the **diff view** — two encounter
cards at the top, a headline strip with Δ duration / Δ damage / Δ raid
DPS / Δ healing, and a per-actor table with **Damage / Damage taken /
Healing** tabs. Toggle between **Side-by-side** (two bars per row, A
blue / B green) and **Delta** (one centered-on-zero bar per row,
colored by *meaning* — green when the change is good for the active
metric, red when it's bad). Useful for "what did this new weapon
actually do?" comparisons inside one raid night.

Tick exactly **one** row instead and the action bar gains a **Compare
across logs** button. Click it to load a second log (drag-drop the
file, OS browse, or paste a path) and pick which encounter from that
log to compare against. Same diff view, with each encounter card
labeled by its log filename so it's obvious which side came from
which raid night. Built for the "before/after gear change *between*
raid nights" case — same boss, two different log files. Swapping the
primary log automatically clears the comparison.

Drag-and-drop a log file anywhere on the page to load it without going
through the file picker. The **Change log** button in the header re-opens
the picker so you can switch logs without restarting. The **Pet owners**
button on encounter detail lets you assign owners to actors that don't
carry the backtick-pet suffix in the log (mage water pets, charmed mobs);
assignments persist to a `<logfile>.flurry.json` sidecar next to the log.

Useful flags:

- `--port N`: pick a different port (default 8765)
- `--gap N`, `--min-damage N`: same as the `session` subcommand
- `--bucket N`: timeline bucket size in seconds (default 5)
- `--encounter-gap N`: window for grouping fights into encounters (default 10)
- `--since-hours N`: only parse the last N hours of the log (default 8;
  set to 0 for the whole log)
- `--no-browser`: don't auto-open a browser tab

### Auto-detect every fight in a log

The same listing the UI shows, in your terminal:

```bash
python -m flurry session eqlog_Hacral_firiona.txt
```

Output:

```
=== Session: eqlog_Hacral_firiona.txt ===

   ID  Start                   Dur  Target                                Damage         DPS  Status
  ---  -------------------  ------  ----------------------------  --------------  ----------  ----------
    1  2026-04-25 12:21:17   1m31s  Shei Vinitras                    322,252,497   3,541,236  Killed
    2  2026-04-25 12:24:50     45s  a frost drake                     18,500,000     411,111  Killed
    3  2026-04-25 12:25:30     12s  a snow worm                          850,200      70,850  Killed

  3 fights detected (3 killed). Total damage: 341,602,697
```

Each detected fight is one mob taking damage in a contiguous combat
window. A boss plus its adds will appear as multiple fights — the idea
is to keep per-target stats clean and let you group them into a named
"encounter" later (planned for the UI).

Useful flags:

- `--gap N`: seconds of inactivity before a fight is considered over
  (default 15 — short enough to split distinct engagements; the UI is
  where you regroup phase-pausing boss kills into one encounter)
- `--min-damage N`: hide fights below this total damage (default 10,000)

### Fight timeline

```bash
python -m flurry timeline eqlog_Hacral_firiona.txt "Shei Vinitras" \
    --bucket 5 \
    --html shei.html
```

Prints a per-bucket text table to the terminal and writes an interactive
Chart.js stacked-area report to `shei.html`. Open the HTML in any browser
for hover tooltips and a biggest-hits table.

Useful flags:

- `--bucket N`: change bucket width (default 5 seconds)
- `--top-hits N`: number of biggest single hits to list (default 10)
- `--min-dps N`: hide attackers with avg DPS below N (declutters tables; default 100)

## Library API

Flurry can also be used as a Python library:

```python
from flurry import (
    analyze_fight, detect_fights, detect_combat,
    group_into_encounters, merge_encounter, bucket_hits,
)
from flurry.report import text_dps_report, html_timeline_report

# detect_combat returns both fights and a flat heal list in one walk.
fights, heals = detect_combat('eqlog_Hacral_firiona.txt')
encounters = group_into_encounters(fights, gap_seconds=10, heals=heals)

for enc in encounters:
    flat = merge_encounter(enc)
    print(f'#{enc.encounter_id} {enc.name}: {flat.total_damage:,} '
          f'({flat.duration_seconds:.0f}s, {len(enc.heals)} heals)')

# Or analyze one specific target without auto-detection.
result = analyze_fight('eqlog_Hacral_firiona.txt', 'Shei Vinitras')
print(text_dps_report(result))

# Defender-perspective stats — what hit me, how often did they land,
# how were the misses distributed.
for (atk, defender), d in result.defends_by_pair.items():
    if d.damage_taken == 0: continue
    print(f'{atk} → {defender}: {d.damage_taken:,} dmg over '
          f'{d.hits_landed} hits, {d.total_avoided} avoided '
          f'({d.avoided})')

# Build and render a timeline.
timeline = bucket_hits(result, bucket_seconds=5)
with open('fight.html', 'w') as f:
    f.write(html_timeline_report(result, timeline))
```

Key types from `flurry`:

- **`FightResult`** — `target`, `start`, `end`, `duration_seconds`,
  `fight_complete`, `fight_id`, `total_damage`, `raid_dps`, `hits`
  (list of `Hit`), `stats_by_attacker` (dict of `AttackerStats`),
  `defends_by_pair` (dict of `DefenseStats` keyed by `(attacker,
  defender)`), `attackers_by_damage()`.
- **`AttackerStats`** — `damage`, `hits`, `misses`, `crits`, `biggest`,
  `special_damage`, `special_hits`.
- **`DefenseStats`** — defender-perspective: `damage_taken`,
  `hits_landed`, `biggest_taken`, `avoided` (dict keyed by outcome
  label — see `DEFENSE_OUTCOMES`), `total_avoided`, `total_swings`.
- **`HealerStats`** — `healing`, `casts`, `crits`, `biggest`,
  `spell_amount`, `spell_casts`.
- **`Encounter`** — `encounter_id`, `members` (list of `FightResult`),
  `name`, `fight_complete`, `heals`, `total_healing`, `start`, `end`,
  `duration_seconds`, `target_count`. Use `merge_encounter(enc)` to
  flatten back into one `FightResult` for reuse with the existing
  reporting helpers.
- **Events** — `MeleeHit`, `MeleeMiss` (carries `outcome`),
  `SpellDamage`, `SpellResist`, `HealEvent`, `DeathMessage`,
  `ZoneEntered`, `UnknownEvent`. `parse_line(line)` is the entry point.

## Project layout

```
flurry/
├── flurry/                   # the package
│   ├── __init__.py           # public API exports
│   ├── __main__.py           # `python -m flurry` subcommand routing
│   ├── events.py             # event dataclasses
│   ├── parser.py             # log line → event
│   ├── tail.py               # file follower + byte-offset slicing
│   ├── analyzer.py           # fight analysis (FightResult, Encounter, Timeline)
│   ├── sidecar.py            # per-log persistence: pet owners + manual encounters
│   ├── report.py             # text + HTML rendering
│   ├── server.py             # local web UI (stdlib http.server + JSON API)
│   ├── static/               # front-end assets served via /static/<name>
│   │   ├── styles.css
│   │   └── app.js
│   └── cli.py                # argparse entry functions
├── flurry.bat                # Windows launcher (wraps `python -m flurry`)
├── flurry.sh                 # POSIX launcher
├── _pyinstaller_entry.py     # tiny launcher used by the .exe build
├── build_exe.py              # `python build_exe.py` -> dist/flurry.exe
├── tests/                    # 119 tests across 6 files (see Tests section)
└── pyproject.toml
```

## Tests

Tests are runnable as plain scripts — they import from the source tree
directly, no pip install needed. Each file has its own `main()` runner
that prints PASS/FAIL per test:

```bash
python tests/test_parser.py          # 53 tests — parser correctness
python tests/test_analyzer.py        # 10 tests — analyze_fight against a real log fixture
python tests/test_detect_fights.py   # 14 tests — fight auto-detection + defends_by_pair
python tests/test_overrides.py       # 13 tests — apply_pet_owners + manual encounter groups
python tests/test_sidecar.py         # 17 tests — sidecar JSON load/save + atomic writes
python tests/test_tail_window.py     # 13 tests — byte-offset slicing + progress callbacks
```

The analyzer tests use a real raid log as a fixture and auto-skip if
the fixture isn't present; the rest synthesize their own log content
in tempfiles or work entirely off in-memory dataclasses, so they run
anywhere.

## Roadmap

What's left, in rough order of usefulness:

- **JSON export** — `--json` flags on the CLI tools for piping into
  other tools. The UI already has its own JSON via `/api/*`.
- **Live tail mode** — watch DPS / HPS / DTPS update in real time as
  the log grows. `tail.py` already supports follow-mode; the analyzer
  and server don't yet.
  - **Player overlay** — a compact, always-on-top window with four
    counters for the active character (damage out, damage in, healing
    out, healing in) so you can glance at your own performance mid-fight
    without alt-tabbing to the full UI.
  - **HP delta indicator** — net HP change over the last second
    (damage taken + heals received), green for gains and red for losses,
    so you can see trouble coming before the health bar does.

Things that used to be on this list and have shipped — encounter
grouping, pet ownership, session-summary rollups, healing tab, tanking
tab with avoidance breakdown, life-delta toggle, in-log encounter
diff, cross-log encounter diff. See `RELEASES.md` for the running
history.

## License

MIT.
