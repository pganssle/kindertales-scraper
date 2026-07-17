# kindertales-scraper

`kindertales-scraper` creates a portable SQLite/JSON/media archive of photos and
videos from every child linked to a Kindertales family account. Activity dates,
captions, authors, source identifiers, and provenance are retained with each
medium.

Kindertales' public terms prohibit automated downloading. Get written
authorization from Kindertales before using this program. Authorization should
cover the family account, its linked children, session reuse, and the configured
request rate. See the [Kindertales Terms of Service](https://www.kindertales.com/terms-of-service/).

The scraper does not cover billing, forms, standalone documents, general
messages, attendance, or activities without media.

## Installation

The project requires Python 3.13 or newer. ExifTool is also required for media
metadata enrichment and verification.

```console
python3.13 -m pip install .
playwright install chromium
exiftool -ver
```

Copy `config.toml.example` to `config.toml`, or create the initial private file
interactively:

```console
kindertales-scraper configure --email parent@example.com
```

The account email remains in `config.toml`. The password and randomly generated
session-encryption key are stored by the operating-system keyring. If no keyring
is available, the password is prompted for each process and browser state stays
in memory. Setting `allow_plaintext_session_cache = true` explicitly permits a
mode-`0600` plaintext cache and prints a warning.

Request quotas are repeatable `{ count, window_seconds }` entries. The defaults
are 8 requests per second, 120 requests per minute, 8 total requests in flight,
and 2 media downloads. Only increase them when the written authorization allows
it. Center coordinates and timezones are keyed by Kindertales center ID; the
optional global coordinates are used only after a center-specific value.

## Commands

```console
# Inspect a bounded run without changing the archive.
kindertales-scraper sync --from 2026-07-01 --through 2026-07-02 --dry-run --headed

# Synchronize all exposed history, or resume with the configured overlap.
kindertales-scraper sync --headed

# Check SQLite integrity, files, hashes, sidecars, and embedded metadata.
kindertales-scraper verify

# Remove the account password, encryption key, and cached browser state.
kindertales-scraper credentials delete
```

The first run traverses all exposed media history unless date bounds are given.
Later runs use the latest per-child activity timestamp with the configured
seven-day overlap. Records missing during a complete initial traversal become
unavailable in the index; archived files are never deleted.

## Archive and privacy

`index.sqlite3` uses a versioned schema with `children`, `activities`, `media`,
`activity_media`, and `sync_runs` tables. Each enriched file has a versioned JSON
sidecar containing the source hash, final hash, redacted source URL, complete
pre-edit ExifTool output, scraped context, HTTP properties, and inference flags.
Sidecars are authoritative where a container cannot embed a field.

The archive contains sensitive information about children. It intentionally uses
ordinary destination filesystem permissions so it remains portable; securing
the destination, backups, and full-disk encryption is the operator's
responsibility. Logs and errors redact authorization headers, cookies, bearer
tokens, and all signed URL query values. Never commit `config.toml`, browser
state, live responses, or archive data.

## Development

```console
tox
```

The test environment runs Python 3.13 tests with branch coverage, Ruff, and
strict mypy. Synthetic fixtures are the only Kindertales-shaped data committed
to the repository. See [docs/live-smoke-run.md](docs/live-smoke-run.md) for the
authorized live acceptance procedure.

This project is licensed under the Apache License 2.0.
