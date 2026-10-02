"""Fail-closed verification and source-integrity regression tests."""
import argparse
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from framebridge.api import Api
from framebridge.commands import verify_asset
from framebridge.storage import Journal, UploaderError
from framebridge.uploader import Slice, upload


class Verification(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.bin'
        self.source.write_bytes(b'abcdefgh')
        self.journal = Journal(self.root / 'uploads.sqlite3')
        self.addCleanup(self.journal.close)
        self.api = Mock()
        self.api.create_batch.return_value = 'batch'
        self.api.create_asset.return_value = {'id':'asset','totalPartCount':1}
        self.api.status.side_effect = ['CREATED','UPLOADED']
        self.evidence = {'id':'asset','status':'UPLOADED','filesize':8,
                         'project':{'id':'project'},'parent':{'id':'folder'}}
        self.api.upload_evidence.return_value = self.evidence
        self.http = Mock()
        def consume(*args, **kwargs):
            while kwargs['data'].read(3): pass
            return Mock(status_code=200)
        self.http.put.side_effect = consume

    def run_upload(self):
        return upload(self.api,self.journal,self.source,'project','account','folder',http=self.http,sleep=lambda _:None)

    def record(self):
        return json.loads(self.journal.db.execute('SELECT record FROM uploads').fetchone()[0])

    def test_hashes_recorded_and_remote_rechecked(self):
        self.run_upload()
        record = self.record()
        self.assertEqual(record['phase'],'complete')
        self.assertEqual(len(record['source_sha256']),64)
        self.assertEqual(record['verification']['level'],'metadata')
        self.assertFalse(record['verification']['checksum_verified'])
        self.run_upload()
        self.assertEqual(self.api.upload_evidence.call_count,2)
        self.assertEqual(self.http.put.call_count,1)

    def test_wrong_remote_size_prevents_completion(self):
        self.evidence['filesize'] = 7
        with self.assertRaisesRegex(UploaderError,'size'): self.run_upload()
        self.api.complete_batch.assert_not_called()
        self.assertNotEqual(self.record()['phase'],'complete')

    def test_missing_remote_size_prevents_completion(self):
        self.evidence['filesize'] = None
        with self.assertRaisesRegex(UploaderError,'size'): self.run_upload()
        self.api.complete_batch.assert_not_called()

    def test_changed_project_prevents_completion(self):
        self.evidence['project']['id'] = 'other'
        with self.assertRaisesRegex(UploaderError,'project'): self.run_upload()
        self.api.complete_batch.assert_not_called()

    def test_failed_verification_resumes_without_resending(self):
        self.evidence['filesize'] = None
        with self.assertRaises(UploaderError): self.run_upload()
        self.evidence['filesize'] = 8
        self.api.status.side_effect = None
        self.api.status.return_value = 'UPLOADED'
        self.run_upload()
        self.assertEqual(self.http.put.call_count,1)
        self.assertEqual(self.api.create_asset.call_count,1)
        self.assertEqual(self.record()['phase'],'complete')

    def test_changed_parent_prevents_completion(self):
        self.evidence['parent']['id'] = 'other'
        with self.assertRaisesRegex(UploaderError,'folder'): self.run_upload()
        self.api.complete_batch.assert_not_called()

    def test_previously_complete_but_deleted_refused(self):
        self.run_upload()
        self.evidence['status'] = 'DELETED'
        with self.assertRaisesRegex(UploaderError,'not complete'): self.run_upload()
        self.assertEqual(self.api.create_asset.call_count,1)
        self.assertEqual(self.http.put.call_count,1)

    def test_previously_complete_but_inaccessible_refused(self):
        self.run_upload()
        self.api.upload_evidence.side_effect = UploaderError('Unavailable')
        with self.assertRaises(UploaderError): self.run_upload()
        self.assertEqual(self.api.create_asset.call_count,1)

    def mutate_preserving_stat(self):
        info = self.source.stat()
        self.source.write_bytes(b'xxxxxxxx')
        os.utime(self.source,ns=(info.st_atime_ns,info.st_mtime_ns))

    def test_same_size_and_timestamp_mutation_detected_on_resume(self):
        self.run_upload()
        self.mutate_preserving_stat()
        with self.assertRaisesRegex(UploaderError,'content changed'): self.run_upload()

    def test_modified_stream_detected(self):
        def corrupt(*args, **kwargs):
            self.mutate_preserving_stat()
            while kwargs['data'].read(3): pass
            return Mock(status_code=200)
        self.http.put.side_effect = corrupt
        with self.assertRaisesRegex(UploaderError,'part bytes'): self.run_upload()
        self.assertEqual(self.record()['parts'],[])
        self.api.complete_batch.assert_not_called()

    def test_200_without_consuming_body_is_not_success(self):
        self.http.put.side_effect = None
        self.http.put.return_value = Mock(status_code=200)
        with self.assertRaisesRegex(UploaderError,'part bytes'): self.run_upload()
        self.assertEqual(self.record()['parts'],[])

    def test_initial_failed_asset_does_not_send(self):
        self.api.status.side_effect = None
        self.api.status.return_value = 'FAILED'
        with self.assertRaisesRegex(UploaderError,'failed asset'): self.run_upload()
        self.http.put.assert_not_called()

    def test_retry_source_mismatch_when_server_already_ready(self):
        self.api.complete_batch.side_effect = UploaderError('Lost response')
        with self.assertRaises(UploaderError): self.run_upload()
        self.api.status.side_effect = None
        self.api.status.return_value = 'UPLOADED'
        self.mutate_preserving_stat()
        with self.assertRaisesRegex(UploaderError,'content changed'): self.run_upload()

    def test_legacy_completed_record_can_recheck_metadata(self):
        self.run_upload()
        key = self.journal.db.execute('SELECT key FROM uploads').fetchone()[0]
        record = self.record()
        record.pop('source_sha256')
        record.pop('part_sha256')
        self.journal.put(key,record)
        self.run_upload()
        self.assertFalse(self.record()['verification']['checksum_verified'])
        self.assertEqual(self.api.upload_evidence.call_count,2)

    def test_missing_asset_evidence_refused(self):
        store = Mock(); store.load.return_value = {}
        api = Api(store)
        api.call = Mock(return_value={'asset':None})
        with self.assertRaises(UploaderError): api.upload_evidence('asset')

    def args(self, **kwargs):
        values = dict(asset_id='asset',local_file=self.source,download_original_to=None,max_download_bytes=None)
        return argparse.Namespace(**dict(values,**kwargs))

    def test_verify_metadata_does_not_download(self):
        with patch('framebridge.commands.download') as transfer:
            result = verify_asset(self.api,self.args())
            self.assertTrue(result['size_matches_local'])
            self.assertFalse(result['checksum_verified'])
            transfer.assert_not_called()

    def test_original_requires_cap(self):
        with patch('framebridge.commands.download') as transfer:
            with self.assertRaisesRegex(UploaderError,'cap'):
                verify_asset(self.api,self.args(download_original_to=self.root/'original'))
            transfer.assert_not_called()

    def test_original_over_cap_not_downloaded(self):
        self.api.rendition.return_value = {'filesizeInBytes':8}
        with patch('framebridge.commands.download') as transfer:
            with self.assertRaisesRegex(UploaderError,'cap exceeded'):
                verify_asset(self.api,self.args(download_original_to=self.root/'original',max_download_bytes=7))
            transfer.assert_not_called()

    def test_original_checksum_passed_to_downloader(self):
        self.api.rendition.return_value = dict(asset_id='asset',media_id='media',key='original',filesizeInBytes=8)
        with patch('framebridge.commands.download',return_value={'bytes':8}) as transfer:
            result = verify_asset(self.api,self.args(download_original_to=self.root/'original',max_download_bytes=8))
            self.assertTrue(result['checksum_verified'])
            self.assertEqual(transfer.call_args.kwargs['expected_sha256'],result['sha256'])

    def test_original_checksum_failure_propagated(self):
        self.api.rendition.return_value = dict(asset_id='asset',media_id='media',key='original',filesizeInBytes=8)
        with patch('framebridge.commands.download',side_effect=UploaderError('Checksum mismatch')):
            with self.assertRaisesRegex(UploaderError,'Checksum mismatch'):
                verify_asset(self.api,self.args(download_original_to=self.root/'original',max_download_bytes=8))

    def test_original_size_change_after_cap_check_refused(self):
        original = dict(asset_id='asset',media_id='media',key='original',filesizeInBytes=8)
        self.api.rendition.side_effect = [original,dict(original,filesizeInBytes=100)]
        def start(adapter, asset_id, selection, *args, **kwargs):
            adapter.rendition(asset_id, selection)
            self.fail('Changed original must never start transferring')
        with patch('framebridge.commands.download',side_effect=start):
            with self.assertRaisesRegex(UploaderError,'Original changed'):
                verify_asset(self.api,self.args(download_original_to=self.root/'original',max_download_bytes=8))

    def test_original_output_cannot_be_local_source(self):
        with patch('framebridge.commands.download') as transfer:
            with self.assertRaisesRegex(UploaderError,'separate output'):
                verify_asset(self.api,self.args(download_original_to=self.source,max_download_bytes=8))
            transfer.assert_not_called()

    def test_legacy_partial_record_without_hash_refused(self):
        self.evidence['filesize'] = None
        with self.assertRaises(UploaderError): self.run_upload()
        key = self.journal.db.execute('SELECT key FROM uploads').fetchone()[0]
        record = self.record()
        record.pop('source_sha256')
        self.journal.put(key,record)
        self.api.status.side_effect = None
        self.api.status.return_value = 'UPLOADING'
        with self.assertRaisesRegex(UploaderError,'Legacy partial'): self.run_upload()
        self.assertEqual(self.http.put.call_count,1)
