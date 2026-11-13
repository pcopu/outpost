# Cloud catalogs for agents

Outpost is the private library for cross-project marketing records, original
media, lineage, production history and metrics. It does not publish posts or
collect new metrics automatically. Start with `outpost app list` and
`outpost cloud status APP` to locate a project's records.

Cloud mode keeps searchable tables in a separate Cloudflare D1 database per app.
Scalar values too large for D1 live in private, content-addressed R2 objects.
Outpost verifies and expands those values when returning rows, without a local
cache. Queries, records, metrics, lineage and production inspection use the cloud:

```sh
outpost cloud status example
outpost cloud schema example
outpost catalog query example source_entities --limit 10
outpost cloud sql example --sql 'SELECT kind, COUNT(*) AS total FROM source_entities GROUP BY kind'
outpost catalog record example accounts --json ./account.json --dry-run
outpost catalog record example accounts --json ./account.json
outpost cloud transact example --json ./statements.json --revision 0 --operation-id reviewed-change-1 --dry-run
outpost cloud transact example --json ./statements.json --revision 0 --operation-id reviewed-change-1
```

`statements.json` is an array of statements, for example:

```json
[{"sql":"UPDATE accounts SET display_name=? WHERE handle=?","params":["Example","example"]}]
```

Read the current revision with `cloud status` first. Writes are atomic, reject
stale revisions, and accept retries with the same operation ID and identical
content. Dry runs validate request structure and revision without executing SQL
or uploading values; database constraints are checked during the real write.
Use parameterized statements and inspect `cloud schema` before editing. Generic
transactions require the caller to preserve the schema's domain meaning.

SQL is evaluated on D1's stored representation. JSON functions, LIKE, sorting,
or aggregation inside an oversized scalar see its R2 reference, not its expanded
contents. Select the value and inspect the expanded result through Outpost.

## Connection and recovery

`data/apps/APP/cloud.json` is a small nonsecret connection reference;
`cloud-migration.json` contains the migration receipt. Credentials resolve from
the named environment variable or an existing owner-only file, never from a
secret embedded in the reference. `offload-recovery.json` and snapshot manifests
retain the original SQLite/media recovery paths. A network failure never creates
a local database or silently switches the app back to SQLite.

Archive retrieval has its own nonsecret `data/storage.json` transport reference.
For an existing private Worker, `storage configure --worker-url ORIGIN
--token-file /absolute/path/to/existing.env --token-key ADMIN_TOKEN
--private-confirmed` can reference the same existing owner-only credential file.
An exported `OUTPOST_ARCHIVE_TOKEN` takes precedence. No secret value is copied
into configuration. Existing configured transports are not silently replaced
by `storage configure`; inspect them before changing destinations.

Provision the app's D1 binding in the private Worker and prepare a reference
with `format_version: 1`, `backend: "d1"`, `app_id`, `worker_url`, `database_id`,
and `token_env` (or `token_file`/`token_key`). Then:

```sh
outpost cloud migrate example --reference ./private-reference.json --dry-run
outpost cloud migrate example --reference ./private-reference.json
```

`app add` still creates a local catalog. Provisioning and migration are explicit;
check cloud status before assuming a newly added app is cloud-managed.

Migration requires an exact verified backup of the current SQLite file. It
preserves schemas, row identities, relationships and autoincrement counters,
uploads large values with independent read-back, compares every cloud row to
the source, checks table counts and foreign keys, and only then activates the
reference and removes local SQLite. Retries reuse import batches. Failed checks
retain the source. The receipt states whether a full SQLite restore was tested.

## Current workflow boundaries

Legacy bulk bundle/asset imports, catalog exports, snapshot creation, and browser
catalog exports still operate on local SQLite. Explicitly restore a selected
archive into a separate root for those workflows; do not recreate SQLite under
a cloud-managed app. Restore archives represent their recorded snapshot date,
so reconcile later D1 changes before using a restored copy for new imports.

The browser viewer uses its last published static catalog for legacy records;
cloud SQL/record changes do not refresh that export automatically. Exact video
source revisions, renders and checks use a current D1 projection through the
`source` commands; see [SOURCE_VERSIONS.md](SOURCE_VERSIONS.md). Publishing these
records does not recreate a local SQLite catalog or overwrite the static export.
Existing `asset fetch` retrieves an
archived file to an explicit destination without restoring the database.
Migration preserves a pre-migration SQLite archive. Later D1 writes are not
automatically added to those historical R2 snapshots; use Cloudflare's D1
recovery/export facilities for the current cloud database.
