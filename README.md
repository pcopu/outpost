# Outpost

A small CLI for marketing files and social/ad catalogs across multiple apps.
Each app has its own SQLite database, original files, provenance, snapshots,
and remote object prefix. Python 3.10+ on macOS or Linux; no runtime Python
dependencies, web UI, background processes, or hosted model calls.

Run from a checkout with `python3 -m outpost`. Installation is optional:
`python3 -m pip install .` provides the `outpost` command.
Use an explicit local `--root` or `OUTPOST_ROOT`; the source-checkout default
is `data/` next to this README. Never put this root on an object/network mount.

## Quick start

```sh
python3 -m outpost --root ./data app add example --name "Example App"
python3 -m outpost --root ./data asset import example ~/media/creative.mp4 --kind video --dry-run
python3 -m outpost --root ./data asset import example ~/media/creative.mp4 --kind video
python3 -m outpost --root ./data catalog query example vault_assets
python3 -m outpost --root ./data catalog export example --out ./example-catalog.json
```

App IDs are strict lowercase ASCII slugs, without normalization or silent
identifier collisions. Equal platform IDs and hashes can exist independently
in different apps. Registering the same ID with a different name fails.
File imports are addressed by SHA-256 and retain all source paths. They copy
originals and never move or rewrite them. On APFS, independent copy-on-write
clones reduce disk use. No hardlinks are used for originals.

## Catalog and legacy apparatus

The relational schema retains accounts, videos/posts, comments, organic metric
history, ad usage/metrics, imported assets, original file provenance, and
reviewed creative relationships. Campaign IDs/names stay on ad-usage rows.
There is no separate campaign framework. Existing TikTok column names remain
for compatibility; namespace account/video identifiers explicitly when using
several platforms in one app.

```sh
python3 -m outpost catalog record example accounts --json ./account.json
python3 -m outpost catalog query example videos --filter account_handle=creator --limit 25
```

Records are JSON objects using the columns in `outpost/schema.sql`. Include
the primary key (including all columns of a composite key); updates change
only supplied columns. Foreign keys and transactions are enforced. Record
paths must reference files inside that app. No arbitrary writable SQL endpoint
is exposed. `catalog export` reads all tables in one consistent transaction.

To import an existing apparatus whose catalog is `ugc/catalog.sqlite3`:

```sh
python3 -m outpost app add example --name "Example App"
python3 -m outpost import-legacy example ~/marketing/ads --dry-run
python3 -m outpost import-legacy example ~/marketing/ads
```

This baseline import requires an empty app. It takes a consistent SQLite
backup, preserves relationships and original primary keys, maps file columns
into `files/legacy/`, and records original paths. It copies the apparatus,
including scripts/configuration/reports, without executing those scripts.
The preserved source catalog is a separate consistent backup. A repeated,
unchanged import is a no-op even after new Outpost records are added. Changed
legacy baselines fail visibly; use normal asset/record imports for new work
or a new app ID for a separate baseline. This is not a bidirectional live sync.

Dry runs report counts and exclusions without changing library/remote state.
Catalogs with unfamiliar tables require a local scope review rather than
silently copying extra records into a marketing backup.
Caches, credential/session filenames, unknown databases, and files containing
recognized credential patterns are excluded. Review the report before a real
upload. JSON containing recognized credential values can be imported as a
redacted derivative, preserving other marketing metadata. The report records
redaction counts, and `vault_file_provenance` retains both original and stored
SHA-256 values. Source originals remain untouched and never leave the Mac.
If the catalog references another excluded/missing file, the import fails
instead of dropping the relationship. The scanner is a guardrail, not a proof
that arbitrary input contains no secrets. Limit inputs to marketing records.

## Backup, upload, restore

```sh
python3 -m outpost snapshot create example
python3 -m outpost snapshot list example
python3 -m outpost snapshot export example FULL_SNAPSHOT_SHA --out ./snapshot-export
python3 -m outpost snapshot verify ./snapshot-export
python3 -m outpost --root ./restored-data restore ./snapshot-export
```

Snapshot creation holds the app writer lock, uses the SQLite backup API,
checks database integrity/foreign keys/file references, hashes every file,
and writes a manifest. Snapshot IDs are the SHA-256 of canonical manifest
content. Snapshots are read-only and write-once through this CLI.
Restore requires a fresh app directory and verifies all SHA-256 values and
catalog counts before admitting the result. Mode bits, nanosecond mtimes,
and safe internal symlinks round-trip. Ownership, ACLs, resource forks, and
extended attributes are not portable backup guarantees.

For private R2 storage, reference existing credentials through either tool:

```sh
# Existing Wrangler login: no token extraction or new credentials.
python3 -m outpost snapshot upload example FULL_SNAPSHOT_SHA \
  --wrangler-bucket YOUR_PRIVATE_BUCKET --account-id YOUR_ACCOUNT_ID \
  --private-confirmed --dry-run

# Existing rclone remote with bucket-specific access.
python3 -m outpost snapshot upload example FULL_SNAPSHOT_SHA \
  --rclone EXISTING_REMOTE:YOUR_PRIVATE_BUCKET --private-confirmed
```

Remove `--dry-run` to upload through Wrangler. Verify the bucket's r2.dev URL
is disabled and no public custom domains exist before using
`--private-confirmed`. Outpost never changes public-access settings, manages
credentials, or substitutes another bucket when access fails. Explicit
Wrangler account IDs prevent accidental account selection. The live SQLite
database always stays local. Only immutable snapshots are uploaded under
`apps/<app>/snapshots/<manifest-sha>/`.

Wrangler uploads use deterministic chunks of at most 128 MiB. The transport
descriptor maps every original file to its byte range, and the original
manifest retains each file's SHA-256 and metadata. Only one chunk and one
read-back file are staged at a time. rclone stores snapshot files as separate
objects. Keep one transport format per remote snapshot prefix.

Every object is downloaded and independently SHA-256 checked after upload;
multipart ETags are never treated as content hashes. Existing objects must
match, otherwise the operation stops without overwriting. `COMMITTED.json`
is written last, after all objects verify. Interrupted uploads can resume
using the same snapshot ID; a missing completion marker is never a complete
backup. Remote immutability is a CLI convention with content-addressed keys,
not provider Object Lock or a defense against other privileged clients.

```sh
python3 -m outpost snapshot download example FULL_SNAPSHOT_SHA \
  --wrangler-bucket YOUR_PRIVATE_BUCKET --account-id YOUR_ACCOUNT_ID \
  --private-confirmed --out ./downloaded-snapshot
python3 -m outpost --root ./restored-data restore ./downloaded-snapshot
```

All mutating commands support `--dry-run`. For an entirely offline object
round-trip, replace cloud options with `--local-store ./object-store`.
Do not use the same object directory as a live library root.

## Layout and limitations

```text
data/
  apps/<app>/
    app.json
    catalog.sqlite3
    .writer.lock
    files/assets/<sha256>
    files/legacy/<original-apparatus-path>
    snapshots/<manifest-sha>/
    import-report.json
    receipts/<manifest-sha>.json
```

Each mutating app operation takes an exclusive nonblocking `flock`; SQLite
also uses transactions, foreign keys, a busy timeout, and FULL synchronization.
Two writers on the same app fail clearly. Different apps can operate
independently. Do not bypass the CLI with parallel direct SQLite writes.
Remote devices make separate snapshots and never merge live databases.
An interrupted local import/restore keeps staging files for inspection;
there is no automatic deletion or unsafe overwrite of existing work.

No acquisition, transcription, ad-platform publishing, web interface,
background scheduling, or hosted CI is included. Original legacy scripts
remain available but retain their own operational constraints.

## Local verification and publication

```sh
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

Tests use synthetic records and files only. The source tree and Git history
must contain no real catalogs, private media/comments/accounts, credentials,
session state, local paths, manifests, or receipts. `data/`, `local/`, and
exports are ignored. Inspect staged files and the full history before any
public push; `.gitignore` does not sanitize history. No GitHub Actions
workflows or hosted test dispatch are used.
