"""
Flurry - EverQuest combat log analyzer.

Quick usage:

    from flurry import analyze_fight, bucket_hits
    from flurry.report import text_dps_report, text_timeline_report

    result = analyze_fight('eqlog_Hacral_firiona.txt', 'Shei Vinitras')
    print(text_dps_report(result))

    timeline = bucket_hits(result, bucket_seconds=5)
    print(text_timeline_report(result, timeline))

For HTML output:

    from flurry.report import html_timeline_report
    with open('fight.html', 'w') as f:
        f.write(html_timeline_report(result, timeline))
"""

__version__ = '0.1.0'

# Re-export the most useful names so callers can `from flurry import ...`
from .analyzer import (
    analyze_fight,
    detect_fights,
    detect_combat,
    group_into_encounters,
    merge_encounter,
    apply_pet_owners,
    bucket_hits,
    AttackerStats,
    HealerStats,
    FightResult,
    Encounter,
    Hit,
    Heal,
    Timeline,
    DEFAULT_SPECIAL_MODS,
    is_crit,
    extract_specials,
)
from .sidecar import (
    Sidecar,
    ManualEncounter,
    fight_key,
    load_sidecar,
    save_sidecar,
    sidecar_path,
)
from .events import (
    Event,
    MeleeHit,
    MeleeMiss,
    SpellDamage,
    SpellResist,
    HealEvent,
    DeathMessage,
    ZoneEntered,
    UnknownEvent,
)
from .parser import parse_line
from .tail import tail_file

__all__ = [
    '__version__',
    # analyzer
    'analyze_fight', 'detect_fights', 'detect_combat',
    'group_into_encounters', 'merge_encounter', 'apply_pet_owners',
    'bucket_hits',
    'AttackerStats', 'HealerStats', 'FightResult', 'Encounter',
    'Hit', 'Heal', 'Timeline',
    'DEFAULT_SPECIAL_MODS', 'is_crit', 'extract_specials',
    # events
    'Event', 'MeleeHit', 'MeleeMiss', 'SpellDamage', 'SpellResist',
    'HealEvent', 'DeathMessage', 'ZoneEntered', 'UnknownEvent',
    # parser & tail
    'parse_line', 'tail_file',
    # sidecar
    'Sidecar', 'ManualEncounter', 'fight_key',
    'load_sidecar', 'save_sidecar', 'sidecar_path',
]
