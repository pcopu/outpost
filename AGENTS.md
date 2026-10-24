# Outpost ownership and operating rules

Outpost owns a small Python CLI, app-scoped local SQLite catalogs, preserved
marketing originals, and immutable verified object-store snapshots. Read
README.md for commands, layout, auth references, and backup/restore details.

## Maintained code

- `bin/outpost`, `outpost/cli.py`, `__main__.py`: command parsing and JSON output.
- `outpost/library.py`, `schema.sql`: catalog, records, asset and baseline imports.
- `outpost/model.py`, `bundle.py`: source identities, full records, histories,
  lineage and transactional, checksum-validated bundle imports.
- `outpost/configuration.py`: nonsecret default backend references.
- `outpost/util.py`: local filesystem guards, locking, hashing, secret screening.
- `outpost/archive.py`: SQLite snapshots, manifests, export and restore.
- `outpost/storage.py`, `packed.py`: existing CLI auth references, immutable
  remote uploads, independent SHA-256 read-back, bounded snapshot chunks.
- `tests/`: local synthetic regression tests and a data-free legacy DDL fixture.

## Invariants

Keep apps separate in databases, files, receipts, and object prefixes. Reject
traversal/ambiguous identifiers and symlink escapes. Keep SQLite on a local
filesystem with one writer per app. Never mount a writable catalog on object
storage or overwrite another device's live database. Do not weaken locks or
use remote ETags as SHA-256 evidence.

Preserve originals and existing source workflows. No automatic cleanup,
deletion, force overwrite, source-script execution, external messages, ad
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
Do not rehydrate media without a request or describe automatic per-file fetch
as available. Keep privacy holds distinct from missing offloaded payloads.
Use streaming remote verification when full local downloads are not requested;
never substitute HEAD/ETag or metadata-only checks for content proof. No background processes or hosted workflows.

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
