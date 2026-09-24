"""Read-only navigation and media catalog, based on observed web operations."""
from .api import Api
from .storage import UploaderError

MEDIA = '''query Media($ids: [ID!]!) { assets(assetIds: $ids) {
id name status __typename isWatermarked
... on VideoAsset { forensicallyWatermarked media { id duration fps timecode
original { key downloadUrl filesizeInBytes codec }
videoTranscodes { key downloadUrl encodeStatus filesizeInBytes width height codec }
metadata { id originalWidth originalHeight } } }
... on AudioAsset { media { id original { key downloadUrl filesizeInBytes codec }
audioTranscodes { key downloadUrl encodeStatus } } }
} }'''


class Catalog(Api):
    def media(self, asset_id):
        items = self.call('Media', MEDIA, {'ids': [asset_id]})['assets']
        if len(items) != 1 or not items[0]:
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
        q = '''query InspectAsset($id: ID!) { asset(assetId: $id) {
id name status __typename project { id } parent { id }
... on VersionStackAsset { versions { id name status __typename } }
} }'''
        asset = self.call('InspectAsset', q, {'id': asset_id}).get('asset')
        if not asset:
            raise UploaderError('Asset is missing or inaccessible.')
        return asset

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
        pending, seen, count = [(folder_id, '')], set(), 0
        while pending:
            folder, prefix = pending.pop()
            if folder in seen:
                continue
            seen.add(folder)
            for node in self.children(folder):
                count += 1
                if count > limit:
                    raise UploaderError('Traversal limit reached; narrow the folder scope or increase --limit.')
                item = self.asset(node['id'])
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
