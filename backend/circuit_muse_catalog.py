"""Offline Circuit Muse catalog supplement; see vendor/circuit-muse/ORIGIN.md."""

import copy
import json
from functools import lru_cache
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / 'public/circuit-muse-catalog.json'
UPSTREAM_REVISION = 'd3cd945fe9bd25437d6559171ae06f8f03cc49ed'
CATALOG_ALIASES = {
    'esp32': 'esp32-devkit-v1',
    'raspberry-pi-pico': 'pi-pico',
    'raspberry-pi-pico-w': 'pi-pico-w',
}


@lru_cache(maxsize=1)
def _catalog_entries():
    data = json.loads(CATALOG_PATH.read_text(encoding='utf-8'))
    if data.get('schema_version') != 1 or data.get('provenance', {}).get('revision') != UPSTREAM_REVISION:
        raise ValueError('Invalid Circuit Muse catalog provenance or schema.')
    entries = data.get('components')
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError('Invalid Circuit Muse catalog entries.')
    ids = [entry.get('id') for entry in entries]
    if any(not isinstance(kind, str) or not kind for kind in ids) or len(set(ids)) != len(ids):
        raise ValueError('Invalid or duplicate Circuit Muse catalog IDs.')
    return entries


def extra_catalog_entries():
    """Return independent metadata copies without enabling any component runtime.

    Merge by ID before ComponentCatalog normalizes pins and properties. Existing
    entries must win; aliases are identities, not substitutes for SSD1306 presets.
    """
    return copy.deepcopy(_catalog_entries())
