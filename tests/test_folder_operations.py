import argparse
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from framebridge.commands import folder_command, main, parser
from framebridge.folders import FolderCache, child_index, make_directory, move, operation_key
from framebridge.storage import Journal, OperationJournal, UploaderError
from framebridge.tree_upload import scan_tree, upload_tree
from framebridge.uploader import fingerprint, upload_key


class FakeApi:
    def __init__(self):
        self.nodes = {}
        self.created, self.moved = [], []
        self.lose_create = self.lose_move = False
        self.project_value = {'id': 'p', 'rootAssetId': 'root', 'account': {'id': 'account'},
                              'permissions': {'canCreateAsset': True, 'canMoveAsset': True}}
        self.add('root', 'root', None)
        self.add('left', 'left', 'root')
        self.add('right', 'right', 'root')

    def add(self, id, name, parent, type='FolderAsset', project='p', **extra):
        self.nodes[id] = {'id': id, 'name': name, '__typename': type, 'status': 'UPLOADED',
                          'parent': {'id': parent} if parent else None, 'project': {'id': project},
                          'restricted': False, 'isRestrictedDescendant': False,
                          'permissions': {'canCreateChildren': True, 'canViewChildren': True,
                                          'canMoveChildren': True, 'canDownloadChildren': True}, **extra}
        return copy.deepcopy(self.nodes[id])

    def project(self, id):
        return copy.deepcopy(self.project_value)

    def asset(self, id):
        if id not in self.nodes:
            raise UploaderError('Missing asset')
        return copy.deepcopy(self.nodes[id])

    def folder(self, id, project):
        item = self.asset(id)
        if item['__typename'] != 'FolderAsset' or item['project']['id'] != project:
            raise UploaderError('Not a folder in the selected project')
        return item

    def children_assets(self, id, limit=10000):
        children = [copy.deepcopy(n) for n in self.nodes.values() if (n['parent'] or {}).get('id') == id]
        if len(children) > limit:
            raise UploaderError('Traversal limit')
        return children

    def walk(self, id, recursive=False, limit=10000):
        result, pending = [], [id]
        while pending:
            for item in self.children_assets(pending.pop()):
                result.append(item)
                if len(result) > limit:
                    raise UploaderError('Traversal limit')
                if recursive and item['__typename'] == 'FolderAsset':
                    pending.append(item['id'])
        return result

    def create_folder(self, parent, name):
        item = self.add('new-' + str(len(self.created)), name, parent)
        self.created.append(item['id'])
        if self.lose_create:
            raise UploaderError('Lost response')
        return item

    def move_asset(self, asset, parent):
        self.nodes[asset]['parent'] = {'id': parent}
        self.moved.append((asset, parent))
        if self.lose_move:
            raise UploaderError('Lost response')
        return self.asset(asset)


class OperationFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = OperationJournal(self.root / 'operations.sqlite3')
        self.addCleanup(self.journal.close)
        self.api = FakeApi()


class FolderOperations(OperationFixture):

    def mkdir(self, **kwargs):
        return make_directory(self.api, self.journal, 'p', 'left', 'new', **kwargs)

    def move(self, **kwargs):
        return move(self.api, self.journal, 'p', 'left', 'right', **kwargs)

    def test_mkdir_is_plan_by_default(self):
        self.assertEqual(self.mkdir()['action'], 'create')
        self.assertEqual(self.api.created, [])
        self.assertIsNone(self.journal.get(operation_key('mkdir', 'p', 'left', 'new')))

    def test_sibling_creation_lists_parent_once_and_remembers_names(self):
        with patch.object(self.api, 'children_assets', wraps=self.api.children_assets) as listing:
            cache = FolderCache(self.api, 'p', 10000)
            for index in range(100):
                make_directory(self.api, self.journal, 'p', 'right', f'folder-{index}',
                               execute=True, cache=cache)
            self.assertEqual(listing.call_count, 1)
            self.assertEqual(len(cache.children('right')), 100)
        self.assertEqual(len(self.api.created), 100)

    def test_cached_duplicate_normalized_names_remain_conflicts(self):
        self.api.add('one', 'folder', 'right')
        self.api.add('two', 'FOLDER', 'right')
        cache = FolderCache(self.api, 'p', 10000)
        self.assertEqual(len(cache.children('right')['folder']), 2)
        with self.assertRaisesRegex(UploaderError, 'conflict'):
            make_directory(self.api, self.journal, 'p', 'right', 'folder',
                           execute=True, existing='reuse', cache=cache)

    def test_cached_creation_still_verifies_response_and_retains_ambiguity(self):
        cache = FolderCache(self.api, 'p', 10000)
        self.api.lose_create = True
        with self.assertRaises(UploaderError):
            make_directory(self.api, self.journal, 'p', 'right', 'x', execute=True, cache=cache)
        self.api.lose_create = False
        with self.assertRaisesRegex(UploaderError, 'ambiguous'):
            make_directory(self.api, self.journal, 'p', 'right', 'x', execute=True, cache=cache)
        self.assertEqual(len(self.api.created), 1)

    def test_mkdir_resumes_known_id(self):
        first = self.mkdir(execute=True)
        second = self.mkdir(execute=True)
        self.assertEqual(first['folder_id'], second['folder_id'])
        self.assertEqual(second['action'], 'resume')
        self.assertEqual(len(self.api.created), 1)

    def test_mkdir_existing_folder_requires_explicit_reuse(self):
        self.api.add('existing', 'new', 'left')
        with self.assertRaisesRegex(UploaderError, 'conflicts'):
            self.mkdir(execute=True)
        self.assertEqual(self.mkdir(existing='reuse', execute=True)['folder_id'], 'existing')
        self.assertEqual(self.api.created, [])

    def test_mkdir_conflicting_file_never_reused(self):
        self.api.add('existing', 'new', 'left', type='UnsupportedAsset')
        with self.assertRaises(UploaderError):
            self.mkdir(existing='reuse', execute=True)
        self.assertEqual(self.api.created, [])

    def test_mkdir_case_conflict_and_duplicate_names_refused(self):
        self.api.add('existing', 'NEW', 'left')
        with self.assertRaises(UploaderError):
            self.mkdir(existing='reuse', execute=True)
        self.api.nodes['existing']['name'] = 'new'
        self.api.add('duplicate', 'new', 'left')
        with self.assertRaises(UploaderError):
            self.mkdir(existing='reuse', execute=True)

    def test_mkdir_invalid_names_and_limits(self):
        for name in ('', '..', '.', 'a/b', 'a\\b', 'bad\x00'):
            with self.subTest(name=name), self.assertRaises(UploaderError):
                make_directory(self.api, self.journal, 'p', 'left', name, execute=True)
        with self.assertRaises(UploaderError): self.mkdir(limit=0)
        self.assertEqual(self.api.created, [])

    def test_mkdir_ambiguous_creation_never_replayed(self):
        self.api.lose_create = True
        with self.assertRaises(UploaderError): self.mkdir(execute=True)
        self.api.lose_create = False
        with self.assertRaisesRegex(UploaderError, 'ambiguous'): self.mkdir(execute=True)
        self.assertEqual(len(self.api.created), 1)
        self.assertEqual(self.mkdir(adopt_id='new-0')['action'], 'adopt')
        with self.assertRaisesRegex(UploaderError, 'ambiguous'): self.mkdir()
        self.mkdir(adopt_id='new-0', execute=True)
        self.assertEqual(self.mkdir(execute=True)['folder_id'], 'new-0')
        self.assertEqual(len(self.api.created), 1)

    def test_mkdir_adoption_checks_exact_identity(self):
        self.api.lose_create = True
        with self.assertRaises(UploaderError): self.mkdir(execute=True)
        self.api.nodes['new-0']['parent'] = {'id': 'right'}
        with self.assertRaisesRegex(UploaderError, 'identity'):
            self.mkdir(adopt_id='new-0', execute=True)

    def test_mkdir_moved_or_deleted_record_not_recreated(self):
        self.mkdir(execute=True)
        self.api.nodes['new-0']['name'] = 'renamed'
        with self.assertRaises(UploaderError): self.mkdir(execute=True)
        del self.api.nodes['new-0']
        with self.assertRaises(UploaderError): self.mkdir(execute=True)
        self.assertEqual(len(self.api.created), 1)

    def test_mkdir_missing_or_denied_permission(self):
        for field in ('canCreateChildren', 'canViewChildren'):
            self.api.nodes['left']['permissions'][field] = False
            with self.assertRaises(UploaderError): self.mkdir(execute=True)
            self.api.nodes['left']['permissions'][field] = True
        self.api.project_value['permissions'].pop('canCreateAsset')
        with self.assertRaises(UploaderError): self.mkdir(execute=True)
        self.assertEqual(self.api.created, [])

    def test_move_is_plan_then_idempotent_execution(self):
        self.assertTrue(self.move()['dry_run'])
        self.assertEqual(self.api.moved, [])
        self.assertEqual(self.move(execute=True)['confirmed_parent_id'], 'right')
        self.assertEqual(self.move(execute=True)['action'], 'already_at_destination')
        self.assertEqual(len(self.api.moved), 1)

    def test_move_lost_response_reconciles_by_id(self):
        self.api.lose_move = True
        with self.assertRaises(UploaderError): self.move(execute=True)
        self.assertEqual(self.move(execute=True)['action'], 'already_at_destination')
        self.assertEqual(self.journal.get(operation_key('move', 'p', 'left', 'right'))['phase'], 'complete')
        self.assertEqual(len(self.api.moved), 1)

    def test_move_unresolved_intent_not_replayed(self):
        key = operation_key('move', 'p', 'left', 'right')
        self.journal.put(key, {'phase': 'submitting'})
        with self.assertRaisesRegex(UploaderError, 'ambiguous'): self.move(execute=True)
        self.assertEqual(self.api.moved, [])

    def test_move_project_root_self_and_descendant_refused(self):
        with self.assertRaisesRegex(UploaderError, 'root'):
            move(self.api, self.journal, 'p', 'root', 'right', execute=True)
        with self.assertRaisesRegex(UploaderError, 'itself'):
            move(self.api, self.journal, 'p', 'left', 'left', execute=True)
        self.api.add('child', 'child', 'left')
        with self.assertRaisesRegex(UploaderError, 'descendants'):
            move(self.api, self.journal, 'p', 'left', 'child', execute=True)
        self.assertEqual(self.api.moved, [])

    def test_move_cross_project_refused(self):
        self.api.nodes['right']['project']['id'] = 'other'
        with self.assertRaises(UploaderError): self.move(execute=True)
        self.api.nodes['right']['project']['id'] = 'p'
        self.api.nodes['left']['project']['id'] = 'other'
        with self.assertRaisesRegex(UploaderError, 'same-project'): self.move(execute=True)

    def test_move_restricted_subtree_or_destination_refused(self):
        self.api.add('child', 'private', 'left', restricted=True)
        with self.assertRaisesRegex(UploaderError, 'Restricted'): self.move(execute=True)
        del self.api.nodes['child']
        self.api.nodes['right']['isRestrictedDescendant'] = True
        with self.assertRaisesRegex(UploaderError, 'Restricted'): self.move(execute=True)
        self.assertEqual(self.api.moved, [])

    def test_move_unknown_restriction_refused(self):
        self.api.nodes['right']['restricted'] = None
        with self.assertRaisesRegex(UploaderError, 'unverified'): self.move(execute=True)

    def test_move_conflicts_and_parent_guard(self):
        with self.assertRaisesRegex(UploaderError, 'source parent|Source parent'):
            self.move(execute=True, expected_parent='other')
        self.api.add('duplicate', 'LEFT', 'right')
        with self.assertRaisesRegex(UploaderError, 'name'): self.move(execute=True)
        self.assertEqual(self.api.moved, [])

    def test_move_rejects_version_member(self):
        self.api.add('stack', 'stack', 'root', type='VersionStackAsset')
        self.api.add('member', 'member', 'stack', type='VideoAsset')
        with self.assertRaises(UploaderError):
            move(self.api, self.journal, 'p', 'member', 'right', execute=True)

    def test_move_permission_and_subtree_limit(self):
        self.api.nodes['root']['permissions']['canMoveChildren'] = False
        with self.assertRaises(UploaderError): self.move(execute=True)
        self.api.nodes['root']['permissions']['canMoveChildren'] = True
        self.api.add('one', 'one', 'left')
        self.api.add('two', 'two', 'left')
        with self.assertRaises(UploaderError): self.move(execute=True, limit=1)
        self.assertEqual(self.api.moved, [])

    def test_move_rechecks_source_after_preflight(self):
        original = self.api.walk
        def changing(*args, **kwargs):
            self.api.nodes['left']['name'] = 'changed'
            return original(*args, **kwargs)
        self.api.walk = changing
        with self.assertRaisesRegex(UploaderError, 'changed during'): self.move(execute=True)
        self.assertEqual(self.api.moved, [])

    def test_move_rechecks_restrictions_at_write_boundary(self):
        original = self.api.walk
        def changing(*args, **kwargs):
            self.api.nodes['left']['restricted'] = True
            return original(*args, **kwargs)
        self.api.walk = changing
        with self.assertRaisesRegex(UploaderError, 'Restricted'): self.move(execute=True)
        self.assertEqual(self.api.moved, [])


class TreeUpload(OperationFixture):
    def setUp(self):
        super().setUp()
        self.uploads = Journal(self.root / 'uploads.sqlite3')
        self.addCleanup(self.uploads.close)
        self.source = self.root / 'delivery'
        (self.source / 'a' / 'nested').mkdir(parents=True)
        (self.source / 'empty').mkdir()
        (self.source / 'root.txt').write_text('root')
        (self.source / 'a' / 'nested' / 'clip.txt').write_text('nested')
        self.sent = []

    def tree(self, **kwargs):
        return upload_tree(self.api, self.journal, self.uploads, self.source, 'p', 'right', **kwargs)

    def fake_upload(self, api, journal, path, project, account, folder, **kwargs):
        key = upload_key(path, folder)
        record = journal.get(key)
        if record:
            return record['asset_id']
        item = api.add('file-' + key, Path(path).name, folder,
                       type='UnsupportedAsset', filesize=Path(path).stat().st_size)
        journal.put(key, {'asset_id': item['id'], 'project_id': project, 'folder_id': folder,
                          'fingerprint': fingerprint(Path(path)), 'phase': 'complete'})
        self.sent.append(path)
        return item['id']

    def test_tree_plan_preserves_nested_and_empty_folders(self):
        plan = self.tree()
        self.assertEqual({row['relative_path'] for row in plan['folders']}, {'.', 'a', 'a/nested', 'empty'})
        self.assertEqual(plan['total_bytes'], 10)
        self.assertEqual(plan['file_count'], 2)
        self.assertEqual(self.api.created, [])
        self.assertEqual(self.sent, [])

    def test_tree_execution_and_repeat_do_not_duplicate(self):
        with patch('framebridge.tree_upload.upload', side_effect=self.fake_upload):
            result = self.tree(execute=True, max_total_bytes=10)
            self.assertEqual(result['failed'], [])
            self.assertEqual(len(result['folders']), 4)
            self.assertEqual(len(result['completed']), 2)
            result = self.tree(execute=True, max_total_bytes=10)
            self.assertEqual(result['failed'], [])
        self.assertEqual(len(self.api.created), 4)
        self.assertEqual(len(self.sent), 2)

    def test_concurrent_tree_execution_and_repeat(self):
        with patch('framebridge.tree_upload.upload', side_effect=self.fake_upload):
            first = self.tree(execute=True, max_total_bytes=10, workers=2)
            second = self.tree(execute=True, max_total_bytes=10, workers=2)
        self.assertEqual(first['failed'], [])
        self.assertEqual(second['failed'], [])
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len(self.api.created), 4)
        self.assertLessEqual(first['metrics']['upload_pool']['peak_active'], 2)

    def test_invalid_workers_fail_before_remote_writes(self):
        with self.assertRaisesRegex(UploaderError, 'workers'):
            self.tree(execute=True, max_total_bytes=10, workers=0)
        self.assertEqual(self.api.created, [])

    def test_tree_contents_and_explicit_reuse(self):
        self.api.add('existing-a', 'a', 'right')
        with self.assertRaises(UploaderError): self.tree(contents=True)
        with patch('framebridge.tree_upload.upload', side_effect=self.fake_upload):
            result = self.tree(contents=True, existing_folders='reuse', execute=True, max_total_bytes=10)
        self.assertEqual(result['failed'], [])
        self.assertEqual(len(self.api.created), 2)
        self.assertNotIn('delivery', [self.api.nodes[x]['name'] for x in self.api.created])

    def test_tree_caps_and_large_file_preflight(self):
        with self.assertRaises(UploaderError): self.tree(execute=True)
        with self.assertRaises(UploaderError): self.tree(max_total_bytes=9, execute=True)
        with self.assertRaises(UploaderError): self.tree(limit=2)
        with (self.source / 'large.bin').open('wb') as stream: stream.truncate(6 * 1024 * 1024)
        plan = self.tree()
        self.assertEqual(plan['total_bytes'], 6 * 1024 * 1024 + 10)
        with self.assertRaises(UploaderError): self.tree(max_total_bytes=10, execute=True)
        self.assertEqual(self.api.created, [])

    def test_tree_empty_file_fails_before_remote_writes(self):
        (self.source / 'zero').touch()
        with self.assertRaisesRegex(UploaderError, 'Empty files'):
            self.tree(execute=True, max_total_bytes=100)
        self.assertEqual(self.api.created, [])

    def test_tree_skips_empty_files_in_plan_and_execution(self):
        (self.source / 'zero').touch()
        (self.source / 'a' / 'nested' / 'zero').touch()
        plan = self.tree(skip_empty_files=True)
        expected = [{'relative_path': 'zero', 'reason': 'empty_file'},
                    {'relative_path': 'a/nested/zero', 'reason': 'empty_file'}]
        self.assertEqual(plan['skipped'], expected)
        self.assertEqual(plan['file_count'], 2)
        self.assertEqual(plan['total_bytes'], 10)
        self.assertEqual(self.api.created, [])
        with patch('framebridge.tree_upload.upload', side_effect=self.fake_upload):
            result = self.tree(skip_empty_files=True, execute=True, max_total_bytes=10)
        self.assertEqual(result['skipped'], expected)
        self.assertEqual(result['failed'], [])
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(Path(path).stat().st_size > 0 for path in self.sent))

    def test_tree_all_empty_preserves_directories_without_uploads(self):
        for path in self.source.rglob('*.txt'):
            path.write_bytes(b'')
        with patch('framebridge.tree_upload.upload') as send:
            result = self.tree(skip_empty_files=True, execute=True, max_total_bytes=1)
        send.assert_not_called()
        self.assertEqual(len(result['folders']), 4)
        self.assertEqual(len(result['skipped']), 2)
        self.assertEqual(result['total_bytes'], 0)

    def test_skipped_empty_files_still_count_toward_limit(self):
        (self.source / 'zero').touch()
        with self.assertRaisesRegex(UploaderError, 'limit'):
            self.tree(skip_empty_files=True, limit=6)

    def test_skip_empty_cli_option(self):
        from framebridge.commands import parser
        args = ['upload-folder', str(self.source), '--project', 'p', '--folder-id', 'right']
        self.assertFalse(parser().parse_args(args).skip_empty_files)
        self.assertTrue(parser().parse_args(args + ['--skip-empty-files']).skip_empty_files)

    def test_tree_file_conflict_preflight(self):
        self.api.add('delivery', 'delivery', 'right')
        self.api.add('conflict', 'root.txt', 'delivery', type='UnsupportedAsset', filesize=4)
        with self.assertRaisesRegex(UploaderError, 'conflicts'):
            self.tree(existing_folders='reuse', execute=True, max_total_bytes=100)
        self.assertEqual(self.api.created, [])

    def test_tree_interruption_resumes_without_duplicate_folders(self):
        attempts = []
        def interrupted(*args, **kwargs):
            attempts.append(True)
            if len(attempts) == 2:
                raise KeyboardInterrupt()
            return self.fake_upload(*args, **kwargs)
        with patch('framebridge.tree_upload.upload', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt): self.tree(execute=True, max_total_bytes=100)
        with patch('framebridge.tree_upload.upload', side_effect=self.fake_upload):
            result = self.tree(execute=True, max_total_bytes=100)
        self.assertEqual(result['failed'], [])
        self.assertEqual(len(self.api.created), 4)
        self.assertEqual(len(self.sent), 2)

    def test_tree_changed_journaled_source_refused(self):
        with patch('framebridge.tree_upload.upload', side_effect=self.fake_upload):
            self.tree(execute=True, max_total_bytes=100)
        (self.source / 'root.txt').write_text('changed')
        with self.assertRaisesRegex(UploaderError, 'source changed'): self.tree()
        self.assertEqual(len(self.api.created), 4)

    def test_tree_new_files_can_be_added_without_recreating_folders(self):
        with patch('framebridge.tree_upload.upload', side_effect=self.fake_upload):
            self.tree(execute=True, max_total_bytes=100)
            (self.source / 'new.txt').write_text('new')
            result = self.tree(execute=True, max_total_bytes=100)
        self.assertEqual(result['failed'], [])
        self.assertEqual(len(self.api.created), 4)
        self.assertEqual(len(self.sent), 3)

    def test_tree_symlink_default_skip_and_root_rejection(self):
        link = self.source / 'linked'
        try:
            link.symlink_to(self.source / 'a', target_is_directory=True)
        except OSError:
            self.skipTest('Symlinks unavailable for this Windows user')
        self.assertEqual(len(self.tree()['skipped']), 1)
        with self.assertRaises(UploaderError): scan_tree(link)

    def test_tree_special_file_refused(self):
        if not hasattr(os, 'mkfifo'): self.skipTest('POSIX FIFO test')
        os.mkfifo(self.source / 'pipe')
        with self.assertRaisesRegex(UploaderError, 'special file'): self.tree()


class FolderCommandTests(unittest.TestCase):
    def test_dry_run_wins_over_execute(self):
        args = parser().parse_args(['mkdir', 'x', '--parent-id', 'root', '--project', 'p', '--execute', '--dry-run'])
        with tempfile.TemporaryDirectory() as temp, patch('framebridge.folders.make_directory') as mkdir:
            folder_command(None, Path(temp), args)
            self.assertFalse(mkdir.call_args.kwargs['execute'])

    def test_operations_is_available_without_credentials(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['--state-dir', temp, 'operations']), 0)
            self.assertEqual(json.loads(out.getvalue()), [])

    def test_transcript_and_etag_cli_contracts(self):
        args = parser().parse_args(['verify', 'asset', '--local-file', 'source', '--etag', '--part-count', '35'])
        self.assertTrue(args.etag)
        self.assertEqual(args.part_count, 35)
        args = parser().parse_args(['transcript', 'asset', '--transcription-id', 't', '--format', 'srt', '--output', 'out.srt'])
        self.assertEqual(args.max_bytes, 10 * 1024 * 1024)
