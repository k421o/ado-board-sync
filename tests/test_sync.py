import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ado_board_sync.board import membership_query, read_items
from ado_board_sync.client import Client, SyncError
from ado_board_sync.storage import current_snapshot, locked_folder
from ado_board_sync.sync import sync
from fake_ado import COLUMN, FakeADO, item, server


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'board'

    def client(self, url):
        return Client('orbit-lab', 'OrbitLab', 'invented-test-token', url)

    def test_first_sync_and_second_sync_edit_move_add_remove(self):
        with server() as (fake, url):
            manifest = sync(self.client(url), self.root)
            self.assertEqual(manifest['card_ids'], [1, 2, 3])
            first_pointer = json.loads((self.root / 'current.json').read_text())
            first_folder = self.root / first_pointer['snapshot']
            self.assertEqual(len(list((first_folder / 'cards').glob('*.json'))), 3)
            fake.items[1]['fields']['System.Title'] = 'Edited fictional observation'
            fake.items[1]['rev'] += 1
            fake.items[2]['fields'][COLUMN] = 'Review'  # state stays Doing
            fake.items[2]['rev'] += 1
            fake.items[4] = item(4, column='To Do', state='To Do')
            del fake.items[3]
            second = sync(self.client(url), self.root)
            self.assertEqual(second['changes'], {'added': [4], 'updated': [1, 2],
                                                  'moved': [2], 'removed': [3], 'unchanged': 0})
            _, cards = current_snapshot(self.root)
            self.assertEqual(cards[1]['title'], 'Edited fictional observation')
            self.assertEqual(cards[2]['column'], 'Review')
            self.assertEqual(cards[2]['state'], 'Doing')
            self.assertNotIn(3, cards)
            self.assertTrue((first_folder / 'cards' / '3.json').exists())
            third = sync(self.client(url), self.root)
            self.assertEqual(third['changes']['unchanged'], 3)
            self.assertFalse(third['changes']['updated'])
            self.assertTrue(all(call[0] != 'PATCH' for call in fake.calls))

    def test_failed_partial_or_changed_config_retains_previous_snapshot(self):
        with server() as (fake, url):
            sync(self.client(url), self.root)
            pointer = (self.root / 'current.json').read_bytes()
            old_manifest, old_cards = current_snapshot(self.root)
            fake.items[1]['fields']['System.Title'] = 'Do not publish this partial change'
            for failure in ('http', 'partial', 'settings', 'bad_json', 'bad_page', 'truncated'):
                with self.subTest(failure=failure):
                    fake.fail_path = '/workitemsbatch' if failure == 'http' else None
                    fake.partial_batch = failure == 'partial'
                    fake.change_config_on_batch = failure == 'settings'
                    fake.invalid_json = failure == 'bad_json'
                    fake.query_mode = 'missing_time' if failure == 'bad_page' else None
                    fake.truncate_body = failure == 'truncated'
                    with patch('ado_board_sync.client.time.sleep'), self.assertRaises(SyncError):
                        sync(self.client(url), self.root)
                    self.assertEqual((self.root / 'current.json').read_bytes(), pointer)
                    self.assertEqual(current_snapshot(self.root), (old_manifest, old_cards))
                    self.assertFalse((self.root / '.sync-lock').exists())

    def test_disk_write_failure_retains_previous_snapshot(self):
        with server() as (_, url):
            sync(self.client(url), self.root)
            pointer = (self.root / 'current.json').read_bytes()
            from ado_board_sync.storage import write_json

            def fail_card(path, data):
                if path.name == '2.json':
                    raise OSError('invented disk full')
                write_json(path, data)

            with patch('ado_board_sync.storage.write_json', side_effect=fail_card):
                with self.assertRaises(OSError):
                    sync(self.client(url), self.root)
            self.assertEqual((self.root / 'current.json').read_bytes(), pointer)
            self.assertEqual(len(current_snapshot(self.root)[1]), 3)
            self.assertFalse(list((self.root / 'snapshots').glob('.tmp-*')))

    def test_publish_pointer_failure_retains_previous_snapshot(self):
        with server() as (_, url):
            sync(self.client(url), self.root)
            pointer = (self.root / 'current.json').read_bytes()
            with patch('ado_board_sync.storage.os.replace', side_effect=OSError('invented failure')):
                with self.assertRaises(OSError):
                    sync(self.client(url), self.root)
            self.assertEqual((self.root / 'current.json').read_bytes(), pointer)
            self.assertEqual(len(current_snapshot(self.root)[1]), 3)

    def test_paging_batches_and_historical_snapshot(self):
        fake = FakeADO()
        fake.items = {i: item(i) for i in range(1, 1004)}

        def edit_live(data):
            data.items[1003]['fields']['System.Title'] = 'Changed after snapshot'
            del data.items[201]
            data.items[1004] = item(1004)

        fake.after_first_query = edit_live
        with server(fake) as (_, url):
            manifest = sync(self.client(url), self.root)
            self.assertEqual(manifest['card_count'], 1003)
            cards = current_snapshot(self.root)[1]
            self.assertEqual(cards[1003]['title'], 'Observe a fictional nebula')
            self.assertIn(201, cards)
            self.assertNotIn(1004, cards)
            queries = [c for c in fake.calls if c[1].endswith('/wiql')]
            self.assertEqual(len(queries), 2)
            self.assertIn('ASOF', queries[1][3]['query'])
            batches = [c for c in fake.calls if c[1].endswith('/workitemsbatch')]
            self.assertEqual(len({c[3]['asOf'] for c in batches}), 1)
            self.assertEqual(fake.batch_calls, 6)

    def test_later_batch_failure_preserves_previous_and_first_failure_has_no_pointer(self):
        fake = FakeADO()
        with server(fake) as (_, url):
            fake.partial_batch = True
            with self.assertRaises(SyncError):
                sync(self.client(url), self.root)
            self.assertFalse((self.root / 'current.json').exists())
            fake.partial_batch = False
            sync(self.client(url), self.root)
            old = current_snapshot(self.root)
            fake.items = {i: item(i) for i in range(1, 405)}
            fake.fail_batch = fake.batch_calls + 2
            with self.assertRaises(SyncError):
                sync(self.client(url), self.root)
            self.assertEqual(current_snapshot(self.root), old)

    def test_empty_board_and_scope_removals(self):
        with server() as (fake, url):
            sync(self.client(url), self.root)
            fake.items[1]['fields']['System.State'] = 'Removed'
            fake.items[2]['fields']['System.AreaPath'] = 'OtherLab'
            fake.items[3]['fields']['System.IterationPath'] = 'OtherLab'
            manifest = sync(self.client(url), self.root)
            self.assertEqual(manifest['changes']['removed'], [1, 2, 3])
            self.assertEqual(current_snapshot(self.root)[1], {})

    def test_destination_guards_and_concurrent_lock(self):
        self.root.mkdir()
        (self.root / 'keep.txt').write_text('unrelated')
        with self.assertRaises(SyncError), locked_folder(self.root):
            pass
        (self.root / 'keep.txt').unlink()
        with locked_folder(self.root):
            with self.assertRaises(SyncError), locked_folder(self.root):
                pass
        with server() as (_, url):
            sync(self.client(url), self.root)
            with self.assertRaises(SyncError):
                sync(self.client(url), self.root, team='other-team')

    def test_invalid_query_results_abort(self):
        with server() as (fake, url):
            for mode in ('duplicate', 'reverse', 'missing_time'):
                fake.query_mode = mode
                with self.subTest(mode=mode), self.assertRaises(SyncError):
                    read_items(self.client(url), 'SELECT [System.Id] FROM WorkItems '
                               "WHERE [System.TeamProject] = 'OrbitLab'")

    def test_query_escapes_project_and_areas(self):
        client = Client('orbit-lab', "Moon's Harbor", 'invented')
        fake = FakeADO()
        fake.areas['values'][0]['value'] = "Moon's Harbor"
        query = membership_query(client, {'board': fake.board,
                                         'team_field_values': fake.areas,
                                         'backlog_iteration': fake.settings['backlogIteration']})
        self.assertIn("'Moon''s Harbor'", query)
