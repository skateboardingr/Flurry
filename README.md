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
- Damage shields and weapon procs (`X is pierced by Y's thorns`)
- Both death-message formats (`has been slain by` / `was slain by`)
- Special attacks: Headshot, Assassinate, Slay Undead, Decapitate,
  Double Bow Shot, Flurry, Twincast, Rampage

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
every detected fight; click a row to drill into per-attacker DPS, special
attacks, biggest hits, and a stacked-area timeline chart. The "Change log"
button in the header re-opens the file picker so you can switch logs
without restarting.

Useful flags:

- `--port N`: pick a different port (default 8765)
- `--gap N`, `--min-damage N`: same as the `session` subcommand
- `--bucket N`: timeline bucket size in seconds (default 5)
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
from flurry import analyze_fight, detect_fights, bucket_hits
from flurry.report import text_dps_report, html_timeline_report

# Auto-detect every fight in a log.
fights = detect_fights('eqlog_Hacral_firiona.txt')
for f in fights:
    print(f'#{f.fight_id} {f.target}: {f.total_damage:,} ({f.duration_seconds:.0f}s)')

# Or analyze a specific named target.
result = analyze_fight('eqlog_Hacral_firiona.txt', 'Shei Vinitras')

# Print the same DPS table the CLI prints.
print(text_dps_report(result))

# Or work with the raw stats.
top = result.attackers_by_damage()[0]
print(f'{top.attacker} did {top.damage:,} damage ({top.crits} crits)')

# Build and render a timeline.
timeline = bucket_hits(result, bucket_seconds=5)
with open('fight.html', 'w') as f:
    f.write(html_timeline_report(result, timeline))
```

The `FightResult` dataclass exposes:

- `target`, `start`, `end`, `duration_seconds`, `fight_complete`
- `fight_id` — set by `detect_fights` (1-indexed by start time); `None`
  for results from `analyze_fight`
- `total_damage`, `raid_dps`
- `hits` — list of every individual damage event
- `stats_by_attacker` — dict of `AttackerStats` keyed by attacker name
- `attackers_by_damage()` — sorted list, biggest first

`AttackerStats` exposes:

- `damage`, `hits`, `misses`, `crits`, `biggest`
- `special_damage`, `special_hits` — dicts keyed by special-attack name

## Project layout

```
flurry/
├── flurry/                   # the package
│   ├── __init__.py           # public API exports
│   ├── __main__.py           # `python -m flurry` subcommand routing
│   ├── events.py             # event dataclasses
│   ├── parser.py             # log line → event
│   ├── tail.py               # file follower (replay + live)
│   ├── analyzer.py           # fight analysis (FightResult, Timeline)
│   ├── report.py             # text + HTML rendering
│   ├── server.py             # local web UI (stdlib http.server + inline HTML)
│   └── cli.py                # argparse entry functions
├── flurry.bat                # Windows launcher (wraps `python -m flurry`)
├── flurry.sh                 # POSIX launcher
├── _pyinstaller_entry.py     # tiny launcher used by the .exe build
├── build_exe.py              # `python build_exe.py` -> dist/flurry.exe
├── tests/
│   ├── test_parser.py         # parser correctness (13 tests)
│   ├── test_analyzer.py       # analyzer correctness (10 tests)
│   └── test_detect_fights.py  # fight auto-detection (11 tests)
└── pyproject.toml
```

## Tests

```bash
python tests/test_parser.py          # 13 tests
python tests/test_analyzer.py        # 10 tests
python tests/test_detect_fights.py   # 11 tests
```

These don't require pip-installing anything — they import from the source
tree directly.

The analyzer tests use a real raid log as a fixture. They auto-skip if
the fixture file isn't present. The detect_fights tests synthesize their
own log content in tempfiles, so they run anywhere.

## Roadmap

The web UI shipped as Pass 1 (navigation only). Plausible next features:

- **Encounter grouping in the UI** — multi-select fights from the
  session table, name them, persist to a `<logfile>.flurry.json`
  sidecar. Lets you group boss + adds (or trash + boss) into one
  named encounter with merged stats.
- **Pet ownership in the UI** — click a named pet ("Bonebreaker") and
  assign it to its owner. Persists in the same sidecar.
- **Multi-fight session reports** — average DPS per attacker across a
  raid night, with min/max/median/p95.
- **Healing and tanking views** — same per-attacker model, but for
  HPS/damage-mitigated.
- **Log diffing** — compare the same boss before and after a gear change.
- **JSON export** — `--json` flags on the CLI tools for piping into
  other tools.
- **Live tail mode** — watch DPS update in real time as the log grows.
  - **Player overlay** — a compact, always-on-top window with four
    counters for the active character (damage out, damage in, healing
    out, healing in) so you can glance at your own performance mid-fight
    without alt-tabbing to the full UI.
  - **HP delta indicator** — net HP change over the last second
    (damage taken + heals received), shown green for gains and red
    for losses, so you can see trouble coming before the health bar
    does.

## License

MIT.
