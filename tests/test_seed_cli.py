import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ado_board_sync.client import Client, SyncError
from ado_board_sync.seed import PLACED_MARKER, PROCESSES, plan, seed
from ado_board_sync.storage import current_snapshot
from fake_ado import COLUMN, FakeADO, server

REPO = Path(__file__).resolve().parents[1]


class SeedTests(unittest.TestCase):
    def client(self, url):
        return Client('orbit-lab', 'OrbitLab', 'invented-test-token', url)

    def test_offline_plan_has_types_states_columns_personas_and_parents(self):
        for process in PROCESSES:
            with self.subTest(process=process):
                planned = plan(process)
                self.assertEqual(len(planned['cards']), 24)
                seen = set()
                for card in planned['cards']:
                    if card['parent_key']:
                        self.assertIn(card['parent_key'], seen)
                    self.assertIn(card['assignee']['display_name'], ('Mira Fern', 'Oren Finch',
                                                                    'Lena Vale', 'Tavi Moss'))
                    seen.add(card['key'])
                self.assertEqual(len({c['type'] for c in planned['cards']}), 3)
                self.assertEqual({c['column'] for c in planned['cards'] if c['column']},
                                 {'To Do', 'Doing', 'Done'})

    def test_seed_creates_then_reconciles_and_syncs(self):
        fake = FakeADO()
        fake.items = {}
        fake.settings['backlogIteration']['path'] = ''
        with server(fake) as (_, url):
            result = seed(self.client(url))
            self.assertEqual(result['created'], 24)
            self.assertEqual(len(fake.items), 24)
            planned = plan()['cards']
            keyed = {c['key']: c['id'] for c in result['cards']}
            for card in planned:
                created = fake.items[keyed[card['key']]]
                fields = created['fields']
                self.assertIn(card['assignee']['display_name'], fields['System.Description'])
                self.assertNotIn('System.AssignedTo', fields)
                if card['parent_key']:
                    self.assertTrue(created['relations'][0]['url'].endswith(
                        '/' + str(keyed[card['parent_key']])))
                if card['column']:
                    self.assertEqual(fields[COLUMN], card['column'])
            again = seed(self.client(url))
            self.assertEqual(again['created'], 0)
            self.assertEqual(again['cards'], result['cards'])
            with tempfile.TemporaryDirectory() as directory:
                from ado_board_sync.sync import sync
                manifest = sync(self.client(url), directory)
                self.assertEqual(manifest['card_count'],
                                 sum(c['type'] == 'Issue' for c in planned))
                cards = current_snapshot(Path(directory))[1]
                self.assertTrue(all(c['parent_ids'] for c in cards.values()))

    def test_ambiguous_creation_is_not_retried_and_resume_does_not_duplicate(self):
        fake = FakeADO()
        fake.items = {}
        fake.ambiguous_create = True
        with server(fake) as (_, url):
            with self.assertRaises(SyncError):
                seed(self.client(url))
            self.assertEqual(len(fake.items), 1)
            creates = [c for c in fake.calls if c[0] == 'POST' and '/workitems/$' in c[1]]
            self.assertEqual(len(creates), 1)
            result = seed(self.client(url))
            self.assertEqual(result['created'], 23)
            self.assertEqual(len(fake.items), 24)

    def test_each_supported_process_seeds_against_its_board_mappings(self):
        for process, (primary, states, task_states) in PROCESSES.items():
            with self.subTest(process=process):
                fake = FakeADO()
                fake.items = {}
                for col, state in zip(fake.board['columns'][:2] + fake.board['columns'][-1:], states):
                    col['stateMappings'] = {primary: state}
                fake.board['columns'][2]['stateMappings'] = {primary: states[1]}
                epic_state = plan(process)['cards'][0]['state']
                fake.types = [{'name': kind, 'states': [{'name': s} for s in state_names]}
                              for kind, state_names in [('Epic', (epic_state,)),
                                                        (primary, states), ('Task', task_states)]]
                with server(fake) as (_, url):
                    self.assertEqual(seed(self.client(url), process=process)['created'], 24)
                    calls_before = len(fake.calls)
                    self.assertEqual(seed(self.client(url), process=process)['created'], 0)
                    self.assertFalse(any(c[0] == 'PATCH' or '/workitems/$' in c[1]
                                         for c in fake.calls[calls_before:]))

    def test_patch_failure_after_create_can_resume(self):
        fake = FakeADO()
        fake.items = {}
        fake.fail_path = '/workitems/102'
        with server(fake) as (_, url):
            with self.assertRaises(SyncError):
                seed(self.client(url))
            self.assertEqual(len(fake.items), 3)
            fake.fail_path = None
            self.assertEqual(seed(self.client(url))['created'], 21)
            self.assertEqual(len(fake.items), 24)

    def test_manual_move_between_columns_with_same_state_refused_before_writes(self):
        fake = FakeADO()
        fake.items = {}
        with server(fake) as (_, url):
            seed(self.client(url))
            moved = next(i for i in fake.items.values() if i['fields'].get(COLUMN) == 'Doing')
            self.assertIn(PLACED_MARKER, moved['fields']['System.Tags'].split('; '))
            moved['fields'][COLUMN] = 'Review'
            moved['rev'] += 1
            calls_before = len(fake.calls)
            with self.assertRaisesRegex(SyncError, 'placement was edited'):
                seed(self.client(url))
            self.assertEqual(moved['fields'][COLUMN], 'Review')
            self.assertFalse(any(c[0] == 'PATCH' or '/workitems/$' in c[1]
                                 for c in fake.calls[calls_before:]))

    def test_lost_placement_response_resumes_without_repatching_completed_card(self):
        fake = FakeADO()
        fake.items = {}
        fake.ambiguous_patch = True
        with server(fake) as (_, url):
            with self.assertRaises(SyncError):
                seed(self.client(url))
            self.assertEqual(len(fake.items), 3)
            self.assertIn(PLACED_MARKER, fake.items[102]['fields']['System.Tags'].split('; '))
            calls_before = len(fake.calls)
            self.assertEqual(seed(self.client(url))['created'], 21)
            self.assertFalse(any(c[0] == 'PATCH' and c[1].endswith('/workitems/102')
                                 for c in fake.calls[calls_before:]))

    def test_pending_card_with_initial_column_still_records_completed_placement(self):
        fake = FakeADO()
        fake.items = {}
        fake.fail_path = '/workitems/102'
        with server(fake) as (_, url):
            with self.assertRaises(SyncError):
                seed(self.client(url))
            pending = fake.items[102]
            pending['fields'][COLUMN] = 'Done'
            fake.fail_path = None
            self.assertEqual(seed(self.client(url))['created'], 21)
            self.assertIn(PLACED_MARKER, fake.items[102]['fields']['System.Tags'].split('; '))

    def test_pending_manual_move_refused_before_resume_writes(self):
        fake = FakeADO()
        fake.items = {}
        fake.fail_path = '/workitems/103'
        with server(fake) as (_, url):
            with self.assertRaises(SyncError):
                seed(self.client(url))
            self.assertEqual(len(fake.items), 4)
            pending = fake.items[103]
            self.assertNotIn(PLACED_MARKER, pending['fields']['System.Tags'])
            self.assertEqual(pending['fields']['System.State'], 'Doing')
            pending['fields'][COLUMN] = 'Review'
            pending['rev'] += 1
            before = copy.deepcopy(fake.items)
            fake.fail_path = None
            calls_before = len(fake.calls)
            with self.assertRaisesRegex(SyncError, 'placement was edited'):
                seed(self.client(url))
            self.assertEqual(fake.items, before)
            self.assertFalse(any(c[0] == 'PATCH' or '/workitems/$' in c[1]
                                 for c in fake.calls[calls_before:]))

    def test_pending_concurrent_edit_survives_revision_conflict(self):
        fake = FakeADO()
        fake.items = {}
        fake.fail_path = '/workitems/103'
        with server(fake) as (_, url):
            with self.assertRaises(SyncError):
                seed(self.client(url))
            read_revision = fake.items[103]['rev']
            changed = {}

            def manual_edit(data):
                pending = data.items[103]
                pending['fields'][COLUMN] = 'Review'
                pending['fields']['System.Tags'] += '; manual-note'
                pending['rev'] += 1
                changed.update(copy.deepcopy(data.items))

            fake.fail_path = None
            fake.after_first_query = manual_edit
            calls_before = len(fake.calls)
            with self.assertRaisesRegex(SyncError, 'HTTP 412'):
                seed(self.client(url))
            self.assertEqual(fake.items, changed)
            patches = [c for c in fake.calls[calls_before:] if c[0] == 'PATCH']
            self.assertEqual(len(patches), 1)
            self.assertEqual(patches[0][3][0], {'op': 'test', 'path': '/rev',
                                              'value': read_revision})
            self.assertFalse(any('/workitems/$' in c[1] for c in fake.calls[calls_before:]))

    def test_fresh_create_concurrent_edit_survives_revision_conflict(self):
        fake = FakeADO()
        fake.items = {}
        changed = {}

        def manual_edit(data, item_id):
            if item_id == 102:
                created = data.items[item_id]
                created['fields'][COLUMN] = 'Review'
                created['fields']['System.Tags'] += '; manual-note'
                created['rev'] += 1
                changed.update(copy.deepcopy(data.items))

        fake.after_create = manual_edit
        with server(fake) as (_, url):
            with self.assertRaisesRegex(SyncError, 'HTTP 412'):
                seed(self.client(url))
            self.assertEqual(fake.items, changed)
            self.assertEqual(len(fake.items), 3)
            patches = [c for c in fake.calls if c[0] == 'PATCH']
            self.assertEqual(len(patches), 1)
            self.assertEqual(patches[0][3][0], {'op': 'test', 'path': '/rev', 'value': 1})

    def test_failed_patch_test_is_atomic_even_after_field_operations(self):
        with server() as (fake, url):
            before = copy.deepcopy(fake.items[1])
            with self.assertRaisesRegex(SyncError, 'HTTP 412'):
                self.client(url).request('PATCH', '/OrbitLab/_apis/wit/workitems/1',
                                        [{'op': 'add', 'path': '/fields/' + COLUMN, 'value': 'Review'},
                                         {'op': 'add', 'path': '/fields/System.Tags', 'value': 'overwrite'},
                                         {'op': 'test', 'path': '/rev', 'value': before['rev'] + 1}],
                                        patch=True, retry=False)
            self.assertEqual(fake.items[1], before)

    def test_seed_rejects_existing_work_and_process_mismatch_before_writes(self):
        with server() as (fake, url):
            with self.assertRaises(SyncError):
                seed(self.client(url))
            self.assertFalse(any('/workitems/$' in c[1] or c[0] == 'PATCH' for c in fake.calls))
            fake.items = {}
            with self.assertRaises(SyncError):
                seed(self.client(url), process='Agile')
            self.assertEqual(fake.items, {})

    def test_edited_seed_and_different_size_refused(self):
        fake = FakeADO()
        fake.items = {}
        with server(fake) as (_, url):
            seed(self.client(url))
            with self.assertRaises(SyncError):
                seed(self.client(url), count=12)
            fake.items[100]['fields']['System.Description'] = 'Edited fictional card'
            calls_before = len(fake.calls)
            with self.assertRaises(SyncError):
                seed(self.client(url))
            self.assertFalse(any(c[0] == 'PATCH' or '/workitems/$' in c[1]
                                 for c in fake.calls[calls_before:]))

    def test_resume_with_larger_count_refused_before_writes(self):
        fake = FakeADO()
        fake.items = {}
        with server(fake) as (_, url):
            seed(self.client(url), count=12)
            calls_before = len(fake.calls)
            with self.assertRaises(SyncError):
                seed(self.client(url), count=24)
            self.assertEqual(len(fake.items), 12)
            self.assertFalse(any(c[0] == 'PATCH' or '/workitems/$' in c[1]
                                 for c in fake.calls[calls_before:]))


class CLITests(unittest.TestCase):
    def run_cli(self, *args, cwd=None, authenticated=True):
        env = dict(os.environ)
        env.pop('ADO_PAT', None)
        env.pop('AZURE_DEVOPS_EXT_PAT', None)
        if authenticated:
            env['ADO_PAT'] = 'invented-test-token'
        if sys.platform == 'win32':
            env['PYTHONPATH'] = str(REPO) + (os.pathsep + env['PYTHONPATH']
                                            if env.get('PYTHONPATH') else '')
            entry = [sys.executable, '-m', 'ado_board_sync']
        else:
            entry = [str(REPO / 'ado-board-sync')]
        return subprocess.run([*entry, *args], cwd=cwd or REPO,
                              env=env, capture_output=True, text=True, timeout=20)

    def test_windows_entry_point_runs_sync_from_another_directory(self):
        with server() as (_, url), tempfile.TemporaryDirectory() as directory:
            with patch.object(sys, 'platform', 'win32'):
                result = self.run_cli('sync', '--organization', 'orbit-lab', '--project', 'OrbitLab',
                                      '--base-url', url, '--output', 'local-cards', cwd=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['card_count'], 3)
            self.assertTrue((Path(directory) / 'local-cards' / 'current.json').exists())

    def test_help_and_dry_run_without_auth_or_network(self):
        result = self.run_cli('--help', authenticated=False)
        self.assertEqual(result.returncode, 0)
        result = self.run_cli('seed', '--organization', 'orbit-lab', '--project', 'OrbitLab',
                              '--dry-run', authenticated=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['count'], 24)
        from ado_board_sync.__main__ import main
        with patch('ado_board_sync.__main__.credentials', side_effect=AssertionError('auth attempted')):
            with patch('ado_board_sync.__main__.Client', side_effect=AssertionError('network attempted')):
                with patch('builtins.print'):
                    self.assertEqual(main(['seed', '--organization', 'orbit-lab', '--project',
                                           'OrbitLab', '--dry-run']), 0)

    def test_cli_sync_relative_output_from_another_directory_and_secret_errors(self):
        with server() as (fake, url), tempfile.TemporaryDirectory() as directory:
            args = ('sync', '--organization', 'orbit-lab', '--project', 'OrbitLab', '--base-url', url,
                    '--output', 'local-cards')
            result = self.run_cli(*args, cwd=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['card_count'], 3)
            self.assertTrue((Path(directory) / 'local-cards' / 'current.json').exists())
            pointer = (Path(directory) / 'local-cards' / 'current.json').read_bytes()
            fake.fail_path, fake.fail_code = '/wiql', 401
            failed = self.run_cli(*args, cwd=directory)
            self.assertEqual(failed.returncode, 1)
            self.assertIn('token scopes', failed.stderr)
            self.assertNotIn('invented-test-token', failed.stdout + failed.stderr)
            self.assertEqual((Path(directory) / 'local-cards' / 'current.json').read_bytes(), pointer)
            for file in (Path(directory) / 'local-cards').rglob('*.json'):
                self.assertNotIn('invented-test-token', file.read_text())

    def test_missing_auth_fails_without_creating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli('sync', '--organization', 'orbit-lab', '--project', 'OrbitLab',
                                  '--output', 'local-cards', cwd=directory, authenticated=False)
            self.assertEqual(result.returncode, 1)
            self.assertFalse((Path(directory) / 'local-cards').exists())
