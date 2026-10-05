import unittest
from unittest.mock import patch

from ado_board_sync.client import Client, SyncError, credentials, literal, retry_delay, segment


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
        self.assertEqual(retry_delay('999', 0), 30)
        self.assertEqual(retry_delay('invalid', 1), 2)
