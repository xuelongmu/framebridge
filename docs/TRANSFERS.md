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
status, and rendition metadata. Signed URL fields are excluded from output.

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
bearer to the media host. Only HTTPS `stream-download.frame.io` is currently allowed.

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
are trusted locally. No folders are created and no local tree is mapped implicitly.

## Inspect transfer and review state

```powershell
framebridge transfers
framebridge --profile downloads verify ASSET_UUID
framebridge --profile downloads versions VERSION_STACK_UUID
framebridge --profile downloads comments ASSET_UUID --output .\reports\comments.json
```

`transfers` compares the selected profile's upload journal with remote status and
flags ambiguous creation or inaccessible assets. It does not repair journals or
resolve uncertain creation automatically. `verify` reports available ready video
proxies; it does not download or compare checksums. `TRANSCODED` alone does not
prove that an asset has a usable video proxy: the tested BRAW upload was classified
as audio by Frame.io. `versions` lists members of a version stack. Comments are
read-only, with timestamp values in microseconds and parent IDs for replies.

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
remote batch runs still need representative live validation. Offline tests cover
real HTTP range completion and interruption/resume, checksums, expired URLs,
wrong ranges, ignored ranges, local corruption, and output collisions.

There are no remote delete, move, rename, or folder-creation commands. The API
client allowlists exact upload and renewal mutation documents. This prevents
accidental use through this client, not destructive actions by other software
using the same broadly authorized browser session.
