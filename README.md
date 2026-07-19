# kindertales-scraper

`kindertales-scraper` creates a portable SQLite/JSON/media archive of photos and
videos from every child linked to a Kindertales family account. Daily-report
activities are retained even when they have no media, including visible care
details and non-secret form values. Activity dates, per-media captions, authors,
source identifiers, and provenance are retained with each medium.

Kindertales' public terms prohibit automated downloading. Get written
authorization from Kindertales before using this program. Authorization should
cover the family account, its linked children, session reuse, and the configured
request rate. See the [Kindertales Terms of Service](https://www.kindertales.com/terms-of-service/).

The scraper also snapshots baby bulletins, immunization, medication, milestone,
and profile/document pages for each child. Attendance is extracted from bounded,
child-linked news-feed events, including check-in and check-out. Enrollment is
read from the application's structured active-forms endpoint and retains only
completed values, not unused controls or pull-down choices. Linked standalone
documents are not yet downloaded.

Message-folder listings and the billing dashboard are separately opt-in because
they can contain private correspondence and financial information. Set
`messages = true` and/or `billing = true` under `[exports]` only when those
areas are covered by the written authorization and should be included in this
archive. The message snapshots cover inbox, sent, draft, scheduled, and contact
listings; they don't yet follow individual messages or download attachments.

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
it.

Discover the linked Kindertales center IDs and configure metadata defaults
interactively:

```console
kindertales-scraper configure-centers --headed
```

The command lists each center with its linked children, then prompts for
latitude, longitude, IANA timezone, and horizontal GPS uncertainty in meters.
It updates `config.toml` while retaining existing comments and formatting.
Latitude and longitude must always be entered together.

Values under `[metadata.centers."CENTER-ID"]` take precedence. Any missing
coordinate pair, timezone, or uncertainty inherits independently from
`[metadata.defaults.center]`. This lets the default table describe the common
daycare location while a specific center overrides only what differs. The
uncertainty is embedded as `GPSHPositioningError`; it represents a horizontal
radius in meters, not a claim that the photo was taken at the exact coordinate.

Archive names default to
`{timestamp:%Y%m%d_%H%M%S}_{sequence:02d}{extension}`. The timestamp prefers
authentic capture metadata and falls back to the activity time; `sequence` is
the one-based collision number. `folder_format = "{child_name}"` creates a
stable child directory; `folder_frequency` then appends `none`, daily, monthly,
or yearly calendar grouping beneath it. Metadata sidecars are exceptional: one
is written only when enrichment would overwrite meaningful original metadata.
It contains only the portable pre-edit ExifTool metadata. Set
`sidecar_layout = "parallel"` to mirror those exceptional files under a sibling
`sidecars` directory instead of placing them beside media. Folder and filename
templates may use `timestamp`,
`sequence`, `extension`, `child_name`, `child_id`, `activity_type`,
`activity_id`, `media_id`, `original_name`, and `original_stem`.

## Commands

```console
# Inspect a bounded run without changing the archive.
kindertales-scraper sync --from 2026-07-01 --through 2026-07-02 --dry-run --headed

# Synchronize the same authorized bounded range.
kindertales-scraper sync --from 2026-07-01 --through 2026-07-02 --headed

# Check SQLite integrity, files, hashes, sidecars, and embedded metadata.
kindertales-scraper verify

# Remove the account password, encryption key, and cached browser state.
kindertales-scraper credentials delete
```

If `--through` is omitted, synchronization runs through the current local date.

The current legacy HTML adapter still requires `--from`. Later bounded runs use
the latest per-child activity timestamp with the configured seven-day overlap.
Archived files are never deleted.

## Archive and privacy

`index.sqlite3` uses a versioned schema with `children`, `activities`, `media`,
`activity_media`, and `sync_runs` tables. It retains source/final hashes,
redacted source URLs, scraped context, and the metadata fields selected for
embedding. A sidecar is created only to back up meaningful original metadata
that ExifTool will replace; it contains the original portable ExifTool object
and none of the scraper's metadata. Host-derived fields such as `SourceFile`,
`File:Directory`, local file timestamps, and permissions are omitted.

The archive contains sensitive information about children. It intentionally uses
ordinary destination filesystem permissions so it remains portable; securing
the destination, backups, and full-disk encryption is the operator's
responsibility. Logs and errors redact authorization headers, cookies, bearer
tokens, and all signed URL query values. Never commit `config.toml`, browser
state, live responses, or archive data.

`[exports].child_records` controls the child snapshots and defaults to `true`.

## Development

```console
tox
```

The test environment runs Python 3.13 tests with branch coverage, Ruff, and
strict mypy. Synthetic fixtures are the only Kindertales-shaped data committed
to the repository. See [docs/live-smoke-run.md](docs/live-smoke-run.md) for the
authorized live acceptance procedure.

This project is licensed under the Apache License 2.0.
