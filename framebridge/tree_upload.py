"""Preserve local directory hierarchies using explicit plans and durable IDs."""
import os
from pathlib import Path
import stat
import time

import requests

from .folders import (FolderCache, child_index, directory_plan, make_directory,
                      matching_children, name_key, require_permission, validate_name, writable_folder)
from .storage import UploaderError
from .uploader import fingerprint, upload, upload_key
from .upload_pool import upload_many


def _linked(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) &
                                           getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def scan_tree(path, limit=10000, *, skip_empty_files=False):
    path = Path(path).absolute()
    if limit <= 0 or _linked(path) or not path.is_dir():
        raise UploaderError('Choose a real directory and a positive entry limit; root symlinks/junctions are refused.')
    root = path.resolve(strict=True)
    validate_name(root.name)
    directories, files, skipped = [], [], []
    pending, count = [root], 1
    while pending:
        directory = pending.pop()
        relative = directory.relative_to(root).as_posix()
        if len(directory.relative_to(root).parts) > 256:
            raise UploaderError('Local tree exceeds the maximum supported depth.')
        if _linked(directory):
            raise UploaderError('Local directory changed to a symlink or junction during planning.')
        directories.append({'relative_path': relative, 'name': directory.name})
        with os.scandir(directory) as scan:
            entries = []
            for entry in scan:
                entries.append(entry.name)
                count += 1
                if count > limit:
                    raise UploaderError('Local tree exceeds --limit; no upload started.')
        seen, subdirs = set(), []
        for name in sorted(entries):
            validate_name(name)
            if name_key(name) in seen:
                raise UploaderError('Local names collide after case/Unicode normalization; no upload started.')
            seen.add(name_key(name))
            item = directory / name
            item_relative = item.relative_to(root).as_posix()
            if _linked(item):
                skipped.append({'relative_path': item_relative, 'reason': 'symlink_or_reparse_point'})
            elif item.is_dir():
                subdirs.append(item)
            elif item.is_file():
                mark = fingerprint(item)
                if mark[0] <= 0:
                    if skip_empty_files:
                        skipped.append({'relative_path': item_relative, 'reason': 'empty_file'})
                        continue
                    raise UploaderError('Empty files are unsupported; use --skip-empty-files to skip them. No upload started.')
                files.append({'path': str(item), 'relative_path': item_relative,
                              'relative_parent': relative, 'name': name, 'bytes': mark[0],
                              'fingerprint': mark})
            else:
                raise UploaderError('Local tree contains a special file; no upload started.')
            if count > limit:
                raise UploaderError('Local tree exceeds --limit; no upload started.')
        pending.extend(reversed(subdirs))
    return {'root': str(root), 'directories': directories, 'files': files,
            'skipped': skipped, 'total_bytes': sum(item['bytes'] for item in files)}


def _file_plan(api, journal, item, project_id, folder_id, children):
    matches = matching_children(children, item['name'])
    record = journal.get(upload_key(item['path'], folder_id))
    if record:
        if record['fingerprint'] != item['fingerprint'] or record['project_id'] != project_id:
            raise UploaderError('A journaled source changed; reconcile before uploading the tree.')
        if record['phase'] in {'creating_asset', 'creating_batch'}:
            raise UploaderError('A previous file creation has an ambiguous outcome; reconcile before resuming the tree.')
        if record.get('asset_id'):
            if len(matches) != 1 or matches[0]['id'] != record['asset_id'] or matches[0]['name'] != item['name']:
                raise UploaderError('A journaled upload moved, disappeared, was renamed, or has a name conflict.')
            remote = matches[0]
            if ((remote.get('project') or {}).get('id') != project_id or
                    remote.get('filesize') != item['bytes'] or remote.get('status') in {'DELETED', 'FAILED', 'ERROR'}):
                raise UploaderError('A journaled upload has mismatched or failed remote metadata.')
        elif matches:
            raise UploaderError('Destination file name conflicts with an existing item.')
        return dict(item, folder_id=folder_id, action='resume', asset_id=record.get('asset_id'))
    if matches:
        raise UploaderError('Destination file name conflicts with an existing item. '
                            'Tree uploads never overwrite, auto-version, or trust same-name files.')
    return dict(item, folder_id=folder_id, action='upload')


def plan_tree(api, folders, uploads, path, project_id, parent_id, *, contents=False,
              existing_folders='error', limit=10000, max_total_bytes=None, experimental=False,
              skip_empty_files=False):
    if existing_folders not in {'error', 'reuse'}:
        raise UploaderError('Existing-folder policy must be error or reuse.')
    if max_total_bytes is not None and max_total_bytes <= 0:
        raise UploaderError('--max-total-bytes must be positive.')
    local = scan_tree(path, limit, skip_empty_files=skip_empty_files)
    if max_total_bytes is not None and local['total_bytes'] > max_total_bytes:
        raise UploaderError('Local tree exceeds --max-total-bytes; no upload started.')
    project = api.project(project_id)
    require_permission(project, 'canCreateAsset', 'Project upload')
    writable_folder(api, parent_id, project_id)
    ids, plans, listings = {}, [], {}

    def children(folder_id):
        if folder_id not in listings:
            writable_folder(api, folder_id, project_id)
            listings[folder_id] = child_index(api.children_assets(folder_id, limit))
        return listings[folder_id]

    for directory in local['directories']:
        relative = directory['relative_path']
        if relative == '.' and contents:
            ids[relative] = parent_id
            continue
        relative_parent = Path(relative).parent.as_posix()
        remote_parent = parent_id if relative == '.' else ids[relative_parent]
        if remote_parent:
            plan = directory_plan(api, folders, project_id, remote_parent, directory['name'],
                                  existing=existing_folders, limit=limit, children=children(remote_parent))
        else:
            plan = {'action': 'create', 'name': directory['name'], 'folder_id': None,
                    'parent_id': None}
        ids[relative] = plan['folder_id']
        plans.append(dict(plan, relative_path=relative, relative_parent=relative_parent))
    file_plans = []
    for item in local['files']:
        folder_id = ids[item['relative_parent']]
        file_plans.append(_file_plan(api, uploads, item, project_id, folder_id, children(folder_id))
                          if folder_id else dict(item, folder_id=None, action='upload'))
    return {'dry_run': True, 'root': local['root'], 'project_id': project_id,
            'destination_parent_id': parent_id, 'contents_only': contents,
            'total_bytes': local['total_bytes'], 'file_count': len(file_plans),
            'folder_count': len(plans), 'folders': plans, 'files': file_plans,
            'skipped': local['skipped'], 'account_id': project['account']['id']}


def _check_source(root, item):
    path = Path(item['path'])
    try:
        path.resolve(strict=True).relative_to(root)
    except (ValueError, FileNotFoundError):
        raise UploaderError('Local source escaped or disappeared from the planned tree.') from None
    current = path
    while True:
        if _linked(current):
            raise UploaderError('Local source or ancestor changed to a symlink/junction.')
        if current == root:
            break
        current = current.parent
    if not path.is_file() or fingerprint(path) != item['fingerprint']:
        raise UploaderError('Local source changed after planning.')


def upload_tree(api, folders, uploads, path, project_id, parent_id, *, execute=False,
                contents=False, existing_folders='error', limit=10000, max_total_bytes=None,
                experimental=False, progress=None, skip_empty_files=False, workers=1, part_workers=1):
    if type(workers) is not int or not 1 <= workers <= 16:
        raise UploaderError('--workers must be between 1 and 16.')
    if type(part_workers) is not int or not 1 <= part_workers <= 4:
        raise UploaderError('--part-workers must be between 1 and 4.')
    if part_workers > 1 and workers < 2:
        raise UploaderError('--part-workers above 1 requires --workers above 1.')
    started = time.monotonic()
    if execute and (max_total_bytes is None or max_total_bytes <= 0):
        raise UploaderError('Executing a tree upload requires a positive --max-total-bytes cap.')
    plan = plan_tree(api, folders, uploads, path, project_id, parent_id,
                     contents=contents, existing_folders=existing_folders, limit=limit,
                     max_total_bytes=max_total_bytes, experimental=experimental,
                     skip_empty_files=skip_empty_files)
    if not execute:
        return dict(plan, workers=workers, part_workers=part_workers)
    metrics = {'planning_seconds': round(time.monotonic() - started, 3)}
    folder_started = time.monotonic()
    root = Path(plan['root'])
    for item in plan['files']:
        _check_source(root, item)
    remote = {'.': parent_id} if contents else {}
    created, completed, failed = [], [], []
    folder_cache = FolderCache(api, project_id, limit)
    for directory in plan['folders']:
        relative = directory['relative_path']
        destination = parent_id if relative == '.' else remote[directory['relative_parent']]
        result = make_directory(api, folders, project_id, destination, directory['name'],
                                execute=True, existing=existing_folders, limit=limit, cache=folder_cache)
        if directory['folder_id'] and result['folder_id'] != directory['folder_id']:
            raise UploaderError('Destination folder mapping changed during execution.')
        remote[relative] = result['folder_id']
        created.append(dict(result, relative_path=relative))
        if progress:
            progress({'resolved_folders': len(created), 'total_folders': plan['folder_count']})
    execution_listings = {}
    metrics['folder_seconds'] = round(time.monotonic() - folder_started, 3)
    metrics['folder_parent_listings'] = len(folder_cache.listings)

    def jobs():
        for item in plan['files']:
            destination = remote[item['relative_parent']]
            job = dict(item, folder_id=destination, project_id=project_id, account_id=plan['account_id'])
            try:
                _check_source(root, item)
                if destination not in execution_listings:
                    writable_folder(api, destination, project_id)
                    execution_listings[destination] = child_index(api.children_assets(destination, limit))
                # Refresh once per destination during execution, including new folders.
                # Other clients must not concurrently modify this tree: no remote name lock exists.
                _file_plan(api, uploads, item, project_id, destination, execution_listings[destination])
            except (UploaderError, OSError) as error:
                job['error'] = str(error) if isinstance(error, UploaderError) else 'Local I/O failure'
            yield job

    def sequential():
        with requests.Session() as transport:
            transport.trust_env = False
            for job in jobs():
                if job.get('error'):
                    yield job, None, job['error']
                    continue
                try:
                    asset_id = upload(api, uploads, job['path'], project_id, plan['account_id'],
                                      job['folder_id'], experimental=experimental, http=transport)
                    yield job, asset_id, None
                except (UploaderError, OSError) as error:
                    yield job, None, str(error) if isinstance(error, UploaderError) else 'Local I/O failure'

    pool_metrics = {}
    results = (sequential() if workers == 1 else
               upload_many(api, uploads, jobs(), workers=workers, experimental=experimental,
                           upload_fn=upload, check_source=lambda job: _check_source(root, job),
                           metrics=pool_metrics, part_workers=part_workers))
    try:
        for item, asset_id, error in results:
            if error is not None:
                failed.append({'relative_path': item['relative_path'], 'error': error})
            else:
                completed.append({'relative_path': item['relative_path'], 'asset_id': asset_id,
                                  'folder_id': item['folder_id']})
            if progress:
                progress({'completed_files': len(completed), 'failed_files': len(failed),
                          'total_files': plan['file_count']})
    finally:
        results.close()
    metrics.update(workers=workers, total_seconds=round(time.monotonic() - started, 3),
                   upload_pool=pool_metrics)
    return {'dry_run': False, 'root': plan['root'], 'total_bytes': plan['total_bytes'],
            'folders': created, 'completed': completed, 'failed': failed, 'skipped': plan['skipped'],
            'metrics': metrics}
