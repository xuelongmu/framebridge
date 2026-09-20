import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .api import Api
from .storage import Journal, SessionStore, UploaderError, exclusive_lock
from .uploader import upload

ROOT = Path(__file__).resolve().parent.parent


def normalized(value):
    return str(value).strip().replace('\\', '/').rstrip('/').casefold()


def remaining_plan(data, contains=None):
    files = [line.strip() for line in (data / 'missing_files.txt').read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    progress_path = data / 'upload_progress.json'
    progress = json.loads(progress_path.read_text(encoding='utf-8-sig')) if progress_path.exists() else []
    if not isinstance(progress, list):
        raise UploaderError('Legacy progress must be a JSON list. It has not been modified.')
    completed = {normalized(p) for p in progress}
    folders = {}
    for line in (data / 'folder_ids.csv').read_text(encoding='utf-8-sig').splitlines():
        if '|' in line:
            folder_id, path = line.split('|', 1)
            folders[normalized(path)] = folder_id.strip()
    pending = []
    for name in files:
        if normalized(name) in completed or (contains and contains.casefold() not in name.casefold()):
            continue
        parent = normalized(name).rsplit('/', 1)[0]
        pending.append((Path(name), folders.get(parent)))
    return files, completed, pending


def main(argv=None):
    load_dotenv(ROOT / '.env')
    parser = argparse.ArgumentParser(description='Experimental Frame.io V4 web API uploader (not public V4 REST).')
    parser.add_argument('--state-dir', type=Path, default=ROOT / '.state')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('login', help='Capture your signed-in browser session into Windows DPAPI storage')
    sub.add_parser('status', help='Read the authenticated user')
    project = sub.add_parser('project')
    project.add_argument('project_id')
    listing = sub.add_parser('list')
    listing.add_argument('folder_id')
    for name in ('upload', 'remaining'):
        command = sub.add_parser(name)
        command.add_argument('--project', default=os.getenv('FRAMEIO_PROJECT_ID'))
        command.add_argument('--experimental-multipart', action='store_true')
        if name == 'upload':
            command.add_argument('path', type=Path)
            command.add_argument('--folder-id', default=os.getenv('FRAMEIO_FOLDER_ID'))
        else:
            command.add_argument('--data-dir', type=Path, default=ROOT / 'data')
            mode = command.add_mutually_exclusive_group()
            mode.add_argument('--upload', action='store_true', help='Explicitly start uploads; default is dry-run')
            mode.add_argument('--dry-run', action='store_true')
            command.add_argument('--folder', help='Filter local paths by substring')
    args = parser.parse_args(argv)
    try:
        if args.command == 'remaining':
            files, done, pending = remaining_plan(args.data_dir, args.folder)
            print(f'Manifest: {len(files)}; legacy completed entries: {len(done)}; pending: {len(pending)}')
            print('Legacy completion is trusted as local history, not remote verification.')
            if not args.upload:
                print(f'Dry run. Missing folder mappings: {sum(folder is None for _, folder in pending)}')
                return 0
            if not pending:
                return 0
        if args.command in ('upload', 'remaining') and not args.project:
            raise UploaderError('Set --project or FRAMEIO_PROJECT_ID to the V4 project ID.')
        if args.command == 'upload' and not args.folder_id:
            raise UploaderError('Set --folder-id or FRAMEIO_FOLDER_ID to an existing V4 folder ID.')
        with exclusive_lock(args.state_dir / 'process.lock'):
            store = SessionStore(args.state_dir / 'session.dpapi')
            if args.command == 'login':
                from .login import login
                login(store, args.state_dir)
                print('Encrypted browser session saved. Run status to validate it.')
                return 0
            api = Api(store)
            if args.command == 'status':
                print(json.dumps(api.me(), indent=2))
            elif args.command == 'project':
                print(json.dumps(api.project(args.project_id), indent=2))
            elif args.command == 'list':
                for item in api.children(args.folder_id):
                    print(json.dumps(item))
            else:
                project = api.project(args.project)
                if not project['permissions']['canCreateAsset']:
                    raise UploaderError('This session cannot create assets in the project.')
                targets = [(args.path, args.folder_id)] if args.command == 'upload' else pending
                for path, folder_id in targets:
                    if not folder_id or not path.is_file():
                        raise UploaderError('Preflight failed: missing file or folder mapping. No upload started.')
                for folder_id in {f for _, f in targets}:
                    if not api.folder(folder_id, args.project)['permissions']['canCreateChildren']:
                        raise UploaderError('Destination folder does not permit uploads.')
                journal = Journal(args.state_dir / 'uploads.sqlite3')
                try:
                    for path, folder_id in targets:
                        asset_id = upload(api, journal, path, args.project, project['account']['id'], folder_id,
                                          experimental=args.experimental_multipart)
                        print(f'Complete: {path.name} (asset {asset_id})')
                finally:
                    journal.close()
        return 0
    except UploaderError as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Interrupted. Keep local state and rerun to resume.', file=sys.stderr)
        return 130
    except Exception as error:
        # Third-party errors can contain HTTP credentials or signed URLs.
        print(f'Operation stopped ({type(error).__name__}); secrets omitted. Keep state and check the documentation.', file=sys.stderr)
        return 1
