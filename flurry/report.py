"""
report.py - render FightResult and Timeline objects into human-readable
output (text or HTML).

This module is the only place that knows about formatting. Keeping it
separate from the analyzer means the same fight data can power text
reports, charts, dashboards, JSON exports, and downstream tools without
duplicating analysis logic.
"""

import json
from typing import List, Optional

from .analyzer import (
    FightResult, Timeline, AttackerStats, DEFAULT_SPECIAL_MODS,
)


def _fmt_duration(seconds: float) -> str:
    """Compact mm:ss for fights longer than a minute, otherwise '<n>s'."""
    if seconds < 60:
        return f'{int(round(seconds))}s'
    m, s = divmod(int(round(seconds)), 60)
    return f'{m}m{s:02d}s'


# ----- Helpers -----

def _short_damage(n: int) -> str:
    """Compact representation of large damage numbers."""
    if n == 0:
        return '   . '
    if n >= 1_000_000:
        return f'{n/1_000_000:>4.1f}M'
    if n >= 1_000:
        return f'{n/1_000:>4.0f}k'
    return f'{n:>5}'


# ----- Text reports -----

def text_dps_report(result: FightResult) -> str:
    """Render a FightResult as a multi-line text report."""
    lines = []
    if result.start is None:
        return f'No damage events found targeting "{result.target}".'

    if not result.fight_complete:
        lines.append(f'WARNING: no death event found for "{result.target}" - '
                     f'fight may not have ended.')

    lines.append('')
    lines.append(f'=== Fight: {result.target} ===')
    lines.append(f'  Start:    {result.start}')
    lines.append(f'  End:      {result.end}')
    lines.append(f'  Duration: {result.duration_seconds:.1f}s')
    lines.append('')
    lines.append(f'  Total damage dealt: {result.total_damage:,}')
    lines.append(f'  Raid DPS:           {result.raid_dps:,.0f}')
    lines.append('')

    # Main per-attacker table
    lines.append(f'  {"Attacker":<20} {"Damage":>14} {"DPS":>10} '
                 f'{"Hits":>6} {"Miss":>5} {"Crit":>5} {"Biggest":>11} {"%":>5}')
    lines.append(f'  {"-"*20} {"-"*14} {"-"*10} {"-"*6} {"-"*5} {"-"*5} '
                 f'{"-"*11} {"-"*5}')

    duration = result.duration_seconds or 1.0
    total = result.total_damage or 1
    for s in result.attackers_by_damage():
        dps = s.damage / duration
        pct = s.damage / total * 100
        lines.append(
            f'  {s.attacker:<20} {s.damage:>14,} {dps:>10,.0f} '
            f'{s.hits:>6} {s.misses:>5} {s.crits:>5} '
            f'{s.biggest:>11,} {pct:>4.1f}%'
        )

    # Special-attack breakdown
    has_specials = any(s.special_hits for s in result.stats_by_attacker.values())
    if has_specials:
        lines.append('')
        lines.append('  Special attacks:')
        lines.append(f'    {"Attacker":<20} {"Type":<18} {"Hits":>5} '
                     f'{"Damage":>14} {"% of their dmg":>14}')
        lines.append(f'    {"-"*20} {"-"*18} {"-"*5} {"-"*14} {"-"*14}')
        for s in result.attackers_by_damage():
            if not s.special_hits:
                continue
            for special in DEFAULT_SPECIAL_MODS:
                if special in s.special_hits:
                    hits = s.special_hits[special]
                    dmg = s.special_damage[special]
                    pct = (dmg / s.damage * 100) if s.damage else 0
                    lines.append(
                        f'    {s.attacker:<20} {special:<18} {hits:>5} '
                        f'{dmg:>14,} {pct:>13.1f}%'
                    )

    return '\n'.join(lines)


def text_timeline_report(result: FightResult,
                         timeline: Timeline,
                         min_dps_to_show: int = 100,
                         top_n_hits: int = 10) -> str:
    """Render a per-bucket timeline view + biggest-hits list.

    Args:
      min_dps_to_show: hide rows whose total avg DPS is below this threshold
                       (declutters the table by dropping pets/trivial contributors).
      top_n_hits: how many biggest hits to list.
    """
    if result.start is None:
        return f'No damage events found targeting "{result.target}".'

    duration = result.duration_seconds
    lines = []
    lines.append('')
    lines.append(f'=== Fight timeline ({duration:.0f}s, '
                 f'{timeline.bucket_seconds}s buckets) ===')
    lines.append('')

    # Header row: bucket time offsets
    n_buckets = timeline.n_buckets
    header = f'  {"":<22}'
    for bs in timeline.bucket_starts:
        offset = (bs - result.start).total_seconds()
        header += f' {int(offset):>4}s'
    header += '   TOTAL'
    lines.append(header)
    lines.append('  ' + '-' * 22 + '-' * (6 * n_buckets) + '----------')

    # Sort by total damage descending
    totals = {a: sum(series) for a, series in timeline.per_attacker.items()}
    sorted_attackers = sorted(timeline.per_attacker.keys(),
                              key=lambda a: totals[a], reverse=True)

    # Per-attacker rows
    threshold = duration * min_dps_to_show
    for attacker in sorted_attackers:
        if totals[attacker] < threshold:
            continue
        row = timeline.per_attacker[attacker]
        line = f'  {attacker[:22]:<22}'
        for v in row:
            line += f' {_short_damage(v)}'
        line += f'   {totals[attacker]:>9,}'
        lines.append(line)

    # Raid total row + DPS row
    raid_per_bucket = timeline.raid_total_per_bucket()
    raid_total = sum(raid_per_bucket)

    lines.append('  ' + '-' * 22 + '-' * (6 * n_buckets) + '----------')
    raid_line = f'  {"RAID":<22}'
    for v in raid_per_bucket:
        raid_line += f' {_short_damage(v)}'
    raid_line += f'   {raid_total:>9,}'
    lines.append(raid_line)

    dps_line = f'  {"DPS":<22}'
    for v in raid_per_bucket:
        dps_line += f' {_short_damage(int(v / timeline.bucket_seconds))}'
    lines.append(dps_line)

    # Biggest hits
    lines.append('')
    lines.append('  Biggest single hits:')
    biggest = sorted(result.hits, key=lambda h: h.damage, reverse=True)[:top_n_hits]
    for h in biggest:
        offset = (h.timestamp - result.start).total_seconds()
        spec = (' ' + ', '.join(h.specials)) if h.specials else ''
        lines.append(f'    +{int(offset):>3}s  {h.attacker:<22} '
                     f'{h.damage:>11,}{spec}')

    return '\n'.join(lines)


# ----- Session report -----

def text_session_report(fights: List[FightResult],
                        logfile: Optional[str] = None) -> str:
    """Render a list of detected fights as a session-overview table.

    Args:
      fights: result of `detect_fights(...)`. Already sorted, fight_id set.
      logfile: optional path string included in the header for context.
    """
    lines = []
    if not fights:
        return 'No fights detected.'

    lines.append('')
    if logfile:
        lines.append(f'=== Session: {logfile} ===')
    else:
        lines.append('=== Session ===')
    lines.append('')

    # Width chosen so a typical mob name like "Shei Vinitras" fits without
    # truncation. Long names get clipped.
    target_w = 28

    lines.append(f'  {"ID":>3}  {"Start":<19}  {"Dur":>6}  '
                 f'{"Target":<{target_w}}  {"Damage":>14}  {"DPS":>10}  Status')
    lines.append(f'  {"-"*3}  {"-"*19}  {"-"*6}  {"-"*target_w}  '
                 f'{"-"*14}  {"-"*10}  {"-"*10}')

    for f in fights:
        status = 'Killed' if f.fight_complete else 'Incomplete'
        start_str = f.start.strftime('%Y-%m-%d %H:%M:%S') if f.start else '?'
        dur_str = _fmt_duration(f.duration_seconds)
        target = f.target if len(f.target) <= target_w else f.target[:target_w-1] + '…'
        lines.append(
            f'  {f.fight_id:>3}  {start_str:<19}  {dur_str:>6}  '
            f'{target:<{target_w}}  {f.total_damage:>14,}  '
            f'{f.raid_dps:>10,.0f}  {status}'
        )

    lines.append('')
    n_killed = sum(1 for f in fights if f.fight_complete)
    total_dmg = sum(f.total_damage for f in fights)
    lines.append(f'  {len(fights)} fights detected ({n_killed} killed). '
                 f'Total damage: {total_dmg:,}')

    return '\n'.join(lines)


# ----- HTML report -----

def html_timeline_report(result: FightResult,
                         timeline: Timeline,
                         top_n_attackers: int = 8,
                         top_n_hits: int = 5) -> str:
    """Render an interactive Chart.js stacked-area timeline as a full HTML page.

    Args:
      top_n_attackers: how many top contributors get individual lines on the
                       chart; the rest are lumped into 'Other'.
      top_n_hits: how many biggest hits to include in the side table.
    """
    if result.start is None or timeline.n_buckets == 0:
        return f'<html><body>No data for "{result.target}".</body></html>'

    duration = result.duration_seconds
    totals = {a: sum(s) for a, s in timeline.per_attacker.items()}
    sorted_attackers = sorted(timeline.per_attacker.keys(),
                              key=lambda a: totals[a], reverse=True)

    top = sorted_attackers[:top_n_attackers]
    rest = sorted_attackers[top_n_attackers:]

    n = timeline.n_buckets
    labels = [f'+{int((bs - result.start).total_seconds())}s'
              for bs in timeline.bucket_starts]

    # Convert to per-second DPS so the y-axis is meaningful regardless of bucket size.
    datasets = []
    for attacker in top:
        dps_series = [timeline.per_attacker[attacker][i] / timeline.bucket_seconds
                      for i in range(n)]
        datasets.append({'label': attacker, 'data': dps_series})

    if rest:
        other = [
            sum(timeline.per_attacker[a][i] for a in rest) / timeline.bucket_seconds
            for i in range(n)
        ]
        datasets.append({'label': 'Other', 'data': other})

    biggest = sorted(result.hits, key=lambda h: h.damage, reverse=True)[:top_n_hits]
    markers = [
        {
            'offset_s': int((h.timestamp - result.start).total_seconds()),
            'attacker': h.attacker,
            'damage': h.damage,
            'specials': h.specials,
        }
        for h in biggest
    ]

    summary = [
        f'Fight: {result.target}',
        f'Duration: {duration:.1f}s ({n} x {timeline.bucket_seconds}s buckets)',
        f'Total damage: {result.total_damage:,}',
        f'Raid DPS: {result.raid_dps:,.0f}',
    ]

    payload = {
        'labels': labels,
        'datasets': datasets,
        'markers': markers,
        'summary': summary,
        'target': result.target,
    }

    return _HTML_TEMPLATE.replace('__TITLE__', result.target).replace(
        '__PAYLOAD__', json.dumps(payload)
    )


# Inline HTML template. The styling aims for a calm dark theme that
# reads well on a second monitor while you're playing.
_HTML_TEMPLATE = '''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Flurry: __TITLE__</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #0f1419; color: #e5e7eb; margin: 0; padding: 24px;
    max-width: 1100px; margin: 0 auto;
  }
  h1 { font-size: 1.5rem; margin: 0 0 4px; color: #f8fafc; }
  h1 .brand { color: #60a5fa; font-weight: 700; }
  .summary { color: #94a3b8; font-size: 0.95rem; margin-bottom: 24px; }
  .summary div { margin: 2px 0; }
  .chart-wrap {
    background: #1a2030; border-radius: 8px; padding: 20px; margin-bottom: 24px;
  }
  table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
  th, td { text-align: left; padding: 6px 12px; border-bottom: 1px solid #2a3142; }
  th { color: #94a3b8; font-weight: 600; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .specials { color: #fbbf24; font-size: 0.85rem; }
  h2 { font-size: 1.1rem; color: #f8fafc; }
</style>
</head>
<body>
<h1><span class="brand">Flurry</span> &middot; __TITLE__</h1>
<div class="summary" id="summary"></div>
<div class="chart-wrap"><canvas id="chart" height="120"></canvas></div>
<h2>Biggest hits</h2>
<table id="biggest"></table>

<script>
const PAYLOAD = __PAYLOAD__;

const summaryEl = document.getElementById('summary');
PAYLOAD.summary.forEach(line => {
  const div = document.createElement('div');
  div.textContent = line;
  summaryEl.appendChild(div);
});

const COLORS = [
  '#60a5fa', '#34d399', '#fbbf24', '#f87171',
  '#a78bfa', '#22d3ee', '#fb923c', '#facc15', '#94a3b8'
];

const datasets = PAYLOAD.datasets.map((d, i) => ({
  label: d.label,
  data: d.data,
  backgroundColor: COLORS[i % COLORS.length] + 'cc',
  borderColor: COLORS[i % COLORS.length],
  borderWidth: 1,
  fill: true,
  pointRadius: 0,
  tension: 0.3,
}));

new Chart(document.getElementById('chart'), {
  type: 'line',
  data: { labels: PAYLOAD.labels, datasets },
  options: {
    responsive: true,
    interaction: { mode: 'index', intersect: false },
    scales: {
      x: { stacked: true, ticks: { color: '#94a3b8' }, grid: { color: '#2a3142' } },
      y: {
        stacked: true,
        ticks: {
          color: '#94a3b8',
          callback: v => (v >= 1_000_000 ? (v/1_000_000).toFixed(1) + 'M' :
                          v >= 1_000     ? (v/1_000).toFixed(0)     + 'k' : v) + ' DPS'
        },
        grid: { color: '#2a3142' },
      },
    },
    plugins: {
      legend: { labels: { color: '#e5e7eb' } },
      tooltip: {
        callbacks: {
          label: ctx => {
            const v = ctx.parsed.y;
            const formatted = v >= 1_000_000 ? (v/1_000_000).toFixed(2) + 'M' :
                              v >= 1_000     ? (v/1_000).toFixed(1)    + 'k' : v;
            return ctx.dataset.label + ': ' + formatted + ' DPS';
          }
        }
      }
    }
  }
});

const t = document.getElementById('biggest');
t.innerHTML = '<tr><th>Time</th><th>Attacker</th><th class="num">Damage</th><th>Special</th></tr>';
PAYLOAD.markers.forEach(m => {
  const row = t.insertRow();
  row.insertCell().textContent = '+' + m.offset_s + 's';
  row.insertCell().textContent = m.attacker;
  const dmg = row.insertCell();
  dmg.className = 'num';
  dmg.textContent = m.damage.toLocaleString();
  const spec = row.insertCell();
  spec.className = 'specials';
  spec.textContent = m.specials.join(', ') || '\u2014';
});
</script>
</body>
</html>
'''
