"""Journaled folder creation and conservative, same-project moves."""
import hashlib
import json
import unicodedata

from .storage import UploaderError


def operation_key(kind, *values):
    return hashlib.sha256(json.dumps([kind, *values], ensure_ascii=False).encode()).hexdigest()


def name_key(name):
    return unicodedata.normalize('NFC', name).casefold()


def validate_name(name):
    if not isinstance(name, str) or not name.strip() or name in {'.', '..'} or any(
            c in '/\\' or ord(c) < 32 for c in name):
        raise UploaderError('Use a nonempty single folder name without path separators or control characters.')


def require_permission(values, field, description):
    if values.get('permissions', {}).get(field) is not True:
        raise UploaderError(f'{description} permission is missing or denied ({field}).')


def writable_folder(api, folder_id, project_id):
    folder = api.folder(folder_id, project_id)
    require_permission(folder, 'canCreateChildren', 'Destination folder creation/upload')
    require_permission(folder, 'canViewChildren', 'Destination folder listing')
    return folder


def check_folder_identity(folder, project_id, parent_id, name):
    if (folder.get('__typename') != 'FolderAsset' or folder.get('name') != name or
            (folder.get('project') or {}).get('id') != project_id or
            (folder.get('parent') or {}).get('id') != parent_id):
        raise UploaderError('Recorded folder identity, name, or location changed; reconcile before retrying.')


def directory_plan(api, journal, project_id, parent_id, name, *, existing='error',
                   limit=10000, children=None):
    validate_name(name)
    if existing not in {'error', 'reuse'}:
        raise UploaderError('Existing-folder policy must be error or reuse.')
    key = operation_key('mkdir', project_id, parent_id, name)
    record = journal.get(key)
    if record:
        if not record.get('folder_id'):
            raise UploaderError('Previous folder creation has an ambiguous outcome. '
                                'Use mkdir with the same parent/name and --adopt-id after reviewing Frame.io; '
                                'do not delete the journal and retry.')
        folder = api.folder(record['folder_id'], project_id)
        check_folder_identity(folder, project_id, parent_id, name)
        return {'action': 'resume', 'folder_id': folder['id'], 'name': name,
                'parent_id': parent_id, 'operation_id': key}
    children = list(api.children_assets(parent_id, limit)) if children is None else children
    matches = [row for row in children if name_key(row['name']) == name_key(name)]
    if matches:
        if (existing != 'reuse' or len(matches) != 1 or matches[0]['__typename'] != 'FolderAsset'
                or matches[0]['name'] != name):
            raise UploaderError('Destination name conflicts with an existing item; '
                                'only one exactly named folder can be explicitly reused.')
        return {'action': 'reuse', 'folder_id': matches[0]['id'], 'name': name,
                'parent_id': parent_id, 'operation_id': key}
    return {'action': 'create', 'folder_id': None, 'name': name,
            'parent_id': parent_id, 'operation_id': key}


def make_directory(api, journal, project_id, parent_id, name, *, execute=False,
                   existing='error', adopt_id=None, limit=10000):
    validate_name(name)
    if limit <= 0:
        raise UploaderError('Folder inspection limit must be positive.')
    project = api.project(project_id)
    require_permission(project, 'canCreateAsset', 'Project folder creation')
    writable_folder(api, parent_id, project_id)
    key = operation_key('mkdir', project_id, parent_id, name)
    previous = journal.get(key)
    if adopt_id:
        if not previous or previous.get('folder_id'):
            raise UploaderError('--adopt-id is only for a recorded folder creation with no returned identity.')
        folder = api.folder(adopt_id, project_id)
        check_folder_identity(folder, project_id, parent_id, name)
        plan = {'action': 'adopt', 'folder_id': adopt_id, 'name': name,
                'parent_id': parent_id, 'operation_id': key}
    else:
        plan = directory_plan(api, journal, project_id, parent_id, name,
                              existing=existing, limit=limit)
    if not execute:
        return dict(plan, dry_run=True)
    if plan['action'] in {'create', 'adopt', 'reuse'}:
        record = {'kind': 'mkdir', 'project_id': project_id, 'parent_id': parent_id,
                  'name': name, 'phase': 'submitting'}
        if plan['action'] == 'create':
            journal.put(key, record)  # durable intent BEFORE the external write
            created = api.create_folder(parent_id, name)
            record.update(folder_id=created['id'], phase='verifying')
            journal.put(key, record)  # preserve returned identity before another request
            plan['folder_id'] = created['id']
        else:
            record.update(folder_id=plan['folder_id'], phase='verifying')
            journal.put(key, record)
        folder = api.folder(plan['folder_id'], project_id)
        check_folder_identity(folder, project_id, parent_id, name)
        record['phase'] = 'complete'
        journal.put(key, record)
    elif previous and previous.get('phase') != 'complete':
        previous['phase'] = 'complete'
        journal.put(key, previous)
    return dict(plan, dry_run=False)


def _unrestricted(folder):
    if folder.get('restricted') is not False or folder.get('isRestrictedDescendant') is not False:
        raise UploaderError('Restricted or unverified folder access: moves that may change access are not supported.')


def _destination_chain(api, destination, project_id, source_id):
    current, seen = destination, set()
    while current:
        if current['id'] == source_id:
            raise UploaderError('Cannot move a folder into itself or one of its descendants.')
        if current['id'] in seen or len(seen) >= 256:
            raise UploaderError('Destination ancestry is cyclic or exceeds the safety limit.')
        seen.add(current['id'])
        if current['__typename'] != 'FolderAsset' or (current.get('project') or {}).get('id') != project_id:
            raise UploaderError('Destination ancestry is not entirely within the selected project.')
        _unrestricted(current)
        parent = (current.get('parent') or {}).get('id')
        current = api.asset(parent) if parent else None


def move(api, journal, project_id, asset_id, destination_id, *, execute=False,
         expected_parent=None, limit=10000):
    if limit <= 0:
        raise UploaderError('Move inspection limit must be positive.')
    project = api.project(project_id)
    asset = api.asset(asset_id)
    if (asset.get('project') or {}).get('id') != project_id:
        raise UploaderError('Only same-project moves are supported.')
    parent_id = (asset.get('parent') or {}).get('id')
    if not parent_id or asset_id == project.get('rootAssetId'):
        raise UploaderError('The project root cannot be moved.')
    destination = api.folder(destination_id, project_id)
    if asset.get('status') in {'DELETED', 'FAILED', 'ERROR'}:
        raise UploaderError('Failed or deleted assets cannot be moved by this command.')
    key = operation_key('move', project_id, asset_id, destination_id)
    record = journal.get(key)
    plan = {'operation_id': key, 'asset_id': asset_id, 'name': asset['name'],
            'project_id': project_id, 'source_parent_id': parent_id,
            'destination_id': destination_id, 'dry_run': not execute}
    if parent_id == destination_id:
        if execute and record:
            record['phase'] = 'complete'
            journal.put(key, record)
        return dict(plan, action='already_at_destination')
    if expected_parent and parent_id != expected_parent:
        raise UploaderError('Source parent does not match --from-folder-id; no move performed.')
    if record and record['phase'] != 'complete':
        raise UploaderError('Previous move has an ambiguous outcome and is not at the destination. '
                            'Reconcile the recorded source and destination before retrying.')
    require_permission(project, 'canMoveAsset', 'Project move')
    require_permission(destination, 'canCreateChildren', 'Destination folder creation')
    require_permission(destination, 'canViewChildren', 'Destination folder listing')
    source_parent = api.folder(parent_id, project_id)  # excludes individual version-stack members
    require_permission(source_parent, 'canMoveChildren', 'Source folder move')
    _unrestricted(source_parent)
    _destination_chain(api, destination, project_id, asset_id)
    inspected = 0
    if asset['__typename'] == 'FolderAsset':
        _unrestricted(asset)
        for child in api.walk(asset_id, recursive=True, limit=limit):
            inspected += 1
            if (child.get('project') or {}).get('id') != project_id:
                raise UploaderError('Source subtree changed projects; no move performed.')
            if child['__typename'] == 'FolderAsset':
                _unrestricted(child)
    for child in api.children_assets(destination_id, limit):
        if child['id'] != asset_id and name_key(child['name']) == name_key(asset['name']):
            raise UploaderError('Destination already contains an item with this name; moves never merge or overwrite.')
    plan.update(action='move', inspected_descendants=inspected)
    if not execute:
        return plan
    current = api.asset(asset_id)
    if any(current.get(k) != asset.get(k) for k in ('name', 'parent', 'project', '__typename')):
        raise UploaderError('Source changed during move preflight; no move performed.')
    if current['__typename'] == 'FolderAsset':
        _unrestricted(current)
    current_parent = api.folder(parent_id, project_id)
    require_permission(current_parent, 'canMoveChildren', 'Source folder move')
    _unrestricted(current_parent)
    # Re-read destination ancestry immediately before persisting the write intent.
    current_destination = api.folder(destination_id, project_id)
    require_permission(current_destination, 'canCreateChildren', 'Destination folder creation')
    _destination_chain(api, current_destination, project_id, asset_id)
    record = dict(plan, kind='move', phase='submitting')
    journal.put(key, record)
    api.move_asset(asset_id, destination_id)
    current = api.asset(asset_id)
    if ((current.get('project') or {}).get('id') != project_id or
            (current.get('parent') or {}).get('id') != destination_id):
        raise UploaderError('Move completion is not confirmed; journal retained for reconciliation.')
    record['phase'] = 'complete'
    journal.put(key, record)
    return dict(plan, confirmed_parent_id=destination_id)
