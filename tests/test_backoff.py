from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import threading
import unittest
from unittest.mock import Mock, patch

from framebridge.api import Api
from framebridge.backoff import retry_delay
from framebridge.storage import UploaderError
from framebridge.upload_pool import _StorageCooldown


class BackoffTests(unittest.TestCase):
    def test_retry_after_seconds_and_date(self):
        self.assertEqual(retry_delay({'Retry-After': '7'}, 0), 7)
        when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20))
        self.assertTrue(18 <= retry_delay({'Retry-After': when}, 0) <= 20)

    def test_invalid_values_fall_back_to_jitter(self):
        for value in ('garbage', '-1', 'NaN', 'inf', None):
            self.assertTrue(4 <= retry_delay({'Retry-After': value}, 2) <= 4.5)

    def test_api_read_honors_retry_after(self):
        store, http, sleep = Mock(), Mock(), Mock()
        store.load.return_value = {'access_token': 'not-a-token', 'client_name': 'test', 'client_version': 'test'}
        api = Api(store, http=http, sleep=sleep)
        limited = Mock(status_code=429, headers={'Retry-After': '7'})
        success = Mock(status_code=200, ok=True)
        success.json.return_value = {'data': {'me': {'id': 'a'}}}
        http.post.side_effect = [limited, success]
        api._request('Me', 'query Me { me { id } }', {})
        self.assertEqual(http.post.call_count, 2)
        self.assertTrue(6 <= sleep.call_args.args[0] <= 7)
        limited.close.assert_called_once()

    def test_api_write_is_not_replayed_after_rate_limit(self):
        store, http = Mock(), Mock()
        store.load.return_value = {'access_token': 'not-a-token', 'client_name': 'test',
                                   'client_version': 'test', 'expires_at': 9999999999}
        api = Api(store, http=http)
        http.post.return_value = Mock(status_code=429, ok=False, headers={'Retry-After': '7'})
        with self.assertRaises(UploaderError): api.create_batch('a', 'name')
        self.assertEqual(http.post.call_count, 1)

    def test_shared_storage_cooldown_and_cancellation(self):
        stop = threading.Event()
        cooldown = _StorageCooldown(stop)
        cooldown.note(Mock(status_code=429, headers={'Retry-After': '60'}))
        self.assertGreater(cooldown.until, 0)
        stop.set()
        with self.assertRaisesRegex(UploaderError, 'interrupted'): cooldown.wait()
