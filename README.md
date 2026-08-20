# ASSfixer
Fetches the latest default config template from the SLSsteam GitHub repo,
parases your existing config.yaml, and produces a perfectly-formatted output
that carries over ALL your personal values into the new template structure.
 
This tool is fully self-adapting: key types are inferred automatically from:
  1. Inline default values in the template (scalar detection)
  2. Commented-out examples in the template header (list vs map detection)
  3. The structure of your own config.yaml for any remaining unknowns
 
No manual updates are needed when the SLSsteam developer adds or removes keys.
 
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
 

## Direct Launch (One-liner)

Download and run the tool interactively:
```bash
curl -sSfLO https://raw.githubusercontent.com/niwia/ASSfixer/main/assfixer.py && chmod +x assfixer.py && ./assfixer.py
```

---

## Local Usage

Run the script directly:
```bash
python3 assfixer.py
```

### Options
```bash
# Validate your config for errors without changing anything
python3 assfixer.py --validate-only

# Preview clean output without writing to disk
python3 assfixer.py --dry-run

# Run on a custom config path
python3 assfixer.py --config /path/to/config.yaml --output /path/to/output.yaml
```
