# Outpost ownership and operating rules

Outpost owns a small Python CLI, app-scoped D1 or local SQLite catalogs, preserved
marketing originals, and immutable verified object-store snapshots. Read
README.md for commands, layout, auth references, and backup/restore details.

## Cloud catalog entry point

Outpost is the private source of marketing records, media provenance, production
history and metrics across projects. Read [CLOUD.md](CLOUD.md). Start with
`outpost app list`, `outpost cloud status APP`, and `outpost cloud schema APP`.
A per-app `cloud.json` means the working database is in Cloudflare D1; large
values and media are in private R2. Use `catalog query`, `catalog record`,
`metrics`, `lineage`, or `cloud sql`/`cloud transact`. Use expected revisions,
parameterized statements, dry runs, and stable operation IDs for writes. Do not
create or hydrate a local catalog to answer queries. Connection and recovery
references remain local; credentials stay in the existing environment/file.
The legacy browser catalog is a static export and is not refreshed by generic
D1 writes. Exact source revisions/render links/checks use current D1 reads;
see [SOURCE_VERSIONS.md](SOURCE_VERSIONS.md). Freeze source inputs before a new
render, retain original narration/archives, and keep historical associations
unverified unless evidence establishes them. Never execute a stored recipe
or install dependencies automatically.

## Maintained code

- `bin/outpost`, `outpost/cli.py`, `__main__.py`: command parsing and JSON output.
- `outpost/library.py`, `schema.sql`: catalog, records, asset and baseline imports.
- `outpost/model.py`, `bundle.py`: source identities, full records, histories,
  lineage and transactional, checksum-validated bundle imports.
- `outpost/production.py`: production workspaces, transcript and source history,
  reusable raw generation files and links to published variants.
- `outpost/source_versions.py`, `source_restore.py`, `viewer/sources.mjs`: exact
  source revisions, render links, independent evidence, private publication and
  explicit verified restores. The standalone restorer avoids POSIX imports;
  native Windows runtime verification must be reported separately.
- `outpost/configuration.py`: nonsecret default backend references.
- `outpost/cloud.py`, `cloud_migration.py`, `viewer/catalog.mjs`: authenticated
  D1 queries/transactions, lossless R2 values, verified migration and references.
- `outpost/util.py`: local filesystem guards, locking, hashing, secret screening.
- `outpost/archive.py`: SQLite snapshots, manifests, export and restore.
- `outpost/fetch.py`: verified individual media retrieval to explicit external files.
- `outpost/offload.py`: explicit upload, full remote verification and local payload
  removal with durable recovery mappings and bounded-memory repeat offloads.
- `outpost/gateway.py`: bounded, immutable archive transfers through the optional
  private Worker with environment-owned admin authentication.
- `outpost/storage.py`, `packed.py`: existing CLI auth references, immutable
  remote uploads, independent SHA-256 read-back, bounded snapshot chunks.
- `outpost/viewer.py`, `viewer/`: private project exports and the optional
  authenticated Cloudflare browser library with manual observation updates.
- `tests/`: local synthetic regression tests and a data-free legacy DDL fixture.

## Invariants

Keep apps separate in databases, files, receipts, and object prefixes. Reject
traversal/ambiguous identifiers and symlink escapes. D1 writes use atomic batches
with optimistic revision checks. Keep SQLite on a local
filesystem with one writer per app. Never mount a writable catalog on object
storage or overwrite another device's live database. Do not weaken locks or
use remote ETags as SHA-256 evidence.

Preserve originals and existing source workflows unless the user explicitly
requests offloading. For a request to move data to Cloudflare and free disk,
use `offload APP` (and only explicitly selected `--source` directories), which
uploads missing data, verifies every remote file, saves recovery mappings,
then removes covered local payloads. `snapshot upload` alone is a backup;
never report it as a completed move or as disk space reclaimed.
No background cleanup, unrelated deletion, force overwrite, source-script execution, external messages, ad
publication, model calls, or new credentials. Every mutation must support
dry-run and fail visibly on a conflict. Credentials stay with existing tools;
never read/copy their secret values into source, logs, data, or chat.

Keep marketing storage private. Verify public URL/domain settings before
upload. Scope inputs to marketing records; exclude secrets, sessions, caches,
and unrelated/customer databases. Report exclusions before real upload.
Do not claim migration complete until all selected remote file bytes and the
exact catalog hash verify against a locally checked snapshot with intact
relationships/counts. State whether a fresh full restore was performed.
After explicitly approved media offload, retain exact remote recovery mappings
and identify local snapshot trees as partial caches. Metadata queries may work
while file access and complete local snapshot operations require retrieval.
Use `asset fetch` only for an explicit external copy: no implicit hydration/cache,
managed-library writes, overwrite, credential grant or remote mutation. Validate
app/snapshot identity, all selected chunks and the requested file before atomic
publication; report response-body bytes separately from materialized bytes.
Keep privacy holds distinct from missing offloaded payloads.
Use streaming remote verification when full local downloads are not requested;
never substitute HEAD/ETag or metadata-only checks for content proof. No background publication, model calls or automatic metric collection. The
optional authenticated viewer is deployed only when the user requests it.

## Verification and publication

Write regression tests before changing data safety behavior, run them locally
with `python3 -W error::ResourceWarning -m unittest discover -s tests -v`, and
keep app isolation, transaction/snapshot consistency, relationships, original
preservation, dry-run nonmutation, interrupted-upload recovery, idempotency,
metadata/symlinks, and independent SHA-256 checks covered.

The reusable source may be public; real marketing data must remain private.
Never stage `data/`, `local/`, exports, source manifests, catalog DBs, media,
account/comment records, local operational logs/paths, or credentials.
Review staged contents and every reachable Git blob before a public push.
Keep fixtures synthetic. Do not add GitHub Actions, dispatch hosted tests,
force push, or change repository visibility without explicit authorization.
