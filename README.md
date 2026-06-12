# ASSfixer

An automated, smart `config.yaml` cleanup, validation, and migration tool for [SLSsteam](https://github.com/AceSLS/SLSsteam).

## Features
- **Strict Value Sanitization**: Automatically normalizes booleans (`true`/`on`/`1` to `yes`), checks `LogLevel` limits, and fixes malformed/unclosed quoted strings (like `Title: " ;` back to `Title: ""`).
- **YAML Structure Healing**: Automatically converts scalar values on single lines (e.g. `FakeOffline: 1274570`) into correct YAML lists or map structures.
- **Deduplication**: Merges duplicate list items and map keys (latest value wins).
- **GitHub Sync**: Fetches the latest default config template from the SLSsteam repository and updates your local config with new keys (preserving comments).
- **Game Name Resolver**: Queries the SteamCMD API to resolve names/comments for AppIds in `AdditionalApps` and `FakeAppIds` that do not have comments.

---

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
