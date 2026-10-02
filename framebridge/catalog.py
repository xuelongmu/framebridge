"""Read-only navigation and media catalog, based on observed web operations."""
from itertools import islice

from .api import Api
from .storage import UploaderError

MEDIA = '''query Media($ids: [ID!]!) { assets(assetIds: $ids) {
id name status __typename isWatermarked filesize project { id } parent { id }
... on VideoAsset { forensicallyWatermarked media { id duration fps timecode mimeType frames
original { key downloadUrl filesizeInBytes codec }
videoTranscodes { key downloadUrl encodeStatus filesizeInBytes width height codec }
metadata { id originalWidth originalHeight } } }
... on AudioAsset { media { id duration fps mimeType original { key downloadUrl filesizeInBytes codec }
audioTranscodes { key downloadUrl encodeStatus } } }
... on ImageAsset { media { id mimeType original { key downloadUrl filesizeInBytes codec }
metadata { id originalWidth originalHeight hasAlpha } } }
... on DocumentAsset { media { id mimeType pageCount original { key downloadUrl filesizeInBytes codec } } }
... on UnsupportedAsset { media { id mimeType original { key downloadUrl filesizeInBytes codec } } }
} }'''

ASSET_FIELDS = '''id name status filesize __typename project { id } parent { id }
... on FolderAsset { restricted isRestrictedDescendant }
... on VersionStackAsset { versions { id name status __typename } }'''

FIELD_VALUES = '''query FieldValues($id: ID!, $page: PageInput!) { asset(assetId: $id) {
id ... on Collectible { fieldValuesNew(page: $page) {
nodes { id fieldType isValueTrimmed fieldDefinition { id } value { __typename
... on LongTextFieldValue { longText } ... on TextFieldValue { text }
... on NumberFieldValue { number } ... on RatingFieldValue { rating }
... on SelectFieldValue { selectedOptions } ... on SelectMultiFieldValue { multiSelectedOptions }
... on ToggleFieldValue { toggle } ... on DateFieldValue { date }
... on UsersFieldValue { selectedMembers { id displayName } }
... on UserSingleFieldValue { singleSelectedMember { id displayName } }
... on UserMultiFieldValue { multiSelectedMembers { id displayName } }
} } pageInfo { endCursor hasNextPage } } } } }'''

FIELD_DEFINITIONS = '''query FieldDefinitions($id: ID!, $page: PageInput!) {
project(projectId: $id) { fieldDefinitions(page: $page) {
nodes { id name fieldType isSystemField configuration {
... on SelectFieldConfiguration { options { id displayName } } } }
pageInfo { endCursor hasNextPage } } } }'''

TRANSCRIPT_FIELDS = '''localeTranscriptions(page: $page) {
nodes { id name displayName encodeStatus locale source lastEditedAt
vtt { downloadUrl } srt { downloadUrl } text { downloadUrl } }
pageInfo { endCursor hasNextPage } }'''
TRANSCRIPTS = '''query Transcripts($id: ID!, $page: PageInput!) { asset(assetId: $id) {
id __typename ... on AudioAsset { ''' + TRANSCRIPT_FIELDS + ''' }
... on VideoAsset { ''' + TRANSCRIPT_FIELDS + ''' } } }'''


class Catalog(Api):
    def media(self, asset_id):
        items = self.call('Media', MEDIA, {'ids': [asset_id]})['assets']
        if len(items) != 1 or not items[0] or items[0].get('id') != asset_id:
            raise UploaderError('Asset is missing or inaccessible.')
        return items[0]

    def renditions(self, asset_id):
        asset = self.media(asset_id)
        media = asset.get('media') or {}
        return [dict(item, asset_id=asset['id'], asset_name=asset['name'], media_id=media['id'])
                for item in media.get('videoTranscodes', []) + media.get('audioTranscodes', [])
                if item['key'] != 'original']

    def rendition(self, asset_id, selection):
        asset = self.media(asset_id)
        media = asset.get('media') or {}
        if asset.get('isWatermarked') or asset.get('forensicallyWatermarked'):
            raise UploaderError('Watermarked downloads require a separately verified workflow.')
        options = media.get('videoTranscodes', []) + media.get('audioTranscodes', [])
        if selection == 'original':
            options = [dict(media.get('original') or {}, encodeStatus='SUCCESS')]
        matches = [x for x in options if x.get('key') == selection or
                   (selection.endswith('p') and str(x.get('height')) == selection[:-1])]
        matches = [x for x in matches if x.get('encodeStatus') == 'SUCCESS' and x.get('downloadUrl')]
        if len(matches) != 1:
            raise UploaderError('Choose one available, ready rendition key from renditions; no automatic original fallback.')
        chosen = matches[0]
        if not isinstance(chosen.get('filesizeInBytes'), int) or chosen['filesizeInBytes'] <= 0:
            raise UploaderError('Rendition has no verified byte size; downloading it is not supported yet.')
        return dict(chosen, asset_id=asset['id'], asset_name=asset['name'], media_id=media['id'])

    def asset(self, asset_id):
        q = 'query InspectAsset($id: ID!) { asset(assetId: $id) { ' + ASSET_FIELDS + ' } }'
        asset = self.call('InspectAsset', q, {'id': asset_id}).get('asset')
        if not asset or asset.get('id') != asset_id:
            raise UploaderError('Asset is missing or inaccessible.')
        return asset

    def assets(self, asset_ids):
        """Hydrate at most 100 IDs per call; preserve listing order and fail on gaps."""
        ids = list(asset_ids)
        if not ids or len(ids) > 100 or len(set(ids)) != len(ids):
            raise UploaderError('Asset hydration requires 1–100 distinct IDs.')
        q = 'query HydrateAssets($ids: [ID!]!) { assets(assetIds: $ids) { ' + ASSET_FIELDS + ' } }'
        rows = self.call('HydrateAssets', q, {'ids': ids}).get('assets') or []
        by_id = {row['id']: row for row in rows if row and row.get('id')}
        if len(rows) != len(ids) or set(by_id) != set(ids):
            raise UploaderError('Asset listing changed or contains inaccessible entries; results are incomplete.')
        return [by_id[asset_id] for asset_id in ids]

    def children_assets(self, folder_id, limit=10000):
        if limit < 0:
            raise UploaderError('Traversal budget must be nonnegative.')
        children, seen = iter(self.children(folder_id)), set()
        while True:
            batch = list(islice(children, min(100, limit - len(seen) + 1)))
            if not batch:
                return
            ids = [row['id'] for row in batch]
            if len(seen) + len(ids) > limit:
                raise UploaderError('Traversal limit reached; results are incomplete.')
            if seen.intersection(ids) or len(set(ids)) != len(ids):
                raise UploaderError('Folder pagination repeated assets; results are incomplete.')
            seen.update(ids)
            for item in self.assets(ids):
                if (item.get('parent') or {}).get('id') != folder_id:
                    raise UploaderError('Asset moved during listing; rerun to obtain a consistent plan.')
                yield item

    def inspect(self, asset_id):
        asset = self.media(asset_id)
        # Folders and version stacks are not Collectible objects.
        if asset['__typename'] not in {'AudioAsset', 'VideoAsset', 'ImageAsset',
                                      'DocumentAsset', 'UnsupportedAsset', 'InteractiveAsset', 'ModelAsset'}:
            return asset
        values = list(self._pages('FieldValues', FIELD_VALUES, {'id': asset_id},
                                 lambda d: d['asset']['fieldValuesNew']))
        project_id = (asset.get('project') or {}).get('id')
        if not project_id:
            raise UploaderError('Asset project is unavailable; metadata inspection is incomplete.')
        definitions = {item['id']: item for item in self._pages(
            'FieldDefinitions', FIELD_DEFINITIONS, {'id': project_id},
            lambda d: d['project']['fieldDefinitions'])}
        asset['fields'] = [dict(value, definition=definitions.get(value['fieldDefinition']['id']))
                           for value in values]
        media = asset.get('media') or {}
        asset['ready_video_proxy_keys'] = [r['key'] for r in media.get('videoTranscodes', [])
                                           if r.get('encodeStatus') == 'SUCCESS']
        asset['has_ready_video_proxy'] = bool(asset['ready_video_proxy_keys'])
        return asset

    def folder_stats(self, folder_id):
        q = '''query GetFolderStats($folderId: ID!) { folder(folderId: $folderId) {
id folderStats { countFiles countFolders sizeFiles sizeFolders } } }'''
        folder = self.call('GetFolderStats', q, {'folderId': folder_id}).get('folder')
        if not folder or not folder.get('folderStats'):
            raise UploaderError('Folder statistics are missing or inaccessible.')
        return dict(folder, scope='server_reported',
                    note='Counts and sizes are server aggregates, not a verified recursive transfer manifest.')

    def permissions(self, asset_id):
        asset = self.asset(asset_id)
        project_id = (asset.get('project') or {}).get('id')
        if not project_id:
            raise UploaderError('Asset project is missing or inaccessible.')
        project = self.project(project_id)
        containing = asset
        seen = set()
        while containing['__typename'] != 'FolderAsset':
            parent = (containing.get('parent') or {}).get('id')
            if not parent or parent in seen or len(seen) >= 256:
                raise UploaderError('Cannot resolve the containing folder safely.')
            seen.add(parent)
            containing = self.asset(parent)
        folder = self.folder(containing['id'], project_id)
        checks = {f'project.{key}': value for key, value in project.get('permissions', {}).items()}
        checks.update({f'folder.{key}': value for key, value in folder.get('permissions', {}).items()})
        return {'asset_id': asset_id, 'project_id': project_id, 'folder_id': folder['id'],
                'permissions': checks, 'denied': [key for key, value in checks.items() if value is False],
                'restricted': folder.get('restricted'),
                'is_restricted_descendant': folder.get('isRestrictedDescendant'),
                'note': 'Permission snapshots explain preflight failures; the server remains authoritative.'}

    def transcripts(self, asset_id):
        def extract(data):
            asset = data.get('asset')
            if not asset:
                raise UploaderError('Asset is missing or inaccessible.')
            if asset['__typename'] not in {'AudioAsset', 'VideoAsset'}:
                raise UploaderError('Transcripts are supported only for audio and video assets.')
            return asset['localeTranscriptions']
        return list(self._pages('Transcripts', TRANSCRIPTS, {'id': asset_id}, extract))

    def _pages(self, operation, query, variables, extract):
        cursor, seen = None, set()
        while True:
            result = extract(self.call(operation, query, dict(variables, page={'first': 100, 'after': cursor})))
            yield from result['nodes']
            info = result['pageInfo']
            if not info['hasNextPage']:
                return
            cursor = info['endCursor']
            if not cursor or cursor in seen:
                raise UploaderError('Pagination did not advance; results are incomplete.')
            seen.add(cursor)

    def accounts(self):
        q = '''query Accounts($page: PageInput!) { me { ... on AuthenticatedUser {
accounts(page: $page, order: {direction: ASC, field: DISPLAY_NAME}) {
nodes { account { id displayName version } } pageInfo { endCursor hasNextPage }
} } } }'''
        return [n['account'] for n in self._pages('Accounts', q, {}, lambda d: d['me']['accounts'])]

    def workspaces(self, account_id):
        q = '''query Workspaces($id: ID!, $page: PageInput!) { account(accountId: $id) {
workspaces(page: $page, order: {direction: ASC, field: NAME}) {
nodes { id name } pageInfo { endCursor hasNextPage } } } }'''
        return list(self._pages('Workspaces', q, {'id': account_id}, lambda d: d['account']['workspaces']))

    def projects(self, workspace_id):
        q = '''query Projects($id: ID!, $page: PageInput!) { account(by: {workspaceId: $id}) {
workspace(workspaceId: $id) { projects(page: $page) {
nodes { id name rootAssetId } pageInfo { endCursor hasNextPage } } } } }'''
        return list(self._pages('Projects', q, {'id': workspace_id}, lambda d: d['account']['workspace']['projects']))

    def walk(self, folder_id, recursive=False, limit=10000):
        if limit <= 0:
            raise UploaderError('--limit must be positive.')
        pending, seen, assets_seen, count = [(folder_id, '')], set(), set(), 0
        while pending:
            folder, prefix = pending.pop()
            if folder in seen:
                raise UploaderError('Folder traversal repeated a folder; results are incomplete.')
            seen.add(folder)
            for item in self.children_assets(folder, limit - count):
                if item['id'] in assets_seen:
                    raise UploaderError('Traversal repeated an asset; the tree may have changed.')
                assets_seen.add(item['id'])
                count += 1
                if count > limit:
                    raise UploaderError('Traversal limit reached; narrow the folder scope or increase --limit.')
                item['relative_parent'] = prefix
                yield item
                if recursive and item['__typename'] == 'FolderAsset':
                    from .downloader import safe_name
                    pending.append((item['id'], prefix + safe_name(item['name']) + '--' + item['id'] + '/'))

    def comments(self, asset_id):
        q = '''query Comments($id: ID!, $page: Int!) { asset(assetId: $id) {
id commentCount comments(orderBy: [{direction: ASC, field: TIMECODE}, {direction: DESC, field: CREATED_AT}],
includeReplies: true, pagination: {pageNumber: $page, pageSize: 100}) {
result { id text timestampMicroseconds duration insertedAt editedAt completed parentId
owner { id name } } } } }'''
        rows, seen = [], set()
        for page in range(1, 1001):
            asset = self.call('Comments', q, {'id':asset_id,'page':page}).get('asset')
            if not asset: raise UploaderError('Asset is missing or inaccessible.')
            batch = asset['comments']['result']
            if not batch: return {'asset_id':asset_id,'comments':rows,'time_unit':'microseconds','reported_comment_count':asset['commentCount']}
            new = [r for r in batch if r['id'] not in seen]
            if not new: raise UploaderError('Comment pagination did not advance; export aborted.')
            seen.update(r['id'] for r in new)
            rows.extend(new)
            if len(batch)<100:
                return {'asset_id':asset_id,'comments':rows,'time_unit':'microseconds','reported_comment_count':asset['commentCount']}
        raise UploaderError('Comment export exceeded its safety limit.')


def public(value):
    """Keep signed media URLs out of printed/exported API data."""
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if 'url' not in k.lower()}
    if isinstance(value, list):
        return [public(v) for v in value]
    return value
