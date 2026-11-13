# Restore an exact video version

A video has a stable identity within one app. Each **source revision** records
one immutable set of editable inputs. A **render record** pins that revision,
the output checksum, and its format/language/settings. Multiple aspect ratios
can share one source revision. Labels such as “Second cut” are descriptive;
the SHA-256 revision ID is the identity. Renaming a label does not invent a new
creative revision, and a published label is retained on retries.

Working code belongs in its private Git repository. Outpost preserves an exact
archival copy of that code alongside audio, images, fonts, scenes and original
ZIPs. This public repository contains application code and synthetic tests only.
No automatic cleanup, rendering, dependency installation or publication to social
platforms is performed.

## Freeze explicit inputs before rendering

Create a private description, outside this public repository:

```json
{
  "app_id": "example",
  "video_id": "production:documentary",
  "label": "First cut",
  "files": [
    {"path": "src/scene.py", "role": "source"},
    {"path": "audio/original.wav", "role": "audio", "language": "en"},
    {"path": "captions/ro.srt", "role": "transcript", "language": "ro"},
    {"path": "requirements.lock", "role": "dependency"}
  ],
  "dependencies": [
    {"name": "Original narration", "status": "present", "path": "audio/original.wav"},
    {"name": "Display font", "status": "missing"}
  ],
  "recipe": {
    "entrypoint": "src/scene.py",
    "command": ["python", "src/scene.py"],
    "tools": {"python": "3.12", "ffmpeg": "7.1"}
  },
  "lineage": []
}
```

```sh
outpost source freeze example ./workspace --spec ./private-description.json --out ./frozen-source.json --dry-run
outpost source freeze example ./workspace --spec ./private-description.json --out ./frozen-source.json
outpost cloud status example
outpost source publish example ./workspace --manifest ./frozen-source.json --revision 12 --operation-id documentary-source-1 --dry-run
outpost source publish example ./workspace --manifest ./frozen-source.json --revision 12 --operation-id documentary-source-1
```

Use the actual current catalog revision, not the example number. Keep the frozen
manifest outside the selected source inputs. It records repository URL, commit,
subdirectory, actual selected-file tree hash, and dirty/unknown status. A dirty
working tree is never described as an exact checkout of its commit. Inputs that
change after freezing are rejected at publication. To render from an archived
revision, first restore it into a fresh folder, then explicitly run its recipe.

Paths are relative, Unicode NFC, and portable across common filesystems. Symlinks,
reparse points, traversal, Windows device names/streams, case collisions,
file/directory collisions, `.git`, `.env` and restore-control filenames are
rejected. Credential screening runs before upload. Source metadata uses named ASCII
object keys (not numeric-only keys) and exact JSON integers; represent decimal parameters/versions as
strings, such as `"29.97"`. This avoids Python/JavaScript hash ambiguity.

Pin every shared dependency as a file in this revision. Dependencies distinguish
`present`, `declared`, `missing`, `excluded` and `unknown`. `present` requires the
exact file hash. The tool does not infer Blender linked-file closure, a complete
toolchain, licensing rights, or successful rendering from file presence.
Retain original narration and original editable archives even when newer mixes
or extracted members are added. A prompt/provider ID is supplementary metadata,
not a replacement for generated source bytes.

`lineage` entries contain `app_id`, `video_id`, and `revision_id`, pinning an
existing same-app source version. Use this for documentary/Short derivation and
translated editions; it does not overwrite existing production relationships.

## Render records and separate evidence

After explicitly rendering, prepare a private render description:

```json
{
  "path": "renders/portrait.mp4",
  "language": "en",
  "settings": {"aspect_ratio": "9:16", "width": 1080, "height": 1920, "fps": "30"},
  "association": {"status": "unverified", "evidence": "Historical output; exact input match has not been established"}
}
```

```sh
outpost source render example ./workspace --manifest ./frozen-source.json --spec ./render-description.json --revision 13 --operation-id documentary-portrait-1
outpost source list example production:documentary
outpost source show example SOURCE_REVISION_SHA256
```

`unverified` is the default for historic outputs. `frozen_inputs` is an explicit
operator assertion requiring all the selected local inputs to match the frozen
manifest and a nonempty evidence description. It establishes a recorded input
association, not a guarantee of deterministic output or a successful rendering
test. No renderer is launched by this command. Freeze/publish/restore first,
render manually from that fresh folder, retain a run log, then record the output.

Use `source check example --json ./check.json --revision N --operation-id ID`
to append independent evidence. Add `--dry-run` to inspect without writing.
The record has `app_id`, `video_id`, `source_revision_id`, `kind`, `status`,
`observed_at` (UTC/offset timestamp), `environment` (OS/tool versions), and
`evidence`. Kinds `restore` and `render` accept `passed`, `failed`, or `unverified`;
`creative_approval` accepts `approved`, `rejected`, or `unverified`. Render checks
also require the exact `render_id`. Checks are append-only and never rewrite
the frozen source. The viewer displays these separately from dependency state
and stored-file checksums. Missing test evidence remains “Not recorded.”

## Restore or download

```sh
outpost source restore example SOURCE_REVISION_SHA256 --out ./fresh-project --dry-run
outpost source restore example SOURCE_REVISION_SHA256 --out ./fresh-project
outpost source restore example SOURCE_REVISION_SHA256 --out ./fresh-project --resume
outpost source restore example SOURCE_REVISION_SHA256 --out ./git-checkout --missing-only
```

Every downloaded file is bounded by its declared size and verified with SHA-256
before atomic no-replace publication. A failed/interrupted restore leaves already
verified files intact. `--resume` requires the same app/revision receipt.
`--missing-only` hashes existing selected files, downloads only absent files,
and refuses changed files. It leaves other checkout files alone. A stale
`.outpost-restore-lock` after process termination requires explicit inspection
and removal before resuming. The tool never silently steals a live restore lock.

The browser Source view selects a single revision and provides individual-file
and manifest downloads. “Download project ZIP” is limited to 16 MiB / 32 files and verifies
every selected file before returning the ZIP. Larger projects use the CLI;
restores are bounded to 2,000 files and 20 GiB per revision. The ZIP contains
the original files plus `outpost-source.json`, without executing or installing
anything. Historic mixed file collections remain clearly labeled separately.

### Standalone reader for machines without the full catalog CLI

`outpost/source_restore.py` uses only Python's standard library and does not
import the POSIX-only catalog modules. Copy that generic script or install this
repository, obtain a manifest through the authenticated Source view, and use an
existing read key via the environment:

```sh
python -m outpost.source_restore --manifest ./source.json --origin https://library.example --out ./fresh-project
```

`OUTPOST_READ_KEY` is read in memory; never put it in a command argument or commit
it. This entry point authenticates as a reader, uses the same file endpoints as
the browser, and supports `--resume`, `--missing-only`, and `--dry-run`. It does
not grant publication/admin access. The main CLI's older SQLite/archive workflows
remain POSIX-specific. Native Windows execution is **unverified** until an actual
Windows restore is recorded. Reparse points are rejected; do not concurrently
rename destination directories while restoring on Windows.

## Cloud publication and historic bytes

New data uses 8 MiB content-addressed chunks at `sources/APP/objects/SHA256`.
The client rechecks frozen inputs, uploads bounded chunks, reads every uploaded
object back independently, then commits the immutable source/render using one
atomic D1 batch, the shared catalog revision, and a stable operation ID. Repeating
an operation with identical content is idempotent; stale writers and conflicting
retries fail. Failed uploads cannot create a committed revision. Uploaded orphan
chunks are retained; there is no automatic deletion.

Source manifests, render links and evidence use app-scoped private D1 tables.
They are projected directly from a current D1 session, so publishing sources
does not rewrite the legacy static project export or replace later catalog data.
The CLI verifies the committed read model and reports
`archived_and_viewer_verified` only after readback. A readback failure reports
`archived_viewer_verification_pending`; retry the same operation identity.
The browser shows “Source view pending” on a read failure rather than claiming
that source data is absent. Readers must also pass the existing viewer project
allowlist. Admin catalog bindings alone do not expose hidden projects to readers.

Existing files can retain a `locator` with `kind: "snapshot"`, `snapshot_id`, and
`source_path` under `files/`, plus their known `sha256`, `bytes`, `path` and `role`.
Publication verifies that selected file through the existing archive mechanism;
the server validates the committed snapshot identity, manifest/transport hashes,
and file membership. No entire library is restored or repacked. Original ZIPs
remain files in their own right; extracting useful members requires an explicit
new revision and does not delete the original archive.

Checksums prove bytes. Dependency declarations, OS restore tests, renderer tests,
and creative approval are separate evidence. Existing manifests do not establish
source-to-render associations retroactively, and this feature does not auto-backfill
historic projects or claim that all projects have complete source coverage.

## Local verification

```sh
python3 -W error::ResourceWarning -m unittest discover -s tests -v
node --test viewer/*.test.mjs
OUTPOST_PREVIEW_UI=1 node viewer/source-preview.mjs
```

The preview binds loopback only, uses an in-memory synthetic D1/R2 adapter, and
prints its port. Its optional UI authentication bypass exists only in that test
harness. Production Worker code never reads `OUTPOST_PREVIEW_UI`. The integration
test runs the real Worker handler, publishes through Python, then restores through
the standalone reader. Cloudflare runtime/deployment and native Windows checks
remain distinct from these local tests.
