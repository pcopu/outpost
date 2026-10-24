# Marketing migration bundles, version 1

Workers prepare reviewed exports; the importer is the sole app database writer.
Never put real records or media in the public source tree.

`outpost import-bundle APP STAGING_DIRECTORY` reads `bundle.json`:

```json
{
  "format_version": 1,
  "app_id": "example",
  "bundle_id": "example-marketing-v1",
  "records": "records.jsonl",
  "entities": "entities.jsonl",
  "metrics": "metrics.jsonl",
  "relationships": "relationships.jsonl",
  "asset_links": "asset-links.jsonl",
  "assets": [],
  "raw_sources": [],
  "provenance": {},
  "excluded": [],
  "sanitized": []
}
```

`version: 1` is also accepted. JSONL streams may be empty. Use stable IDs and
preserve original source IDs, fields, windows, coordinates and relationships.
All unknown input fields survive in source headers or raw JSON columns, and
original normalized input streams are retained as checksum-verified gzip files.

- Entities: `entity_key`, `kind`, `platform`, `source_id`, optional
  `account_key`, `parent_key`, `title`, `body`, `source_url`, `published_at`.
  Parent/account keys refer to entities. The entire row is retained.
- Records: `record_id`, `kind`, `payload`, `raw_payload_sha256`, optional
  `platform`, `entity_id`, `entity_key`, `source_path`, `source_sha256`,
  `source_coordinate`, `capture_context`, and other original headers.
  Payload hashes use UTF-8 canonical JSON: sorted keys, no extra whitespace,
  `ensure_ascii=False`, no trailing newline. `source_records` exposes payloads
  alongside headers and original hashes; `source_events` stores their mapping.
- Metrics: `key`, `metric`, `value`, optional `record_id`, `entity_key`,
  `captured_at`, `unit`, `source_coordinate`. Each point retains its entire
  input JSON. Null observation times remain null; overlapping reports must
  not be summed as if independent totals.
- Relationships: `from_key`, `to_key`, `relationship`, optional `key`,
  `record_id`. Missing keys derive from the canonical complete input hash.
  Preserve organic-post, creative-variant, asset, ad and paid-result lineage
  only where supported by the source.
- Asset links: optional `key`, `record_id`, `entity_key`, `sha256`, `role`,
  `ordinal`, or a reviewed `source_path` mapping. No guessed ad assignments.
- Assets and raw sources: `path`, `sha256`, `original_path`, optional `bytes`,
  `format` (`json`, `jsonl`, `sqlite`, `binary`), `media_kind`, and provenance.
  Identical file contents are stored once per app while every original path,
  checksum and metadata association remains in `source_import_files`.

Stream paths are inside staging. File references outside staging require
explicit repeatable `--reference-root APPROVED_DIRECTORY` arguments. Resolved
files must remain inside staging or those roots and match declared checksums.
Traversal, escaping symlinks, credential filenames and unsafe payloads fail.
Only explicit manifest entries are imported; directories are never recursively
uploaded. SQLite raw sources must be consistent closed snapshots without WAL.

An optional `--seed-catalog REVIEWED_EXPORT.sqlite3` is allowed only for an
empty app. It admits the supported source-document/record index and provider
Noise tables, preserving original table DDL in `source_schema`. Payload reuse
requires an exact match through `outer_fields.record_key`; original raw SQLite
backups remain separately cataloged. Unfamiliar seed tables are rejected.

Credentials, cookies, API keys, private keys and auth session values may not
be transmitted. Business fields named token/session, token counts, prose and
tool schemas are screened by value/structure rather than blanket key deletion.
If a source requires redaction, retain its original locally and record exact
changed pointers and original hashes. A changed record must include verified
`stored_payload_sha256`, `redacted_fields` and `redaction_pointers` while
`raw_payload_sha256` remains the original hash. Such an import is explicitly
not a byte-identical archive of all original files.

Validation finishes before mutation. Real apply holds the app writer lock and
uses one SQLite transaction with foreign keys. Identical bundle input is a
no-op; a changed manifest or stream under the same bundle ID is an error.
Record, metric and relationship identities are immutable. Entity descriptive
fields may be updated while original entity rows remain in archived streams.
Interrupted apply may leave verified file copies for a retry; no original
source deletion, silent overwrite or background acquisition occurs.
