#!/usr/bin/env python3
"""
SLSsteam config.yaml cleanup & update tool
==========================================
Fetches the latest default config template from the SLSsteam GitHub repo,
parses your existing config.yaml, and produces a perfectly-formatted output
that carries over ALL your personal values into the new template structure.

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
    python3 slssteam_config.py [OPTIONS]

Options:
    --config PATH       Path to config.yaml (default: ~/.config/SLSsteam/config.yaml)
    --output PATH       Where to write the result (default: overwrites --config after backup)
    --dry-run           Print result to stdout, don't write to disk
    --no-backup         Skip creating a .bak backup before writing
    --validate-only     Only check for errors in your current config, don't write
    --template-url URL  Override the GitHub raw URL for the default config template
"""

import argparse
import json
import re
import shutil
import sys
import textwrap
import urllib.request
from pathlib import Path

# ──────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────

FLATPAK_CONFIG_PATH = Path.home() / ".var" / "app" / "com.valvesoftware.Steam" / ".config" / "SLSsteam" / "config.yaml"
NATIVE_CONFIG_PATH = Path.home() / ".config" / "SLSsteam" / "config.yaml"
DEFAULT_CONFIG_PATH = FLATPAK_CONFIG_PATH if FLATPAK_CONFIG_PATH.exists() else NATIVE_CONFIG_PATH

# The C++ source file that contains the YAML default template as a raw string literal.
# Fetching this directly always gives us the latest upstream template.
TEMPLATE_SOURCE_URL = (
    "https://raw.githubusercontent.com/AceSLS/SLSsteam/main/src/config_default.hpp"
)

# ──────────────────────────────────────────────────────────────
# Colours for terminal output
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
# Formatting helpers
# ──────────────────────────────────────────────────────────────

def normalize_comment_spacing(raw: str) -> str:
    """
    Normalize whitespace between a value and its trailing inline comment.

    Input:  '480   # Webfishing'      or  '237990 # The Banner Saga'
    Output: '480 # Webfishing'            '237990 # The Banner Saga'

    Quoted strings are left untouched.
    """
    s = raw.strip()
    if not s:
        return s
    # Don't touch quoted values
    if s.startswith('"') or s.startswith("'"):
        return s
    # Match: <non-space-value>  <optional spaces>  # comment
    m = re.match(r'^(\S+)\s+(#.*)$', s)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return s


def bare_value(raw: str) -> str:
    """
    Return only the value part (no inline comment) for deduplication purposes.
    e.g.  '237990 # The Banner Saga'  →  '237990'
          '480 # Webfishing'           →  '480'
          '"some text"'                →  '"some text"'
    """
    s = raw.strip()
    if s.startswith('"') or s.startswith("'"):
        return s
    m = re.match(r'^(\S+)\s*(?:#.*)?$', s)
    return m.group(1) if m else s


def sanitize_title(title: str) -> str:
    """
    Sanitize the Title/String value to ensure it is properly quoted.
    If it is unclosed or malformed (like `Title: " ;`), we fix it.
    """
    title = title.strip()
    if not title:
        return '""'
    
    # Check if it is correctly wrapped in double quotes
    if title.startswith('"') and title.endswith('"') and len(title) >= 2:
        return title
        
    # Check if it is correctly wrapped in single quotes
    if title.startswith("'") and title.endswith("'") and len(title) >= 2:
        return title
        
    # Strip any leading/trailing quotes and clean
    cleaned = title.strip('"').strip("'").strip()
    
    # If the cleaned title is empty or just stray characters (like C++ comment/semicolon remnants), return ""
    if cleaned in ("", ";"):
        return '""'
        
    # Otherwise wrap in double quotes
    return f'"{cleaned}"'


def sanitize_boolean(val: str, default: str) -> str:
    """Normalize boolean strings to 'yes' or 'no'."""
    if not val:
        return default
    v = val.strip().lower()
    if v in ("yes", "true", "1", "y", "on"):
        return "yes"
    if v in ("no", "false", "0", "n", "off"):
        return "no"
    return default


def sanitize_log_level(val: str, default: str) -> str:
    """LogLevel must be an integer between 0 and 6 inclusive."""
    if not val:
        return default
    v = val.strip()
    if v.isdigit() and 0 <= int(v) <= 6:
        return v
    return default


def sanitize_wallet_balance(val: str, default: str) -> str:
    """FakeWalletBalance must be a non-negative integer."""
    if not val:
        return default
    v = val.strip()
    if v.isdigit():
        return v
    return default


# ──────────────────────────────────────────────────────────────
# Fetch + extract the template YAML from GitHub
# ──────────────────────────────────────────────────────────────

def fetch_template(url: str) -> str:
    """
    Download config_default.hpp and extract the YAML string from between the
    R\"(  )\" raw-string delimiters in the C++ source.
    """
    info(f"Fetching latest template from:\n        {url}")
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            source = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        error(f"Failed to download template: {exc}")
        sys.exit(1)

    # static const char* defaultConfig = R"( ... )";
    match = re.search(r'= R"\((.+?)\)";', source, re.DOTALL)
    if not match:
        error("Could not find the YAML template inside config_default.hpp!")
        error("The upstream file format may have changed. Check the URL.")
        sys.exit(1)

    yaml_text = match.group(1)
    ok("Template fetched successfully.")
    return yaml_text


# ──────────────────────────────────────────────────────────────
# Config parser  (zero external dependencies)
# ──────────────────────────────────────────────────────────────

class ConfigEntry:
    """
    Stores one parsed entry from a raw list or mapping line,
    keeping the full text including any inline comment.

    Examples:
      list item:    '  - 237990 # The Banner Saga'
        → raw_value = '237990 # The Banner Saga'
        → key        = None

      map item:     '  3146520: 480   # Webfishing'
        → raw_value = '480 # Webfishing'  (normalized spacing)
        → key        = '3146520'
    """
    __slots__ = ("key", "raw_value")

    def __init__(self, key, raw_value: str):
        self.key = key
        self.raw_value = normalize_comment_spacing(raw_value)

    @property
    def dedup_key(self) -> str:
        """Value without comment, used for list deduplication."""
        return bare_value(self.raw_value)


class SimpleYAMLReader:
    """
    Parses the specific structure that SLSsteam's config.yaml uses.

    Returns a dict mapping top-level key names to one of:
      None                – empty key (no children)
      str                 – scalar  (DisableFamilyShareLock, LogLevel, …)
      list[ConfigEntry]   – list    (AppIds, AdditionalApps, FakeOffline)
      dict[str, ConfigEntry] – map  (FakeAppIds, AppTokens, GameTitles, …)
      dict[str, list[ConfigEntry]] – map-of-lists  (DenuvoGames)
      dict                – IdleStatus sub-map with plain str values
    """

    # ── Top-level parse ──────────────────────────────────────────
    def parse(self, text: str) -> dict:
        lines = text.splitlines()
        data: dict = {}
        i = 0
        n = len(lines)

        while i < n:
            line = lines[i]
            stripped = line.strip()

            if not stripped or stripped.startswith("#"):
                i += 1
                continue

            m = re.match(r'^([A-Za-z][A-Za-z0-9_]*):\s*(.*)', line)
            if not m:
                i += 1
                continue

            key   = m.group(1)
            rest  = m.group(2).strip()

            # Strip inline comment from scalar values only
            # (not from list/map children — those we handle separately)
            rest_clean = self._strip_scalar_comment(rest)

            if rest_clean:
                # Scalar on the same line
                if key in LIST_KEYS:
                    data[key] = self._parse_scalar_as_list(rest)
                elif key in MAP_KEYS or key in MAP_OF_LIST_KEYS:
                    data[key] = self._parse_scalar_as_map(key, rest)
                elif key in (IDLE_STATUS_KEY, "UnownedStatus"):
                    data[key] = self._parse_scalar_as_submap(rest)
                else:
                    data[key] = rest_clean
                i += 1
            else:
                # Collect child lines.
                # Stop at blank lines, or when encountering a known top-level key.
                i += 1
                children = []
                while i < n:
                    child = lines[i]
                    cs = child.strip()
                    # Blank line → end of this section's children
                    if not cs:
                        break
                    # Comment-only line inside children → skip but keep going
                    if cs.startswith("#"):
                        i += 1
                        continue

                    # Check if the line is a known top-level key
                    m_top = re.match(r'^([A-Za-z][A-Za-z0-9_]*):\s*(.*)', child)
                    if m_top and m_top.group(1) in KNOWN_KEYS:
                        # Valid next top-level key, stop collecting children
                        break

                    # Collect this line as a child (indented or not, we will filter out junk in _parse_children)
                    children.append(child)
                    i += 1

                if not children:
                    data[key] = None
                else:
                    data[key] = self._parse_children(key, children)

        return data

    # ── Scalar helpers ───────────────────────────────────────────
    def _strip_scalar_comment(self, s: str) -> str:
        """Strip trailing # comment from a scalar, respecting quotes."""
        if not s:
            return s
        if s.startswith('"') or s.startswith("'"):
            return s
        idx = s.find(" #")
        if idx >= 0:
            return s[:idx].strip()
        return s.strip()

    # ── Child block parse ────────────────────────────────────────
    def _parse_children(self, parent_key: str, lines: list) -> object:
        """
        Detect whether children form a list, a mapping, or a mapping-of-lists,
        and return the appropriate structure with full raw values preserved.
        """
        stripped = [l.strip() for l in lines]

        # ── List Keys ───────────────────────────────────────────
        if parent_key in LIST_KEYS:
            result_list = []
            for s in stripped:
                if s.startswith("- ") or s == "-":
                    val = s[2:].strip() if s.startswith("- ") else ""
                else:
                    val = s.strip()
                if not val:
                    continue
                # Enforce rule: item must start with a positive integer, optional comment
                m = re.match(r'^([0-9]+)\s*(#.*)?$', val)
                if m:
                    num = m.group(1)
                    comment = m.group(2)
                    raw_val = f"{num} {comment.strip()}" if comment else num
                    result_list.append(ConfigEntry(None, raw_val))
            return result_list

        # ── IdleStatus / UnownedStatus ──────────────────────────
        if parent_key in (IDLE_STATUS_KEY, "UnownedStatus"):
            result = {}
            for s in stripped:
                mm = re.match(r'^([A-Za-z][A-Za-z0-9_]*):\s*(.*)', s)
                if mm:
                    k = mm.group(1)
                    val = mm.group(2).strip()
                    if k == "AppId":
                        if val.isdigit():
                            result[k] = val
                        else:
                            result[k] = "0"
                    elif k == "Title":
                        result[k] = sanitize_title(val)
            if "AppId" not in result:
                result["AppId"] = "0"
            if "Title" not in result:
                result["Title"] = '""'
            return result

        # ── Mapping (k: v) or mapping-of-lists ──────────────────
        result_map: dict = {}
        i = 0
        while i < len(lines):
            s = stripped[i]
            mm = re.match(r'^([^:]+):\s*(.*)', s)
            if not mm:
                i += 1
                continue

            k   = mm.group(1).strip()
            val = mm.group(2).strip()

            # For mapping keys or mapping-of-lists keys, the keys must be numeric digits (AppIds/SteamIds)
            if not k.isdigit():
                i += 1
                continue

            if val:
                # k: val
                cleaned_val = ""
                m = re.match(r'^([^#\s]+)\s*(#.*)?$', val)
                if m:
                    v_part = m.group(1).strip()
                    comment = m.group(2)
                    
                    if parent_key in ("AppTokens", "FakeAppIds", "SubscriptionTimestamps"):
                        if v_part.isdigit():
                            cleaned_val = f"{v_part} {comment.strip()}" if comment else v_part
                    elif parent_key == "GameTitles":
                        v_quoted = sanitize_title(v_part)
                        cleaned_val = f"{v_quoted} {comment.strip()}" if comment else v_quoted
                    else:
                        cleaned_val = val
                
                if cleaned_val:
                    result_map[k] = ConfigEntry(k, cleaned_val)
                i += 1
            else:
                # k: (null) — look ahead for sub-list items
                i += 1
                sub = []
                while i < len(lines):
                    ss = stripped[i]
                    if ss.startswith("- "):
                        sub_val = ss[2:].strip()
                        m_sub = re.match(r'^([0-9]+)\s*(#.*)?$', sub_val)
                        if m_sub:
                            num = m_sub.group(1)
                            comment = m_sub.group(2)
                            raw_sub = f"{num} {comment.strip()}" if comment else num
                            sub.append(ConfigEntry(None, raw_sub))
                        i += 1
                    else:
                        break
                result_map[k] = sub if sub else None

        return result_map

    def _parse_scalar_as_list(self, val: str) -> list:
        val = val.strip()
        if not val:
            return []
        if val.startswith("[") and val.endswith("]"):
            val = val[1:-1].strip()
        parts = [p.strip() for p in val.split(",")]
        result = []
        for part in parts:
            if not part:
                continue
            m = re.match(r'^([0-9]+)\s*(#.*)?$', part)
            if m:
                num = m.group(1)
                comment = m.group(2)
                raw_val = f"{num} {comment.strip()}" if comment else num
                result.append(ConfigEntry(None, raw_val))
        return result

    def _parse_scalar_as_map(self, parent_key: str, val: str) -> dict:
        val = val.strip()
        if not val:
            return {}
        if val.startswith("{") and val.endswith("}"):
            val = val[1:-1].strip()
        parts = [p.strip() for p in val.split(",")]
        result = {}
        for part in parts:
            mm = re.match(r'^([^:]+):\s*(.*)', part)
            if mm:
                k = mm.group(1).strip()
                v = mm.group(2).strip()
                if k.isdigit():
                    cleaned_val = ""
                    m = re.match(r'^([^#\s]+)\s*(#.*)?$', v)
                    if m:
                        v_part = m.group(1).strip()
                        comment = m.group(2)
                        if parent_key in ("AppTokens", "FakeAppIds", "SubscriptionTimestamps"):
                            if v_part.isdigit():
                                cleaned_val = f"{v_part} {comment.strip()}" if comment else v_part
                        elif parent_key == "GameTitles":
                            v_quoted = sanitize_title(v_part)
                            cleaned_val = f"{v_quoted} {comment.strip()}" if comment else v_quoted
                        else:
                            cleaned_val = v
                    if cleaned_val:
                        result[k] = ConfigEntry(k, cleaned_val)
        return result

    def _parse_scalar_as_submap(self, val: str) -> dict:
        val = val.strip()
        result = {}
        m_appid = re.search(r'AppId:\s*([0-9]+)', val)
        if m_appid:
            result["AppId"] = m_appid.group(1)
        m_title = re.search(r'Title:\s*(.*)', val)
        if m_title:
            result["Title"] = sanitize_title(m_title.group(1))
        if "AppId" not in result:
            result["AppId"] = "0"
        if "Title" not in result:
            result["Title"] = '""'
        return result


# ──────────────────────────────────────────────────────────────
# Validation
# ──────────────────────────────────────────────────────────────


def validate_config(config_path: Path) -> list:
    """
    Scan the config for common formatting issues.
    Returns a list of human-readable warning strings.
    """
    issues = []
    try:
        text = config_path.read_text(encoding="utf-8")
    except Exception as exc:
        return [f"Cannot read file: {exc}"]

    lines = text.splitlines()

    for lineno, raw in enumerate(lines, 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if "\t" in raw:
            issues.append(f"Line {lineno}: Contains TAB character (YAML requires spaces)")

        if raw.endswith("\r"):
            issues.append(f"Line {lineno}: Windows-style CRLF line ending")

        if re.match(r'^[A-Za-z][A-Za-z0-9_]*:[^ \n]', raw):
            issues.append(f"Line {lineno}: Missing space after colon  →  '{raw.rstrip()}'")

        if raw != raw.rstrip():
            issues.append(f"Line {lineno}: Trailing whitespace")

        if re.match(r'^\s+-[^ ]', raw) and not re.match(r'^\s+- ', raw):
            issues.append(f"Line {lineno}: List item missing space after dash  →  '{raw.rstrip()}'")

        # Flag unexpected top-level keys (not indented, not a comment)
        m = re.match(r'^([A-Za-z][A-Za-z0-9_]*):', raw)
        if m and m.group(1) not in KNOWN_KEYS:
            issues.append(
                f"Line {lineno}: Unknown/unexpected top-level key '{m.group(1)}' "
                f"— may be a misplaced line or typo (will be dropped)"
            )

    # Duplicate mapping keys (numeric, e.g. AppTokens)
    seen_keys: dict = {}
    for lineno, raw in enumerate(lines, 1):
        m = re.match(r'^(\s*)([0-9]+):\s+', raw)
        if m:
            indent = len(m.group(1))
            key    = m.group(2)
            entry  = (indent, key)
            if entry in seen_keys:
                issues.append(
                    f"Line {lineno}: Duplicate mapping key '{key}' "
                    f"(first seen at line {seen_keys[entry]})"
                )
            else:
                seen_keys[entry] = lineno

    return issues


# ──────────────────────────────────────────────────────────────
# Output formatters
# ──────────────────────────────────────────────────────────────

def fmt_list(entries: list, indent: int = 2) -> str:
    """
    Format a list of ConfigEntry objects.
    Output:
      - 237990 # The Banner Saga
      - 312520 # Rain World
      - 333640
    """
    pad = " " * indent
    lines = []
    for entry in entries:
        lines.append(f"{pad}- {entry.raw_value}")
    return "\n".join(lines)


def fmt_map(entries: dict, indent: int = 2) -> str:
    """
    Format a dict of {key: ConfigEntry} objects.
    Output:
      1274570: 480
      3146520: 480 # Webfishing
      227300: 7693758108019961089
    """
    pad = " " * indent
    lines = []
    for k, v in entries.items():
        if v is None:
            lines.append(f"{pad}{k}:")
        elif isinstance(v, list):
            # map-of-lists sub-entry (DlcData style)
            lines.append(f"{pad}{k}:")
            for item in v:
                lines.append(f"{pad}  - {item.raw_value}")
        else:
            lines.append(f"{pad}{k}: {v.raw_value}")
    return "\n".join(lines)


def fmt_map_of_lists(entries: dict, indent: int = 2) -> str:
    """
    Format DenuvoGames-style dict of {key: list[ConfigEntry]}.
    Output:
      SteamId:
        - AppId1
        - AppId2
    """
    pad = " " * indent
    lines = []
    for k, v in entries.items():
        lines.append(f"{pad}{k}:")
        if isinstance(v, list):
            for item in v:
                lines.append(f"{pad}  - {item.raw_value}")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# Deduplication helpers
# ──────────────────────────────────────────────────────────────

def dedup_list(entries: list) -> list:
    """Remove duplicate list entries, preserving order and keeping comments."""
    seen = set()
    result = []
    for entry in entries:
        key = entry.dedup_key
        if key not in seen:
            seen.add(key)
            result.append(entry)
    return result


def dedup_map(entries: dict) -> dict:
    """
    Remove duplicate map keys. Last occurrence wins (so the most recent
    copy is kept). Returns an OrderedDict-like plain dict (Python 3.7+
    preserves insertion order) with the first-seen key positions kept,
    but values replaced by the last seen.
    """
    # Track first-seen order
    ordered = {}
    for k, v in entries.items():
        if k not in ordered:
            ordered[k] = v
        else:
            # overwrite with last value (keeps key in original position)
            ordered[k] = v
    return ordered


# ──────────────────────────────────────────────────────────────
# Merge old config values into fresh template
# ──────────────────────────────────────────────────────────────

# Which top-level keys hold scalar values
SCALAR_KEYS = {
    "DisableFamilyShareLock", "UseWhitelist", "AutoFilterList",
    "PlayNotOwnedGames", "SafeMode", "Notifications", "WarnHashMissmatch",
    "NotifyInit", "API", "DisableCloud", "FakeEmail", "FakeWalletBalance",
    "LogLevel", "ExtendedLogging",
}
# Which keys hold YAML lists (  - value)
LIST_KEYS = {"AppIds", "AdditionalApps", "FakeOffline"}
# Which keys hold simple mappings (  key: value)
MAP_KEYS = {"AppTokens", "FakeAppIds", "GameTitles", "SubscriptionTimestamps", "DlcData"}
# Which keys hold mapping-of-lists (  key:\n    - item)
MAP_OF_LIST_KEYS = {"DenuvoGames"}
# Special sub-map
IDLE_STATUS_KEY = "IdleStatus"

# All known top-level keys — used by validator to flag unexpected keys
KNOWN_KEYS = (
    SCALAR_KEYS
    | LIST_KEYS
    | MAP_KEYS
    | MAP_OF_LIST_KEYS
    | {IDLE_STATUS_KEY, "UnownedStatus"}
)

def _skip_template_children(template_lines: list, start: int) -> int:
    """Skip over indented child lines in the template (we replace them with user data)."""
    i = start
    n = len(template_lines)
    while i < n:
        child = template_lines[i]
        cs = child.strip()
        # Blank line → end of children block; keep this line (it's a spacer)
        if not cs:
            break
        # Comment → end of children block (next section starts)
        if cs.startswith("#"):
            break
        if child.startswith("  ") or child.startswith("\t"):
            i += 1
        else:
            break
    return i


def merge_config(template_yaml: str, old_data: dict) -> str:
    """
    Walk the template line-by-line, injecting the user's values wherever
    a known key appears. Template comments and structure are preserved.
    """
    out_lines = []
    template_lines = template_yaml.splitlines()
    i = 0
    n = len(template_lines)

    while i < n:
        line = template_lines[i]
        stripped = line.strip()

        # ── Blank line or comment → pass through unchanged ───────
        if not stripped or stripped.startswith("#"):
            out_lines.append(line)
            i += 1
            continue

        # ── Match top-level key ──────────────────────────────────
        m = re.match(r'^([A-Za-z][A-Za-z0-9_]*):\s*(.*)', line)
        if not m:
            out_lines.append(line)
            i += 1
            continue

        key         = m.group(1)
        default_val = m.group(2).strip()

        # ────────────────────────────────────────────────────────
        # SCALAR: DisableFamilyShareLock, LogLevel, FakeEmail, etc.
        # ────────────────────────────────────────────────────────
        if key in SCALAR_KEYS:
            user_val = old_data.get(key)
            val_to_write = user_val if (user_val is not None and isinstance(user_val, str)) else default_val
            
            # Map scalar values to ensure they comply with SLSsteam requirements
            # All boolean keys are normalized to 'yes' or 'no'
            BOOLEAN_KEYS = {
                "DisableFamilyShareLock", "UseWhitelist", "AutoFilterList",
                "PlayNotOwnedGames", "SafeMode", "Notifications", "WarnHashMissmatch",
                "NotifyInit", "API", "DisableCloud", "ExtendedLogging",
            }
            if key in BOOLEAN_KEYS:
                val_to_write = sanitize_boolean(val_to_write, default_val)
            elif key == "LogLevel":
                val_to_write = sanitize_log_level(val_to_write, default_val)
            elif key == "FakeWalletBalance":
                val_to_write = sanitize_wallet_balance(val_to_write, default_val)
            elif key == "FakeEmail":
                # Clean and wrap email in quotes if it's set and not yet quoted
                if not val_to_write or val_to_write == '""':
                    val_to_write = '""'
                elif not (val_to_write.startswith('"') or val_to_write.startswith("'")):
                    val_to_write = f'"{val_to_write.strip()}"'
                    
            out_lines.append(f"{key}: {val_to_write}")
            i += 1

        # ────────────────────────────────────────────────────────
        # LIST: AppIds, AdditionalApps, FakeOffline
        # Output format:
        #   AdditionalApps:
        #     - 237990 # The Banner Saga
        #     - 312520 # Rain World
        # ────────────────────────────────────────────────────────
        elif key in LIST_KEYS:
            out_lines.append(f"{key}:")
            user_val = old_data.get(key)
            if user_val and isinstance(user_val, list):
                clean = dedup_list(user_val)
                if clean:
                    out_lines.append(fmt_list(clean))
            i += 1
            i = _skip_template_children(template_lines, i)

        # ────────────────────────────────────────────────────────
        # MAP: AppTokens, FakeAppIds, GameTitles, etc.
        # Output format:
        #   FakeAppIds:
        #     1274570: 480
        #     3146520: 480 # Webfishing
        # ────────────────────────────────────────────────────────
        elif key in MAP_KEYS:
            out_lines.append(f"{key}:")
            user_val = old_data.get(key)
            if user_val and isinstance(user_val, dict):
                clean = dedup_map(user_val)
                if clean:
                    out_lines.append(fmt_map(clean))
            i += 1
            i = _skip_template_children(template_lines, i)

        # ────────────────────────────────────────────────────────
        # MAP-OF-LISTS: DenuvoGames
        # Output format:
        #   DenuvoGames:
        #     SteamId:
        #       - AppId1
        # ────────────────────────────────────────────────────────
        elif key in MAP_OF_LIST_KEYS:
            out_lines.append(f"{key}:")
            user_val = old_data.get(key)
            if user_val and isinstance(user_val, dict):
                out_lines.append(fmt_map_of_lists(user_val))
            i += 1
            i = _skip_template_children(template_lines, i)

        # ────────────────────────────────────────────────────────
        # IdleStatus / UnownedStatus (structured sub-map)
        # Output format:
        #   IdleStatus:
        #     AppId: 0
        #     Title: ""
        # ────────────────────────────────────────────────────────
        elif key in (IDLE_STATUS_KEY, "UnownedStatus"):
            out_lines.append(f"{key}:")
            user_val = old_data.get(key)
            if isinstance(user_val, dict):
                app_id = user_val.get("AppId", "0")
                title  = user_val.get("Title", '""')
            else:
                app_id = "0"
                title  = '""'
            # Ensure title is properly sanitized and wrapped in quotes
            title = sanitize_title(title)
            out_lines.append(f"  AppId: {app_id}")
            out_lines.append(f"  Title: {title}")
            i += 1
            i = _skip_template_children(template_lines, i)

        # ────────────────────────────────────────────────────────
        # Unknown key → pass through unchanged
        # ────────────────────────────────────────────────────────
        else:
            out_lines.append(line)
            i += 1

    # Single trailing newline
    result = "\n".join(out_lines)
    result = result.rstrip("\n") + "\n"
    return result


def resolve_missing_names(old_data: dict):
    """
    Query steamcmd API to fill in missing game names/comments for:
      - AdditionalApps (list items)
      - FakeAppIds (mapping values)
    """
    additional_apps = old_data.get("AdditionalApps")
    fake_app_ids = old_data.get("FakeAppIds")

    # Quick check: is there anything missing comments?
    missing_additional = []
    if additional_apps and isinstance(additional_apps, list):
        for entry in additional_apps:
            if "#" not in entry.raw_value:
                missing_additional.append(entry)

    missing_fake = []
    if fake_app_ids and isinstance(fake_app_ids, dict):
        for k, entry in fake_app_ids.items():
            if entry and "#" not in entry.raw_value:
                missing_fake.append((k, entry))

    if not missing_additional and not missing_fake:
        return

    info("Resolving missing game names via SteamCMD API...")
    cache = {"480": "Spacewar"}

    def get_name(appid: str) -> str:
        if not appid:
            return ""
        if appid in cache:
            return cache[appid]
        url = f"https://api.steamcmd.net/v1/info/{appid}"
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=4) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                if res.get("status") == "success":
                    app_info = res.get("data", {}).get(appid, {})
                    name = app_info.get("common", {}).get("name")
                    if not name:
                        name = app_info.get("name")
                    if name:
                        cache[appid] = name
                        return name
        except Exception:
            pass
        cache[appid] = ""
        return ""

    # Resolve for AdditionalApps
    for entry in missing_additional:
        appid = entry.dedup_key
        if appid.isdigit():
            name = get_name(appid)
            if name:
                entry.raw_value = f"{appid} # {name}"
                info(f"  Resolved AdditionalApps appid {appid} -> {name}")

    # Resolve for FakeAppIds
    for src_appid, entry in missing_fake:
        tgt_appid = entry.dedup_key
        if src_appid.isdigit() and tgt_appid.isdigit():
            src_name = get_name(src_appid)
            tgt_name = get_name(tgt_appid)
            if src_name:
                if tgt_name:
                    entry.raw_value = f"{tgt_appid} # {src_name} -> {tgt_name}"
                else:
                    entry.raw_value = f"{tgt_appid} # {src_name}"
                info(f"  Resolved FakeAppIds {src_appid}:{tgt_appid} -> {src_name}")


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="SLSsteam config.yaml cleanup & update tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              Validate your current config for errors:
                python3 slssteam_config.py --validate-only

              Preview the cleaned & merged output (nothing is written):
                python3 slssteam_config.py --dry-run

              Update in place (auto-backup created):
                python3 slssteam_config.py

              Write to a different output file:
                python3 slssteam_config.py --output /tmp/config_new.yaml
        """),
    )
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH,
        help=f"Path to your config.yaml (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Output path (default: overwrites --config after backup)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print merged config to stdout, don't write to disk",
    )
    parser.add_argument(
        "--no-backup", action="store_true",
        help="Skip creating a .bak backup before writing",
    )
    parser.add_argument(
        "--validate-only", action="store_true",
        help="Only validate your existing config, don't merge or write",
    )
    parser.add_argument(
        "--template-url", default=TEMPLATE_SOURCE_URL,
        help="Override the GitHub URL for the default config template",
    )

    args = parser.parse_args()
    config_path: Path = args.config

    print()
    print(f"{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}   SLSsteam Config Cleanup & Update Tool{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")
    print()

    # Detect if we should run interactively
    cli_flags = ["--config", "--output", "--dry-run", "--no-backup", "--validate-only"]
    interactive = not any(flag in sys.argv for flag in cli_flags)

    choice = 1
    config_path = args.config

    if interactive:
        print("Please select an option:")
        print("  1) Clean & Update Config (normal flow)")
        print("  2) Restore Backup (from config.bck)")
        print("  3) Apply Recommended Settings for Steam Deck (SafeMode, Notifications, LogLevel: 2, etc.)")
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
            print("Invalid choice. Please enter a number between 1 and 3.")
        print()

    # If Option 2 (Restore Backup)
    if interactive and choice == 2:
        print(f"Default config location is: {DEFAULT_CONFIG_PATH}")
        while True:
            try:
                user_input = input(f"Enter path to your config.yaml (press Enter for default): ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nCancelled.")
                sys.exit(0)
            if not user_input:
                config_path = DEFAULT_CONFIG_PATH
            else:
                config_path = Path(user_input).expanduser().resolve()
            
            bak_path = config_path.with_suffix(".bck")
            if bak_path.exists():
                break
            else:
                error(f"Backup file not found: {bak_path}. Please try again.")
                print()
        
        info(f"Restoring backup from {bak_path} to {config_path}...")
        try:
            shutil.copy2(bak_path, config_path)
            ok("Backup successfully restored.")
        except Exception as exc:
            error(f"Failed to restore backup: {exc}")
            sys.exit(1)
        print()
        return

    # If Option 1 or 3 (or CLI non-interactive flow)
    if interactive:
        print(f"Default config location is: {DEFAULT_CONFIG_PATH}")
        while True:
            try:
                user_input = input(f"Enter path to your config.yaml (press Enter for default): ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nCancelled.")
                sys.exit(0)
            
            if not user_input:
                config_path = DEFAULT_CONFIG_PATH
            else:
                config_path = Path(user_input).expanduser().resolve()
            
            if config_path.exists():
                break
            else:
                error(f"Config file not found: {config_path}. Please try again.")
                print()
    else:
        # If --config was passed on command line, verify it exists once
        if not config_path.exists():
            error(f"Config file not found: {config_path}")
            error("Make sure SLSsteam has been run at least once.")
            sys.exit(1)

    info(f"Config file: {config_path}")
    print()

    # ── 1. Validate ──────────────────────────────────────────────
    print(f"{BOLD}[1/4] Validating existing config...{RESET}")
    issues = validate_config(config_path)
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
    print(f"{BOLD}[2/4] Parsing existing config...{RESET}")
    config_text = config_path.read_text(encoding="utf-8")
    reader = SimpleYAMLReader()
    old_data = reader.parse(config_text)

    # Pretty-print a summary of what was found
    for section, val in old_data.items():
        if val is None:
            ok(f"  {section}: (empty)")
        elif isinstance(val, str):
            ok(f"  {section}: {val}")
        elif isinstance(val, list):
            ok(f"  {section}: {len(val)} entries")
        elif isinstance(val, dict):
            ok(f"  {section}: {len(val)} entries")
    # ── 2.5 Resolve missing names ────────────────────────────────
    resolve_missing_names(old_data)
    print()

    # ── 2.8 Apply overrides (Steam Deck presets) ─────────────────
    if interactive and choice == 3:
        info("Applying Steam Deck recommended overrides:")
        info("  SafeMode: yes")
        info("  Notifications: yes")
        info("  LogLevel: 2")
        info("  ExtendedLogging: no")
        old_data["SafeMode"] = "yes"
        old_data["Notifications"] = "yes"
        old_data["LogLevel"] = "2"
        old_data["ExtendedLogging"] = "no"
        print()

    # ── 3. Fetch template from GitHub ────────────────────────────
    print(f"{BOLD}[3/4] Fetching latest template from GitHub...{RESET}")
    template_yaml = fetch_template(args.template_url)

    # Report new / removed keys vs your existing config
    template_data = reader.parse(template_yaml)
    new_keys     = set(template_data.keys()) - set(old_data.keys())
    removed_keys = set(old_data.keys())      - set(template_data.keys())

    if new_keys:
        info(f"  New upstream key(s) — will use defaults: {', '.join(sorted(new_keys))}")
    if removed_keys:
        warn(f"  Key(s) in your config not in template (dropped): {', '.join(sorted(removed_keys))}")
    print()

    # ── 4. Merge ─────────────────────────────────────────────────
    print(f"{BOLD}[4/4] Merging your values into new template...{RESET}")
    merged = merge_config(template_yaml, old_data)

    # Spot-check deduplication counts
    for key in list(MAP_KEYS) + list(LIST_KEYS):
        old_val = old_data.get(key)
        if isinstance(old_val, dict):
            before = len(old_val)
            after  = len(dedup_map(old_val))
            if before != after:
                info(f"  {key}: deduplicated {before} → {after} entries")
        elif isinstance(old_val, list):
            before = len(old_val)
            after  = len(dedup_list(old_val))
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

    out_path: Path = args.output if args.output else config_path

    if not args.no_backup:
        bak_path = config_path.with_suffix(".bck")
        shutil.copy2(config_path, bak_path)
        ok(f"Backup created: {bak_path}")

    out_path.write_text(merged, encoding="utf-8")
    ok(f"Config written:  {out_path}")
    print()

    if issues:
        print(f"{YELLOW}Fixed {len(issues)} formatting issue(s) from your old config.{RESET}")
    if new_keys:
        print(f"{CYAN}Added {len(new_keys)} new upstream key(s) with default values.{RESET}")
    if removed_keys:
        print(f"{YELLOW}Dropped {len(removed_keys)} key(s) that are no longer in the template.{RESET}")

    print()
    print(f"{GREEN}{BOLD}Done! Your config.yaml has been cleaned up and updated.{RESET}")
    print()


if __name__ == "__main__":
    main()
