import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import requests

from framebridge.storage import UploaderError
from framebridge.transcripts import export_transcript, list_transcripts


class TranscriptExport(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'captions.srt'
        self.api, self.http = Mock(), Mock()
        self.row = {'id': 't', 'locale': 'en-US', 'lastEditedAt': 'date', 'encodeStatus': 'SUCCESS',
                    'srt': {'downloadUrl': 'https://assets.frame.io/captions?signature=private'}}
        self.api.asset.return_value = {'project': {'id': 'p'}}
        self.api.project.return_value = {'permissions': {'canDownloadTranscription': True}}
        self.api.transcripts.return_value = [self.row]
        self.response = Mock(status_code=200, headers={'Content-Length': '5'})
        self.response.raw = io.BytesIO(b'hello')
        self.response.__enter__ = Mock(return_value=self.response)
        self.response.__exit__ = Mock(return_value=False)
        self.http.get.return_value = self.response

    def export(self, **kwargs):
        return export_transcript(self.api, 'asset', 't', 'srt', self.output, http=self.http, **kwargs)

    def test_export_publishes_existing_transcript(self):
        result = self.export()
        self.assertEqual(self.output.read_bytes(), b'hello')
        self.assertEqual(result['bytes'], 5)
        self.assertFalse(result['checksum_verified'])
        self.assertFalse(self.http.get.call_args.kwargs['allow_redirects'])
        self.assertEqual(list(self.root.glob('.framebridge-transcript-*')), [])

    def test_listing_identifies_available_formats(self):
        self.assertEqual(list_transcripts(self.api, 'asset')[0]['available_formats'], ['srt'])

    def test_existing_output_is_never_overwritten(self):
        self.output.write_text('keep')
        with self.assertRaisesRegex(UploaderError, 'exists'): self.export()
        self.http.get.assert_not_called()
        self.assertEqual(self.output.read_text(), 'keep')

    def test_permission_denied_or_missing(self):
        for permissions in ({}, {'canDownloadTranscription': False}):
            self.api.project.return_value = {'permissions': permissions}
            with self.assertRaisesRegex(UploaderError, 'permission'): self.export()
        self.http.get.assert_not_called()

    def test_missing_or_unready_transcription(self):
        self.api.transcripts.return_value = []
        with self.assertRaises(UploaderError): self.export()
        self.api.transcripts.return_value = [dict(self.row, encodeStatus='PENDING')]
        with self.assertRaises(UploaderError): self.export()
        self.http.get.assert_not_called()

    def test_length_over_cap_stops_before_body_read(self):
        with self.assertRaisesRegex(UploaderError, 'cap'): self.export(max_bytes=4)
        self.assertEqual(self.response.raw.tell(), 0)
        self.assertFalse(self.output.exists())

    def test_unknown_length_is_still_bounded(self):
        self.response.headers = {}
        with self.assertRaisesRegex(UploaderError, 'cap'): self.export(max_bytes=4)
        self.assertEqual(self.response.raw.tell(), 5)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob('.framebridge-transcript-*')), [])

    def test_truncated_response_is_not_published(self):
        self.response.headers['Content-Length'] = '6'
        with self.assertRaisesRegex(UploaderError, 'truncated'): self.export()
        self.assertFalse(self.output.exists())

    def test_redirect_and_unverified_host_refused(self):
        self.response.status_code = 302
        with self.assertRaisesRegex(UploaderError, '302'): self.export()
        self.assertEqual(self.response.raw.tell(), 0)
        self.row['srt']['downloadUrl'] = 'https://unverified.example/file'
        self.http.get.reset_mock()
        with self.assertRaisesRegex(UploaderError, 'host'): self.export()
        self.http.get.assert_not_called()

    def test_changed_transcript_not_published(self):
        self.api.transcripts.side_effect = [[self.row], [dict(self.row, lastEditedAt='changed')]]
        with self.assertRaisesRegex(UploaderError, 'changed'): self.export()
        self.assertFalse(self.output.exists())

    def test_network_failure_redacts_request_details(self):
        self.http.get.side_effect = requests.ConnectionError('secret signed url')
        with self.assertRaises(UploaderError) as raised: self.export()
        self.assertNotIn('secret', str(raised.exception))
