"""Only the web-client operations needed by this uploader.

No raw HTTP/GraphQL error, token, cookie, or storage URL is included in errors.
Read retries are bounded; creation mutations are never automatically replayed.
"""
import re
import threading
import time
from urllib.parse import parse_qs, urlparse

import requests

from .storage import UploaderError
from .backoff import retry_delay

ENDPOINT = 'https://api.frame.io/graphql'
ME = 'query Me { me { id email name } }'
PROJECT = '''query Project($id: ID!) { project(projectId: $id) {
id name rootAssetId workspaceId workspace { account { id } }
permissions { canCreateAsset canDownloadAsset canMoveAsset canDownloadTranscription canShare canEditUserPermissions }
} }'''
FOLDER = '''query Folder($id: ID!) { asset(assetId: $id) {
id name __typename project { id } parent { id }
... on FolderAsset { permissions { canCreateChildren canViewChildren canMoveChildren canDownloadChildren }
restricted isRestrictedDescendant }
} }'''
ASSET = 'query Asset($id: ID!) { asset(assetId: $id) { id name status } }'
UPLOAD_EVIDENCE = '''query UploadEvidence($id: ID!) { asset(assetId: $id) {
id name status filesize project { id } parent { id }
} }'''
REFRESH = '''mutation RefreshAccessToken($input: CycleRefreshTokenInput!) {
cycleRefreshToken(input: $input) { token { accessToken refreshToken expiration sessionToken } } }'''


class Api:
    def __init__(self, store, http=None, sleep=time.sleep):
        self.store, self.http, self.sleep = store, http or requests.Session(), sleep
        self.credentials = store.load()
        self.lock = threading.RLock()
        self.not_before = 0.0

    def _request(self, name, query, variables, *, auth=True, retry_reads=True):
        read = query.lstrip().startswith('query ')
        allowed = {'RefreshAccessToken': REFRESH,
                   'CreateTransferBatch': CREATE_BATCH,
                   'AddAssetsToTransferBatch': ADD_ASSET,
                   'UpdateTransferBatches': COMPLETE_BATCH,
                   'CreateFolder': CREATE_FOLDER, 'MoveAssets': MOVE_ASSETS}
        if not read and query != allowed.get(name):
            raise UploaderError('Mutation is not permitted by this client.')
        if read and re.search(r'\bmutation\b', query):
            raise UploaderError('Mixed query/mutation documents are not permitted.')
        headers = {
            'content-type': 'application/json',
            'apollographql-client-name': self.credentials['client_name'],
            'apollographql-client-version': self.credentials['client_version'],
        }
        if auth:
            headers['authorization'] = 'Bearer ' + self.credentials['access_token']
        attempts = 3 if read and retry_reads else 1
        for attempt in range(attempts):
            wait = self.not_before - time.monotonic()
            if wait > 0:
                self.sleep(wait)
            try:
                response = self.http.post(ENDPOINT, headers=headers,
                    json={'operationName': name, 'query': query, 'variables': variables}, timeout=(10, 30))
            except requests.RequestException:
                if attempt + 1 < attempts:
                    self.sleep(retry_delay({}, attempt))
                    continue
                raise UploaderError(f'{name}: network failure; write outcome may need reconciliation.') from None
            if response.status_code in (429, 500, 502, 503, 504):
                self.not_before = time.monotonic() + retry_delay(response.headers, attempt)
                if attempt + 1 < attempts:
                    response.close()
                    continue
            if not response.ok:
                hint = ' Run login again.' if response.status_code in (401, 403) else ''
                raise UploaderError(f'{name}: HTTP {response.status_code}.' + hint)
            try:
                body = response.json()
            except ValueError:
                raise UploaderError(f'{name}: invalid API response.') from None
            if body.get('errors'):
                # Return only known-format extension codes; messages may echo secret inputs.
                codes = [e.get('extensions', {}).get('code', '') for e in body['errors']]
                codes = [c for c in codes if isinstance(c, str) and re.fullmatch(r'[A-Z_]{1,60}', c)]
                raise UploaderError(f'{name}: GraphQL error' + (f' ({", ".join(codes)})' if codes else '') + '.')
            if not isinstance(body.get('data'), dict):
                raise UploaderError(f'{name}: missing response data.')
            return body['data']
        raise UploaderError(f'{name}: retries exhausted.')

    def refresh(self):
        with self.lock:
            c = self.credentials
            result = self._request('RefreshAccessToken', REFRESH, {'input': {
                'accessToken': c['access_token'], 'refreshToken': c.get('refresh_token'),
                'sessionToken': c.get('session_token')}}, auth=False, retry_reads=False)
            token = (result.get('cycleRefreshToken') or {}).get('token')
            if not token or not token.get('accessToken'):
                raise UploaderError('Session renewal failed. Run login again.')
            updated = {**c, 'access_token': token['accessToken'],
                'refresh_token': token.get('refreshToken') or c.get('refresh_token'),
                'session_token': token.get('sessionToken') or c.get('session_token'),
                'expires_at': token['expiration']}
            # Persist rotated tokens before allowing another network operation.
            self.store.save(updated)
            self.credentials = updated

    def call(self, name, query, variables=None):
        with self.lock:
            if float(self.credentials.get('expires_at', 0)) <= time.time() + 120:
                self.refresh()
            return self._request(name, query, variables or {})

    def me(self):
        return self.call('Me', ME)['me']

    def project(self, project_id):
        project = self.call('Project', PROJECT, {'id': project_id}).get('project')
        if not project or project.get('id') != project_id:
            raise UploaderError('Project is not accessible.')
        project['account'] = project['workspace']['account']
        return project

    def folder(self, folder_id, project_id):
        folder = self.call('Folder', FOLDER, {'id': folder_id}).get('asset')
        if not folder or folder.get('id') != folder_id or folder.get('__typename') != 'FolderAsset':
            raise UploaderError('Destination is not an accessible folder.')
        if (folder.get('project') or {}).get('id') != project_id:
            raise UploaderError('Destination folder belongs to a different project.')
        return folder

    def children(self, folder_id, first=100):
        if type(first) is not int or not 1 <= first <= 100:
            raise UploaderError('Folder page size must be between 1 and 100.')
        # The legacy folderAssets query silently excludes folders by default.
        query = '''query FolderChildren($id: ID!, $page: PageInput!) {
asset(assetId: $id) { id ... on FolderAsset {
matchingChildren(filters: [], flattenFolders: false, page: $page, sortBys: []) {
nodes { id } pageInfo { endOffset hasNextPage } totalCount
} } } }'''
        offset, seen, expected_total = 0, set(), None
        while True:
            asset = self.call('FolderChildren', query, {'id': folder_id,
                'page': {'first': first, 'afterOffset': offset, 'mode': 'OFFSET'}}).get('asset')
            if not asset or asset.get('id') != folder_id or not isinstance(asset.get('matchingChildren'), dict):
                raise UploaderError('Folder children are unavailable or inaccessible.')
            connection = asset['matchingChildren']
            total = connection.get('totalCount')
            if type(total) is not int or total < 0 or (expected_total is not None and total != expected_total):
                raise UploaderError('Folder count changed or is missing; rerun the listing.')
            expected_total = total
            nodes = connection['nodes']
            ids = [row.get('id') for row in nodes if isinstance(row, dict)]
            if (len(ids) != len(nodes) or any(not isinstance(id, str) or not id for id in ids)
                    or len(set(ids)) != len(ids) or seen.intersection(ids) or len(nodes) > first):
                raise UploaderError('Folder pagination repeated or returned invalid asset IDs.')
            seen.update(ids)
            page = connection['pageInfo']
            more, end = page.get('hasNextPage'), page.get('endOffset')
            if type(more) is not bool or len(seen) > total:
                raise UploaderError('Folder pagination is inconsistent.')
            if more and (not nodes or type(end) is not int or end != offset + len(nodes) or len(seen) >= total):
                raise UploaderError('Folder pagination offset did not advance consistently.')
            if not more and len(seen) != total:
                raise UploaderError('Folder listing ended before all children were returned.')
            yield from nodes
            if not more:
                break
            offset = end

    def status(self, asset_id):
        asset = self.call('Asset', ASSET, {'id': asset_id}).get('asset')
        if not asset:
            raise UploaderError('Upload asset is unavailable; reconcile before retrying.')
        return asset['status']

    def upload_evidence(self, asset_id):
        asset = self.call('UploadEvidence', UPLOAD_EVIDENCE, {'id': asset_id}).get('asset')
        if not asset or asset.get('id') != asset_id:
            raise UploaderError('Upload asset is unavailable; reconcile before retrying.')
        return asset

    def upload_states(self, asset_ids):
        ids = list(asset_ids)
        if not ids or len(ids) > 100 or len(set(ids)) != len(ids):
            raise UploaderError('Upload status reads require 1–100 distinct asset IDs.')
        query = '''query UploadStates($ids: [ID!]!) { assets(assetIds: $ids) {
id status filesize project { id } parent { id } } }'''
        rows = self.call('UploadStates', query, {'ids': ids}).get('assets') or []
        by_id = {row['id']: row for row in rows if row and row.get('id')}
        if len(rows) != len(ids) or set(by_id) != set(ids):
            raise UploaderError('Upload status response is incomplete or repeats asset IDs.')
        return [by_id[id] for id in ids]

    def create_batch(self, account_id, name):
        query = CREATE_BATCH
        return self.call('CreateTransferBatch', query, {'input': {'accountId': account_id, 'name': name,
            'topLevelFileCount': 1, 'topLevelFolderCount': 0, 'uploadedVia': 'BROWSER'}})['createTransferBatch']['transferBatch']['id']

    def create_asset(self, batch_id, folder_id, name, size, mime):
        query = ADD_ASSET
        data = self.call('AddAssetsToTransferBatch', query, {'input': {'transferBatchId': batch_id,
            'assets': [{'id': 0, 'name': name, 'type': 'file', 'parentId': folder_id, 'filesize': size, 'filetype': mime}]}})
        return data['addAssetsToTransferBatch']['assetItems'][0]['asset']

    def part_url(self, asset_id, index):
        return self.part_urls(asset_id, index, 1)[0]

    def part_urls(self, asset_id, index, count):
        if type(index) is not int or index < 0 or type(count) is not int or not 1 <= count <= 4:
            raise UploaderError('Upload URL windows require a nonnegative offset and 1–4 parts.')
        query = '''query GetAssetUploadUrls($assetId: ID!, $limit: Int, $offset: Int) {
asset(assetId: $assetId) { uploadUrls(limit: $limit, offset: $offset) } }'''
        urls = self.call('GetAssetUploadUrls', query, {'assetId': asset_id, 'limit': count, 'offset': index})['asset']['uploadUrls']
        if not isinstance(urls, list) or len(urls) != count or any(not isinstance(url, str) or not url for url in urls):
            raise UploaderError('Missing storage upload URL.')
        for offset, url in enumerate(urls):
            parsed = urlparse(url)
            host = (parsed.hostname or '').lower()
            if parsed.scheme != 'https' or not host.endswith('.amazonaws.com') or parsed.username or parsed.password:
                raise UploaderError('Unsupported storage backend; only the verified HTTPS S3 path is enabled.')
            params = parse_qs(parsed.query, keep_blank_values=True)
            numbers = [params[key] for key in ('partNumber', 'x-amz-meta-part_number') if key in params]
            expected = [str(index + offset + 1)]
            # Frame.io also stages each part as a separate S3 object, using
            # signed metadata instead of the native S3 multipart parameter.
            if (count > 1 and not numbers) or any(number != expected for number in numbers):
                raise UploaderError('Upload URL window has missing or unexpected part numbers.')
        return urls

    def complete_batch(self, batch_id):
        query = COMPLETE_BATCH
        data = self.call('UpdateTransferBatches', query, {'input': {'transferBatchUpdates': [{'id': batch_id, 'status': 'SUCCEEDED'}]}})
        if not data['updateTransferBatches']['successful']:
            raise UploaderError('Transfer bookkeeping failed; rerun to reconcile the existing asset.')

    def create_folder(self, parent_id, name):
        data = self.call('CreateFolder', CREATE_FOLDER,
                         {'input': {'parentId': parent_id, 'name': name, 'restricted': False}})
        asset = (data.get('createFolder') or {}).get('asset')
        if not asset or not asset.get('id'):
            raise UploaderError('Folder creation returned no identity; reconcile before retrying.')
        return asset

    def move_asset(self, asset_id, parent_id):
        # One asset per request: no partially successful bulk moves to reconcile.
        data = self.call('MoveAssets', MOVE_ASSETS,
                         {'input': {'assetIds': [asset_id], 'parentId': parent_id,
                                    'movePrivateCommentsToNewWorkspace': False}})
        assets = (data.get('moveAssets') or {}).get('assets') or []
        if len(assets) != 1 or not assets[0] or assets[0].get('id') != asset_id:
            raise UploaderError('Move returned no matching asset; reconcile its parent before retrying.')
        return assets[0]


CREATE_BATCH = '''mutation CreateTransferBatch($input: CreateTransferBatchInput!) {
createTransferBatch(input: $input) { transferBatch { id } } }'''
ADD_ASSET = '''mutation AddAssetsToTransferBatch($input: AddAssetsToTransferBatchInput!) {
addAssetsToTransferBatch(input: $input) { assetItems { asset { id totalPartCount } } } }'''
COMPLETE_BATCH = '''mutation UpdateTransferBatches($input: UpdateTransferBatchesInput!) {
updateTransferBatches(input: $input) { successful } }'''

CREATE_FOLDER = '''mutation CreateFolder($input: CreateFolderInput!) {
createFolder(input: $input) { asset { id name __typename project { id } parent { id } } } }'''
MOVE_ASSETS = '''mutation MoveAssets($input: MoveAssetsInput!) {
moveAssets(input: $input) { assets { id name __typename project { id } parent { id } } } }'''
