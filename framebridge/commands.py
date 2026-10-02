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
from .storage import Journal, OperationJournal, SessionStore, UploaderError, atomic_write, exclusive_lock, default_state_dir, source_identity


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
    p = argparse.ArgumentParser(description='Framebridge: unofficial Frame.io V4 transfers, inspection, and planned folder operations')
    p.add_argument('--state-dir', type=Path, default=default_state_dir(ROOT))
    p.add_argument('--profile', default='default')
    p.add_argument('--json', action='store_true', help='One JSON result on stdout; progress on stderr')
    sub = p.add_subparsers(dest='command', required=True)
    for cmd in ('login','logout','profiles','whoami','status','accounts','transfers','operations'):
        sub.add_parser(cmd)
    for cmd, field in (('workspaces','account_id'),('projects','workspace_id'),('project','project_id'),
                       ('inspect','asset_id'),('renditions','asset_id'),('versions','asset_id'),
                       ('permissions','asset_id'),('folder-stats','folder_id'),('transcripts','asset_id')):
        sub.add_parser(cmd).add_argument(field)
    s = sub.add_parser('verify', help='Check remote metadata; optionally compare a downloaded original')
    s.add_argument('asset_id')
    s.add_argument('--local-file', type=Path)
    mode = s.add_mutually_exclusive_group()
    mode.add_argument('--download-original-to', type=Path)
    mode.add_argument('--etag', action='store_true', help='Compare an MD5-shaped original ETag using two one-byte probes')
    s.add_argument('--part-count', type=int, help='Assert the expected ETag part count; requires --etag')
    s.add_argument('--max-download-bytes', type=int)
    s.add_argument('--report', type=Path)
    for cmd in ('browse','search'):
        s = sub.add_parser(cmd)
        s.add_argument('folder_id')
        if cmd == 'search': s.add_argument('query')
        s.add_argument('--recursive', action='store_true')
        s.add_argument('--limit', type=int, default=10000)
    s = sub.add_parser('comments')
    s.add_argument('asset_id')
    s.add_argument('--output', type=Path)
    s = sub.add_parser('transcript', help='Export an existing transcript; never create a transcription job')
    s.add_argument('asset_id')
    s.add_argument('--transcription-id', required=True)
    s.add_argument('--format', choices=('srt','vtt','text'), required=True)
    s.add_argument('--output', type=Path, required=True)
    s.add_argument('--max-bytes', type=int, default=10*1024*1024)
    s.add_argument('--report', type=Path)
    s = sub.add_parser('mkdir', help='Plan folder creation; --execute performs it')
    s.add_argument('name')
    s.add_argument('--project', required=True)
    s.add_argument('--parent-id', required=True)
    s.add_argument('--existing', choices=('error','reuse'), default='error')
    s.add_argument('--adopt-id', help='Explicitly reconcile an ambiguous folder creation with a reviewed remote ID')
    s.add_argument('--limit', type=int, default=10000)
    s.add_argument('--execute', action='store_true')
    s.add_argument('--dry-run', action='store_true')
    s.add_argument('--report', type=Path)
    s = sub.add_parser('move', help='Plan a same-project asset/folder move; never merge or overwrite')
    s.add_argument('asset_id')
    s.add_argument('--project', required=True)
    s.add_argument('--folder-id', '--to-folder-id', dest='folder_id', required=True)
    s.add_argument('--from-folder-id', help='Require this source parent before moving')
    s.add_argument('--limit', type=int, default=10000)
    s.add_argument('--execute', action='store_true')
    s.add_argument('--dry-run', action='store_true')
    s.add_argument('--report', type=Path)
    s = sub.add_parser('upload-folder', help='Plan a recursive upload preserving the local directory hierarchy')
    s.add_argument('path', type=Path)
    s.add_argument('--project', required=True)
    s.add_argument('--folder-id', required=True, help='Existing remote parent folder')
    s.add_argument('--contents', action='store_true', help='Put contents directly in the destination without creating the local root name')
    s.add_argument('--skip-empty-files', action='store_true', help='Skip zero-byte files and include them in the skipped report')
    s.add_argument('--workers', type=int, default=1, help='Concurrent file transfers, 1–16 (default: sequential)')
    s.add_argument('--part-workers', type=int, default=1, help='Parallel parts per file, 1–4; requires workers > 1')
    s.add_argument('--existing-folders', choices=('error','reuse'), default='error')
    s.add_argument('--max-total-bytes', type=int, help='Required for --execute; cap for all planned source files')
    s.add_argument('--limit', type=int, default=10000)
    s.add_argument('--experimental-multipart', action='store_true', help='Deprecated compatibility option; S3 multipart is enabled by default.')
    s.add_argument('--execute', action='store_true')
    s.add_argument('--dry-run', action='store_true')
    s.add_argument('--report', type=Path)
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
    s.add_argument('--workers', type=int, default=1, help='Concurrent file transfers, 1–16 (default: sequential)')
    s.add_argument('--part-workers', type=int, default=1, help='Parallel parts per file, 1–4; requires workers > 1')
    s.add_argument('manifest', type=Path, help='JSON list of {path, folder_id}; remote folders must already exist')
    s.add_argument('--project', required=True)
    s.add_argument('--execute', action='store_true')
    s.add_argument('--experimental-multipart', action='store_true', help='Deprecated compatibility option; S3 multipart is enabled by default.')
    s.add_argument('--report', type=Path)
    s = sub.add_parser('upload', help='Upload one file to an existing folder')
    s.add_argument('path', type=Path)
    s.add_argument('--project', default=os.getenv('FRAMEIO_PROJECT_ID'))
    s.add_argument('--folder-id', default=os.getenv('FRAMEIO_FOLDER_ID'))
    s.add_argument('--experimental-multipart', action='store_true', help='Deprecated compatibility option; S3 multipart is enabled by default.')
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
            if args.command == 'operations':
                emit(folder_command(None, state, args))
                return 0
            api = Catalog(store)
            command = args.command
            if command in ('whoami','status'): result = {'profile':args.profile,'user':api.me()}
            elif command == 'accounts': result = api.accounts()
            elif command == 'workspaces': result = api.workspaces(args.account_id)
            elif command == 'projects': result = api.projects(args.workspace_id)
            elif command == 'project': result = api.project(args.project_id)
            elif command == 'inspect': result = api.inspect(args.asset_id)
            elif command == 'permissions': result = api.permissions(args.asset_id)
            elif command == 'folder-stats': result = api.folder_stats(args.folder_id)
            elif command == 'transcripts':
                from .transcripts import list_transcripts
                result = list_transcripts(api, args.asset_id)
            elif command == 'transcript':
                from .transcripts import export_transcript
                result = export_transcript(api, args.asset_id, args.transcription_id, args.format,
                                           args.output, max_bytes=args.max_bytes)
                report_write(args.report, result)
            elif command in ('mkdir','move','upload-folder','operations'):
                result = folder_command(api, state, args)
                report_write(getattr(args, 'report', None), result)
            elif command == 'versions': result = api.asset(args.asset_id).get('versions', [])
            elif command == 'renditions': result = api.renditions(args.asset_id)
            elif command == 'verify':
                result = verify_asset(api, args)
                report_write(args.report, result)
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
                        row['needs_reconciliation'] = row['phase'] in ('creating_batch','creating_asset') or row.get('remote_status') in ('unavailable','FAILED','ERROR','DELETED')
                        if row['phase'] == 'complete' and row.get('asset_id'):
                            from .uploader import verify_remote
                            try:
                                row['verification'] = verify_remote(api, row['asset_id'], row['fingerprint'][0], row['project_id'], row['folder_id'])
                            except UploaderError as error:
                                row['needs_reconciliation'] = True
                                row['verification_error'] = str(error)
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


def verify_asset(api, args):
    from .uploader import READY, fingerprint, source_hashes, verify_remote
    etag = getattr(args, 'etag', False)
    part_count = getattr(args, 'part_count', None)
    if part_count is not None and (not etag or part_count <= 0):
        raise UploaderError('A positive --part-count requires --etag.')
    if etag and (not args.local_file or args.download_original_to):
        raise UploaderError('--etag requires --local-file and cannot be combined with an original download.')
    if etag and args.local_file.is_symlink():
        raise UploaderError('ETag comparison requires a non-symlink local source.')
    if args.download_original_to and (not args.local_file or not args.max_download_bytes or args.max_download_bytes <= 0):
        raise UploaderError('Original comparison requires --local-file and a positive --max-download-bytes cap.')
    if args.max_download_bytes is not None and not args.download_original_to:
        raise UploaderError('--max-download-bytes requires --download-original-to.')
    evidence = api.upload_evidence(args.asset_id)
    if evidence.get('status') not in READY:
        raise UploaderError('Remote asset is not complete.')
    result = {'asset':evidence, 'verification_level':'status', 'checksum_verified':False,
              'size_matches_local':None}
    if args.local_file:
        path = args.local_file.resolve(strict=True)
        if not path.is_file():
            raise UploaderError('Choose a regular local file.')
        mark = fingerprint(path)
        verify_remote(api, args.asset_id, mark[0])
        result.update(size_matches_local=True, verification_level='metadata')
        if etag:
            from .integrity import verify_etag
            result.update(verify_etag(api, args.asset_id, path, part_count=part_count, progress=progress))
        if args.download_original_to:
            if args.download_original_to.resolve() == path:
                raise UploaderError('Choose a separate output path for the downloaded original.')
            rendition = api.rendition(args.asset_id, 'original')
            if rendition['filesizeInBytes'] != mark[0] or mark[0] > args.max_download_bytes:
                raise UploaderError('Original size mismatch or download cap exceeded; no download started.')
            digest, _ = source_hashes(path, mark[0], 1)
            if fingerprint(path) != mark:
                raise UploaderError('Local file changed while hashing.')
            # Pin size and media identity across the downloader's metadata renewal.
            class Original:
                def rendition(self, asset_id, selection):
                    current = api.rendition(asset_id, selection)
                    if any(current[k] != rendition[k] for k in ('asset_id','media_id','key','filesizeInBytes')):
                        raise UploaderError('Original changed before or during verification.')
                    return current
            result['download'] = download(Original(), args.asset_id, 'original', args.download_original_to,
                                          expected_sha256=digest, progress=progress)
            after, _ = source_hashes(path, mark[0], 1)
            if after != digest or fingerprint(path) != mark:
                raise UploaderError('Local file changed during verification.')
            verify_remote(api, args.asset_id, mark[0])
            result.update(checksum_verified=True, verification_level='original_sha256', sha256=digest)
    return result


def folder_command(api, state, args):
    from .folders import make_directory, move
    from .tree_upload import upload_tree
    operations = OperationJournal(state / 'operations.sqlite3')
    try:
        if args.command == 'operations':
            return [dict(json.loads(raw), operation_id=key)
                    for key, raw in operations.db.execute('SELECT key, record FROM operations ORDER BY key')]
        execute = args.execute and not args.dry_run
        if args.command == 'mkdir':
            return make_directory(api, operations, args.project, args.parent_id, args.name,
                                  execute=execute, existing=args.existing, adopt_id=args.adopt_id, limit=args.limit)
        if args.command == 'move':
            return move(api, operations, args.project, args.asset_id, args.folder_id,
                        execute=execute, expected_parent=args.from_folder_id, limit=args.limit)
        uploads = Journal(state / 'uploads.sqlite3')
        try:
            return upload_tree(api, operations, uploads, args.path, args.project, args.folder_id,
                               execute=execute, contents=args.contents, existing_folders=args.existing_folders,
                               limit=args.limit, max_total_bytes=args.max_total_bytes,
                               experimental=args.experimental_multipart, progress=progress,
                               skip_empty_files=args.skip_empty_files, workers=args.workers,
                               part_workers=args.part_workers)
        finally:
            uploads.close()
    finally:
        operations.close()


def upload_one(api, state, args):
    from .uploader import upload
    if not args.project or not args.folder_id:
        raise UploaderError('Set --project and --folder-id, or FRAMEIO_PROJECT_ID and FRAMEIO_FOLDER_ID.')
    if not args.path.is_file() or args.path.stat().st_size <= 0:
        raise UploaderError('Choose a nonempty regular file.')
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
    import requests
    from .folders import name_key
    from .uploader import upload
    from .upload_pool import upload_many
    workers = getattr(args, 'workers', 1)
    part_workers = getattr(args, 'part_workers', 1)
    if not 1 <= workers <= 16:
        raise UploaderError('--workers must be between 1 and 16.')
    if not 1 <= part_workers <= 4 or (part_workers > 1 and workers < 2):
        raise UploaderError('--part-workers must be 1–4; parallel parts require --workers above 1.')
    entries = json.loads(args.manifest.read_text(encoding='utf-8-sig'))
    if not isinstance(entries,list) or not entries: raise UploaderError('Manifest must be a nonempty JSON list.')
    seen, destination_names = set(), set()
    for item in entries:
        path = Path(item['path']).resolve(strict=True)
        pair = (source_identity(path),item['folder_id'])
        if pair in seen: raise UploaderError('Duplicate source/destination in manifest.')
        seen.add(pair)
        destination_name = (item['folder_id'], name_key(path.name))
        if workers > 1 and destination_name in destination_names:
            raise UploaderError('Concurrent manifest entries target the same normalized destination name.')
        destination_names.add(destination_name)
        if not path.is_file() or path.stat().st_size<=0: raise UploaderError('Manifest includes an empty or non-file path.')
        item['path'] = str(path)
    project = api.project(args.project)
    if not project['permissions']['canCreateAsset']: raise UploaderError('Project uploads are not permitted.')
    for folder in {item['folder_id'] for item in entries}:
        if not api.folder(folder,args.project)['permissions']['canCreateChildren']: raise UploaderError('Folder uploads are not permitted.')
    if not args.execute: return {'dry_run':True,'plan':entries}
    journal = Journal(state/'uploads.sqlite3')
    completed, failed = [], []
    metrics = {}
    transport = requests.Session() if workers == 1 else None
    if transport is not None:
        transport.trust_env = False
    try:
        if workers > 1:
            jobs = [dict(item, project_id=args.project, account_id=project['account']['id']) for item in entries]
            results = upload_many(api, journal, jobs, workers=workers,
                                  experimental=args.experimental_multipart, metrics=metrics,
                                  part_workers=part_workers)
            try:
                for item, asset, error in results:
                    public_item = {k: v for k, v in item.items() if k not in {'project_id', 'account_id'}}
                    if error is None:
                        completed.append(dict(public_item, asset_id=asset))
                    else:
                        failed.append(dict(public_item, error=error))
                    progress({'completed_files': len(completed), 'failed_files': len(failed),
                              'total_files': len(entries)})
            finally:
                results.close()
            return {'completed': completed, 'failed': failed, 'metrics': metrics}
        for item in entries:
            try:
                asset = upload(api,journal,item['path'],args.project,project['account']['id'],item['folder_id'],
                               experimental=args.experimental_multipart, http=transport)
                completed.append(dict(item,asset_id=asset))
                progress({'completed_files':len(completed),'total_files':len(entries)})
            except (UploaderError,OSError) as e:
                failed.append(dict(item,error=str(e) if isinstance(e,UploaderError) else 'Local I/O failure'))
    finally:
        if transport is not None:
            transport.close()
        journal.close()
    return {'completed':completed,'failed':failed}
