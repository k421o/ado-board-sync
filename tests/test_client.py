import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ado_board_sync.client import Client, SyncError, credentials, literal, retry_delay, segment
from fake_ado import server


class ClientTests(unittest.TestCase):
    def test_parameters_are_escaped(self):
        self.assertEqual(literal("Moon's Harbor"), "'Moon''s Harbor'")
        self.assertEqual(segment('Moon Harbor/One'), 'Moon%20Harbor%2FOne')

    def test_credentials_only_from_memory(self):
        with patch.dict('os.environ', {'ADO_PAT': 'invented-test-token'}, clear=True):
            self.assertEqual(credentials('env', 'orbit-lab'), 'invented-test-token')
        with patch.dict('os.environ', {}, clear=True), self.assertRaises(SyncError):
            credentials('env', 'orbit-lab')

    def test_test_endpoint_restricted(self):
        for url in ('http://example.invalid', 'http://user:pass@localhost',
                    'http://localhost/path', 'http://localhost?secret=yes'):
            with self.assertRaises(SyncError):
                Client('orbit-lab', 'OrbitLab', 'invented', url)

    def test_retry_bounded(self):
        self.assertEqual(retry_delay('999', 0), 999)
        self.assertEqual(retry_delay('invalid', 1), 2)
        self.assertEqual(retry_delay('inf', 1), 2)

    def test_successful_response_paces_next_request(self):
        with server() as (fake, url), patch('ado_board_sync.client.time.sleep') as sleep:
            with patch('ado_board_sync.client.time.monotonic', return_value=100):
                fake.response_headers = {'Retry-After': '65'}
                client = Client('orbit-lab', 'OrbitLab', 'invented-test-token', url)
                client.request('GET', '/_apis/projects/OrbitLab')
                sleep.assert_not_called()
                client.request('GET', '/_apis/projects/OrbitLab')
                sleep.assert_called_once_with(65)

    def test_keychain_token_stays_in_memory(self):
        with patch('ado_board_sync.client.sys.platform', 'darwin'):
            with patch('ado_board_sync.client.subprocess.run', return_value=SimpleNamespace(
                    returncode=0, stdout=b'invented-test-token\n')) as run:
                self.assertEqual(credentials('keychain', 'orbit-lab'), 'invented-test-token')
                args = run.call_args.args[0]
                self.assertNotIn('invented-test-token', args)
                self.assertIn('find-generic-password', args)
                self.assertNotIn('add-generic-password', args)

    def test_transient_retries_redirect_refusal_and_error_redaction(self):
        with server() as (fake, url), patch('ado_board_sync.client.time.sleep'):
            client = Client('orbit-lab', 'OrbitLab', 'invented-test-token', url)
            fake.fail_path = '/wiql'
            for code, expected_calls in ((503, 3), (429, 3), (401, 1), (302, 1)):
                fake.calls.clear()
                fake.fail_code = code
                with self.assertRaises(SyncError) as raised:
                    client.query('unused query')
                self.assertEqual(len(fake.calls), expected_calls)
                self.assertNotIn('invented-test-token', str(raised.exception))
