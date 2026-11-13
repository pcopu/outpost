# Outpost

A small CLI for marketing files and social/ad catalogs across multiple apps.
Each app has its own D1 or local SQLite database, original files, provenance, snapshots,
and remote object prefix. Python 3.10+ on macOS or Linux; no runtime Python
dependencies. The optional Cloudflare browser library is documented in
[viewer/README.md](viewer/README.md). Model calls and automatic publication are
outside the CLI.

For cloud catalogs, agent commands, migration and recovery references, read
[CLOUD.md](CLOUD.md). Cloud queries and updates do not require local SQLite.

Run from a checkout with `python3 -m outpost`, or install without downloads:

```sh
mkdir -p ~/.local/bin
ln -s "$PWD/bin/outpost" ~/.local/bin/outpost
outpost --help
```

Use an unused command path and keep the checkout in place. Alternatively,
`python3 -m pip install .` provides the command in a Python environment.
Use an explicit local `--root` or `OUTPOST_ROOT`; the source-checkout default
is `data/` next to this README. Never put this root on an object/network mount.

## Move data to Cloudflare and free local storage

Use `offload` to finish a move to the configured private R2 backend:

```sh
outpost offload example --dry-run
outpost offload example
# Also import and remove an explicitly selected media source directory:
outpost offload example --source ~/creative/example/completed-renders --dry-run
outpost offload example --source ~/creative/example/completed-renders
# Clean an old download/import staging directory using exact archived hashes:
outpost offload example --copies ./local/example-import --dry-run
outpost offload example --copies ./local/example-import
```

The command uploads missing snapshots and a current self-contained archive,
reads every remote file back and checks its SHA-256, persists recovery metadata,
then removes matching local payloads from managed files and snapshot trees.
Local catalogs, app identity, manifests, privacy holds and recovery information
remain. `snapshot upload` alone is still a backup operation and does not free
local storage. An incomplete upload or failed verification never authorizes
deletion. Changed files stop deletion; rerunning resumes safely.

Source files/directories are opt-in. `--source` imports screened regular files with
their original paths and hashes before removing their verified local copies.
Credentials, caches and unselected databases are excluded and stay local; the
dry run reports them. Choose completed asset directories, not an entire working
project whose code or live database still needs local media. Unselected source
originals are untouched.

`--copies` never imports new data. It removes only exact SHA-256/size matches
to this app's freshly verified archives in the named staging paths, records
their recovery mapping, and reports unmatched files that it retains. Abandoned
`.building-*` snapshot stages receive the same exact-match cleanup. Neither
option can select the live library or an archive destination.

After offloading, run `offload` again for new imports. Previously offloaded
files stream from existing archives into the new archive in bounded memory;
they are not downloaded into a local restore tree. Each resulting snapshot
retains the existing full restore format. All historical snapshots are also
uploaded and verified before their local payloads are removed. Stored upload
receipts alone are never used as permission to delete.

An upload interrupted before `transport.json` is written can resume using the
first existing chunk's original size and compression format. Every chunk must
still match the freshly packed source bytes. For legacy chunks larger than the
Worker's 80 MiB upload limit, use the configured Wrangler/R2 backend to finish
the upload; do not overwrite or delete the existing partial archive.

`data/apps/APP/offload-recovery.json` records the current snapshot, verification
results and selected source recovery mappings. Use `asset fetch` below to get
one file, or `snapshot download` and `restore` for a full recovery. The result
reports logical bytes removed and the observed filesystem free-space change
separately: APFS clones share blocks, and other applications may write while
offload runs. No background job or automatic schedule is installed.

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

## Saved history, organic content, and ad lineage

Reviewed migration bundles preserve original IDs, full unknown payload fields,
source coordinates and checksums, metric observations, creative/media links,
and relationships. See [IMPORT_FORMAT.md](IMPORT_FORMAT.md) for the contract.
They can be applied repeatedly without duplicate observations:

```sh
outpost import-bundle example ./reviewed-export --dry-run
outpost import-bundle example ./reviewed-export
outpost catalog query example source_records --filter kind=organic_post --limit 5
outpost metrics example --entity-key provider:post:example --metric views
outpost lineage example provider:post:example
```

`source_records` exposes full JSON payloads and source headers; `source_entities`
provides identity, `source_metrics` preserves individual observations, and
`source_relationships` tracks saved organic/creative/ad lineage. Provider
exports and original relational schemas can coexist with this normalized
model. Missing source relationships, timestamps, or comment bodies stay
missing. Overlapping metric reports are observations, not additive totals.
The CLI tracks saved information; it does not publish content or launch ads.

## Production workspaces

An app can also keep editable sources, transcripts, audio, render revisions and
reusable raw generation files. Create a source workspace outside the managed
catalog, then edit its `production.json`.

```sh
outpost production init example ~/creative/example
outpost production import example ~/creative/example --dry-run
outpost production import example ~/creative/example
outpost production show example
outpost production show example main-film
```

The initializer creates `raw/`, `videos/` and this manifest. Every video has a
stable ID within its app. Directory inputs retain their relative source paths.

```json
{
  "format_version": 1,
  "app_id": "example",
  "raw_directory": "raw",
  "videos": [
    {
      "id": "main-film",
      "title": "Main film",
      "directory": "videos/main-film",
      "uses": ["raw/landscape.jpg"]
    },
    {
      "id": "short-cut",
      "title": "Short cut",
      "directory": "videos/short-cut",
      "derived_from": "main-film",
      "uses": ["raw/landscape.jpg"],
      "posts": ["example:post:1"]
    }
  ]
}
```

Put reusable generation originals in `raw/`, including images, footage, audio,
prompts and provenance files. A raw file is retained even before a video uses it.
Inside each video directory, the `sources/`, `transcripts/`, `audio/`, `renders/`
and `references/` subdirectories determine asset roles. SRT and VTT files always
default to transcripts. A name such as `captions.en.srt` records language `en`.
Other files default to editable sources. Every selected regular file is hashed,
screened and imported. The dry-run report lists excluded credentials, sessions,+caches and databases. Symlink inputs fail visibly.

For existing directory layouts, replace or supplement `directory` with an
explicit `files` array. Each entry has a workspace-relative `path`, optional
`role`, `language`, `variant`, `revision` and other provenance fields. For example,
`{"path":"exports/cut.mp4","role":"render","variant":"portrait","revision":"v2"}`.
Paths must stay inside the workspace. `uses` links raw assets to multiple videos
without duplicating their stored bytes. The reserved ID `raw` identifies the
app's shared generation collection. `posts` must reference existing organic or
paid publication entities in the same app.

Imports preserve all selected file bytes and append immutable production
observations through the existing bundle importer. A changed source, transcript,
prompt, manifest or render creates a new revision. The latest production view
shows current file links while earlier observations and assets remain queryable.
Removing a file from the workspace does not delete its earlier imported bytes.
Snapshots include the production history and all imported files. Normal Outpost
snapshot upload, verification, restore and on-demand retrieval apply.

Use `source_records --filter kind=video_production` through `catalog query` for
production history, `source_asset_links` for the file relationships, and
`lineage` for `derived_from` and `published_as` links. Organic metrics retain
their own captured timestamps through the existing `metrics` command.

## Backup, upload, restore

```sh
python3 -m outpost snapshot create example
python3 -m outpost snapshot list example
python3 -m outpost snapshot export example FULL_SNAPSHOT_SHA --out ./snapshot-export
python3 -m outpost snapshot verify ./snapshot-export
python3 -m outpost --root ./restored-data restore ./snapshot-export
```

Snapshot creation holds the app writer lock and a SQLite write reservation,
then takes an independent filesystem copy of the closed DELETE-journal catalog.
APFS uses copy-on-write; other local filesystems copy the bytes. External
source catalogs are imported with the SQLite backup API. Snapshot creation
checks database integrity/foreign keys/file references, hashes every file,
and writes a manifest. Snapshot IDs are the SHA-256 of canonical manifest
content. Snapshots are read-only and write-once through this CLI.
An explicit `snapshot create APP --hold-file files/PRIVATE_RESPONSE.json`
keeps a reviewed sensitive file local and records its path/hash/size as absent
from this backup. Holds never silently replace content or claim full coverage.
Restores retain `snapshot-holds.json`; referenced held assets stay visibly
unavailable rather than being invented. File holds do not bypass catalog
credential screening. Preserve withheld originals separately.

Restore requires a fresh app directory and verifies all SHA-256 values and
catalog counts before admitting the result. Mode bits, nanosecond mtimes,
and safe internal symlinks round-trip. Ownership, ACLs, resource forks, and
extended attributes are not portable backup guarantees.

For private R2 storage, reference existing CLI credentials or the deployed
[private archive Worker](viewer/README.md#archive-cli-connection).
A default backend stores references only, never tokens:

```sh
outpost storage configure --wrangler-bucket YOUR_PRIVATE_BUCKET \
  --account-id YOUR_ACCOUNT_ID --private-confirmed
outpost storage show
```

Once configured, upload/download need no backend flags. Explicit command
options override the saved reference. CLI credentials remain in their external
tool. The Worker connection reads its token from the named environment variable.
Without a default, select a backend for each transfer:

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

Wrangler uploads default to deterministic gzip chunks of 64 MiB of raw
content. Previously committed uncompressed snapshots remain readable and
reuploads retain their existing format. The transport
descriptor maps every original file to its byte range, and the original
manifest retains each file's SHA-256 and metadata. New Wrangler uploads
compress bounded chunks in memory and use stdin/stdout for upload and read-back.
Payload chunks do not need disk scratch; only small manifest/descriptor metadata
is temporary. Multiple bounded buffers and the external CLI still use memory;
the operating system may swap. Legacy formats remain readable.
No full archive is staged for transport. rclone stores snapshot files as separate
objects. Keep one transport format per remote snapshot prefix.

Every object is read back and independently SHA-256 checked after upload;
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

To verify every remote byte without creating an extraction or restore tree:

```sh
outpost snapshot remote-verify example FULL_SNAPSHOT_SHA
```

This hashes compressed objects, decoded chunks and every file range against
the bound manifest. It uses bounded temporary buffers, and compares a local
manifest when available. It reports `full_local_restore: false`: this is
content verification, not a newly restored catalog query. A matching remote
catalog hash proves the catalog bytes equal the checked local snapshot.
Streaming verification requires packed snapshots from Wrangler or the private
Worker connection.

For low-disk verification, download can take `--reuse-from LOCAL_SNAPSHOT`.
Matching local files are independently cloned into the fresh restore while
every downloaded chunk and every remote file range is still SHA-256 checked.
A valid cache cannot conceal corrupt remote data. This saves local allocation;
it does not skip remote read-back.

All mutating commands support `--dry-run`. For an entirely offline object
round-trip, replace cloud options with `--local-store ./object-store`.
Do not use the same object directory as a live library root.

## Layout and limitations

```text
data/
  storage.json                 # backend references only
  apps/<app>/
    app.json
    catalog.sqlite3
    .writer.lock
    files/assets/<sha256>
    files/legacy/<original-apparatus-path>
    files/imports/<bundle-id>/*.jsonl.gz
    snapshots/<manifest-sha>/
    import-report.json
    receipts/<manifest-sha>.json
```

Each operation mutating managed app state takes an exclusive nonblocking `flock`; SQLite
also uses transactions, foreign keys, a busy timeout, and FULL synchronization.
Two writers on the same app fail clearly. Different apps can operate
independently. Do not bypass the CLI with parallel direct SQLite writes.
Remote devices make separate snapshots and never merge live databases.
An interrupted local import/restore keeps staging files for inspection;
there is no automatic deletion or unsafe overwrite of existing work.

R2 holds private immutable archives; SQLite remains the local live database.
Use `offload APP` to move managed media to R2 after remote verification.
An offloaded file can have its only retained payload in R2, with catalog and
recovery metadata still local. `asset fetch` retrieves an explicit external copy
on demand. Deletion happens only through an explicitly invoked offload, not
through ordinary upload. There is no implicit hydration or cross-device merge.

No acquisition, transcription, ad-platform publishing, web interface,
background scheduling, or hosted CI is included. Original legacy scripts
remain available but retain their own operational constraints.


## Intentionally offloaded media

Metadata queries (`catalog query`, `metrics`, `lineage`) use local SQLite and
continue to work after an explicitly approved media offload. A stored path is
not a promise that its bytes are currently local. Retrieve one file on demand:

```sh
outpost asset fetch example files/assets/FILE_SHA256 \
  --snapshot FULL_SNAPSHOT_SHA --out ~/Downloads/creative.mp4 --dry-run
outpost asset fetch example files/assets/FILE_SHA256 \
  --snapshot FULL_SNAPSHOT_SHA --out ~/Downloads/creative.mp4
```

Use the exact app-relative `files/...` path from its catalog or snapshot manifest
and a full snapshot ID. The snapshot must contain that regular file; symlinks,
privacy holds, absent files, catalogs and identity files are not accepted.
The existing configured private backend is used, or specify the usual explicit
backend flags. A matching local media file or complete local snapshot is not
required. App identity and the content-addressed remote manifest are checked.

`--out` is mandatory: a fresh file outside the managed library, with an existing
parent directory containing no symlink components. The command creates a private
0600 copy, preserves its file timestamp, and refuses to overwrite anything.
It does not populate a hidden cache, change catalog paths, rehydrate the library,
restore other files, or modify remote objects. Choose an app-specific external
output directory when retaining several retrieved assets.

Packed snapshots read only intersecting content chunks. The current gzip layout
requires reading and decoding each intersecting chunk completely, even for a tiny
file. No whole archive or raw chunk is staged on disk. CLI stdout and decompression
use bounded buffers; output disk use grows only to the requested file size.
Unpacked snapshots read only that file object. Compressed-object, decoded-chunk
and final-file SHA-256/size checks must all pass before an atomic, no-replacement
publication. Unrelated snapshot bytes are not reverified by a single-file fetch;
use `remote-verify` when that is needed.

JSON output reports `remote_payload_bytes`, `remote_metadata_bytes`, their sum
`remote_bytes_fetched`, `chunks_fetched`, and `materialized_bytes`. These count
object response bodies, excluding HTTP/TLS overhead and the external tool's auth
traffic. A dry run reads and verifies metadata only, creates nothing, and reports
planned payload/file bytes. There is no implicit provider retry or auth grant.
A failed/interrupted fetch removes its own temporary output and can be retried;
a hard kill or power loss can leave a private `.outpost-fetch-*` partial file,
which is never trusted as a cache. Remote CLI reads time out after 180 seconds
per object; access failures retain safe provider codes without echoing secrets.

Retain exact file SHA-256/size, snapshot path, packed offset/chunk mapping,
and the authoritative remote snapshot when offloading. Local snapshot trees
with removed payload files are partial caches: `snapshot list` lists their
identifiers but does not prove local completeness. Local verify/export/upload
and the legacy `snapshot create` command need missing media restored first.
Use `offload` for a new complete remote archive without restoring previously
offloaded media to disk.
Do not hide media eviction by declaring it a credential/privacy exclusion.
`remote-verify` can still validate the full cloud snapshot without rehydration.

The supported full recovery path is `snapshot download` into a fresh directory
with sufficient local storage, then `restore` into a fresh library root.
Use `asset fetch` for an explicit individual-file copy using the verified
remote mapping; this does not automatically repair partial local snapshot trees.
Do not overwrite existing files or infer reclaimed disk space from logical sizes
of APFS clones.

JSON catalog export materializes tables in memory. Use SQLite snapshot
exports for large complete catalog recovery, with enough storage for the
requested downloaded/restored data.

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
# Exact video source versions

See [SOURCE_VERSIONS.md](SOURCE_VERSIONS.md) for immutable source manifests,
render/version links, multilingual transcripts, pinned reusable inputs, verified
restore and missing-file checkout recovery. Cloud sources use current D1 reads
and private R2 chunks. Originals remain archived; source bytes, dependency
completeness, restore/render tests and creative approval are recorded separately.
