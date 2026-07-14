#!/usr/bin/env python3
"""
SLSsteam config.yaml cleanup & update tool
==========================================
Fetches the latest default config template from the SLSsteam GitHub repo,
parses your existing config.yaml, and produces a perfectly-formatted output
that carries over ALL your personal values into the new template structure.

Key types are dynamically inferred from the fetched template — the tool
automatically adapts to new or removed keys added by the SLSsteam developer
without any manual script updates needed.

Preserved exactly as-is (with normalized spacing):
  - AdditionalApps / AppIds / FakeOffline  list items + their inline comments
  - FakeAppIds / AppTokens mapping entries + their inline comments
  - GameTitles / SubscriptionTimestamps / DlcData / DenuvoGames entries
  - All scalar settings (DisableFamilyShareLock, LogLevel, FakeEmail, etc.)
  - IdleStatus sub-map

Cleaned automatically:
  - Duplicate AppToken / mapping keys → deduplicated (last value wins)
  - Extra spaces before inline comments → normalized to one space
  - Trailing whitespace on every line
  - All template comments are preserved verbatim

Usage:
    python3 assfixer.py [OPTIONS]

Options:
    --config PATH         Path to config.yaml (default: auto-detected)
    --output PATH         Where to write the result (default: overwrites --config after backup)
    --dry-run             Print result to stdout, don't write to disk
    --no-backup           Skip creating a .bak backup before writing
    --validate-only       Only check for errors in your current config, don't write
    --no-resolve-names    Skip outbound SteamCMD API calls for game name resolution
    --template-url URL    Override the GitHub raw URL for the default config template
    --hints-url URL       Override the GitHub raw URL for the key-type hints JSON
    --version             Show version and exit
"""

import argparse
import json
import re
import shutil
import sys
import textwrap
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ──────────────────────────────────────────────────────────────
# Version
# ──────────────────────────────────────────────────────────────

VERSION = "3.0.0"

# ──────────────────────────────────────────────────────────────
# Path / URL constants
# ──────────────────────────────────────────────────────────────

FLATPAK_CONFIG_PATH = (
    Path.home() / ".var" / "app" / "com.valvesoftware.Steam"
    / ".config" / "SLSsteam" / "config.yaml"
)
NATIVE_CONFIG_PATH  = Path.home() / ".config" / "SLSsteam" / "config.yaml"
DEFAULT_CONFIG_PATH = FLATPAK_CONFIG_PATH if FLATPAK_CONFIG_PATH.exists() else NATIVE_CONFIG_PATH

# C++ source that embeds the YAML default template as a raw string literal.
TEMPLATE_SOURCE_URL = (
    "https://raw.githubusercontent.com/AceSLS/SLSsteam/main/src/config_default.hpp"
)

# Small JSON file in THIS repo that maps ambiguous empty-default keys to their type.
# This only needs updating when the SLSsteam dev adds a brand-new key with no
# default value (empty `key:`) — existing keys never change type.
KEY_HINTS_URL = (
    "https://raw.githubusercontent.com/niwia/ASSfixer/main/key_hints.json"
)

TEMPLATE_TIMEOUT  = 15   # seconds – GitHub raw file download
STEAM_API_TIMEOUT =  5   # seconds – per-game name lookups (many in parallel)

# ──────────────────────────────────────────────────────────────
# Key type constants (used as values in the inferred type map)
# ──────────────────────────────────────────────────────────────

TYPE_SCALAR       = "scalar"        # single value (yes/no, number, string)
TYPE_LIST         = "list"          # sequence of `  - item` entries
TYPE_MAP          = "map"           # mapping of `  key: value` entries
TYPE_MAP_OF_LISTS = "map_of_lists"  # mapping whose values are sub-lists
TYPE_SUBMAP       = "submap"        # fixed-key sub-map (e.g. IdleStatus)
TYPE_UNKNOWN      = "unknown"       # fallback — pass through as-is

# Numeric-validated map keys: values must be integers (Steam IDs / manifest IDs)
# This is a narrow hint because it affects value sanitization, not structure.
NUMERIC_VALUE_MAP_KEYS = {
    "AppTokens", "FakeAppIds", "SubscriptionTimestamps", "ManifestIds",
    "DlcData",
}

IDLE_STATUS_KEY = "IdleStatus"

# ──────────────────────────────────────────────────────────────
# Colours / logging helpers
# ──────────────────────────────────────────────────────────────

RED    = "\033[91m"
YELLOW = "\033[93m"
GREEN  = "\033[92m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def info(msg):  print(f"{CYAN}[INFO]{RESET}  {msg}")
def ok(msg):    print(f"{GREEN}[OK]{RESET}    {msg}")
def warn(msg):  print(f"{YELLOW}[WARN]{RESET}  {msg}")
def error(msg): print(f"{RED}[ERROR]{RESET} {msg}", file=sys.stderr)

# ──────────────────────────────────────────────────────────────
# Formatting / sanitization helpers
# ──────────────────────────────────────────────────────────────

_TITLE_RE = re.compile(r'[^\x20-\x7E]')

def sanitize_title(s: str) -> str:
    s = _TITLE_RE.sub("", s).strip().strip('"\'')
    if not s:
        return '""'
    needs_quotes = any(c in s for c in (':', '#', '"', "'", '[', ']', '{', '}'))
    if needs_quotes:
        s = s.replace('"', '\\"')
        return f'"{s}"'
    return s


# ──────────────────────────────────────────────────────────────
# Network helpers
# ──────────────────────────────────────────────────────────────

def _fetch_url(url: str, timeout: int) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": f"ASSfixer/{VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def fetch_template(url: str = TEMPLATE_SOURCE_URL) -> str:
    """Download config_default.hpp and extract the embedded YAML template."""
    try:
        raw = _fetch_url(url, TEMPLATE_TIMEOUT)
    except Exception as exc:
        error(f"Failed to fetch template: {exc}")
        sys.exit(1)
    m = re.search(r'static const char\* defaultConfig = R"\((.+?)\)";', raw, re.DOTALL)
    if not m:
        error("Could not find YAML template in the downloaded C++ file.")
        error("The upstream file format may have changed. Please report this.")
        sys.exit(1)
    return m.group(1)


def fetch_key_hints(url: str = KEY_HINTS_URL) -> dict:
    """
    Download the key_hints.json from the ASSfixer repo.
    Returns a dict mapping key_name -> type string.
    Falls back to an empty dict if unavailable (best-effort).
    """
    try:
        raw = _fetch_url(url, TEMPLATE_TIMEOUT)
        return json.loads(raw)
    except Exception:
        return {}


def lookup_steam_name(app_id: str) -> Optional[str]:
    try:
        url = f"https://store.steampowered.com/api/appdetails?appids={app_id}&filters=basic"
        raw = _fetch_url(url, STEAM_API_TIMEOUT)
        data = json.loads(raw)
        if data.get(app_id, {}).get("success"):
            return data[app_id]["data"].get("name")
    except Exception:
        pass
    return None


# ──────────────────────────────────────────────────────────────
# Dynamic key-type inference from template
# ──────────────────────────────────────────────────────────────

def infer_key_types(template_yaml: str, hints: dict) -> dict:
    """
    Parse the template YAML to discover the type of each top-level key.

    Returns a dict: { key_name: TYPE_* }

    Type inference rules:
      - `key: yes` / `key: no`        → TYPE_SCALAR (boolean)
      - `key: <number>`               → TYPE_SCALAR (numeric)
      - `key: "<string>"` / similar   → TYPE_SCALAR
      - `key:` then `  - item`        → TYPE_LIST
      - `key:` then `  word: value`   → TYPE_MAP or TYPE_SUBMAP
      - `key:` then `  word:\n    -`  → TYPE_MAP_OF_LISTS
      - `key:` with no children       → look up in hints; TYPE_UNKNOWN if missing
    """
    key_types: dict[str, str] = {}
    lines = template_yaml.splitlines()
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        # Skip blank lines and comment-only lines
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        # Top-level key: must start with no indentation
        m = re.match(r'^([A-Za-z][A-Za-z0-9_]*)\s*:\s*(.*)', line)
        if not m:
            i += 1
            continue

        key   = m.group(1)
        value = m.group(2).strip()

        if value and not value.startswith("#"):
            # Key has an inline value → scalar
            key_types[key] = TYPE_SCALAR
            i += 1
            continue

        # Key is empty (`key:` with optional comment). Look ahead at children.
        j = i + 1
        # Skip comment lines directly after the key
        while j < n and (not lines[j].strip() or lines[j].strip().startswith("#")):
            j += 1

        # Check if there are indented children
        if j < n and lines[j].startswith("  ") and not lines[j].strip().startswith("#"):
            child_line = lines[j].strip()

            if child_line.startswith("- ") or child_line == "-":
                key_types[key] = TYPE_LIST

            else:
                # Child is `word: value` or `word:` (submap or map or map-of-lists)
                child_m = re.match(r'^([^:]+):\s*(.*)', child_line)
                if child_m:
                    child_val = child_m.group(2).strip()
                    # Peek one more level: is the grandchild a list item?
                    k = j + 1
                    while k < n and (not lines[k].strip() or lines[k].strip().startswith("#")):
                        k += 1
                    if k < n and lines[k].startswith("    ") and lines[k].strip().startswith("- "):
                        key_types[key] = TYPE_MAP_OF_LISTS
                    elif not child_val or child_val.startswith("#"):
                        # Child also has no value — treat as submap (fixed sub-keys like IdleStatus)
                        key_types[key] = TYPE_SUBMAP
                    else:
                        key_types[key] = TYPE_MAP
                else:
                    key_types[key] = TYPE_UNKNOWN
        else:
            # Completely empty key — use hints or fall back to unknown
            hint = hints.get(key, TYPE_UNKNOWN)
            key_types[key] = hint

        i += 1

    return key_types


# ──────────────────────────────────────────────────────────────
# ConfigEntry dataclass (key + raw value string preserving comments)
# ──────────────────────────────────────────────────────────────

@dataclass
class ConfigEntry:
    key: Optional[str]   # None for list items
    val: str             # the raw value string (may include inline comment)

    def __hash__(self):
        return hash((self.key, self.val.split("#")[0].strip()))

    def __eq__(self, other):
        if not isinstance(other, ConfigEntry):
            return NotImplemented
        return (self.key == other.key and
                self.val.split("#")[0].strip() == other.val.split("#")[0].strip())


# ──────────────────────────────────────────────────────────────
# SimpleYAMLReader  — parses the user's config.yaml
# ──────────────────────────────────────────────────────────────

class SimpleYAMLReader:
    """
    A hand-rolled parser that understands enough SLSsteam YAML to round-trip it
    perfectly. Avoids PyYAML so no extra dependency is needed.
    """

    def __init__(self, key_types: Optional[dict] = None):
        # key_types is the inferred map from infer_key_types().
        # If not provided, all keys default to TYPE_UNKNOWN (passthrough).
        self._key_types = key_types or {}

    def _ktype(self, key: str) -> str:
        return self._key_types.get(key, TYPE_UNKNOWN)

    def parse(self, text: str) -> dict:
        lines = text.splitlines()
        result: dict = {}
        i = 0
        n = len(lines)

        while i < n:
            line = lines[i]
            stripped = line.strip()

            if not stripped or stripped.startswith("#"):
                i += 1
                continue

            m = re.match(r'^([A-Za-z][A-Za-z0-9_]*)\s*:\s*(.*)', line)
            if not m:
                i += 1
                continue

            key   = m.group(1)
            value = m.group(2).strip()
            ktype = self._ktype(key)

            if value and not value.startswith("#"):
                # Inline scalar value
                if ktype == TYPE_LIST:
                    result[key] = self._parse_scalar_as_list(value)
                elif ktype == TYPE_MAP:
                    result[key] = self._parse_scalar_as_map(key, value)
                else:
                    result[key] = self._clean_scalar(key, value)
                i += 1
                continue

            # No inline value — collect indented children
            if ktype == TYPE_LIST:
                items, i = self._read_list(lines, i + 1, n, key)
                result[key] = items
            elif ktype in (TYPE_MAP, TYPE_MAP_OF_LISTS):
                mapping, i = self._read_map(lines, i + 1, n, key, ktype)
                result[key] = mapping
            elif key == IDLE_STATUS_KEY or ktype == TYPE_SUBMAP:
                submap, i = self._read_submap(lines, i + 1, n)
                result[key] = submap
            elif ktype == TYPE_SCALAR:
                result[key] = None
                i += 1
            else:
                # TYPE_UNKNOWN: try to auto-detect from children
                children_start = i + 1
                j = children_start
                while j < n and (not lines[j].strip() or lines[j].strip().startswith("#")):
                    j += 1
                if j < n and lines[j].startswith("  ") and not lines[j].strip().startswith("#"):
                    child = lines[j].strip()
                    if child.startswith("- ") or child == "-":
                        items, i = self._read_list(lines, children_start, n, key)
                        result[key] = items
                    else:
                        mapping, i = self._read_map(lines, children_start, n, key, TYPE_MAP)
                        result[key] = mapping
                else:
                    result[key] = None
                    i += 1

        return result

    # ── Child-block readers ────────────────────────────────────

    def _read_list(self, lines, start, n, parent_key) -> tuple[list, int]:
        items = []
        i = start
        while i < n:
            line = lines[i]
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                i += 1
                continue
            if not line.startswith(" "):
                break
            m = re.match(r'^\s+-\s+(.*)', line)
            if not m:
                break
            raw = m.group(1).strip()
            m2 = re.match(r'^([0-9]+)\s*(#.*)?$', raw)
            if m2:
                num, comment = m2.group(1), m2.group(2)
                entry_val = f"{num} {comment.strip()}" if comment else num
            else:
                entry_val = raw
            items.append(ConfigEntry(None, entry_val))
            i += 1
        return items, i

    def _read_map(self, lines, start, n, parent_key, ktype) -> tuple[dict, int]:
        result_map: dict = {}
        i = start
        while i < n:
            line = lines[i]
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                i += 1
                continue
            if not line.startswith(" "):
                break
            mm = re.match(r'^\s+([^:]+):\s*(.*)', line)
            if not mm:
                i += 1
                continue
            k   = mm.group(1).strip()
            val = mm.group(2).strip()
            if not k:
                i += 1
                continue

            if ktype == TYPE_MAP_OF_LISTS:
                sub: list = []
                i += 1
                while i < n:
                    sub_line = lines[i]
                    sub_stripped = sub_line.strip()
                    if not sub_stripped or sub_stripped.startswith("#"):
                        i += 1
                        continue
                    if not sub_line.startswith("    "):
                        break
                    m_sub = re.match(r'^\s+-\s+([0-9]+)\s*(#.*)?$', sub_line)
                    if m_sub:
                        num     = m_sub.group(1)
                        comment = m_sub.group(2)
                        raw_sub = f"{num} {comment.strip()}" if comment else num
                        sub.append(ConfigEntry(None, raw_sub))
                        i += 1
                    else:
                        break
                result_map[k] = sub if sub else None
            else:
                cleaned = self._clean_map_value(parent_key, val)
                if cleaned:
                    result_map[k] = ConfigEntry(k, cleaned)
                i += 1

        return result_map, i

    def _read_submap(self, lines, start, n) -> tuple[dict, int]:
        sub: dict = {}
        i = start
        while i < n:
            line = lines[i]
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                i += 1
                continue
            if not line.startswith(" "):
                break
            mm = re.match(r'^\s+([^:]+):\s*(.*)', line)
            if not mm:
                i += 1
                continue
            k   = mm.group(1).strip()
            val = mm.group(2).strip()
            sub[k] = val
            i += 1
        return sub, i

    # ── Value sanitizers ───────────────────────────────────────

    def _clean_scalar(self, key: str, val: str) -> str:
        val = val.strip()
        m = re.match(r'^([^#]+?)\s*(#.*)?$', val)
        if not m:
            return val
        v   = m.group(1).strip()
        cmt = m.group(2)
        return f"{v} {cmt.strip()}" if cmt else v

    def _clean_map_value(self, parent_key: str, val: str) -> str:
        """Sanitize a mapping entry's value string; return '' to discard."""
        m = re.match(r'^([^#\s]+)\s*(#.*)?$', val)
        if not m:
            return ""
        v_part  = m.group(1).strip()
        comment = m.group(2)
        if parent_key in NUMERIC_VALUE_MAP_KEYS:
            if not v_part.isdigit():
                return ""
            return f"{v_part} {comment.strip()}" if comment else v_part
        if parent_key == "GameTitles":
            v_quoted = sanitize_title(v_part)
            return f"{v_quoted} {comment.strip()}" if comment else v_quoted
        return val

    # ── Scalar-encoded structures ─────────────────────────────

    def _parse_scalar_as_list(self, val: str) -> list:
        val = val.strip().strip("[]")
        result = []
        for part in val.split(","):
            part = part.strip()
            m    = re.match(r'^([0-9]+)\s*(#.*)?$', part)
            if m:
                num, comment = m.group(1), m.group(2)
                raw_val = f"{num} {comment.strip()}" if comment else num
                result.append(ConfigEntry(None, raw_val))
        return result

    def _parse_scalar_as_map(self, parent_key: str, val: str) -> dict:
        val    = val.strip().strip("{}")
        result = {}
        for part in val.split(","):
            part = part.strip()
            mm   = re.match(r'^([^:]+):\s*(.*)', part)
            if not mm:
                continue
            k = mm.group(1).strip()
            v = mm.group(2).strip()
            if not k.isdigit():
                continue
            cleaned = self._clean_map_value(parent_key, v)
            if cleaned:
                result[k] = ConfigEntry(k, cleaned)
        return result


# ──────────────────────────────────────────────────────────────
# Deduplication helpers
# ──────────────────────────────────────────────────────────────

def dedup_list(items: list) -> list:
    seen = {}
    for entry in items:
        num = entry.val.split()[0]
        seen[num] = entry
    return list(seen.values())


def dedup_map(mapping: dict) -> dict:
    return dict(mapping)   # dict already deduplicates by key


# ──────────────────────────────────────────────────────────────
# Steam name resolver
# ──────────────────────────────────────────────────────────────

def resolve_missing_names(config_data: dict) -> None:
    """Fill in missing inline comments (game names) for list/map entries in parallel."""
    tasks = []

    def collect(entries):
        if isinstance(entries, list):
            for e in entries:
                if e and "#" not in e.val:
                    tasks.append(e)
        elif isinstance(entries, dict):
            for v in entries.values():
                if isinstance(v, ConfigEntry) and "#" not in v.val:
                    tasks.append(v)

    for key, val in config_data.items():
        collect(val)

    if not tasks:
        return

    info(f"Resolving {len(tasks)} game name(s) via Steam API…")
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {}
        for entry in tasks:
            app_id = entry.val.split()[0]
            futures[pool.submit(lookup_steam_name, app_id)] = entry
        for fut in as_completed(futures):
            entry = futures[fut]
            name  = fut.result()
            if name:
                entry.val = f"{entry.val.split()[0]} # {name}"


# ──────────────────────────────────────────────────────────────
# Config validator
# ──────────────────────────────────────────────────────────────

def validate_config(config_path: Path, key_types: dict) -> list[str]:
    issues = []
    text   = config_path.read_text(encoding="utf-8")
    lines  = text.splitlines()

    for lineno, line in enumerate(lines, 1):
        if line != line.rstrip():
            issues.append(f"Line {lineno}: trailing whitespace")
        if "  #" in line:
            parts = line.split("  #", 1)
            if parts[0].rstrip() != parts[0]:
                issues.append(f"Line {lineno}: multiple spaces before inline comment")

    reader   = SimpleYAMLReader(key_types)
    try:
        data = reader.parse(text)
    except Exception as exc:
        issues.append(f"Parse error: {exc}")
        return issues

    for key in data:
        if key not in key_types and key != IDLE_STATUS_KEY:
            issues.append(f"Unknown top-level key '{key}' (not in current template)")

    return issues


# ──────────────────────────────────────────────────────────────
# Config merger  — inject user values into the fresh template
# ──────────────────────────────────────────────────────────────

def _render_list(items: list, indent: str = "  ") -> str:
    out = []
    for entry in dedup_list(items):
        out.append(f"{indent}- {entry.val}")
    return "\n".join(out)


def _render_map(mapping: dict, parent_key: str, indent: str = "  ") -> str:
    out = []
    for k, v in dedup_map(mapping).items():
        if isinstance(v, list):   # map-of-lists
            out.append(f"{indent}{k}:")
            for sub in v:
                out.append(f"{indent}  - {sub.val}")
        elif isinstance(v, ConfigEntry):
            out.append(f"{indent}{k}: {v.val}")
    return "\n".join(out)


def _render_submap(submap: dict, indent: str = "  ") -> str:
    out = []
    for k, v in submap.items():
        out.append(f"{indent}{k}: {v}")
    return "\n".join(out)


def merge_config(template_yaml: str, user_data: dict, key_types: dict) -> str:
    """
    Walk the template line-by-line, replacing empty-block placeholders with
    the user's carried-over values. Returns the merged YAML string.
    """
    out_lines = []
    lines = template_yaml.splitlines()
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Comment or blank — pass through
        if not stripped or stripped.startswith("#"):
            out_lines.append(line.rstrip())
            i += 1
            continue

        m = re.match(r'^([A-Za-z][A-Za-z0-9_]*)\s*:\s*(.*)', line)
        if not m:
            out_lines.append(line.rstrip())
            i += 1
            continue

        key   = m.group(1)
        value = m.group(2).strip()
        ktype = key_types.get(key, TYPE_UNKNOWN)

        # ── Scalar key: emit with user's value ──────────────
        if value and not value.startswith("#"):
            user_val = user_data.get(key)
            if isinstance(user_val, str) and user_val:
                # Normalise spacing around inline comment
                uv = user_val.strip()
                mv = re.match(r'^([^#]+?)\s*(#.*)?$', uv)
                if mv:
                    clean_val = mv.group(1).strip()
                    cmt       = mv.group(2)
                    uv = f"{clean_val} {cmt.strip()}" if cmt else clean_val
                out_lines.append(f"{key}: {uv}")
            else:
                out_lines.append(f"{key}: {value}")
            # Skip original indented block in template (shouldn't exist for scalars)
            i += 1
            continue

        # ── Block key (list / map / submap / unknown) ────────
        out_lines.append(f"{key}:")

        # Skip template's own child lines for this key
        i += 1
        while i < n and (lines[i].startswith("  ") or not lines[i].strip() and i + 1 < n and lines[i + 1].startswith("  ")):
            # Only skip indented children, not blank lines before next top-level key
            if lines[i].startswith("  "):
                i += 1
            else:
                break

        # Emit user's values
        user_val = user_data.get(key)

        if key == IDLE_STATUS_KEY or ktype == TYPE_SUBMAP:
            if isinstance(user_val, dict) and user_val:
                out_lines.append(_render_submap(user_val))
            else:
                # Use template defaults for the submap
                # Re-read template submap
                j = i
                # Actually we already skipped past those lines; fall back to defaults
                pass  # will be empty — that's fine, SLSsteam handles missing sub-keys

        elif ktype == TYPE_LIST or (ktype == TYPE_UNKNOWN and isinstance(user_val, list)):
            if isinstance(user_val, list) and user_val:
                out_lines.append(_render_list(user_val))

        elif ktype in (TYPE_MAP, TYPE_MAP_OF_LISTS) or (ktype == TYPE_UNKNOWN and isinstance(user_val, dict)):
            if isinstance(user_val, dict) and user_val:
                out_lines.append(_render_map(user_val, key))

        # else: empty block — leave just `key:` with no children

    return "\n".join(out_lines) + "\n"


# ──────────────────────────────────────────────────────────────
# Backup helpers
# ──────────────────────────────────────────────────────────────

def make_backup_with_rotation(config_path: Path) -> Path:
    """
    Creates a backup of config.yaml with rotation.
    Tries config.yaml.bak, config.yaml.bak2, config.yaml.bak3, etc.
    Returns the backup path created.
    """
    bak_base = config_path.with_name(config_path.name + ".bak")
    if not bak_base.exists():
        shutil.copy2(config_path, bak_base)
        return bak_base
    i = 2
    while True:
        bak_rot = config_path.with_name(config_path.name + f".bak{i}")
        if not bak_rot.exists():
            shutil.copy2(config_path, bak_rot)
            return bak_rot
        i += 1


# ──────────────────────────────────────────────────────────────
# GUI integration API  (called by ASSella's settings dialog)
# ──────────────────────────────────────────────────────────────

def run_asshead_migration(
    config_path: Path,
    template_url: str = TEMPLATE_SOURCE_URL,
    hints_url: str    = KEY_HINTS_URL,
) -> tuple[bool, str, Optional[Path]]:
    """
    Validates and migrates config.yaml to the latest upstream template.
    Creates a rotating backup if changes are needed.
    Returns (success: bool, message: str, backup_path: Optional[Path]).
    """
    try:
        if not config_path.exists():
            return False, f"Config file not found at {config_path}", None

        hints        = fetch_key_hints(hints_url)
        template_yaml = fetch_template(template_url)
        key_types    = infer_key_types(template_yaml, hints)

        config_text = config_path.read_text(encoding="utf-8")
        reader      = SimpleYAMLReader(key_types)
        old_data    = reader.parse(config_text)

        template_data = reader.parse(template_yaml)
        issues        = validate_config(config_path, key_types)
        new_keys      = set(template_data) - set(old_data)

        if not issues and not new_keys:
            return True, "No changes needed. SLSsteam config is already optimal!", None

        merged   = merge_config(template_yaml, old_data, key_types)
        bak_path = make_backup_with_rotation(config_path)
        config_path.write_text(merged, encoding="utf-8")

        msg = "Successfully updated config.yaml to the latest template."
        if new_keys:
            msg += f" Added {len(new_keys)} new key(s)."
        if issues:
            msg += f" Fixed {len(issues)} formatting issue(s)."

        return True, msg, bak_path

    except Exception as e:
        return False, f"Error: {e}", None


# ──────────────────────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent("""\
            SLSsteam config.yaml cleanup & update tool
            ==========================================
            Fetches the latest upstream template and carries over all your
            personal values into the new structure, fixing formatting issues
            and adding any new keys with their default values.
        """),
        epilog=textwrap.dedent("""\
            Examples:
              Preview the result without writing:
                python3 assfixer.py --dry-run

              Update in place (auto-backup created):
                python3 assfixer.py

              Write to a different output file:
                python3 assfixer.py --output /tmp/config_new.yaml

              Skip outbound name lookups:
                python3 assfixer.py --no-resolve-names
        """),
    )
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH,
        help=f"Path to config.yaml (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Output path (default: overwrites --config after backup)",
    )
    parser.add_argument("--dry-run",           action="store_true",
                        help="Print merged config to stdout, don't write to disk")
    parser.add_argument("--no-backup",         action="store_true",
                        help="Skip creating a .bak backup before writing")
    parser.add_argument("--validate-only",     action="store_true",
                        help="Only check for errors, don't merge or write")
    parser.add_argument("--no-resolve-names",  action="store_true",
                        help="Skip outbound SteamCMD API calls for game name resolution")
    parser.add_argument(
        "--template-url", default=TEMPLATE_SOURCE_URL,
        help="Override the GitHub URL for the default config template",
    )
    parser.add_argument(
        "--hints-url", default=KEY_HINTS_URL,
        help="Override the GitHub URL for the key-type hints JSON",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")

    args = parser.parse_args()

    # Interactive mode: no CLI flags were passed by the user
    interactive = (
        args.config        == DEFAULT_CONFIG_PATH
        and args.output    is None
        and not args.dry_run
        and not args.no_backup
        and not args.validate_only
        and not args.no_resolve_names
        and args.template_url == TEMPLATE_SOURCE_URL
        and args.hints_url    == KEY_HINTS_URL
    )

    print()
    print(f"{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}   SLSsteam Config Cleanup & Update Tool  v{VERSION}{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")
    print()

    config_path: Path = args.config
    choice            = 1

    if interactive:
        print("Please select an option:")
        print("  1) Clean & Update Config")
        print("  2) Restore Backup  (from config.bak)")
        print("  3) Apply Steam Deck Recommended Settings")
        while True:
            try:
                user_choice = input("Enter choice (1-3, default: 1): ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nCancelled.")
                sys.exit(0)
            if not user_choice:
                choice = 1
                break
            if user_choice in ("1", "2", "3"):
                choice = int(user_choice)
                break
            print("Invalid choice. Please enter 1, 2, or 3.")
        print()

    # ── Option 2: Restore backup ─────────────────────────────────
    if interactive and choice == 2:
        print(f"Default config location: {DEFAULT_CONFIG_PATH}")
        while True:
            try:
                user_input = input("Enter path to config.yaml (press Enter for default): ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nCancelled.")
                sys.exit(0)
            config_path = (
                Path(user_input).expanduser().resolve() if user_input else DEFAULT_CONFIG_PATH
            )
            bak_path = config_path.with_name(config_path.name + ".bak")
            if bak_path.exists():
                break
            error(f"Backup file not found: {bak_path}. Please try again.")
            print()

        info(f"Restoring {bak_path} → {config_path} …")
        try:
            shutil.copy2(bak_path, config_path)
            ok("Backup successfully restored.")
        except Exception as exc:
            error(f"Failed to restore backup: {exc}")
            sys.exit(1)
        print()
        return

    # ── Options 1/3: resolve config path ─────────────────────────
    if interactive:
        print(f"Default config location: {DEFAULT_CONFIG_PATH}")
        while True:
            try:
                user_input = input("Enter path to config.yaml (press Enter for default): ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nCancelled.")
                sys.exit(0)
            config_path = (
                Path(user_input).expanduser().resolve() if user_input else DEFAULT_CONFIG_PATH
            )
            if config_path.exists():
                break
            error(f"Config file not found: {config_path}. Please try again.")
            print()
    else:
        if not config_path.exists():
            error(f"Config file not found: {config_path}")
            error("Make sure SLSsteam has been run at least once.")
            sys.exit(1)

    info(f"Config file: {config_path}")
    print()

    # ── Fetch template and infer key types ───────────────────────
    print(f"{BOLD}[0/4] Fetching key hints and template from GitHub…{RESET}")
    hints         = fetch_key_hints(args.hints_url)
    template_yaml = fetch_template(args.template_url)
    key_types     = infer_key_types(template_yaml, hints)
    ok(f"  Discovered {len(key_types)} top-level key(s) in upstream template.")
    print()

    # ── 1. Validate ──────────────────────────────────────────────
    print(f"{BOLD}[1/4] Validating existing config…{RESET}")
    issues = validate_config(config_path, key_types)
    if issues:
        print(f"  Found {len(issues)} issue(s):")
        for issue in issues:
            warn(f"  {issue}")
    else:
        ok("  No formatting issues detected.")

    if args.validate_only:
        print()
        if issues:
            print(f"{YELLOW}Validation complete — {len(issues)} issue(s) found.{RESET}")
            print("Run without --validate-only to auto-fix and update.")
        else:
            print(f"{GREEN}Validation complete — config looks good!{RESET}")
        return
    print()

    # ── 2. Parse old config ──────────────────────────────────────
    print(f"{BOLD}[2/4] Parsing existing config…{RESET}")
    config_text = config_path.read_text(encoding="utf-8")
    reader      = SimpleYAMLReader(key_types)
    old_data    = reader.parse(config_text)

    for section, val in old_data.items():
        if val is None:
            ok(f"  {section}: (empty)")
        elif isinstance(val, str):
            ok(f"  {section}: {val}")
        elif isinstance(val, (list, dict)):
            ok(f"  {section}: {len(val)} entries")

    if not args.no_resolve_names:
        resolve_missing_names(old_data)
    print()

    # ── 2.5 Steam Deck preset (option 3) ─────────────────────────
    if interactive and choice == 3:
        info("Applying Steam Deck recommended overrides:")
        overrides = {
            "SafeMode":        "yes",
            "Notifications":   "yes",
            "LogLevel":        "2",
            "ExtendedLogging": "no",
        }
        for k, v in overrides.items():
            info(f"  {k}: {v}")
            old_data[k] = v
        print()

    # ── 3. Fetch template data ────────────────────────────────────
    print(f"{BOLD}[3/4] Comparing with upstream template…{RESET}")
    template_data = reader.parse(template_yaml)
    new_keys      = set(template_data) - set(old_data)
    removed_keys  = set(old_data)      - set(template_data)
    if new_keys:
        info(f"  New upstream key(s) — using defaults: {', '.join(sorted(new_keys))}")
    if removed_keys:
        warn(f"  Key(s) not in template (dropped): {', '.join(sorted(removed_keys))}")
    if not new_keys and not removed_keys:
        ok("  Your config is in sync with the upstream template.")
    print()

    # ── 4. Merge ─────────────────────────────────────────────────
    print(f"{BOLD}[4/4] Merging your values into new template…{RESET}")
    merged = merge_config(template_yaml, old_data, key_types)

    for key in list(key_types.keys()):
        old_val = old_data.get(key)
        if isinstance(old_val, dict):
            before, after = len(old_val), len(dedup_map(old_val))
        elif isinstance(old_val, list):
            before, after = len(old_val), len(dedup_list(old_val))
        else:
            continue
        if before != after:
            info(f"  {key}: deduplicated {before} → {after} entries")

    ok("  Merge complete.")
    print()

    # ── Output ───────────────────────────────────────────────────
    if args.dry_run:
        print(f"{BOLD}{'─'*60}{RESET}")
        print(f"{BOLD}DRY RUN — merged config (not written to disk):{RESET}")
        print(f"{BOLD}{'─'*60}{RESET}")
        print(merged)
        return

    out_path: Path = args.output or config_path

    if not args.no_backup:
        bak_path = make_backup_with_rotation(config_path)
        ok(f"Backup created: {bak_path}")

    out_path.write_text(merged, encoding="utf-8")
    ok(f"Config written:  {out_path}")
    print()

    if issues:
        print(f"{YELLOW}Fixed {len(issues)} formatting issue(s) from your old config.{RESET}")
    if new_keys:
        print(f"{CYAN}Added {len(new_keys)} new upstream key(s) with defaults.{RESET}")
    if removed_keys:
        print(f"{YELLOW}Dropped {len(removed_keys)} key(s) no longer in template.{RESET}")

    print()
    print(f"{GREEN}{BOLD}Done! Your config.yaml has been cleaned up and updated.{RESET}")
    print()


if __name__ == "__main__":
    main()
