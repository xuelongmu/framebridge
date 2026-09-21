"""Only the web-client operations needed by this uploader.

No raw HTTP/GraphQL error, token, cookie, or storage URL is included in errors.
Read retries are bounded; creation mutations are never automatically replayed.
"""
import re
import threading
import time
from urllib.parse import urlparse

import requests

from .storage import UploaderError

ENDPOINT = 'https://api.frame.io/graphql'
ME = 'query Me { me { id email name } }'
PROJECT = '''query Project($id: ID!) { project(projectId: $id) {
id name rootAssetId workspaceId workspace { account { id } }
permissions { canCreateAsset canDownloadAsset canShare canEditUserPermissions }
} }'''
FOLDER = '''query Folder($id: ID!) { asset(assetId: $id) {
id name __typename project { id } parent { id }
... on FolderAsset { permissions { canCreateChildren canViewChildren } }
} }'''
ASSET = 'query Asset($id: ID!) { asset(assetId: $id) { id name status } }'
REFRESH = '''mutation RefreshAccessToken($input: CycleRefreshTokenInput!) {
cycleRefreshToken(input: $input) { token { accessToken refreshToken expiration sessionToken } } }'''


class Api:
    def __init__(self, store, http=None, sleep=time.sleep):
        self.store, self.http, self.sleep = store, http or requests.Session(), sleep
        self.credentials = store.load()
        self.lock = threading.RLock()

    def _request(self, name, query, variables, *, auth=True, retry_reads=True):
        read = query.lstrip().startswith('query ')
        headers = {
            'content-type': 'application/json',
            'apollographql-client-name': self.credentials['client_name'],
            'apollographql-client-version': self.credentials['client_version'],
        }
        if auth:
            headers['authorization'] = 'Bearer ' + self.credentials['access_token']
        attempts = 3 if read and retry_reads else 1
        for attempt in range(attempts):
            try:
                response = self.http.post(ENDPOINT, headers=headers,
                    json={'operationName': name, 'query': query, 'variables': variables}, timeout=(10, 30))
            except requests.RequestException:
                if attempt + 1 < attempts:
                    self.sleep(2 ** attempt)
                    continue
                raise UploaderError(f'{name}: network failure; write outcome may need reconciliation.') from None
            if response.status_code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                self.sleep(min(30, 2 ** attempt))
                continue
            if not response.ok:
                raise UploaderError(f'{name}: HTTP {response.status_code}. Re-login for expired authorization.')
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
        if not project:
            raise UploaderError('Project is not accessible.')
        project['account'] = project['workspace']['account']
        return project

    def folder(self, folder_id, project_id):
        folder = self.call('Folder', FOLDER, {'id': folder_id}).get('asset')
        if not folder or folder.get('__typename') != 'FolderAsset':
            raise UploaderError('Destination is not an accessible folder.')
        if (folder.get('project') or {}).get('id') != project_id:
            raise UploaderError('Destination folder belongs to a different project.')
        return folder

    def children(self, folder_id, first=100):
        query = '''query GetFolderAssets($page: PageInput!, $query: FolderAssetsQueryInput!) {
folderAssets(page: $page, query: $query) { nodes { id index } pageInfo { endCursor hasNextPage } } }'''
        cursor, seen = None, set()
        while True:
            connection = self.call('GetFolderAssets', query, {'page': {'first': first, 'after': cursor},
                'query': {'folderId': folder_id, 'sortBy': [], 'customSort': False}})['folderAssets']
            yield from connection['nodes']
            page = connection['pageInfo']
            if not page['hasNextPage']:
                break
            cursor = page['endCursor']
            if not cursor or cursor in seen:
                raise UploaderError('Pagination cursor did not advance.')
            seen.add(cursor)

    def status(self, asset_id):
        asset = self.call('Asset', ASSET, {'id': asset_id}).get('asset')
        if not asset:
            raise UploaderError('Upload asset is unavailable; reconcile before retrying.')
        return asset['status']

    def create_batch(self, account_id, name):
        query = '''mutation CreateTransferBatch($input: CreateTransferBatchInput!) {
createTransferBatch(input: $input) { transferBatch { id } } }'''
        return self.call('CreateTransferBatch', query, {'input': {'accountId': account_id, 'name': name,
            'topLevelFileCount': 1, 'topLevelFolderCount': 0, 'uploadedVia': 'BROWSER'}})['createTransferBatch']['transferBatch']['id']

    def create_asset(self, batch_id, folder_id, name, size, mime):
        query = '''mutation AddAssetsToTransferBatch($input: AddAssetsToTransferBatchInput!) {
addAssetsToTransferBatch(input: $input) { assetItems { asset { id totalPartCount } } } }'''
        data = self.call('AddAssetsToTransferBatch', query, {'input': {'transferBatchId': batch_id,
            'assets': [{'id': 0, 'name': name, 'type': 'file', 'parentId': folder_id, 'filesize': size, 'filetype': mime}]}})
        return data['addAssetsToTransferBatch']['assetItems'][0]['asset']

    def part_url(self, asset_id, index):
        query = '''query GetAssetUploadUrls($assetId: ID!, $limit: Int, $offset: Int) {
asset(assetId: $assetId) { uploadUrls(limit: $limit, offset: $offset) } }'''
        urls = self.call('GetAssetUploadUrls', query, {'assetId': asset_id, 'limit': 1, 'offset': index})['asset']['uploadUrls']
        if len(urls) != 1 or not urls[0]:
            raise UploaderError('Missing storage upload URL.')
        url = urls[0]
        host = (urlparse(url).hostname or '').lower()
        if urlparse(url).scheme != 'https' or not host.endswith('.amazonaws.com'):
            raise UploaderError('Unsupported storage backend; only the verified HTTPS S3 path is enabled.')
        return url

    def complete_batch(self, batch_id):
        query = '''mutation UpdateTransferBatches($input: UpdateTransferBatchesInput!) {
updateTransferBatches(input: $input) { successful } }'''
        data = self.call('UpdateTransferBatches', query, {'input': {'transferBatchUpdates': [{'id': batch_id, 'status': 'SUCCEEDED'}]}})
        if not data['updateTransferBatches']['successful']:
            raise UploaderError('Transfer bookkeeping failed; rerun to reconcile the existing asset.')
