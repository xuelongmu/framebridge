# Frame.io V4 web uploader

A Windows Python uploader for the **private Frame.io V4 web GraphQL API**. This is
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

Keep your existing `.env`. If you do not have one, copy `.env.example` to `.env`.
Set `FRAMEIO_PROJECT_ID` and optionally `FRAMEIO_FOLDER_ID` to your V4 resource
IDs, or supply the command-line arguments below. No API key is required.

## Sign in and inspect

Open Frame.io in Chrome, sign in, and click Playwriter's extension icon to enable
the tab. Then run:

```powershell
python -m frameio_uploader login
python -m frameio_uploader status
python -m frameio_uploader project YOUR_PROJECT_UUID
python -m frameio_uploader list YOUR_FOLDER_UUID
```

The login command opens and closes its own tab. It captures only the Frame.io
session and Apollo client headers, then encrypts the session with Windows
user-scoped DPAPI in `.state/session.dpapi`. It does not print the credentials.
Close other Frame.io tabs during a long upload to reduce refresh-token rotation
conflicts. If authorization fails, run `login` again; browser and uploader refresh
are not synchronized. Do not run two state directories using the same session.

## Upload a file

```powershell
python -m frameio_uploader upload .\sample.txt --project YOUR_PROJECT_UUID --folder-id YOUR_FOLDER_UUID
```

The client checks project and destination permissions, creates one transfer batch
and asset, streams bytes to S3, waits for completion, and updates the transfer
batch. It does not create folders or delete assets.

Files over 5 MiB require `--experimental-multipart`. Multipart layout comes from
the observed web client but has **not been live-tested**. Validate with disposable
data before using it for production deliveries. Uploads are sequential.

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
No remote is configured and no source or credential is published automatically.

```powershell
python -m unittest discover -s tests -v
```

See [API evidence and limitations](docs/API.md) for tested operations and remaining
validation. The new CLI's end-to-end browser login and live requests still need
validation after reconnecting the browser extension.
