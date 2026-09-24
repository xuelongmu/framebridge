"""CLI contracts: explicit profiles, URL-free JSON, read-only review tools."""
import argparse
import contextlib
import json
import os
import re
import sys
from pathlib import Path
from dotenv import load_dotenv

from .catalog import Catalog, public
from .downloader import download, safe_name
from .storage import Journal, SessionStore, UploaderError, atomic_write, exclusive_lock, default_state_dir, source_identity


def profile_dir(root, name):
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,47}', name):
        raise UploaderError('Profile name must use lowercase letters, digits, underscores or hyphens.')
    return root if name == 'default' else root / 'profiles' / name


def emit(value):
    print(json.dumps(public(value), indent=2, ensure_ascii=False))


def report_write(path, value):
    if path:
        path = Path(path)
        if path.exists():
            raise UploaderError('Report already exists; choose another path.')
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x', encoding='utf-8') as f:
            json.dump(public(value), f, indent=2, ensure_ascii=False)


def parser():
    from .cli import ROOT
    load_dotenv(ROOT / '.env')
    p = argparse.ArgumentParser(description='Framebridge: unofficial Frame.io V4 transfers and read-only inspection')
    p.add_argument('--state-dir', type=Path, default=default_state_dir(ROOT))
    p.add_argument('--profile', default='default')
    p.add_argument('--json', action='store_true', help='One JSON result on stdout; progress on stderr')
    sub = p.add_subparsers(dest='command', required=True)
    for cmd in ('login','logout','profiles','whoami','status','accounts','transfers'):
        sub.add_parser(cmd)
    for cmd, field in (('workspaces','account_id'),('projects','workspace_id'),('project','project_id'),
                       ('inspect','asset_id'),('renditions','asset_id'),('verify','asset_id'),('versions','asset_id')):
        sub.add_parser(cmd).add_argument(field)
    for cmd in ('browse','search'):
        s = sub.add_parser(cmd)
        s.add_argument('folder_id')
        if cmd == 'search': s.add_argument('query')
        s.add_argument('--recursive', action='store_true')
        s.add_argument('--limit', type=int, default=10000)
    s = sub.add_parser('comments')
    s.add_argument('asset_id')
    s.add_argument('--output', type=Path)
    for cmd in ('download','download-folder'):
        s = sub.add_parser(cmd)
        s.add_argument('asset_id' if cmd == 'download' else 'folder_id')
        s.add_argument('--rendition', '--resolution', dest='rendition', required=True,
                       help='Exact rendition key or resolution such as 1080p; original must be explicit')
        s.add_argument('--output', type=Path, required=True)
        s.add_argument('--dry-run', action='store_true')
        s.add_argument('--report', type=Path)
        if cmd == 'download':
            s.add_argument('--max-bytes', type=int, help='Create a bounded sample, not a complete video')
            s.add_argument('--sha256')
        else:
            s.add_argument('--execute', action='store_true', help='Without this flag, list the plan only')
            s.add_argument('--recursive', action='store_true')
            s.add_argument('--limit', type=int, default=10000)
            s.add_argument('--max-total-bytes', type=int, required=True)
    s = sub.add_parser('upload-batch')
    s.add_argument('manifest', type=Path, help='JSON list of {path, folder_id}; remote folders must already exist')
    s.add_argument('--project', required=True)
    s.add_argument('--execute', action='store_true')
    s.add_argument('--experimental-multipart', action='store_true')
    s.add_argument('--report', type=Path)
    s = sub.add_parser('upload', help='Upload one file to an existing folder')
    s.add_argument('path', type=Path)
    s.add_argument('--project', default=os.getenv('FRAMEIO_PROJECT_ID'))
    s.add_argument('--folder-id', default=os.getenv('FRAMEIO_FOLDER_ID'))
    s.add_argument('--experimental-multipart', action='store_true')
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    p = parser()
    args = p.parse_args(argv)
    try:
        state = profile_dir(args.state_dir, args.profile)
        if args.command == 'profiles':
            names = ['default'] + sorted(x.name for x in (args.state_dir/'profiles').glob('*') if x.is_dir())
            emit([{'name':name,'logged_in':SessionStore(profile_dir(args.state_dir,name)/'session.dpapi').path.exists()} for name in names])
            return 0
        if getattr(args, 'report', None) and args.report.exists():
            raise UploaderError('Report already exists; choose a different path before starting transfers.')
        with exclusive_lock(state / 'process.lock'):
            store = SessionStore(state / 'session.dpapi')
            if args.command == 'logout':
                store.path.unlink(missing_ok=True)
                emit({'profile':args.profile,'logged_out':True,'note':'Local credential removed; browser session and upload history preserved.'})
                return 0
            if args.command == 'login':
                from .login import login
                pending = SessionStore(state / 'pending-session.dpapi')
                identity_path = state / 'identity.json'
                # Bind pre-profile installations before replacing their credentials.
                if store.path.exists() and not identity_path.exists():
                    previous = Catalog(store).me()
                    atomic_write(identity_path, json.dumps(previous).encode())
                try:
                    with contextlib.redirect_stdout(sys.stderr): login(pending, state)
                    identity = Catalog(pending).me()
                    if identity_path.exists() and json.loads(identity_path.read_text())['id'] != identity['id']:
                        raise UploaderError('This profile belongs to another user. Use a new --profile name.')
                    store.save(pending.load())
                    atomic_write(identity_path, json.dumps(identity).encode())
                    emit({'profile':args.profile,'user':identity})
                finally:
                    pending.path.unlink(missing_ok=True)
                return 0
            api = Catalog(store)
            command = args.command
            if command in ('whoami','status'): result = {'profile':args.profile,'user':api.me()}
            elif command == 'accounts': result = api.accounts()
            elif command == 'workspaces': result = api.workspaces(args.account_id)
            elif command == 'projects': result = api.projects(args.workspace_id)
            elif command == 'project': result = api.project(args.project_id)
            elif command == 'inspect': result = api.media(args.asset_id)
            elif command == 'versions': result = api.asset(args.asset_id).get('versions', [])
            elif command == 'renditions': result = api.renditions(args.asset_id)
            elif command == 'verify':
                a = api.media(args.asset_id)
                result = {'asset':a,'ready_video_proxies':[r['key'] for r in a.get('media',{}).get('videoTranscodes',[]) if r.get('encodeStatus')=='SUCCESS' and r.get('downloadUrl')],
                          'checksum_verified':False}
            elif command in ('browse','search'):
                result = [item for item in api.walk(args.folder_id,args.recursive,args.limit)
                          if command != 'search' or args.query.casefold() in item['name'].casefold()]
            elif command == 'comments':
                result = api.comments(args.asset_id)
                report_write(args.output,result)
            elif command == 'transfers':
                journal = Journal(state/'uploads.sqlite3')
                try:
                    result = []
                    for (raw,) in journal.db.execute('SELECT record FROM uploads'):
                        row = json.loads(raw)
                        if row.get('asset_id'):
                            try: row['remote_status'] = api.status(row['asset_id'])
                            except UploaderError: row['remote_status'] = 'unavailable'
                        row['needs_reconciliation'] = row['phase'] in ('creating_batch','creating_asset') or row.get('remote_status') == 'unavailable'
                        result.append(row)
                finally: journal.close()
            elif command == 'download':
                if args.dry_run:
                    result = dict(api.rendition(args.asset_id,args.rendition), output=str(args.output), dry_run=True)
                else:
                    result = download(api,args.asset_id,args.rendition,args.output,max_bytes=args.max_bytes,
                                      expected_sha256=args.sha256,progress=progress)
                report_write(args.report,result)
            elif command == 'download-folder':
                result = download_folder(api,args)
                report_write(args.report,result)
            elif command == 'upload-batch':
                result = upload_batch(api,state,args)
                report_write(args.report,result)
            elif command == 'upload':
                result = upload_one(api, state, args)
            else: raise UploaderError('Unsupported command.')
            emit(result)
            return 1 if isinstance(result,dict) and result.get('failed') else 0
    except KeyboardInterrupt:
        emit({'ok':False,'error':'Interrupted; preserve partial files and state to resume.'})
        return 130
    except Exception as error:
        message = str(error) if isinstance(error,UploaderError) else f'{type(error).__name__}: operation stopped; details suppressed to protect credentials.'
        if args.json: emit({'ok':False,'error':message})
        else: print(message,file=sys.stderr)
        return 1


def progress(event):
    print(json.dumps({'progress':event}),file=sys.stderr,flush=True)


def upload_one(api, state, args):
    from .uploader import upload, PART_MIN
    if not args.project or not args.folder_id:
        raise UploaderError('Set --project and --folder-id, or FRAMEIO_PROJECT_ID and FRAMEIO_FOLDER_ID.')
    if not args.path.is_file() or args.path.stat().st_size <= 0:
        raise UploaderError('Choose a nonempty regular file.')
    if args.path.stat().st_size > PART_MIN and not args.experimental_multipart:
        raise UploaderError('Large files require --experimental-multipart.')
    project = api.project(args.project)
    if not project['permissions']['canCreateAsset']:
        raise UploaderError('Project uploads are not permitted.')
    if not api.folder(args.folder_id, args.project)['permissions']['canCreateChildren']:
        raise UploaderError('Folder uploads are not permitted.')
    journal = Journal(state / 'uploads.sqlite3')
    try:
        asset_id = upload(api, journal, args.path, args.project, project['account']['id'],
                          args.folder_id, experimental=args.experimental_multipart)
    finally:
        journal.close()
    return {'path':str(args.path), 'asset_id':asset_id}


def download_folder(api,args):
    plan, failed = [], []
    if args.max_total_bytes <= 0:
        raise UploaderError('--max-total-bytes must be positive.')
    for a in api.walk(args.folder_id,args.recursive,args.limit):
        if a['__typename'] == 'FolderAsset': continue
        if a['__typename'] == 'VersionStackAsset':
            failed.append({'asset_id':a['id'],'error':'Version stack: use versions and download an explicit asset ID.'})
            continue
        try:
            r = api.rendition(a['id'],args.rendition)
            name = safe_name(Path(a['name']).stem) + '--' + a['id'] + '--' + safe_name(r['key']) + ('.mp4' if r['key']!='original' else Path(safe_name(a['name'])).suffix)
            plan.append({'asset_id':a['id'],'key':r['key'],'bytes':r['filesizeInBytes'],
                         'output':str(args.output/a['relative_parent']/name)})
        except UploaderError as e: failed.append({'asset_id':a['id'],'error':str(e)})
    total = sum(r['bytes'] for r in plan)
    if total > args.max_total_bytes:
        raise UploaderError(f'Plan requires {total} bytes, exceeding --max-total-bytes. No downloads started.')
    if not args.execute or args.dry_run:
        return {'dry_run':True,'total_bytes':total,'plan':plan,'failed':failed}
    completed = []
    for item in plan:
        try: completed.append(download(api,item['asset_id'],item['key'],item['output'],progress=progress))
        except (UploaderError,OSError): failed.append({'asset_id':item['asset_id'],'error':'Transfer failed; partial state retained. Rerun to retry.'})
    return {'completed':completed,'failed':failed,'total_bytes':total}


def upload_batch(api,state,args):
    from .uploader import upload, PART_MIN
    entries = json.loads(args.manifest.read_text(encoding='utf-8-sig'))
    if not isinstance(entries,list) or not entries: raise UploaderError('Manifest must be a nonempty JSON list.')
    seen = set()
    for item in entries:
        path = Path(item['path']).resolve(strict=True)
        pair = (source_identity(path),item['folder_id'])
        if pair in seen: raise UploaderError('Duplicate source/destination in manifest.')
        seen.add(pair)
        if not path.is_file() or path.stat().st_size<=0: raise UploaderError('Manifest includes an empty or non-file path.')
        if path.stat().st_size>PART_MIN and not args.experimental_multipart: raise UploaderError('Large files require --experimental-multipart.')
        item['path'] = str(path)
    project = api.project(args.project)
    if not project['permissions']['canCreateAsset']: raise UploaderError('Project uploads are not permitted.')
    for folder in {item['folder_id'] for item in entries}:
        if not api.folder(folder,args.project)['permissions']['canCreateChildren']: raise UploaderError('Folder uploads are not permitted.')
    if not args.execute: return {'dry_run':True,'plan':entries}
    journal = Journal(state/'uploads.sqlite3')
    completed, failed = [], []
    try:
        for item in entries:
            try:
                asset = upload(api,journal,item['path'],args.project,project['account']['id'],item['folder_id'],experimental=args.experimental_multipart)
                completed.append(dict(item,asset_id=asset))
                progress({'completed_files':len(completed),'total_files':len(entries)})
            except (UploaderError,OSError) as e:
                failed.append(dict(item,error=str(e) if isinstance(e,UploaderError) else 'Local I/O failure'))
    finally: journal.close()
    return {'completed':completed,'failed':failed}
