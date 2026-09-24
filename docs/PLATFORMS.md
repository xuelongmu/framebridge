# Cross-platform and WSL setup

Framebridge supports Python 3.10 or later on Windows, Linux, and macOS. Windows
and Ubuntu WSL have been tested. macOS uses POSIX storage and locking paths but
has not been tested on a Mac. No new package dependencies are required.

## Install on Windows

From the source directory in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
framebridge --help
```

If your shell blocks activation, use `.\.venv\Scripts\python.exe -m pip install -e .`
and `.\.venv\Scripts\python.exe -m framebridge --help` directly. You do not need
to change your system's script execution policy.

## Install on Linux or macOS

From the source directory:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
framebridge --help
python -m unittest discover -s tests -q
```

For browser login, install native Node.js/npm and the Playwriter Chrome
extension on the same operating system as the CLI. Run `framebridge login`.
The Windows Chrome extension is not automatically reachable by a WSL Node.js
installation. For WSL, use the explicit Windows-to-WSL session copy below.
Native Linux/macOS browser capture has not been live-tested.

## Install in WSL (Ubuntu)

Open your Ubuntu terminal. The following example installs the source from the
existing Windows checkout into a Linux virtual environment. It does not copy
credentials or modify the Windows Python environment:

```sh
python3 -m venv "$HOME/.venvs/framebridge"
. "$HOME/.venvs/framebridge/bin/activate"
python -m pip install /mnt/d/framebridge
framebridge --help
```

Replace `/mnt/d/framebridge` if your checkout is elsewhere. If Ubuntu reports that
`ensurepip` is unavailable, install its `python3-venv` package, then retry creating
the environment. Do not use Windows `python.exe` or `npx.cmd` as substitutes for
Linux runtimes.

This installs a snapshot of the source. After changing the Windows checkout,
rerun `python -m pip install --upgrade /mnt/d/framebridge` in the activated WSL
environment. To develop inside WSL instead, keep a source copy in your Linux home
directory and run `python -m pip install -e /path/to/source`.

Next, [copy your authorized Windows session](#copy-an-authorized-windows-session-to-wsl).
After copying, verify the profile from Ubuntu:

```sh
framebridge --profile downloads whoami
framebridge --profile downloads renditions ASSET_UUID
```

Replace `ASSET_UUID` with an accessible asset ID. These commands do not download
media. A bounded download test is:

```sh
framebridge --profile downloads download ASSET_UUID --resolution 360p --max-bytes 1048576 --output ./downloads/sample.mp4
```

The sample is at most 1 MiB and might not be playable. No original is selected
if a 360p rendition is unavailable.

## State and credential protection

Default state roots are:

| Platform | State root |
| --- | --- |
| Existing Windows repository with `.state` | Repository `.state` |
| New Windows installation | `%LOCALAPPDATA%\framebridge` |
| Linux and WSL | `$XDG_STATE_HOME/framebridge`, or `~/.local/state/framebridge` |
| macOS | `~/Library/Application Support/framebridge` |

Override the default with `FRAMEBRIDGE_STATE_DIR` or `--state-dir PATH`.
Profiles live under `profiles/NAME`; the default profile lives directly in the
state root. State no longer defaults to an installed package's directory.

Windows retains DPAPI-encrypted `session.dpapi` files. On Linux/macOS,
`session.json` stores credentials **unencrypted**, with owner-only file
permissions (0600) and an owner-only immediate directory (0700). The client
refuses other ownership, group/world access, and symlink session files or
immediate session directories. Credential updates use a private temporary file
and atomic replacement. No plaintext fallback is used on Windows.

This protects against other ordinary local users, not root, malware running as
you, or unprotected backups. Prefer an encrypted disk, keep the state root outside
source control, and do not share it. On WSL, store state in the Linux home
filesystem, not `/mnt/c` or `/mnt/d`, where Unix permissions may not be enforced
as expected. Logout removes the local session, not backups or browser sessions.

## Copy an authorized Windows session to WSL

First install Framebridge in a WSL virtual environment. Then run the following
from the Windows source directory with **Windows Python**, adapting the paths
and distro name. Replace `YOUR_LINUX_USER` with the output of `whoami` in Ubuntu.
The `downloads` Windows profile must already be signed in, and the WSL destination
must not contain an existing profile:

```powershell
python scripts/copy_session_to_wsl.py --source-state D:/framebridge/.state/profiles/downloads --distro Ubuntu --wsl-python /home/YOUR_LINUX_USER/.venvs/framebridge/bin/python --target-state /home/YOUR_LINUX_USER/.local/state/framebridge/profiles/downloads
```

The helper unlocks DPAPI inside the Windows process and sends the session over
the child process's standard-input pipe. It never prints credentials or includes
them in process arguments. There is no intermediate plaintext file on Windows.
WSL saves the owner-only session file. The destination must be unused; the helper
refuses to replace existing sessions or profile identities. It copies identity
metadata when present, but does not copy upload journals or delivery files.

The two copies share renewable credentials. Use only one at a time: refreshing
one can invalidate the other. Sign in again before switching if authorization
fails. Do not run Windows and WSL against a shared state directory; their process
locks are not a cross-OS coordination mechanism. Existing Windows upload journals
also contain Windows paths and are not portable to Linux.

## Paths and troubleshooting

- Use native paths: `D:\delivery\clip.mov` on Windows, `/mnt/d/delivery/clip.mov`
  in WSL, or `/home/you/delivery/clip.mov` on Linux. Quote paths with spaces.
- Put global options before the command, for example
  `framebridge --profile downloads --json whoami`.
- If the CLI rejects credential permissions, select a private state directory
  in your Linux home. Do not relax the permission checks or print the session to
  diagnose the problem.
- If authorization fails after using the other session copy, sign in again on
  the active platform. Copying a DPAPI file directly into Linux does not work.
- Run `python -m unittest discover -s tests -q` from a source checkout, not from
  a directory containing only an installed package.

## This machine's WSL installation

The Ubuntu source copy is `/home/xuelong/framebridge`. Its virtual environment is
`/home/xuelong/.venvs/framebridge`. Activate and verify it with:

```sh
. /home/xuelong/.venvs/framebridge/bin/activate
framebridge --profile downloads whoami
```

On September 23, 2026, 44 tests ran successfully on each platform: Windows skipped
two POSIX permission tests; WSL skipped the Windows-only DPAPI test. A live WSL
identity check succeeded, followed by a bounded 1 MiB 360p download with the same
SHA-256 as the Windows sample. No full large remote download or live Linux upload
was performed. The offline suite exercises real HTTP upload/download recovery.

The WSL source is a copy, not another Git checkout. Changes in `D:\framebridge`
do not automatically update it. Keep the copy in sync before further testing.
The CLI now preserves case in POSIX upload identities; Windows identities retain
their previous case-insensitive behavior. The legacy Windows manifest adapter
keeps its original normalization rules and is not a Linux path converter.
