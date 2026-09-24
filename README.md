# Framebridge

An unofficial Frame.io V4 CLI. The distribution, Python module, and installed
command are all named `framebridge`.

A Windows Python transfer and inspection CLI for the **private Frame.io V4 web GraphQL API**. This is
not Adobe's supported public V4 REST API. It uses your own browser login without
Adobe Developer Console setup. Private operations can change without notice.

The previous API-key uploader is preserved in the local Git tag
`legacy-v2-baseline`. The current version never uses `FRAMEIO_TOKEN`.

## Install

You need Windows, Python 3.10 or later, Node.js with `npx`, Chrome, and the
[Playwriter extension](https://github.com/remorses/playwriter).

From this directory, run:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

After installation, use `framebridge login` or `python -m framebridge login`.
If you installed the previous `frameio-web-uploader` distribution, uninstall it
with `python -m pip uninstall frameio-web-uploader` before reinstalling this one.
The repository directory is `D:\framebridge`. Credentials and upload journals
move with the repository and remain in its `.state` directory. Update any saved
terminal shortcuts or scheduled tasks that reference the old directory.

Keep your existing `.env`. If you do not have one, copy `.env.example` to `.env`.
Set `FRAMEIO_PROJECT_ID` and optionally `FRAMEIO_FOLDER_ID` to your V4 resource
IDs, or supply the command-line arguments below. No API key is required.

## Sign in and inspect

Open Frame.io in Chrome, sign in, and click Playwriter's extension icon to enable
the tab. Then run:

```powershell
python -m framebridge login
python -m framebridge status
python -m framebridge project YOUR_PROJECT_UUID
python -m framebridge list YOUR_FOLDER_UUID
```

The login command opens and closes its own tab. It captures only the Frame.io
session and Apollo client headers, then encrypts the session with Windows
user-scoped DPAPI in `.state/session.dpapi`. It does not print the credentials.
Close other Frame.io tabs during a long upload to reduce refresh-token rotation
conflicts. If authorization fails, run `login` again; browser and uploader refresh
are not synchronized. Do not run two state directories using the same session.

## Upload a file

```powershell
python -m framebridge upload .\sample.txt --project YOUR_PROJECT_UUID --folder-id YOUR_FOLDER_UUID
```

The client checks project and destination permissions, creates one transfer batch
and asset, streams bytes to S3, waits for completion, and updates the transfer
batch. It does not create folders or delete assets.

Files over 5 MiB require `--experimental-multipart`. A 68.1 GB, 35-part live upload
completed successfully, including a forced interruption and resume. Broader
failure coverage and full-file checksum verification remain incomplete. Validate
with disposable data before production deliveries. Uploads are sequential.

## Continue the legacy manifest

```powershell
python upload_remaining.py --dry-run
python upload_remaining.py --folder admin --upload --project YOUR_PROJECT_UUID
```

The default is a dry run. Inputs are `data/missing_files.txt`, pipe-delimited
`data/folder_ids.csv` (`folder UUID|local directory`), and the optional JSON list
`data/upload_progress.json`. Existing completion entries are trusted as local
history, not proof of remote presence. The new client never modifies these files.
The supplied legacy history currently marks every manifest path completed, so
the default plan has no pending uploads. Do not clear history to force a rerun.
Folder IDs must belong to the specified V4 project. Missing mappings stop uploads.

## Resume safely

Keep `.state/uploads.sqlite3`. Each completed part and external creation boundary
is committed to SQLite. Rerunning the same command resumes the recorded asset.
If the process loses a creation response, it stops with an **ambiguous outcome**
instead of creating a duplicate. Reconcile the recorded path, batch ID, and asset
ID with Frame.io before changing the journal; there is no automatic reset command.
A changed file (size or modification time) is rejected on resume. Already complete
journal entries are skipped locally, not revalidated remotely.

## Security and verification

`.gitignore` excludes `.env`, Python environments/caches, credentials, local state,
delivery manifests, logs, and the original handoff. Only `.env.example` is tracked.
DPAPI protects against offline casual inspection, not malicious software running
as your Windows user. Tokens and signed S3 URLs must never appear in Git or logs.
The repository has a private GitHub remote. Nothing is published automatically.
The client exposes no remote delete, move, rename, or folder-creation operations.
Its mutation allowlist permits only session renewal and the existing upload flow.
This is a client restriction, not a restriction on the browser credential's permissions.

```powershell
python -m unittest discover -s tests -v
```

See [API evidence and limitations](docs/API.md) for tested operations and remaining
validation. Browser login, identity, project/folder permission checks, and a full
68.1 GB multipart upload with process-interruption recovery were live-tested on
September 21, 2026. All 35 parts completed, the uploader exited successfully, and
a subsequent server query returned `TRANSCODED`. A download-and-checksum comparison
has not been performed. Do not treat this test as production certification.

## Download proxies and browse media

See [Transfer and inspection commands](docs/TRANSFERS.md) for profiles, proxy
selection, resumable downloads, batch plans, reports, and read-only review tools.

```powershell
python -m framebridge --profile downloads whoami
python -m framebridge --profile downloads renditions YOUR_ASSET_UUID
python -m framebridge --profile downloads download YOUR_ASSET_UUID --resolution 360p --output .\downloads\proxy.mp4 --dry-run
```

Remove `--dry-run` to download the complete selected rendition. To test only
1 MiB, add `--max-bytes 1048576`; a sample might not be playable. The downloader
never falls back to the original. Originals require `--rendition original`.
