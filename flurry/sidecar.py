"""
sidecar.py - per-log persistence for user edits.

A log file's manual encounter overrides and pet-owner assignments live
in a sibling JSON file (`<logfile>.flurry.json`). Loaded when a log is
opened, written on every edit. A missing or unreadable sidecar degrades
silently to defaults so a fresh log just works.

The sidecar is keyed by stable identifiers that survive parameter
changes: pet owners by attacker name, encounters by per-fight composite
keys (target name + start timestamp). `fight_id` and `encounter_id` are
NOT used in the on-disk format because both can shift when detection
parameters change (lower min_damage admits new short fights, lower
gap_seconds slices long fights into more pieces).

Schema (version 1):

    {
      "version": 1,
      "pet_owners": {"<actor>": "<owner>", ...},
      "manual_encounters": [
        {"fight_keys": ["target|iso_ts", ...], "name": null}, ...
      ]
    }
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


SIDECAR_VERSION = 1


def fight_key(target: str, start: Optional[datetime]) -> Optional[str]:
    """Stable identifier for a FightResult: lowercased target + ISO start.

    `None` if the fight has no start (placeholder/empty fights). Two
    fights with the same target and same start timestamp are treated as
    the same fight — collisions only happen if the log itself is
    duplicated, which we accept.
    """
    if start is None:
        return None
    return f'{target.lower()}|{start.isoformat()}'


@dataclass
class ManualEncounter:
    """User-asserted grouping: these specific fights are one encounter.

    `name` overrides the auto-derived display name when set. An empty
    string is treated the same as `None` for round-tripping convenience.
    """
    fight_keys: List[str]
    name: Optional[str] = None

    def to_json(self) -> dict:
        return {'fight_keys': list(self.fight_keys), 'name': self.name}


@dataclass
class Sidecar:
    """All user edits attached to one log file."""
    pet_owners: Dict[str, str] = field(default_factory=dict)
    manual_encounters: List[ManualEncounter] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            'version': SIDECAR_VERSION,
            'pet_owners': dict(self.pet_owners),
            'manual_encounters': [m.to_json() for m in self.manual_encounters],
        }

    @classmethod
    def from_json(cls, data: dict) -> 'Sidecar':
        return cls(
            pet_owners=dict(data.get('pet_owners') or {}),
            manual_encounters=[
                ManualEncounter(
                    fight_keys=[str(k) for k in (m.get('fight_keys') or [])],
                    name=(m.get('name') or None),
                )
                for m in (data.get('manual_encounters') or [])
            ],
        )

    @classmethod
    def empty(cls) -> 'Sidecar':
        return cls()

    def is_empty(self) -> bool:
        return not self.pet_owners and not self.manual_encounters

    # ----- Mutation helpers -----

    def set_pet_owner(self, actor: str, owner: Optional[str]) -> None:
        """Assign `actor` to `owner`, or clear the mapping when owner is
        falsy. Mapping is stored case-sensitively under the actor name as
        it was first observed; lookups in apply_pet_owners are
        case-insensitive."""
        actor = actor.strip()
        if not actor:
            return
        if owner is None or not str(owner).strip():
            # Clear by case-insensitive match so a casing mismatch in the
            # request doesn't leave a stale entry.
            lo = actor.lower()
            for k in list(self.pet_owners.keys()):
                if k.lower() == lo:
                    del self.pet_owners[k]
            return
        self.pet_owners[actor] = owner.strip()

    def merge_encounter(self, fight_keys: List[str],
                        name: Optional[str] = None) -> None:
        """Record a manual encounter grouping over the given fight keys.

        Removes the keys from any prior manual encounters first so a
        fight only ever lives in one manual group; prunes manual groups
        that drop below 2 keys (singletons fall back to auto-grouping).
        """
        keys = [k for k in fight_keys if k]
        if len(keys) < 2:
            return
        keyset = set(keys)
        for m in self.manual_encounters:
            m.fight_keys = [k for k in m.fight_keys if k not in keyset]
        self.manual_encounters = [m for m in self.manual_encounters
                                  if len(m.fight_keys) >= 2]
        self.manual_encounters.append(ManualEncounter(fight_keys=keys,
                                                     name=name or None))

    def remove_keys_from_manual(self, fight_keys: List[str]) -> None:
        """Drop the listed fight keys from every manual group; prunes
        groups that fall below 2 members. Used by 'split' actions."""
        if not fight_keys:
            return
        keyset = set(fight_keys)
        for m in self.manual_encounters:
            m.fight_keys = [k for k in m.fight_keys if k not in keyset]
        self.manual_encounters = [m for m in self.manual_encounters
                                  if len(m.fight_keys) >= 2]

    def manual_groups_for_grouper(self) -> List[dict]:
        """Convert to the {fight_keys, name} dict shape expected by
        analyzer.group_into_encounters. Trivial today, but isolates the
        on-disk format from the analyzer's call signature."""
        return [m.to_json() for m in self.manual_encounters]


# ----- I/O -----

def sidecar_path(logfile: str) -> str:
    """Sidecar lives next to the log with `.flurry.json` appended.

    For an upload at /tmp/flurry-uploads/foo.txt this yields
    /tmp/flurry-uploads/foo.txt.flurry.json — same dir, persists for the
    life of that temp file.
    """
    return logfile + '.flurry.json'


def load_sidecar(logfile: str) -> Sidecar:
    """Load the sidecar for `logfile`, returning an empty one if the
    file is missing or unreadable. Corrupt JSON is treated the same as
    missing — the user's intent is preserved by leaving the file alone
    (we don't overwrite until the next save)."""
    path = sidecar_path(logfile)
    if not os.path.isfile(path):
        return Sidecar.empty()
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return Sidecar.empty()
    if not isinstance(data, dict):
        return Sidecar.empty()
    return Sidecar.from_json(data)


def save_sidecar(logfile: str, sidecar: Sidecar) -> None:
    """Write the sidecar atomically. If the sidecar is empty AND the
    file already exists, we still write it — keeping a zero-edit file
    next to the log signals 'I tried to edit and rolled back to nothing,'
    which is different from 'I never edited at all.'"""
    path = sidecar_path(logfile)
    tmp = path + '.tmp'
    payload = json.dumps(sidecar.to_json(), indent=2)
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.write(payload)
    os.replace(tmp, path)
