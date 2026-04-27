# Flurry — project context

This file exists so that a fresh collaborator (human or AI) can pick up
work on Flurry without re-reading the entire commit history or relitigating
old decisions. If you're an AI assistant starting a new session: **read this
file first**.

Keep it current. When we make a non-obvious decision, lock it in here.
When we finish a roadmap item, move it to "Done." When we discover a
gotcha, write it down so the next person doesn't trip on it.

---

## What Flurry is

A standalone, pure-Python EverQuest combat log analyzer. It parses
`eqlog_<character>_<server>.txt` files — the standard logs EQ writes when
you run `/log on` — and produces per-fight damage breakdowns and timelines.

Think GamParse, but library-first: the analysis core is a clean Python
package you can `pip install`, with CLIs (`flurry-dps`, `flurry-timeline`)
on top. No GUI yet.

It works on live retail (Daybreak), and should work on emu servers (P99,
Quarm, TAKP) since they share the log format. Confirmed against live
Firiona Vie logs.

---

## Why it exists

Two reasons:

1. **It's a useful tool on its own.** EQ veterans have wanted a modern,
   scriptable parser for a long time.

2. **It's the foundation for a separate EQ bot project.** That bot will
   need exactly this parser to perceive the game world. Building Flurry
   first as a standalone library means the bot can vendor or import it
   later without dragging bot-specific code into the analyzer.

The two projects are peers, not parent/child. Flurry never imports from
the bot. The bot will import (or vendor) Flurry's parser when it's ready.

---

## Where things live

```
flurry/
├── README.md                # public-facing docs (install, usage, roadmap)
├── CONTEXT.md               # this file
├── pyproject.toml           # package metadata; declares CLI entry points
├── flurry/                  # the package itself
│   ├── __init__.py          # public API exports
│   ├── __main__.py          # `python -m flurry` subcommand routing
│   ├── events.py            # event dataclasses (the vocabulary)
│   ├── parser.py            # regex patterns: line text -> Event
│   ├── tail.py              # file follower (replay + live tail modes)
│   ├── analyzer.py          # FightResult, Timeline; pure data, no I/O
│   ├── sidecar.py           # per-log persistence: <log>.flurry.json
│   ├── report.py            # text + HTML rendering
│   ├── server.py            # local web UI (stdlib http.server + inline HTML)
│   └── cli.py               # argparse-backed entry functions
├── flurry.bat / flurry.sh   # source-tree launchers (wrap `python -m flurry`)
├── _pyinstaller_entry.py    # tiny launcher; PyInstaller bundles this
├── build_exe.py             # `python build_exe.py` -> dist/flurry.exe
└── tests/
    ├── test_parser.py        # 29 tests: parser correctness
    ├── test_analyzer.py      # 10 tests: analyzer correctness
    ├── test_detect_fights.py # 11 tests: fight auto-detection
    ├── test_overrides.py     # 12 tests: pet-owner + manual encounter
    └── test_sidecar.py       # 17 tests: sidecar load/save + helpers
```

---

## The architecture in one paragraph

`parser.py` turns text lines into typed events (`MeleeHit`, `SpellDamage`,
`DeathMessage`, etc.) and that's all it does — no opinions about what
fights are, no aggregation. `analyzer.py` consumes those events and
produces structured fight data (`FightResult`, `Timeline`) — also with no
formatting opinions. `report.py` is the *only* place that knows about
text tables or HTML. This separation is deliberate: future things like
JSON export, a web UI, a Discord bot, or feeding fights into another
tool just need a new file alongside `report.py`. They don't touch parser
or analyzer.

---

## Hard-won parser knowledge — things that are not obvious

These are real gotchas we've already hit. Don't re-discover them.

### First-person verbs differ from third-person

EQ writes the player's own attacks in first person with bare verbs, and
everyone else's in third person with -es/-s endings:

- `You slash X for N points`         (first person, bare verb)
- `Soloson slashes X for N points`   (third person, -es ending)
- `You try to slash X, but miss!`    (first-person miss, bare verb)
- `Soloson tries to slash X, but misses!`  (third-person miss, -es)

The parser has *separate* regexes for each. The original prototype only
matched third person, which silently dropped 100% of the player's own
melee damage. That bug under-reported Hacral's damage by 94% before we
caught it.

### Pets use a backtick, not an apostrophe

`Soloson\`s pet slashes X` — that's a literal backtick character (`` ` ``)
between the name and the `s`, not a curly quote, not an apostrophe.
EQ has done this since launch. The `NAME` regex pattern in `parser.py`
includes both `'` and `` ` `` in the allowed character set.

### `was slain` vs `has been slain`

Two death-message formats exist in the wild:

- `X has been slain by Y!`   (common mobs, NPCs, most kills)
- `X was slain by Y!`        (raid bosses often use this form)

We match both. Originally we only matched the first, and missed every
raid kill — including the Shei Vinitras kill that started this project.

### `You hit X for N points of damage` (no spell name) is melee

Looks like spell damage, but if there's no `by SPELLNAME` suffix, it's
a melee/ranged hit attributed in first person. Distinct from
`You hit X for N points of cold damage by Strike of Ice` which IS spell
damage. The parser checks for the spell-suffix variant first; the bare
form falls through to the melee handler.

### Spell damage uses bare 'hit' for both persons

Unlike melee (where third-person uses '-s' verbs like `slashes`), spell
damage uses bare `hit` for both first AND third person:
  - `You hit Shei for N points of cold damage by Strike of Ice.`
  - `Onyx hit a Solusek foot soldier for N points of fire damage by Flamebrand VII.`

We briefly split SPELL_DAMAGE into 1ST/3RD with `-s` for 3RD as a
defense against the 'has been' slurp; that broke every third-person
spell hit in real logs. The current pattern is one combined regex with
a `(?!by )` lookahead at the start of `target` — that blocks the passive
form (`X has been hit by Y for ...`) without rejecting any active form.

### Names with comma-titles

Some EQ NPCs have epithets after a comma, like `Keltakun, Last Word` or
`General Usira, the Mighty`. NAME accepts `, ` as a word separator so
these parse as a single attacker/target name rather than getting
truncated at the comma.

### Riposte is a miss

`X tries to hit Y, but Y ripostes!` is a missed attack from X's
perspective — same fight semantics as a regular miss. Y's counter-damage
shows up as its own `X hits Y for N points of damage. (Riposte ...)`
line and is parsed as a normal MeleeHit. The MELEE_MISS_*_RE patterns
include a riposte tail; the riposting party's name isn't captured (the
hit line attributes the counter-damage on its own).

### Avoidance comes in seven flavors, only one is "miss"

EQ writes a missed swing with a tail clause that names *how* the swing
failed. The MELEE_MISS patterns capture the whole tail and
`_classify_miss_tail` maps it to one of seven `outcome` labels on the
`MeleeMiss` event:

- `miss`         — `but miss(es)!` / `but fail(s)!`
- `riposte`      — `but Y ripostes!` / `but YOU riposte!`
- `parry`        — `but Y parries!` / `but YOU parry!`
- `block`        — `but Y blocks!` / `but YOU block!` /
                   `but Y blocks with her shield!` (the "with X
                   shield/staff/..." suffix is observed only on block,
                   never on parry/dodge)
- `dodge`        — `but Y dodges!` / `but YOU dodge!`
- `rune`         — `but Y's magical skin absorbs the blow!` /
                   `but YOUR magical skin absorbs the blow!` (rune buff
                   absorbed the hit)
- `invulnerable` — `but Y is INVULNERABLE!` / `but YOU are INVULNERABLE!`
                   (god mode / divine aura)

First-person avoider uses bare verbs (`YOU dodge!`), third-person uses
`-s/-ies` (`Soloson dodges!`, `Soloson parries!`). The avoider is always
also the line's `target` — no separate avoider field is needed.

Spell resists are a related-but-separate event (`SpellResist`):
`<target> resisted your <spell>!`. Only the first-person form exists in
EQ logs — the log filters to your perspective, so other players' resists
never appear. Caster is hardcoded `'You'`.

### Body-start NAME accepts lowercase articles; mid-line NAME does not

`NAME` requires a capital letter so it doesn't slurp common English
words mid-sentence. But mob spell-damage lines start with lowercase:
`a shadowstone grabber hit Robbinwuud for N points by Spell.`. EQ does
*not* capitalize the article when the mob name leads a line.

`BODY_NAME` is a relaxed variant (`[A-Za-z]` start) used at body-start
positions: `SPELL_DAMAGE`, `MELEE_HIT_3RD`, `MELEE_MISS_3RD`, `DOT_DAMAGE`
target, `HEAL_RE` healer, `HEAL_PASSIVE` target, `SPELL_RESIST` target.
Mid-line uses (`by NAME` clauses, damage shield attacker) keep the strict
`NAME` to avoid grabbing prepositions.

### Hyphens are valid inside NAME

Boss-tier names like `Cazic-Thule` and `Terris-Thule` use a hyphen.
The NAME char class is `[\w'`-]` (note the trailing `-`, which is
literal at the end of a character class) so these parse as a single
attacker/target, not as `Cazic` truncated at the dash.

### EQ injects double spaces between melee tokens, sometimes

A small fraction of melee lines have an extra space between subject
and verb (`A Valorian Sentry  tries to punch ...`) or verb and target
(`Redfreddy slashes  Sigismond Windwalker for ...`). The MELEE_HIT and
MELEE_MISS patterns use `[ ]+` rather than literal ` ` at every token
join so these still parse. Specific to combat patterns — other regexes
were unaffected in observed logs.

### Greedy NAME + multi-form verbs is a recurring trap

`NAME = r"[A-Z][\w'`]*(?:[ '`][\w'`]+)*"` is intentionally loose so it
accepts pet names like `Soloson\`s pet`. The cost is that NAME will
happily slurp lowercase auxiliary words, so `Shei has been` matches as
one NAME. If a pattern then allows a verb that's the same in active and
passive participle forms (e.g. `(?:hit|hits)` matches both `hit` and
`hits`), passive log lines silently mis-parse: `Shei has been hit by
Soloson for ...` is read as `attacker='Shei has been'`.

**Mitigation pattern**: split active forms by person (1st-person uses
literal `You` + bare verb; 3rd-person uses NAME + `-s` verb). Keeps NAME
loose for legitimate uses but eliminates the passive-collision path.
`MELEE_HIT_*_RE` already does this; `SPELL_DAMAGE_*_RE` and the heal
patterns now do too. If you add a new damage/heal pattern, follow this
shape — don't combine `(?:verb|verbs)` with NAME unless you're certain
no passive form exists.

### Generic non-melee lines have no source — attributed to "(unattributed)"

`Onyx was struck for 2301 points of non-melee damage.` — no `by Y`
suffix, so EQ doesn't tell us who did it (DoT ticks, lifetap effects,
some environmental damage). The parser uses the sentinel attacker name
`(unattributed)` so the per-attacker table makes sense. Older versions
used `?`, which was confusing in the UI.

### Damage shields and weapon procs use a third format

`Shei Vinitras is pierced by Soloson's thorns for 5175 points of non-melee damage.`

This is a damage shield proc on Soloson's gear. The DAMAGE_SHIELD_RE
pattern catches it and attributes the damage to **Soloson** (the source
owner), not to "thorns". This matters for accurate per-player DPS.

### `You have entered an area where...` is NOT a zone change

`You have entered an area where levitation effects do not function.` is
a sub-zone notification, not a real zone transition. ZONE_ENTERED_RE
uses a negative lookahead `(?!an area )` to skip these.

### Modifiers are space-separated keywords

A line ending `(Lucky Critical Headshot)` contains three modifiers in
one paren group: "Lucky Critical" indicates the crit type, "Headshot"
is the special attack. The analyzer checks for substring matches inside
the combined string rather than expecting separate tokens. See
`is_crit()` and `extract_specials()` in `analyzer.py`.

### Dispatch order matters

`PATTERNS` in `parser.py` is a list checked in order, first-match-wins.
Specific patterns (with `by SPELLNAME`) come before general patterns
(bare melee), or the general one would eat them. If you add a new
pattern, think about ordering.

---

## Done so far

### Parser (parser.py)
- [x] Combat: melee hits/misses (1st + 3rd person), spell damage, damage
      shields/procs, generic non-melee damage, heals
- [x] Death messages (both `has been slain` and `was slain` forms,
      plus `You have been slain`)
- [x] Zone transitions with sub-zone false-match guard
- [x] Pet attribution via backtick names
- [x] Tested against real raid log: 322,252,497 damage on Shei Vinitras
      attributed correctly across 7 attackers including 2 pets
- [x] Damage avoidance and resists. `MeleeMiss.outcome` is one of
      `miss | riposte | parry | block | dodge | rune | invulnerable`
      and a new `SpellResist` event covers `<target> resisted your
      <spell>!`. Combat-line coverage validated at 99%+ across six
      real logs (Hacral, Erebseth, Lyricil, Merkkava, Roobius, Treefer).
      Same pass added two missing melee verbs (`rend`/`stab`), hyphen
      support in NAME (`Cazic-Thule`), `BODY_NAME` for lowercase-article
      mob names (`a shadowstone grabber hit ...`), and `[ ]+` tolerance
      for EQ's stray double-spaces between melee tokens.
- [x] First-person bare non-melee (`You were hit by non-melee for N
      damage.`) routes to `attacker='(unattributed)'`. EQ literally
      writes `non-melee` as the source, so no further attribution is
      possible — this just lifts the damage out of UnknownEvent so it
      reaches damage-taken views.
- [x] Falling damage parser (`You take N points of falling damage.`)
      routes to `attacker='(falling)'`, `damage_type='falling'`. **The
      pattern is UNVERIFIED** — none of our six fixture logs contain a
      single fall-damage event, so the regex is from community memory
      rather than direct observation. Motivation: a tank thrown off a
      platform during a boss mechanic should show up labeled rather
      than collapsing into the generic non-melee bucket. If a real
      fall-damage line appears with a different shape, replace the
      regex AND `test_falling_damage_speculative_format`.

### Analyzer (analyzer.py)
- [x] `analyze_fight(logfile, target)` -> `FightResult`
- [x] Per-attacker stats: damage, hits, misses, crits, biggest hit
- [x] Special attack breakout: Headshot, Assassinate, Slay Undead,
      Decapitate, Double Bow Shot, Flurry, Twincast, Rampage
- [x] `bucket_hits(result, bucket_seconds)` -> `Timeline`
- [x] Auto-end fight on target death; flag `fight_complete=False` if
      log ends mid-fight
- [x] `detect_fights(logfile, gap_seconds=60, min_damage=10_000)` -> per-target
      `FightResult` list with 1-indexed `fight_id`. Fights are split per
      mob (a boss + adds become multiple fights); UI groups them into
      encounters. `_FightBuilder` is shared between `analyze_fight` and
      `detect_fights` so attribution logic lives in one place.

### Healing (parser + analyzer + UI)
- [x] `events.HealEvent` with healer, target, amount, spell, modifiers.
      Self-targeted heals get the pronoun ("yourself", "himself", etc.)
      normalized to the healer's name so the (healer, target) matrix
      doesn't fragment.
- [x] `parser.HEAL_RE` matches lines like `Soloson healed Hacral for
      50000 hit points by Word of Restoration.` Optional `(N)` overheal
      parenthetical and `over time` HoT marker are tolerated; spell name
      is optional too.
- [x] `parser.HEAL_PASSIVE_RE` matches the passive form
      `<target> has been healed (over time) by <healer> for N hit points
      by <spell>.` (used for HoT ticks and many proc heals). Must run
      before `HEAL_RE` because the loose `NAME` regex would otherwise
      slurp `<target> has been` as the active healer and mangle the
      parse — that's how "Lunarya has been" originally showed up as a
      healer in the encounter view.
- [x] `analyzer.Heal` and `analyzer.HealerStats` dataclasses for heal
      data.
- [x] `analyzer.detect_combat()` returns `(fights, heals)` from a single
      walk of the log; `detect_fights()` is a thin wrapper that discards
      heals for backward compat.
- [x] `Encounter.heals` carries the heals whose timestamps fell inside
      the encounter's `[start, end]` window. `group_into_encounters` now
      takes an optional `heals` arg.
- [x] Debug view (`#/debug`, accessible via the **Debug** button in the
      header) — walks the log with `analyzer.collect_parser_stats()` and
      shows event counts per type plus unparsed-line shapes (digits
      collapsed to `N`) sorted by frequency. Useful for spotting
      formats that need new regexes (e.g. DoT ticks, lifetap drains —
      neither is currently parsed and both fall through to UnknownEvent).
      Result is cached on `_State.parser_stats` and invalidated on log
      switch / reload.
- [x] "Heals extend fights" checkbox in the params panel (default off).
      When on, `detect_combat` treats each heal event as combat activity:
      runs staleness expiration on all in-progress fights at the heal's
      timestamp, then bumps every surviving fight's `last_ts` forward.
      Heals outside any fight still don't open new ones — the checkbox
      controls extension only, not initiation. Useful for boss fights
      with phase-pause heal-ups that would otherwise cause the encounter
      to split.
- [x] Encounter detail page has Damage / Healing tabs at the top. Healing
      tab mirrors the damage tab: Healers/Enemy-healers split (reuses
      damage-side classification), expandable rows with "Healing dealt
      to" / "Healing taken from" breakdowns, click-pair-row to pop a
      modal HPS chart, stacked-area HPS timeline, biggest-heals list.
      The healing chart is built lazily on first switch so Chart.js
      doesn't size a hidden canvas. Per-pair `hits_detail` is populated
      on healing cells the same way damage cells do it — the pair modal
      rebuilds the chart, source-filter table, and bucket-click list
      entirely from `hits_detail`, so omitting it left the modal empty
      ("0 healing · 0 casts") even when the underlying row had data.

### Reports (report.py)
- [x] `text_dps_report` - per-attacker table + special attack breakdown
- [x] `text_timeline_report` - per-bucket damage table + biggest hits
- [x] `html_timeline_report` - interactive Chart.js stacked-area chart

### CLI / launcher
- [x] Single entry point: `python -m flurry [SUBCOMMAND] [args]`
- [x] Subcommands routed in `flurry/__main__.py`: `ui` (default), `session`,
      `dps`, `timeline`. With no subcommand, launches the UI.
- [x] `flurry.bat` (Windows) and `flurry.sh` (POSIX) at repo root: thin
      wrappers around `python -m flurry "$@"`. Double-click `flurry.bat`
      to open the UI.
- [x] No `[project.scripts]`, no PATH pollution. `pip install -e .`
      remains useful for importing the library from another project
      (e.g. the bot).

### Standalone executable (PyInstaller)
- [x] `build_exe.py` runs PyInstaller to produce a single-file
      `dist/flurry.exe` (~9 MB) that bundles Python and the package.
      End users download the .exe, double-click, browser opens to UI.
- [x] Entry point is `_pyinstaller_entry.py` (a 3-line launcher) instead
      of `flurry/__main__.py` directly — bundling the package's `__main__`
      as a top-level script breaks its relative imports. The launcher
      imports the package properly and delegates to `main()`.
- [x] Build dep declared in `pyproject.toml` under
      `[project.optional-dependencies] build = ["pyinstaller>=6.0"]`.
      Install with `pip install -e ".[build]"` then `python build_exe.py`.

### Web UI (server.py — pass 1: navigation only)
- [x] `flurry/server.py`: stdlib `http.server`-based local web UI
- [x] Routes: `GET /` (index HTML), `GET /api/session`,
      `GET /api/encounter/<id>`, `GET /api/fight/<id>` (still available
      for individual-fight drilldowns), `GET /api/browse?path=DIR`,
      `POST /api/open` (body: `{path}`), `POST /api/reload` (re-parse the
      current log), `POST /api/params` (body: any subset of
      `{gap_seconds, min_damage, encounter_gap_seconds, bucket_seconds}` —
      updates `_State`, invalidates fights+encounters caches, returns the
      new session payload)
- [x] Parameters panel in the session view: inline number inputs for
      Fight gap, Min damage, Min duration, and Encounter gap; Apply
      button POSTs to `/api/params` and re-renders. Bounces back to
      `#/` on apply since encounter ids may shift under the new
      groupings.
- [x] `detect_fights(min_duration_seconds=0)` — additional filter for
      dropping fights shorter than N seconds. Default 0 keeps existing
      behavior; tunable via `--min-duration` (CLI) or the UI panel.
- [x] In-UI file picker: browse subdirs + `eqlog_*.txt` files, navigate
      via clicks or by pasting a path, "Change log" button always
      visible once a log is loaded
- [x] Drag-and-drop: dropping a log file anywhere on the page streams
      the content to `POST /api/upload` (raw body, `X-Filename` header
      carries the original name). The server writes it under
      `tempfile.gettempdir()/flurry-uploads/` and treats that path as
      the active log. Browsers don't expose disk paths for dropped
      files, so upload-and-save is the only path; trade-off is the temp
      copy lives until the OS reclaims the dir.
- [x] Session table view (clickable rows) → encounter detail view with
      per-attacker DPS table, special-attack table, biggest-hits list,
      member-fights breakdown, and a Chart.js stacked-area timeline.
      Hash-routed: `#/`, `#/picker`, `#/encounter/<id>`
- [x] Threaded server (`ThreadingHTTPServer`); fights and encounters
      cached per-load and invalidated on log switch or Refresh button
- [x] Session table has clickable column headers (sort asc/desc, click
      the active column to flip). Default sort is `encounter_id` desc so
      the most recent encounter is at the top of a freshly-loaded log.
      Sort state lives at module scope in the front-end so it persists
      when the user drills into a detail and comes back.

### Encounter auto-grouping
- [x] `analyzer.group_into_encounters(fights, gap_seconds=0)` clusters
      fights whose time windows overlap (or are within `gap_seconds`)
      into `Encounter` rows. Default is **strict overlap** (gap_seconds=0)
      — bumped down from a wider default after testing showed 30s glued
      unrelated kills together. Phase-transition pauses now stay split
      across encounters; bump `--encounter-gap` if you want them merged.
- [x] Encounter display name = the enemy that took the most damage.
      "Enemy" mirrors the per-row classification on the encounter detail
      view: pets and "You" are excluded, and a target qualifies as an
      enemy if it took more damage than it dealt across the encounter.
      Falls back to the highest-damage member when no member qualifies
      (rare — e.g. encounters that are only player environmental deaths).
      This replaced an earlier "most recent killed target → fallback to
      highest damage" rule that could surface a friendly's name when a
      teammate died mid-encounter. Multi-target encounters still get a
      `+N` badge in the session table (counted case-insensitively so "A
      subterranean digger" and "a subterranean digger" don't double-count).
- [x] `analyzer.merge_encounter(encounter)` flattens an encounter into a
      single `FightResult` (concatenated hits, summed per-attacker stats,
      max-of-biggest, merged specials). Plugs straight into the existing
      `bucket_hits` + `_fight_detail` pipeline so the encounter detail
      view reuses every renderer the per-fight view had — only addition
      is a "Member fights" panel listing constituent slices.
- [x] `flurry-ui --encounter-gap N` to tune the grouping window from the
      CLI. Default 10s — small enough to keep distinct kills separated,
      large enough that brief lulls (target swaps, mid-fight resurrects)
      don't split a single engagement into two encounters. The library
      default for `group_into_encounters()` itself is still 0 (strict
      overlap); only the UI/CLI surfaces opt into the wider window.
- [x] Manual encounter override (pass 2) is wired through:
      `analyzer.group_into_encounters(..., manual_groups=[{fight_keys, name}])`
      pulls the listed fights into their own encounter ahead of auto-
      grouping. The session table renders a checkbox column + an action
      bar with Merge / Split / Clear; selections persist across sort
      re-renders but reset on log/param changes (encounter ids shift).
      Manually-pinned encounters get a "★ pinned" badge. Singleton or
      stale-keyed manual groups are silently ignored (the auto-grouper
      handles them) so a sidecar referencing fights that disappeared
      under new params doesn't break.
- [x] Encounter detail splits the per-attacker table into "Friendlies"
      and "Enemies" sections. Classification compares per-name dealt vs
      received damage in the encounter: pets (backtick suffix) are always
      friendly; otherwise `received > dealt` → enemy, else friendly.
      This correctly handles friendlies who got hit hard enough to spawn
      their own player-target fight (still classified friendly because
      they dealt more than they took). Edge case — a mob that attacks
      players but is never attacked back would receive=0 and get
      misclassified as friendly; rare in practice, accept for now.
- [x] Each row in the Friendlies / Enemies tables is expandable: click
      the row to reveal a "Damage dealt to" / "Damage taken from"
      breakdown sourced from a per-(attacker,target) hit matrix the
      server builds in `_encounter_payload`. Encounter JSON now carries
      `dealt_to` and `taken_from` arrays on each attacker, each with a
      bucketed `series` aligned to the encounter timeline. Clicking a
      target/source name in a breakdown pops a modal Chart.js graph of
      "damage from X to Y over time" using that series.
- [x] Synthesized **All** row at the top of every breakdown table
      (damage dealt-to / taken-from, healing dealt-to / taken-from).
      Client-side aggregation in `breakdownTable`: sums `damage` and
      `hits`, element-wise sums `series`, concatenates `hits_detail`.
      Registers as a normal pair via `registerPair` so clicking it
      opens the same modal — chart shows the attacker's whole
      dealt-to-everyone (or taken-from-everyone) line, and the modal's
      by-source grouping naturally mixes hits from all targets. Styled
      with a subtle blue stripe (`tr.pair-row-all`) so it reads as a
      rollup, not just another row.

### Time-window slicing + parse progress
- [x] `tail.read_last_timestamp(path, max_tail_bytes=4MB)` — reads only
      the tail of a file to find the latest `[Day Mon DD HH:MM:SS YYYY]`
      timestamp. Used to anchor `since_hours` to log-end (NOT wall
      clock), so picking "last 4h" works on a log that ended yesterday.
- [x] `tail.find_offset_for_timestamp(path, since)` — backwards scan in
      64KB chunks (with 64-byte overlap to handle timestamps that
      straddle chunk boundaries) returning the byte offset of the
      first line at-or-after `since`. Returns 0 if `since` predates the
      log, file size if it postdates the log. Line-aligned by
      construction so `tail_file(start_offset=...)` doesn't need to
      discard a fragment.
- [x] `tail.tail_file(start_offset=, progress_cb=, progress_interval_bytes=)` —
      added byte-offset start and a progress callback invoked every
      ~256KB of reads with the absolute byte position. Callback errors
      are swallowed so a flaky observer can't kill the parse.
- [x] `analyzer.detect_combat(since=, progress_cb=)` plumbs both
      through: `since` is resolved to a starting byte offset (with the
      inline `ev.timestamp < since` filter as backstop), and the
      progress callback receives `(bytes_read, slice_size)` where both
      values are RELATIVE to the slice (so the UI bar fills 0→100%
      across the work actually done, not jumping to 67% at start
      because the offset was two-thirds into the file).
- [x] Server: `_State.since_hours` param (default 0 = whole log).
      `_ensure_combat_cached` writes to `_State.parse_progress` dict
      with state ∈ {idle, parsing, done, error} as it walks. Reads are
      lock-free (single-attribute access is GIL-safe), so a concurrent
      `GET /api/parse-status` request can serve progress while another
      request thread holds the parse. Cache invalidation on any change
      that affects the parse (params, reload, log switch) resets
      progress to 'idle' so the next parse starts the bar from 0.
- [x] UI: "Last N hours" input in the params panel; live progress bar
      during upload (post-bytes phase) and during initial render of
      session / encounter / debug views. Polling stops as soon as the
      relevant request resolves — we don't try to detect 'done' from
      the status alone since the cache could be filled by a still-in-
      flight response. `withParseProgress(promise, app, headline)` is
      the helper; `parseProgressHTML(s, headline)` renders the bar.
- [x] CLI: `flurry-ui --since-hours N` matches the UI knob.

### Multi-fight session summary
- [x] `_session_summary_payload(encounters, killed_only=)` aggregates
      per-attacker stats across the session (total, avg/median/p95/best
      DPS, biggest hit, encounters present) plus a per-(attacker,
      encounter) DPS array for the heatmap and trend chart. Side
      classification is computed at the session level using the same
      `received > dealt + healed` rule the encounter detail uses, so a
      pure healer with stray AoE damage still rolls up friendly.
- [x] Endpoint: `GET /api/session-summary?killed_only=1` (default 0).
      `killed_only=1` strips wipes / aborted pulls so they don't drag
      down averages — useful default for raid-night stats; the UI
      defaults this on.
- [x] Route `#/session-summary`, opened from a "Session summary"
      button in the session-view header. Layout: charts stacked on the
      left (top: Chart.js DPS-by-encounter line chart with top-N + an
      "Other" rollup; bottom: HTML-table heatmap with sticky row/col
      headers, attacker × encounter, cell color intensity scaled to
      max DPS observed in the matrix). Right column: per-attacker
      rollup table. Stacks vertically below 1100px.
- [x] Heatmap empty cells (player absent from an encounter) render as
      a faint `·` placeholder with no fill, distinct from "showed up
      but did low damage." Click any cell → encounter detail.
- [x] Filter bar: "Killed encounters only" toggle (server-side via
      query param) and "Min avg DPS" (client-side row filter on the
      table; chart and heatmap show all friendlies regardless).
      Healers without damage drop out of the rollup naturally — the
      view is per-attacker by definition.
- [x] Selection-scoped summary: tick rows on the session table, then
      the **Session summary** button (label flips to "Session summary
      (N selected)") navigates to `#/session-summary?ids=...` which
      hits `/api/session-summary?encounter_ids=72,71,...`. When scoped,
      `killed_only` is forced off and disabled in the UI — explicit
      selection wins over the default "skip wipes" filter. A "Show
      whole log" link appears in the scoped view to drop the scope.
      Stale ids (param changes shifted the encounter ids) → empty-state
      message points the user back to the whole-log view rather than
      faking partial results.
- [x] Per-attacker rollup table: sticky **Attacker** column when the
      table scrolls horizontally (`position: sticky; left: 0` on the
      first cell, requires `border-collapse: separate` so borders move
      with the cell instead of ghosting). Wrapped in `.ss-table-wrap`
      with `overflow-x: auto` so the 8-column min-content width can
      exceed the right grid track without bleeding past the panel BG.
      Right grid track widened from `1fr` to `1.2fr` (vs charts'
      `1.6fr → 1.2fr`) so the rollup gets ~45% width by default.
- [x] `.ss-grid` uses `align-items: start` so each column sizes to its
      own content. Without it, grid stretch was inflating the right
      column to match the (taller) charts column, leaving the table's
      panel BG ending mid-content.

### Tanking views (analyzer + server + UI)
- [x] Defender-perspective accounting: `DefenseStats` per `(attacker,
      defender)` pair tracked in `FightResult.defends_by_pair`.
      Populated in lockstep with `AttackerStats` inside `_FightBuilder`
      (no second event walk needed). `merge_encounter` sums across
      member fights; `apply_pet_owners` remaps the attacker side keys.
- [x] Encounter detail Tanking tab — third tab next to Damage / Healing.
      Friendlies sorted by damage taken with parry / block / dodge /
      rune / invuln / miss / riposte counts and avoid %. Each row
      expands into a per-attacker breakdown with the same columns and a
      synthesized "All" row at the top. Click any row to pop a DTPS-
      over-time modal — same windowing/source-filter machinery as the
      Damage tab, just keyed by `(attacker, defender)`.
- [x] Session-summary Damage / Healing / Tanking tabs. The session
      payload returns three parallel rollups (`damage_actors`,
      `healing_actors`, `tanking_actors`) sharing a common shape; a
      generic `_build_session_actor_rollup` helper plugs different
      per-encounter value-getters into the same averaging/p95/sort
      logic. Side classification computed once at the session level
      so an actor stays on the same side across tabs.
- [x] Damage / Healing / Δ Life metric toggle on tanking surfaces.
      Switches the session-summary chart + heatmap and the per-defender
      All-row pair modal between damage taken, healing received, and
      life delta (heals − damage per bucket). Delta renders as a filled
      area dipping below zero on charts; the heatmap colors positive
      blue and negative red. The toggle only appears on the modal's
      All row — heals aren't keyed by attacker, so per-attacker rows
      stay damage-only. Delta mode in the modal hides the by-source
      breakdown and skips the click-to-window UX (no individual events).

### Sidecar / user overrides (`flurry/sidecar.py`)
- [x] `<logfile>.flurry.json` next to each log holds two kinds of edits:
      pet-owner assignments (`{actor: owner}`) and manual encounter
      groupings (`[{fight_keys: [...], name: <opt>}]`). Schema is
      versioned (`SIDECAR_VERSION = 1`); missing/corrupt files load as
      empty so the UI never blocks on a bad sidecar.
- [x] Stable identifiers: pet owners are keyed by attacker name (case-
      insensitive lookup); fights are keyed by `target.lower()|start.isoformat()`
      (`flurry.sidecar.fight_key()` is the canonical builder, mirrored
      in `analyzer._fight_key`). `fight_id` and `encounter_id` are
      deliberately NOT used in the on-disk format because both shift
      when detection params change.
- [x] Atomic writes via tmp + `os.replace`; an interrupted save can't
      leave a corrupted sidecar.
- [x] `Sidecar.set_pet_owner`, `merge_encounter`, and `remove_keys_from_manual`
      are the mutation entry points; merge_encounter dedupes (a fight
      can only live in one manual group at a time) and prunes groups
      below 2 keys back to auto-grouping.
- [x] `analyzer.apply_pet_owners(fights, heals, pet_owners)` rewrites
      attacker/healer names to `<owner>\`s pet`, re-aggregating per-
      attacker stats so two raw actors collapsing to the same owner sum
      cleanly. Applied at encounter-build time (NOT at the per-fight
      cache layer) so the raw attacker names stay visible to the pet-
      owner edit modal — the trade-off is one rewrite pass per sidecar
      edit, vs. having to walk and revert the cached fights.
- [x] Server endpoints: `POST /api/pet-owners` (`{actor, owner}`,
      owner=null clears) and `POST /api/encounters` (`{action: merge|split,
      encounter_ids: [...], name?}`). Encounter ids are resolved to
      stable fight keys server-side under the lock, so id instability
      across param changes isn't a wire-format concern.
- [x] Pet-owner edit modal on the encounter detail page: opened from a
      "Pet owners" header button, lists this encounter's RAW attackers
      (`raw_attackers` is exposed on `/api/encounter/<id>` for exactly
      this purpose), each with their current owner mapping plus an
      input + Save/Clear. Owners assigned to actors NOT in the current
      encounter are listed below as "Other assignments" so the user can
      still see and clear them — otherwise an assignment made on one
      encounter would be invisible from any other.

### Packaging
- [x] `pyproject.toml`, installs cleanly with `pip install -e .`
- [x] Zero runtime dependencies (pure stdlib)
- [x] CLIs registered as console_scripts

### Tests
- [x] 29 parser tests (regex correctness against real log lines)
- [x] 10 analyzer tests (using real Hacral log as fixture, with skip-if-missing)
- [x] 11 detect_fights tests (synthetic log fixtures via tmpfile — no
      external dependency, run anywhere)
- [x] 12 override tests for `apply_pet_owners` + manual_groups (no log
      fixture needed — synthesized FightResults)
- [x] 17 sidecar tests for the persistence layer (round-trip through
      JSON, mutation helpers, atomic writes, corrupt-file handling)
- [x] 13 tail-window tests for `read_last_timestamp`, byte-offset
      slicing, progress callbacks, and `since=` filtering

---

## Fight detection — design decisions

`detect_fights()` is the auto-detector. The design is deliberately small
and a few non-obvious choices are baked in:

- **One fight per target.** A boss + 3 adds in the same combat window
  produces 4 fights. We do *not* try to merge them at the data layer —
  the user groups them into encounters in the UI. This keeps per-target
  stats clean and means the detector has no judgement to make about
  "which mobs belong together."

- **15-second gap.** Default `gap_seconds=15`. Aggressively splits
  distinct engagements, even ones that *might* be the same boss across
  a phase-transition pause. We chose short over long deliberately: the
  UI is where fights get recombined into encounters, and finer-grained
  atoms give the user more flexibility. A long emote pause splitting a
  boss kill into "phase 1 fight" + "phase 2 fight" is fine — the UI
  groups them.

- **Filter rules are minimal.** We drop `target == 'You'` and
  `target.endswith('\`s pet')` because those are obviously not encounters.
  Other player deaths (target = some proper name) can still slip through
  as "fights" — the raid attacking the boss while a teammate dies will
  produce a separate fight with the teammate as target. Acceptable for
  now; the UI is the right place to suppress these once it has a player
  roster. Min-damage default of 10,000 catches most trivia.

- **Misses extend the fight window.** A miss is combat activity, so a
  long unlucky streak doesn't expire the fight before the next hit lands.

- **`fight_id` is 1-indexed by start time.** Stable across runs of the
  same log (assuming the log doesn't change). The UI uses these IDs to
  define encounters.

- **`_FightBuilder` is shared with `analyze_fight`.** Both entry points
  produce the same `FightResult` shape via the same accumulator, so
  attribution logic (crits, specials, pet handling) only lives once.

- **Same-name simultaneous mobs produce skewed slices.** EQ logs don't
  disambiguate instances of the same name, so three `a goblin`s engaged
  at once all accumulate under one `"a goblin"` key in the in-progress
  map. We slice on each death event: fight 1 closes on the first death
  with all three goblins' damage in it, fight 2 opens on the next hit
  and runs through the second death, etc. Result: N fights for N kills,
  but the per-fight damage is fat-then-thin (fight 1 is large, the
  trailing slices shrink). Per-attacker totals across all slices are
  still correct — every hit lands somewhere. We accept this rather than
  trying to disambiguate, since the user can recognize the situation
  and merge the slices into one encounter in the UI. Note that
  `min_damage` may filter the smallest slices entirely.

## Web UI — design decisions

`flurry/server.py` is the Pass-1 navigation-only UI. The choices behind
it shape what Pass 2 (encounter grouping) and Pass 3 (pet ownership)
should look like:

- **Local HTTP server, not a static Pyodide page.** We considered shipping
  a static `flurry-ui.html` driven by a JSON dump from a CLI flag. The
  server path won because (a) zero parser duplication — the in-process
  Python analyzer is reused as-is, (b) one-command UX, (c) it scales
  toward the live-tail roadmap item, and (d) writing the encounter +
  pet sidecar JSON on save is server-trivial vs awkward in a static
  page. Stdlib only (`ThreadingHTTPServer`, no Flask/FastAPI dep).

- **In-UI file picker.** `flurry-ui` accepts an optional logfile arg
  but the same UI lets you pick / switch logs via `/api/browse` and
  `POST /api/open`. The picker filters listings to subdirs + `eqlog_*.txt`
  files (case-insensitive); a manual path-input handles "I know exactly
  where my log is" and unusual filenames. Hidden directories (`.git`,
  `.venv`, etc.) are filtered out to keep listings calm. Filesystem
  root is detected by `os.path.dirname(p) == p` and renders without
  a parent link.

- **Hash-routed SPA, plain vanilla JS.** No framework. Three views
  switched by `location.hash`: `#/` (session list), `#/picker` (file
  picker), `#/fight/<id>` (fight detail). The whole front-end is one
  HTML constant in `server.py` to keep packaging trivial. We can
  extract to `flurry/static/` if the file gets unwieldy.

- **JSON shapes mirror the existing text/HTML reports.** `_fight_summary`
  and `_fight_detail` produce the same numbers as `text_dps_report` and
  `html_timeline_report` would, just structured for the front-end to
  render. This keeps the data model honest — no UI-only state shoved
  into the API — and means a JSON export CLI (roadmap item 6) is a
  near-freebie.

- **Cache fights per-process.** `_get_fights()` caches `detect_fights`
  results behind a lock so repeat requests don't re-parse. Switching
  logs (or restarting the server) invalidates. Live-tail mode will
  need finer-grained invalidation (mtime check or push-based).

- **Chart.js from CDN.** Same choice as the existing `html_timeline_report`.
  Vendoring is a future improvement (we'd want to do it for the static
  HTML report at the same time).

## Standalone packaging — design decisions

Flurry ships as a **standalone app**, not as a CLI installer. The shape:

- **No `[project.scripts]`.** `pip install` doesn't put any binaries on
  the user's PATH. Power users who want pip-install can still get the
  library import (for the bot), but the day-to-day flow is double-click,
  not `flurry-ui`.
- **`python -m flurry` is the canonical entry point.** All run paths
  (the .bat, the .sh, the .exe via `_pyinstaller_entry.py`) eventually
  invoke `flurry/__main__.py`. Subcommand routing lives there. Default
  with no args = launch UI.
- **`flurry.bat` / `flurry.sh`** are for the source-tree case (cloned
  the repo, has Python). They're one-liners.
- **`flurry.exe` (PyInstaller, single-file)** is for the no-Python case
  (downloaded a release, no install). The entry point can't be
  `flurry/__main__.py` directly because PyInstaller bundles it as a
  top-level script and that breaks the relative imports — hence
  `_pyinstaller_entry.py`, a 3-line launcher.
- **Console mode, not windowed.** When the user double-clicks the .exe
  a console window opens with `Flurry UI: http://localhost:8765/` and
  the browser auto-opens. They close the console to stop. A future
  pass could replace this with a tray icon (windowed mode + a small
  GUI for the URL + quit), but that's polish, not blocking.
- **Build is reproducible-ish via `build_exe.py`.** A small Python
  script invokes PyInstaller with the canonical flags. Storing the
  recipe in source means future rebuilds match.

## Known issues / debt

- **Test fixture path is hardcoded** to `/mnt/user-data/uploads/...` in
  `tests/test_analyzer.py`. That's the path on the container Flurry was
  developed in. On a real machine, the analyzer tests will all skip.
  Fix: parameterize via env var (`FLURRY_TEST_LOG`) or move a sample
  log into `tests/fixtures/` and update `SAMPLE_LOG`.

- **`analyze_fight` reads the whole log** even if the fight is at the
  start. Fine for ~30-min sessions; will be slow on multi-day logs.
  Note that `detect_combat` accepts `since=` for byte-offset slicing
  (used by `flurry-ui --since-hours`); `analyze_fight` does not yet —
  it would be a small change to plumb it through, similar to
  detect_combat.

- **No tests for `report.py`** — the rendering layer is untested.
  Mostly cosmetic, hard to test text formatting without making tests
  brittle, but should at least have smoke tests that the functions
  don't crash.

- **HTML report uses a CDN** for Chart.js. Won't work offline unless
  you've cached the CDN. Acceptable for now; vendoring Chart.js is the
  fix when it matters.

---

## Roadmap (in rough order of usefulness)

These are documented in README.md too; this is the working list.

1. **Healing and tanking views** — same per-attacker model but for
   HPS (heals received per target) and damage mitigated/taken. Touches
   parser (new event types), analyzer (new accumulators), and reports.

2. **Log diffing** — compare same-boss fights before and after a gear
   change. "What did this new weapon actually do?"

3. **JSON export** — `flurry-dps --json`, `flurry-timeline --json`, and
   `flurry-session --json` for piping to other tools. The UI has its
   own JSON via `/api/*` already; the CLI flags would just shell out.

4. **Live tail mode** — `tail.py` already supports follow-mode; the
   analyzer and server don't. Would let you watch DPS in real time
   during a fight, and push UI updates via SSE or polling.

   - **Player overlay (sub-feature of live tail).** A compact,
     always-on-top window for the active character with four live
     counters: damage out, damage in, healing out, healing in. The
     idea is to glance at your own performance mid-fight without
     alt-tabbing to the full UI. Open questions: which char is "you"
     (read from the log filename `eqlog_<char>_<server>.txt`, or let
     the user pick from a roster?), how to render an always-on-top
     window stdlib-only (probably can't — likely needs a small Tk or
     wx layer, or a borderless browser pop-out from the existing
     server), and whether the counters reset per-fight or per-encounter
     or run as rolling N-second windows. Worth deciding all three
     before building.

   - **HP delta indicator (sub-feature of live tail).** A live readout
     of net HP change over the last second — green when net positive
     (heal > damage taken), red when net negative — so you can spot
     trouble before the health bar gets to it. Derived from log events
     (damage-taken + heals-received summed over a rolling window),
     since EQ doesn't continuously emit absolute HP. Open questions:
     rolling-1s vs fixed-bucket aggregation, a "no change" threshold so
     small ticks don't flicker the color, and whether this lives inside
     the player overlay or as a separate always-on-top widget. Probably
     same window as the overlay above, but cleanly factor the delta
     calculation so a future "raid HP deltas for the whole group"
     feature can reuse it.

---

## Conventions

- **Formatting**: just be readable. No black/ruff config yet; we can
  add one if formatting drift becomes an issue.

- **Comments**: comment the *why*, not the *what*. The parser regex
  file has the most useful comments — it documents EQ log quirks that
  are not derivable from looking at the code.

- **Tests**: when you fix a bug or add a pattern, add a test for it
  using a real log line as the fixture string. The parser tests are
  the model: paste in an actual line from the wild, assert on the
  parsed event.

- **Don't break the public API** without bumping the version. The
  things in `flurry/__init__.py`'s `__all__` are the public contract;
  rename or remove with care.

---

## EQ-side context (for AI assistants who might not know)

- EverQuest writes a text log per character to `Logs/eqlog_<char>_<server>.txt`
  when you type `/log on` in-game.
- Default install location on Windows: somewhere under
  `C:\Users\Public\Daybreak Game Company\Installed Games\EverQuest\Logs\`
- The log captures combat, chat (tells, group, raid, say), spell casts,
  zone changes, death messages, and a lot of flavor text.
- Line format is always `[Day Mon DD HH:MM:SS YYYY] <body>` with `\r\n`
  endings (Windows-style).
- "Damage shields" are passive procs: when you melee a mob, the mob takes
  damage from your DS effect. Shows in the log as `X is <effect> by Y's <source>`.
- "Pets" are NPC servants belonging to a player (necro pets, mage pets, etc).
  In the log they're named `Owner\`s pet` with that backtick.
- Special attacks like Headshot (ranger), Assassinate (rogue),
  Slay Undead (paladin), Decapitate (berserker) are class AAs that
  proc one-shot huge damage with their name as a modifier.
- Boss fights typically have everyone burning cooldowns in the first 15-30s
  ("the burn"), then sustained DPS, then a cleanup phase.

---

## Working with this project across sessions

If you're an AI assistant in a new chat: read this file, then ask which
roadmap item or issue the user wants to tackle. Don't re-explain the
architecture — it's documented. Don't re-derive the parser quirks — they're
documented above. Spend the session on the actual work.

If you're a human collaborator: same advice. The README is the public
docs; this file is the operational ones.
