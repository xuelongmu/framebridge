# Framebridge

An unofficial, cross-platform CLI for uploading, downloading, and browsing
Frame.io V4 media. Sign in with your existing browser session—no Adobe Developer
Console setup required.

Framebridge uses the **private Frame.io V4 web GraphQL API**, not Adobe's supported
public REST API. Private operations can change without notice.

## Features

- Resumable uploads and proxy downloads.
- Account, workspace, project, and folder browsing, with folder-scoped search.
- Batch transfer plans, named login profiles, and JSON reports.
- Recursive folder uploads, including nested and empty folders.
- Planned folder creation and same-project asset/folder moves.
- Batched browsing, custom-field inspection, folder statistics, and permission diagnostics.
- Existing transcript export, comments, and version inspection.
- Explicit original ETag comparison without a full-original download.

There are no remote delete or rename commands. Folder creation, moves, transcript
export, and ETag verification are new in this working checkout; they have not
been published as a release.

## Install

You need Python 3.10 or later. Browser login also needs native Node.js with `npx`, Chrome, and the
[Playwriter extension](https://github.com/remorses/playwriter).

### Windows (PowerShell)

From the repository directory, run:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

### macOS and Linux with uv (Bash or Zsh)

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run
from the repository directory:

```sh
uv venv --python 3.12 .venv
. .venv/bin/activate
uv pip install -e .
framebridge --help
```

See [macOS and Linux setup with uv](docs/PLATFORMS.md#macos-and-linux-setup-with-uv) for details.

#### Alternative: standard venv

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
framebridge --help
```

### WSL (Ubuntu)

Use a Linux virtual environment and keep credentials in the Linux filesystem.
Follow [WSL installation and session transfer](docs/PLATFORMS.md#install-in-wsl-ubuntu)
to reuse an authorized Windows login without printing its credentials.
See [Cross-platform setup](docs/PLATFORMS.md) for state locations, credential
protection, and platform limitations. After installation, use `framebridge` or
`python -m framebridge` interchangeably.

## Sign in and inspect

Open Frame.io in Chrome, sign in, and click Playwriter's extension icon to enable
the tab. Then run:

```powershell
framebridge login
framebridge whoami
framebridge accounts
framebridge project PROJECT_UUID
framebridge browse FOLDER_UUID
```

The login command opens and closes its own tab. It captures only the Frame.io
session and Apollo client headers. Windows encrypts the session with user-scoped
DPAPI. Linux/macOS use an unencrypted owner-only `session.json` (0600) in a
private directory (0700). It does not print the credentials.
Once signed in, routine commands use the saved session and renew it while the
refresh credentials remain valid. Playwriter is needed for login, not each
transfer. If renewal fails, sign in again. Do not use copies of the same session
concurrently; browser and CLI token renewal are not synchronized.

Replace `PROJECT_UUID`, `FOLDER_UUID`, and `ASSET_UUID` in examples with your
Frame.io resource IDs. Use `framebridge COMMAND --help` for command options.

## Upload a file

```powershell
framebridge upload ./clip.mov --project PROJECT_UUID --folder-id FOLDER_UUID --experimental-multipart
```

The destination folder must already exist. Uploads are sequential, and files
over 5 MiB require `--experimental-multipart`.

## Upload a folder hierarchy

```sh
framebridge upload-folder ./delivery --project PROJECT_UUID --folder-id PARENT_FOLDER_UUID
framebridge upload-folder ./delivery --project PROJECT_UUID --folder-id PARENT_FOLDER_UUID --execute --max-total-bytes 1000000000 --experimental-multipart
```

The first command previews the tree. The second creates `delivery` and its nested
folders, then uploads files to their matching destinations, with a 1 GB source
size cap. Empty folders are preserved; symlinks and Windows junctions are skipped.
Use `--contents` to omit the local root folder. Existing folders require
`--existing-folders reuse` unless their IDs are already in this profile's journal.
Existing files are never overwritten or silently accepted based on their names.
Keep both `uploads.sqlite3` and `operations.sqlite3` to resume safely.

See [Recursive uploads and folder operations](docs/TRANSFERS.md#upload-a-local-folder-hierarchy)
for conflicts, limits, and recovery.

## Create or move folders

```sh
framebridge mkdir NewFolder --project PROJECT_UUID --parent-id PARENT_FOLDER_UUID
framebridge move ASSET_OR_FOLDER_UUID --project PROJECT_UUID --folder-id DESTINATION_FOLDER_UUID
```

Both commands preview by default. Add `--execute` after reviewing the result.
Moves are same-project only, never merge or overwrite, and refuse restricted
folder paths or subtrees that could change access.

## Download a proxy

```sh
framebridge renditions ASSET_UUID
framebridge download ASSET_UUID --resolution 720p --output ./downloads/proxy.mp4 --dry-run
```

Inspect the plan, then remove `--dry-run` to download. Use an exact rendition key
if multiple variants have the same resolution. Framebridge never falls back to
the original; original downloads require `--rendition original`.

For a bounded sample, add `--max-bytes 1048576` and use a separate output name.
The sample is at most 1 MiB and might not be playable.

## Profiles and batch transfers

```sh
framebridge --profile work login
framebridge --profile work whoami
framebridge --profile work --json browse FOLDER_UUID
```

Global options such as `--profile` and `--json` go before the command. Each profile
has separate credentials and upload history.

`upload-batch` and `download-folder` produce plans by default. Add `--execute` to
transfer files. See [Transfer and inspection commands](docs/TRANSFERS.md) for
manifest formats, download limits, reports, comments, and version inspection.

## Resume safely

Keep `uploads.sqlite3` in the selected profile's state directory; see
[state locations](docs/PLATFORMS.md#state-and-credential-protection).
Each completed part and external creation boundary
is committed to SQLite. Rerunning the same command resumes the recorded asset.
If the process loses a creation response, it stops with an **ambiguous outcome**
instead of creating a duplicate. Reconcile the recorded path, batch ID, and asset
ID with Frame.io before changing the journal; there is no automatic reset command.
A changed file is rejected on resume. New uploads record SHA-256 hashes of the
source and planned parts, check streamed bytes, and recheck the source afterward.
Before marking completion, Framebridge checks remote status, file size, project,
and destination folder. Completed entries are revalidated before they are skipped.

For downloads, keep the `.part` and `.framebridge.json` files beside the output
and rerun the same command. Existing unrelated files are never overwritten.

## Verify a transfer

Check remote status and compare the reported size with a local file:

```sh
framebridge verify ASSET_UUID --local-file ./clip.mov
```

Size agreement is not a checksum match. To download the original and compare its
SHA-256, explicitly choose a separate destination and a byte cap:

```sh
framebridge verify ASSET_UUID --local-file ./clip.mov --download-original-to ./downloads/original.mov --max-download-bytes 1000000000 --report ./reports/verification.json
```

This example permits an original up to 1 GB. The command downloads the whole
original, never a proxy, and reports `checksum_verified: true` only after the
comparison succeeds. See [Verification details](docs/TRANSFERS.md#verify-upload-integrity).

To compare a compatible original ETag without downloading the original:

```sh
framebridge verify ASSET_UUID --local-file ./clip.mov --etag
```

This reads the entire local source and consumes two remote body bytes, probing
the original before and after hashing. Multipart comparison assumes the
Framebridge part layout; optionally assert its count with `--part-count 35`.
A match reports `etag_match: true` with `verification_level: multipart_etag_match`
or `etag_md5_match`. It keeps `checksum_verified: false` to distinguish this
conditional MD5 evidence from a full-original SHA-256 comparison.

## Security and limitations

`.gitignore` excludes `.env`, Python environments/caches, credentials, local state,
delivery manifests, and logs. Keep credentials and media outside source control.
DPAPI protects against offline casual inspection, not malicious software running
as your Windows user. Tokens and signed S3 URLs must never appear in Git or logs.
Its mutation allowlist permits session renewal, the upload flow, folder creation,
and the single-asset move document. CLI preflight restricts moves to one project.
This is a client restriction, not a restriction on the browser credential's permissions.

Some media types and watermark workflows are unsupported. See
[API evidence and limitations](docs/API.md) for compatibility and validation details.

## Development

Run the test suite from the repository directory with your virtual environment active:

```powershell
python -m unittest discover -s tests -v
```
