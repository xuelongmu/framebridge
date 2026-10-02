import copy
import unittest
from unittest.mock import Mock

from framebridge.api import Api, CREATE_FOLDER, MOVE_ASSETS
from framebridge.catalog import Catalog, public
from framebridge.storage import UploaderError


def asset(id, parent='root', type='UnsupportedAsset'):
    return {'id': id, 'name': id, '__typename': type, 'parent': {'id': parent},
            'project': {'id': 'project'}, 'filesize': 1, 'status': 'UPLOADED'}


class CatalogFeatures(unittest.TestCase):
    def setUp(self):
        store = Mock()
        store.load.return_value = {}
        self.api = Catalog(store)
        self.api.call = Mock()

    def test_batch_hydration_preserves_requested_order(self):
        self.api.call.return_value = {'assets': [asset('b'), asset('a')]}
        self.assertEqual([x['id'] for x in self.api.assets(['a', 'b'])], ['a', 'b'])

    def test_hydration_refuses_missing_duplicate_and_unexpected_ids(self):
        for result in ([asset('a')], [asset('a'), None], [asset('a'), asset('a')], [asset('a'), asset('c')]):
            self.api.call.return_value = {'assets': result}
            with self.assertRaises(UploaderError): self.api.assets(['a', 'b'])
        for ids in ([], ['a', 'a'], [str(i) for i in range(101)]):
            with self.assertRaises(UploaderError): self.api.assets(ids)

    def test_children_use_two_hydration_calls_for_101_assets(self):
        self.api.children = Mock(return_value=iter({'id': str(i)} for i in range(101)))
        self.api.call.side_effect = lambda name, query, variables: {'assets': [asset(id) for id in variables['ids']]}
        self.assertEqual(len(list(self.api.children_assets('root'))), 101)
        self.assertEqual(self.api.call.call_count, 2)
        self.assertEqual(len(self.api.call.call_args_list[0].args[2]['ids']), 100)

    def test_children_limit_and_parent_consistency(self):
        self.api.children = Mock(return_value=iter([{'id': 'a'}, {'id': 'b'}]))
        with self.assertRaisesRegex(UploaderError, 'limit'):
            list(self.api.children_assets('root', 1))
        self.api.children.return_value = iter([{'id': 'a'}])
        self.api.call.return_value = {'assets': [asset('a', parent='other')]}
        with self.assertRaisesRegex(UploaderError, 'moved'):
            list(self.api.children_assets('root'))

    def test_children_duplicate_page_nodes_refused(self):
        self.api.children = Mock(return_value=iter([{'id': 'a'}, {'id': 'a'}]))
        with self.assertRaisesRegex(UploaderError, 'repeated'):
            list(self.api.children_assets('root'))

    def test_recursive_walk_exact_limit_allows_empty_folder(self):
        self.api.children = Mock(side_effect=lambda id: iter([{'id': 'folder'}] if id == 'root' else []))
        self.api.call.return_value = {'assets': [asset('folder', type='FolderAsset')]}
        result = list(self.api.walk('root', recursive=True, limit=1))
        self.assertEqual(len(result), 1)

    def test_recursive_walk_preserves_paths(self):
        rows = {'root': [asset('folder', type='FolderAsset')], 'folder': [asset('file', parent='folder')]}
        self.api.children_assets = Mock(side_effect=lambda id, limit: copy.deepcopy(rows[id]))
        result = list(self.api.walk('root', recursive=True))
        self.assertEqual(result[1]['relative_parent'], 'folder--folder/')

    def test_inspect_joins_definitions_without_claiming_video_readiness(self):
        self.api.media = Mock(return_value={'id': 'a', '__typename': 'AudioAsset', 'project': {'id': 'p'},
                                           'media': {'audioTranscodes': [{'key': 'aac', 'encodeStatus': 'SUCCESS'}]}})
        self.api.call.side_effect = [
            {'asset': {'fieldValuesNew': {'nodes': [{'fieldDefinition': {'id': 'f'}, 'value': {'text': 'hello'}}],
                                          'pageInfo': {'hasNextPage': False}}}},
            {'project': {'fieldDefinitions': {'nodes': [{'id': 'f', 'name': 'Notes'}],
                                               'pageInfo': {'hasNextPage': False}}}}]
        result = self.api.inspect('a')
        self.assertEqual(result['fields'][0]['definition']['name'], 'Notes')
        self.assertFalse(result['has_ready_video_proxy'])

    def test_transcript_pagination_and_type_guard(self):
        self.api.call.side_effect = [
            {'asset': {'__typename': 'VideoAsset', 'localeTranscriptions': {
                'nodes': [{'id': 't1'}], 'pageInfo': {'hasNextPage': True, 'endCursor': 'c'}}}},
            {'asset': {'__typename': 'VideoAsset', 'localeTranscriptions': {
                'nodes': [{'id': 't2'}], 'pageInfo': {'hasNextPage': False}}}}]
        self.assertEqual(len(self.api.transcripts('a')), 2)
        self.api.call.side_effect = None
        self.api.call.return_value = {'asset': {'__typename': 'FolderAsset'}}
        with self.assertRaises(UploaderError): self.api.transcripts('a')

    def test_permission_diagnostics_resolve_version_parent(self):
        self.api.asset = Mock(side_effect=[asset('version', 'stack'), asset('stack', 'folder', 'VersionStackAsset'),
                                          asset('folder', 'root', 'FolderAsset')])
        self.api.project = Mock(return_value={'permissions': {'canMoveAsset': False}})
        self.api.folder = Mock(return_value={'id': 'folder', 'restricted': False, 'isRestrictedDescendant': False,
                                             'permissions': {'canMoveChildren': True}})
        result = self.api.permissions('version')
        self.assertEqual(result['folder_id'], 'folder')
        self.assertEqual(result['denied'], ['project.canMoveAsset'])

    def test_stats_keep_server_scope(self):
        self.api.call.return_value = {'folder': {'id': 'f', 'folderStats': {'countFiles': 0, 'countFolders': 0,
                                                                           'sizeFiles': 0, 'sizeFolders': 0}}}
        self.assertEqual(self.api.folder_stats('f')['scope'], 'server_reported')

    def test_nested_urls_are_not_emitted(self):
        result = public({'transcripts': [{'srt': {'downloadUrl': 'secret'}, 'fields': [{'text': 'value'}]}]})
        self.assertNotIn('secret', str(result))
        self.assertIn('value', str(result))


class MutationContracts(unittest.TestCase):
    def setUp(self):
        store = Mock()
        store.load.return_value = dict(access_token='secret', client_name='web', client_version='test', expires_at=9999999999)
        self.http = Mock()
        self.api = Api(store, self.http)

    def test_only_exact_new_mutation_documents_are_allowed(self):
        self.http.post.return_value = Mock(status_code=200, ok=True)
        self.http.post.return_value.json.return_value = {'data': {'createFolder': {'asset': {'id': 'folder'}}}}
        self.assertEqual(self.api.create_folder('parent', 'name')['id'], 'folder')
        payload = self.http.post.call_args.kwargs['json']
        self.assertEqual(payload['query'], CREATE_FOLDER)
        self.assertEqual(payload['variables']['input'], {'parentId': 'parent', 'name': 'name', 'restricted': False})
        with self.assertRaises(UploaderError):
            self.api.call('CreateFolder', CREATE_FOLDER + '\nmutation Delete { delete }', {})
        self.assertEqual(self.http.post.call_count, 1)

    def test_move_document_and_single_asset_response(self):
        self.http.post.return_value = Mock(status_code=200, ok=True)
        self.http.post.return_value.json.return_value = {'data': {'moveAssets': {'assets': [{'id': 'a'}]}}}
        self.api.move_asset('a', 'parent')
        payload = self.http.post.call_args.kwargs['json']
        self.assertEqual(payload['query'], MOVE_ASSETS)
        self.assertEqual(payload['variables']['input']['assetIds'], ['a'])
        self.assertFalse(payload['variables']['input']['movePrivateCommentsToNewWorkspace'])

    def test_new_mutations_do_not_retry(self):
        import requests
        self.http.post.side_effect = requests.ConnectionError('private request data')
        for call in (lambda: self.api.create_folder('p', 'n'), lambda: self.api.move_asset('a', 'p')):
            self.http.post.reset_mock()
            with self.assertRaises(UploaderError) as error: call()
            self.assertEqual(self.http.post.call_count, 1)
            self.assertNotIn('private request data', str(error.exception))
