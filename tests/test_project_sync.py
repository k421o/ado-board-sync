import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ado_board_sync.client import Client, SyncError
from ado_board_sync.storage import current_snapshot
from ado_board_sync.sync import sync
from fake_ado import FakeADO, item, server


class ProjectSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'

    def client(self, url):
        return Client('orbit-lab', 'OrbitLab', 'invented-test-token', url)

    def test_default_exports_all_types_and_fields_without_board_metadata(self):
        with server() as (fake, url):
            fake.fail_path = '/_apis/work'
            fake.items[1]['fields'].update({'System.WorkItemType': 'Custom Approval',
                                           'System.State': 'Awaiting comet',
                                           'Custom.Payload': {'arbitrary': [1, True, '星']},
                                           'System.AreaPath': 'OrbitLab\\AnotherTeam'})
            fake.items[2]['fields']['System.WorkItemType'] = 'Epic'
            fake.items[3]['fields']['System.WorkItemType'] = 'Task'
            manifest = sync(self.client(url), self.root)
            self.assertEqual(manifest['card_count'], 3)
            self.assertTrue(all(c[0] == 'POST' for c in fake.calls))
            query = next(c[3]['query'] for c in fake.calls if c[1].endswith('/wiql'))
            for field in ('System.WorkItemType', 'System.State', 'System.AreaPath', 'System.IterationPath'):
                self.assertNotIn(field, query)
            cards = current_snapshot(self.root)[1]
            self.assertEqual(cards[1]['fields'], fake.items[1]['fields'])
            self.assertIsNone(cards[1]['column'])
            self.assertEqual(manifest['enrichment']['comments'], 'not_fetched')
            self.assertEqual(sync(self.client(url), self.root)['changes']['unchanged'], 3)

    def test_resume_continues_after_last_committed_batch_at_original_timestamp(self):
        fake = FakeADO()
        fake.items = {i: item(i) for i in range(1, 1204)}
        fake.fail_batch = 2
        with server(fake) as (_, url):
            with self.assertRaises(SyncError):
                sync(self.client(url), self.root)
            self.assertFalse((self.root / 'current.json').exists())
            checkpoint = json.loads((self.root / '.in-progress/checkpoint.json').read_text())
            self.assertEqual(checkpoint['last_id'], 200)
            self.assertEqual(checkpoint['count'], 200)
            fake.items[201]['fields']['System.Title'] = 'Edited after interruption'
            fake.items[2000] = item(2000)
            fake.fail_batch = None
            calls_before = len(fake.calls)
            manifest = sync(self.client(url), self.root, resume=True)
            self.assertEqual(manifest['as_of'], checkpoint['as_of'])
            self.assertEqual(manifest['card_count'], 1203)
            cards = current_snapshot(self.root)[1]
            self.assertNotEqual(cards[201]['title'], fake.items[201]['fields']['System.Title'])
            batches = [c for c in fake.calls[calls_before:] if c[1].endswith('/workitemsbatch')]
            self.assertTrue(all(min(c[3]['ids']) > 200 for c in batches))
            self.assertTrue(all(c[3]['asOf'] == checkpoint['as_of'] for c in batches))
            self.assertFalse((self.root / '.in-progress').exists())

    def test_partial_disk_batch_is_discarded_on_resume_and_prior_snapshot_survives(self):
        from ado_board_sync.export import write_json

        with server() as (fake, url):
            sync(self.client(url), self.root)
            old_pointer = (self.root / 'current.json').read_bytes()
            fake.items[1]['fields']['System.Title'] = 'Updated synthetic card'

            def fail(path, data):
                if path.name == '2.json':
                    path.write_text('{incomplete')
                    raise OSError('Disk interrupted')
                write_json(path, data)

            with patch('ado_board_sync.export.write_json', side_effect=fail), self.assertRaises(OSError):
                sync(self.client(url), self.root)
            self.assertEqual((self.root / 'current.json').read_bytes(), old_pointer)
            manifest = sync(self.client(url), self.root, resume=True)
            self.assertEqual(manifest['changes']['updated'], [1])

    def test_resume_rejects_missing_committed_file_and_source_changes(self):
        fake = FakeADO()
        fake.items = {i: item(i) for i in range(1, 405)}
        fake.fail_batch = 2
        with server(fake) as (_, url):
            with self.assertRaises(SyncError):
                sync(self.client(url), self.root)
            calls_before = len(fake.calls)
            other = Client('orbit-lab', 'OtherProject', 'invented-test-token', url)
            with self.assertRaisesRegex(SyncError, 'source or configuration'):
                sync(other, self.root, resume=True)
            self.assertEqual(len(fake.calls), calls_before)
            (self.root / '.in-progress/cards/1.json').unlink()
            with self.assertRaisesRegex(SyncError, 'incomplete'):
                sync(self.client(url), self.root, resume=True)
            self.assertFalse((self.root / 'current.json').exists())
            fake.fail_batch = None
            self.assertEqual(sync(self.client(url), self.root)['card_count'], 404)

    def test_checkpoint_failure_does_not_skip_uncommitted_batch(self):
        with server() as (_, url):
            with patch('ado_board_sync.export.os.replace', side_effect=OSError('checkpoint failed')):
                with self.assertRaises(OSError):
                    sync(self.client(url), self.root)
            checkpoint = json.loads((self.root / '.in-progress/checkpoint.json').read_text())
            self.assertEqual(checkpoint['count'], 0)
            self.assertFalse((self.root / 'current.json').exists())
            self.assertEqual(sync(self.client(url), self.root, resume=True)['card_ids'], [1, 2, 3])

    def test_scope_switch_needs_new_folder_and_filters_need_board_scope(self):
        with server() as (_, url):
            sync(self.client(url), self.root, scope='board')
            pointer = (self.root / 'current.json').read_bytes()
            with self.assertRaisesRegex(SyncError, 'different project/scope'):
                sync(self.client(url), self.root)
            with self.assertRaisesRegex(SyncError, 'require --scope board'):
                sync(self.client(url), self.root, team='orbit-team')
            self.assertEqual((self.root / 'current.json').read_bytes(), pointer)
