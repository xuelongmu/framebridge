# Transfer and inspection commands

Framebridge uses the private Frame.io V4 web API and your authorized browser
session. These operations are unofficial and can break when the web app changes.
Follow the installation instructions in the README and the
[platform setup guide](PLATFORMS.md).

## Choose a login profile

Place global options before the command:

```powershell
framebridge --profile downloads login
framebridge profiles
framebridge --profile downloads whoami
framebridge --profile downloads logout
```

Each named profile has its own session and upload journal under
`STATE_ROOT/profiles/NAME`. Windows encrypts sessions with DPAPI; Linux/macOS
protect unencrypted sessions with owner-only permissions. The existing Windows
installation retains its original `.state` layout.
Use `--state-dir PATH` to choose another state root. Do not share session files
between profiles or concurrent processes.

A profile is bound to the user verified at login. A different user needs a new
profile. Existing profiles without identity records must successfully verify
their old session before replacement; if that session has expired, use a new
profile. Logout removes the local credential only. It retains identity and
transfer history and does not sign out Chrome or revoke the server session.

## Find an asset

```powershell
framebridge --profile downloads accounts
framebridge --profile downloads workspaces ACCOUNT_UUID
framebridge --profile downloads projects WORKSPACE_UUID
framebridge --profile downloads browse FOLDER_UUID --recursive
framebridge --profile downloads search FOLDER_UUID camera --recursive
framebridge --profile downloads inspect ASSET_UUID
framebridge --profile downloads renditions ASSET_UUID
```

Search matches names within the selected folder, not across the account. Browse
and search default to immediate children. Recursive traversal stops with an error
at `--limit` (default 10000), rather than reporting truncated results as complete.
Inspection includes available media duration, frame rate, timecode, dimensions,
status, rendition metadata, and custom fields with their definitions. It reports
whether successful video transcodes are listed; this is not proof of playability
or download permission. Signed URL fields are excluded from output.

Browse and search hydrate metadata in batches of up to 100 IDs, preserving the
folder listing's order. Missing IDs, repeated IDs, and parent changes stop the
traversal instead of silently producing an incomplete manifest. Hydrating 101
assets uses two metadata requests, in addition to folder pagination requests.

```sh
framebridge folder-stats FOLDER_UUID
framebridge permissions ASSET_OR_FOLDER_UUID
```

Folder statistics expose the server's `countFiles`, `countFolders`, `sizeFiles`,
and `sizeFolders`. They are not a verified recursive transfer manifest or a
download-size estimate for selected proxies. Permission diagnostics show project
and containing-folder flags, denied checks, and restriction state. The server
remains authoritative; permissions can change after inspection.

## Download one rendition

```powershell
framebridge --profile downloads download ASSET_UUID --resolution 720p --output .\downloads\proxy.mp4 --dry-run
framebridge --profile downloads download ASSET_UUID --resolution 720p --output .\downloads\proxy.mp4 --report .\reports\download.json
```

Use an exact rendition key if multiple variants have the same height. `--resolution`
and `--rendition` are aliases. A missing proxy never selects the original instead.
Original downloads require `--rendition original` and the same verified host and
range response contract. Watermarked downloads and renditions without a positive
known byte size are refused. Audio transcodes currently lack verified size fields
in the adapter and are listed but cannot be downloaded.

To run a bounded sample, use a separate output name:

```powershell
framebridge --profile downloads download ASSET_UUID --resolution 360p --max-bytes 1048576 --output .\downloads\sample.mp4
```

This saves at most 1 MiB, not a complete or necessarily playable MP4. You cannot
extend a sample into a full download using the same output name.

Downloads use bounded 4 MiB GET ranges, three attempts per range, and refreshed
metadata after transient failures or expired URLs. They require HTTP 206 with
the exact requested range, do not follow redirects, and never send the browser
bearer to the media host. Only HTTPS `stream-download.frame.io` and
`assets.frame.io` are allowed; the latter serves original downloads.

To resume after cancellation, rerun the same command. Keep the `.part`,
`.framebridge.json`, and `.framebridge.lock` files beside the destination. The
checkpoint records identity, progress, a local SHA-256, and a strong ETag when
available; it never records signed URLs. The client verifies the partial hash
before resuming, rejects changed identities or ETags, and publishes the finished
file without overwriting an existing destination. Completion requires hard-link
support on the destination filesystem. A completed matching file is hash-checked
and skipped. An unrelated destination is never overwritten.

Use `--sha256 EXPECTED_HEX` if you have an independently trusted checksum. Without
it, the returned SHA-256 documents the received bytes, not independent proof that
they match a server checksum. When the server provides no strong ETag, remote
continuity is checked by media ID, rendition key, and size only.

## Plan a folder download

```powershell
framebridge --profile downloads download-folder FOLDER_UUID --recursive --resolution 360p --output .\downloads --max-total-bytes 10000000000
```

The default is a plan. Inspect its sizes and errors, then add `--execute` to
download. `--dry-run` takes precedence over `--execute`. The byte cap applies to
the sum of selected rendition sizes, including already completed files. It is
not a bandwidth allowance. Names include asset IDs to prevent collisions, and
recursive paths include folder IDs. Version stacks are reported as unsupported
instead of silently choosing a version. Other supported assets can still transfer
when the plan contains failures. Review the result's `failed` list.

Add `--report .\reports\batch.json` to save the final result. Reports never
overwrite existing files; choose a new report path for a resumed run. Per-file
checkpoints survive cancellation even if the final report has not been written.

## Upload a batch

Create a JSON manifest with explicit local paths and existing destination folders:

```json
[
  {"path": "D:\\delivery\\clip.mov", "folder_id": "DESTINATION_FOLDER_UUID"}
]
```

```powershell
framebridge upload-batch .\manifest.json --project PROJECT_UUID --experimental-multipart
```

This validates sources and permissions and prints a plan. Add `--execute` to
upload sequentially using the existing recovery journal. Files over 5 MiB require
`--experimental-multipart`. Rerun the manifest to resume; completed journal entries
are rechecked against remote status, size, project, and folder before being skipped.
No folders are created and no local tree is mapped implicitly.

## Upload a local folder hierarchy

```sh
framebridge upload-folder ./delivery --project PROJECT_UUID --folder-id PARENT_FOLDER_UUID
framebridge upload-folder ./delivery --project PROJECT_UUID --folder-id PARENT_FOLDER_UUID --execute --max-total-bytes 1000000000 --experimental-multipart --report ./reports/tree-upload.json
```

The default is a preview of every directory and file, their relative paths,
planned destination IDs when known, skipped links, and total source bytes.
Execution requires `--execute` and a positive `--max-total-bytes`. `--dry-run`
overrides `--execute`. The cap includes files already completed, not just bytes
that need retransmission. Files over 5 MiB also require
`--experimental-multipart`.

By default, uploading `delivery` creates a remote `delivery` folder under the
given parent. Nested folders and empty directories retain their names and
hierarchy. `--contents` places the root's contents directly under the destination.
The entry limit defaults to 10,000 and includes directories, files, and skipped
links. Increase `--limit` explicitly for larger trees; it also bounds inspection
of each existing remote folder. Local directory depth is limited to 256.

Conflict and source rules:

- Existing remote folders fail preflight unless you pass
  `--existing-folders reuse`. Reuse requires exactly one matching folder with
  exactly the same name. Case-only and Unicode-normalization collisions fail.
- Existing file names always fail unless the current profile's upload journal
  identifies that exact remote asset as this source's resumable upload. There
  is no overwrite, auto-version, or same-name/size shortcut.
- Symlinks, junctions, and other Windows reparse points inside the tree are
  reported and skipped. A linked root directory is refused.
- Empty files fail preflight by default. Add `--skip-empty-files` to
  `upload-folder` to omit zero-byte files from uploads. Both previews and execution
  reports list them in `skipped` with reason `empty_file`. They still count toward
  the entry limit. Empty directories are preserved, including directories that
  contain only skipped files. Special files still fail preflight.
- Sources changed since a journaled upload, renamed/moved destinations, and
  incomplete creation outcomes require reconciliation.

Rerun the same command with the same profile and intact state. Directory intents
in `operations.sqlite3` retain remote folder IDs, and `uploads.sqlite3` retains
file identities, accepted parts, and source hashes. Known directories are reused
without another `--existing-folders reuse`. Completed files are revalidated by
the existing uploader. New files can be added on a later run; removed local
files do not delete remote content.

The profile lock protects only cooperating processes using that state directory.
Do not run other writers against the same remote tree during a transfer. The
private API has no verified unique-name lock or conditional-create contract.
Destination listings are refreshed once per folder during file execution, not
once for every file; concurrent external changes cannot be made transactional.

File failures retain their state and appear in `failed`; other planned files can
continue. A folder-creation failure stops the run before entering that unresolved
branch. No automatic cleanup or rollback deletes partially created folders.

## Create a folder

```sh
framebridge mkdir Deliverables --project PROJECT_UUID --parent-id PARENT_FOLDER_UUID
framebridge mkdir Deliverables --project PROJECT_UUID --parent-id PARENT_FOLDER_UUID --execute
```

Use `--existing reuse` to reuse one exactly named existing folder. A successful
creation is journaled and can be rerun without duplication. Folder creation does
not grant users access or change restriction settings on existing folders.

If a request may have succeeded but returned no folder ID, automatic replay is
blocked. Inspect the local operation record and the remote folder before adopting
an explicitly reviewed ID:

```sh
framebridge operations
framebridge mkdir Deliverables --project PROJECT_UUID --parent-id PARENT_FOLDER_UUID --adopt-id REVIEWED_FOLDER_UUID
framebridge mkdir Deliverables --project PROJECT_UUID --parent-id PARENT_FOLDER_UUID --adopt-id REVIEWED_FOLDER_UUID --execute
```

Adoption validates type, exact name, project, and parent, then records that ID
locally. It does not create another folder. `operations` works without a login
and contains no credential or signed-URL data. Do not clear journals to force a
retry after an uncertain write.

## Move within one project

```sh
framebridge move ASSET_OR_FOLDER_UUID --project PROJECT_UUID --folder-id DESTINATION_FOLDER_UUID
framebridge move ASSET_OR_FOLDER_UUID --project PROJECT_UUID --folder-id DESTINATION_FOLDER_UUID --from-folder-id EXPECTED_SOURCE_FOLDER_UUID --execute
```

Preview is the default. `--from-folder-id` optionally guards the source location.
One asset or folder moves per request. The client checks source/destination
permissions, project identity, destination ancestry, name conflicts, and folder
subtree restrictions before writing. A folder cannot move into itself or a
descendant. Project roots, cross-project moves, restricted paths/subtrees, and
individual members of version stacks are refused. Moves never merge folders or
overwrite a same-name item. Subtree inspection uses `--limit` (default 10,000).
Do not concurrently change the source subtree or destination through other
clients: these checks are not a server-side transaction or permission snapshot.

A successful move is confirmed by reading the asset's parent again. Repeating
the command when the asset is already at the destination is a no-op, including
reconciliation after a lost response. If an uncertain move is not at the expected
destination, the client stops rather than blindly replaying it. Moving an upload
destination can invalidate its earlier upload/folder journal mapping; resume then
refuses the changed location rather than recreating the old hierarchy.

## Verify upload integrity

New uploads hash the source and planned parts before transferring bytes. Each
successful storage request must consume the complete part and match its planned
SHA-256. The client checks source size and modification time around each part,
then hashes the full source again before completion. These extra local reads
increase disk I/O. They detect local changes and stream truncation; they do not
prove what the server stored.

Before marking a transfer complete, the client queries the asset again and
requires a ready status, matching reported size, project, and destination folder.
Missing or mismatched metadata stops completion and retains the journal. Rerun
to recheck the same asset; do not clear the journal to force another upload.
Completed entries also undergo remote checks and, when a saved source hash is
available, a local hash comparison on rerun. Older completed entries have no
historical source hash; they can receive metadata checks, not retroactive content
verification. Older incomplete entries with accepted parts but no source hash
refuse automatic resume and require manual reconciliation.

Check status without downloading:

```sh
framebridge verify ASSET_UUID
```

Add `--local-file` to compare the local size with Frame.io's reported size:

```sh
framebridge verify ASSET_UUID --local-file ./clip.mov
```

This produces `verification_level: metadata` and `checksum_verified: false`.
Frame.io's reported size may reflect the size declared during asset creation;
matching it is not independent proof of stored byte integrity.

### Compare the original ETag

```sh
framebridge verify ASSET_UUID --local-file ./clip.mov --etag --report ./reports/etag-check.json
```

This explicit mode hashes every local byte and consumes one byte from the remote
original before hashing and one afterward. Both probes require a bounded HTTP
206, matching size and media identity, a supported strong ETag, and no redirect
or content encoding. Remote metadata and the local source's identity, size, and
timestamps are rechecked before reporting a match. Do not modify the source
during verification.

Single-part MD5-shaped ETags are compared with the local MD5. For a multipart
ETag, the suffix supplies the part count, and the client uses Framebridge's
equal-width part layout with a 5 MiB minimum. `--part-count N` asserts an expected
count. It does not select a different chunk size. Different upload layouts,
encryption, or object-copy behavior can make this interpretation inapplicable;
a mismatch does not by itself prove corruption.

A match returns `etag_match: true` and either `etag_md5_match` or
`multipart_etag_match`. `checksum_verified` remains false because this is not a
remote SHA-256 comparison. `local_sha256` documents the local source only. MD5
evidence is useful against accidental corruption, not adversarial alteration.
`--etag` and `--download-original-to` are mutually exclusive. There is no
automatic original download if the ETag is unsupported.

### Download and compare SHA-256

For an end-to-end comparison, request an original download explicitly:

```sh
framebridge verify ASSET_UUID --local-file ./clip.mov --download-original-to ./downloads/original.mov --max-download-bytes 1000000000 --report ./reports/verification.json
```

The positive byte cap is required. The client refuses an original larger than
the cap before starting the download, pins media identity and size across URL
renewal, downloads the full original, and compares SHA-256 with the local file.
It rechecks the local source and remote metadata afterward. A successful result
has `verification_level: original_sha256` and `checksum_verified: true`.
Use a new report path; existing reports are never overwritten. The downloaded
original is retained. Downloads can resume with their sidecars; a checksum
mismatch retains the partial file and reports failure.

This requires original-download permission and a supported media type and host.
There is no proxy fallback or automatic large download during ordinary uploads.
The upload journal's verification remains a metadata receipt; save the separate
verification report as evidence of the original comparison.

## Inspect transfer and review state

```powershell
framebridge transfers
framebridge --profile downloads verify ASSET_UUID
framebridge --profile downloads versions VERSION_STACK_UUID
framebridge --profile downloads comments ASSET_UUID --output .\reports\comments.json
```

`transfers` compares the selected profile's upload journal with remote status and
rechecks metadata for completed entries. It flags ambiguous creation, inaccessible
or failed assets, and completed-entry metadata mismatches. It does not repair
journals or resolve uncertain creation automatically. Use `renditions` to inspect
available proxies and `verify` for the integrity checks above. `TRANSCODED` alone does not
prove that an asset has a usable video proxy: the tested BRAW upload was classified
as audio by Frame.io. `versions` lists members of a version stack. Comments are
read-only, with timestamp values in microseconds and parent IDs for replies.

## Export an existing transcript

```sh
framebridge transcripts ASSET_UUID
framebridge transcript ASSET_UUID --transcription-id TRANSCRIPTION_UUID --format srt --output ./captions.srt
```

The list includes locale, readiness, and available formats, with signed URLs
removed. Select an explicit ready transcription ID and `srt`, `vtt`, or `text`.
The command checks transcript-download permission and downloads only an existing
resource. It never starts transcription or translation jobs.

The default cap is 10 MiB; change it with `--max-bytes`. Downloads use an
allowlisted HTTPS host, no redirects, no browser bearer, bounded streaming, and
no overwrites. Temporary output is removed after failure. A successful file is
published atomically after checking transcript metadata again. The returned
SHA-256 describes received bytes, not an independently verified server checksum.
There is no resumable partial-file contract for these small transcript exports.

Commands return JSON. `--json` also formats handled errors as JSON.
Progress goes to stderr. Exit codes are
0 for success, 1 for operation or per-file failure, 2 for invalid arguments, and
130 for interruption. Credentials and signed URL fields are not included in
reports; comments and filenames can still contain sensitive user content.

## Verification and limits

On September 23, 2026, the new CLI downloaded a bounded 1 MiB live 360p sample.
Live account/workspace/project browsing, folder listing, rendition metadata, and
an empty comment export succeeded. No full large proxy download was performed.
Populated comment exports, multi-page replies, version-stack members, and full
production-scale batch runs still need representative live validation. Offline tests cover
real HTTP range completion and interruption/resume, checksums, expired URLs,
wrong ranges, ignored ranges, local corruption, and output collisions.

The October 1 feature pass live-tested five folder creations, two tiny file
uploads totaling 125 bytes, a repeat without new folder/asset IDs, and same-project
file and folder moves. Test moves were returned to their original locations.
The nested test file also passed a full-original SHA-256 round trip (64 bytes).
The test hierarchy remains on Frame.io; no existing production content was moved.
Read-only metadata, folder statistics, permissions, and batch hydration were also
checked live. Transcript listing succeeded with an empty result; populated
transcript downloads still need representative live validation.

On October 2, populated transcript listing succeeded for a completed English
transcript with SRT, VTT, and text resources. The SRT export and its retry stopped
at the download-host allowlist check before requesting transcript bytes. The
download host still needs verification and support; populated live export is
not yet validated. The existing host restriction remains in place.

The expanded offline suite passes on Windows and WSL using the same working
checkout. It covers ambiguous folder creation, explicit adoption, repeated
execution, interrupted tree upload, conflict/permission checks, restricted and
cyclic moves, batching, ETag continuity, and bounded transcript exports.
The new ETag CLI also matched the existing single-part WAV and the 35-part BRAW,
consuming two original body bytes per verification. Neither ETag result is labeled
as a full-original SHA-256 comparison.

There are no remote delete or rename commands. The API client allowlists exact
upload, renewal, folder-creation, and single-asset move mutation documents. This prevents
accidental use through this client, not destructive actions by other software
using the same broadly authorized browser session.
