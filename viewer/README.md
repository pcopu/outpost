# Outpost browser library

The optional Cloudflare Worker provides authenticated, project-scoped playback,
source downloads, reusable raw inputs, organic posting history and ad usage.
Media stays in private R2. A snapshot remains the authoritative recovery archive.
The browser reads an exported catalog and streams individual files from packed
snapshots. An optional direct object map supports existing R2 media collections.

## Checks

```sh
node --test viewer/*.test.mjs
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

## Deploy

Copy `wrangler.example.jsonc` into an ignored local configuration. Set the private
R2 binding and account through existing Wrangler authentication. Paths are
relative to that configuration file. Keep actual bucket names, project catalogs,
URLs and credentials outside the public source tree.

Set `READ_KEY`, `SESSION_SECRET` and `ADMIN_TOKEN` with `wrangler secret put`.
The read key opens a seven-day HttpOnly session. An admin bearer token can create
90-second launch tickets through `POST /api/launch`. Put a launch ticket in the
URL fragment as `#ticket=...` for a temporary browser handoff. The page removes
the fragment before loading the catalog.

Generate each project catalog from a checked snapshot and the matching remote
`transport.json`. Use the backend's completion marker to verify the transport
hash before export. A fresh output path is required.

```sh
python3 -m outpost.viewer ./private-snapshot \
  --transport ./private-transport.json \
  --out ./private-project.json --dry-run
python3 -m outpost.viewer ./private-snapshot \
  --transport ./private-transport.json \
  --out ./private-project.json
```

Upload the result to `viewer/projects/APP_ID.json`. Upload `viewer/index.json`
with `{"projects":[{"id":"example","name":"Example"}]}`. The index selects
which apps the deployment can expose. SQLite remains local. Each export retains
its snapshot and catalog hashes. Re-export after importing production revisions.
Existing observation objects remain separate and survive catalog refreshes.

For efficient direct media access, `--locations private-map.json` accepts SHA-256
keys mapped to `{"binding":"MEDIA","key":"private/object","bytes":123}`.
`ARCHIVE` is an optional second private R2 binding for existing media. Each map
entry must match the snapshot file's byte count. The authenticated API resolves
only files explicitly declared in the requested project's catalog.

## Manual posting and metrics

Send confirmed observations to `POST /api/projects/APP_ID/events` with the admin
bearer token. Each observation has a fresh UUID `id`, the catalog's `videoId`,
a stable `postId`, `platform`, `status`, an evidence `source` and an ISO
`observedAt` with a timezone. Published posts require an HTTPS platform URL and
`postedAt` or a verified date-only `postedDate`; scheduled posts require
`scheduledAt`. Optional counters are
`views`, `likes`, `comments`, `shares`, `saves` and `reach`.

Observations append to a project-specific R2 ledger. Retry the same event UUID
and identical contents after a timeout. Conflicting IDs fail. Concurrent edits
use conditional R2 writes. Preserve each observation locally and import it through
a reviewed Outpost bundle for the next catalog snapshot. R2 ledger updates and
local SQLite imports are separate operations.

Publication status and metrics stay exactly as last recorded. An elapsed schedule
does not confirm publication. Counts with no verified observation stay empty.
The viewer displays saved organic and advertising records and supports manual
updates from an operator.

## Archive CLI connection

The same private Worker exposes bounded snapshot-object transfers for Outpost's
CLI. It accepts admin-authenticated GET and immutable PUT requests under app and
snapshot prefixes. R2 checks the supplied SHA-256 before accepting a PUT. The
CLI independently reads and hashes every object before committing an archive.
Reader sessions can stream catalog assets through the viewer.

Supply the existing deployment admin token through `OUTPOST_ARCHIVE_TOKEN` in
the operator's environment. A nonsecret backend reference can store its variable
name. Redirects fail visibly to keep credentials scoped to the chosen origin.

```sh
outpost snapshot upload example SNAPSHOT_ID \
  --worker-url https://your-worker.example \
  --private-confirmed --dry-run
outpost snapshot upload example SNAPSHOT_ID \
  --worker-url https://your-worker.example \
  --private-confirmed
```

`--token-env` selects another existing environment variable. The Worker connection keeps up to two bounded payload uploads in flight and
waits for every full read-back before storing transport or completion metadata.
It uses the same packed snapshot protocol and recovery checks as the Wrangler connection.
The default backend remains the one explicitly configured in the local registry.
