import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ado_board_sync import SCHEMA_VERSION
from ado_board_sync.board import card_from_item
from ado_board_sync.client import SyncError
from ado_board_sync.local import Filters, LocalSnapshot
from ado_board_sync.storage import load_json
from fake_ado import item

REPO = Path(__file__).resolve().parents[1]


class LocalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'data'
        self.items = [item(3), item(9, kind='Custom Request'), item(21, state='Done')]
        self.items[0]['fields'].update({
            'System.Title': 'Plan the Moon', 'System.Description': '<p>Stars &amp; plan<b>ets</b></p>',
            'System.Tags': 'Urgent; science; Urgent', 'System.AreaPath': 'OrbitLab\\North',
            'System.IterationPath': 'OrbitLab\\Release 1\\Sprint 2',
            'System.AssignedTo': {'id': 'invented-person', 'displayName': 'Mira Fern',
                                  'uniqueName': 'mira@example.invalid'},
            'Custom.Priority': 1, 'Custom.Flag': True, 'Custom.Nullable': None,
            'Custom.Text': 'x=y'})
        self.items[0]['relations'] = [{'rel': 'System.LinkTypes.Hierarchy-Reverse',
                                       'url': 'http://localhost/_apis/wit/workItems/99'}]
        self.items[1]['fields'].update({'System.AreaPath': 'OrbitLab\\Northern',
                                       'System.AssignedTo': None, 'System.Tags': 'science',
                                       'Custom.Priority': 2, 'Custom.Flag': False})
        self.generation = self.publish(self.items)
        self.local = LocalSnapshot(self.root)

    def publish(self, items, number=1):
        generation = f'snapshots/20261006T120000Z-{number:032x}'
        folder = self.root / generation
        (folder / 'cards').mkdir(parents=True)
        for saved in items:
            (folder / 'cards' / f"{saved['id']}.json").write_text(json.dumps(card_from_item(saved)))
        manifest = {'schema_version': SCHEMA_VERSION, 'card_ids': sorted(c['id'] for c in items),
                    'card_count': len(items), 'source': {'project': 'OrbitLab', 'scope': 'project'},
                    'as_of': '2026-10-06T12:00:00Z',
                    'enrichment': {'comments': 'not_fetched', 'attachments': 'links_only'}}
        (folder / 'manifest.json').write_text(json.dumps(manifest))
        (self.root / 'current.json').write_text(json.dumps({
            'schema_version': SCHEMA_VERSION, 'snapshot': generation,
            'cards': generation + '/cards', 'manifest': generation + '/manifest.json'}))
        return generation

    def ids(self, **kwargs):
        return [card['id'] for card in self.local.listing(Filters(**kwargs))['cards']]

    def test_combines_filters_and_accepts_custom_types_and_states(self):
        self.assertEqual(self.ids(states=['doing', 'done'], types=['custom request', 'Issue'],
                                  tag_values=['URGENT', 'science'], fields=['Custom.Priority=1',
                                                                          'Custom.Priority=2']), [3, 9])
        self.assertEqual(self.ids(states=['Done'], tag_values=['science']), [])
        self.assertEqual(self.ids(ids=[9, 999], types=['Custom Request']), [9])
        self.assertEqual(self.ids(text='stars & planets'), [3])
        self.assertEqual(self.ids(text='MOON'), [3])

    def test_identity_and_unassigned_filters(self):
        for value in ('MIRA FERN', 'mira@example.invalid', 'INVENTED-PERSON'):
            self.assertIn(3, self.ids(assignees=[value]))
        self.assertEqual(self.ids(unassigned=True), [9])
        with self.assertRaises(SyncError):
            Filters(assignees=['someone'], unassigned=True)

    def test_path_filters_include_only_actual_descendants(self):
        self.assertEqual(self.ids(areas=['orbitlab\\NORTH']), [3])
        self.assertEqual(self.ids(areas=['OrbitLab']), [3, 9, 21])
        self.assertEqual(self.ids(iterations=['OrbitLab\\Release 1\\']), [3])
        self.assertEqual(self.ids(iterations=['OrbitLab\\Release']), [])

    def test_raw_fields_preserve_json_types_and_distinguish_missing_from_null(self):
        self.assertEqual(self.ids(fields=['Custom.Priority=1']), [3])
        self.assertEqual(self.ids(fields=['Custom.Priority="1"']), [])
        self.assertEqual(self.ids(fields=['Custom.Flag=1']), [])
        self.assertEqual(self.ids(fields=['Custom.Flag=true']), [3])
        self.assertEqual(self.ids(fields=['Custom.Nullable=null']), [3])
        self.assertEqual(self.ids(fields=['Missing.Field=null']), [])
        self.assertEqual(self.ids(fields=['Custom.Text=x=y']), [3])
        self.assertEqual(self.ids(fields=['Custom.Priority=1', 'Custom.Flag=false']), [])
        for expression in ('MissingEquals', '=value', 'Custom.Priority=NaN'):
            with self.subTest(expression=expression), self.assertRaises(SyncError):
                Filters(fields=[expression])

    def test_pagination_stays_pinned_when_current_snapshot_changes(self):
        first = self.local.listing(Filters(), limit=1)
        self.assertEqual(first['next_after_id'], 3)
        self.assertTrue(first['has_more'])
        self.publish([item(90)], number=2)
        old = LocalSnapshot(self.root, snapshot=first['snapshot'])
        second = old.listing(Filters(), limit=1, after_id=first['next_after_id'])
        third = old.listing(Filters(), limit=1, after_id=second['next_after_id'])
        self.assertEqual([c['id'] for page in (first, second, third) for c in page['cards']], [3, 9, 21])
        self.assertFalse(third['has_more'])
        self.assertIsNone(third['next_after_id'])
        self.assertEqual(LocalSnapshot(self.root).show(90)['card']['id'], 90)
        self.assertEqual(old.show(3)['card']['id'], 3)

    def test_listing_loads_only_a_page_and_one_lookahead_card(self):
        with patch('ado_board_sync.storage.load_json', wraps=load_json) as read:
            result = self.local.listing(Filters(), limit=1)
        self.assertEqual(read.call_count, 2)
        self.assertNotIn('fields', result['cards'][0])
        self.assertNotIn('description', result['cards'][0])
        for limit, after in ((0, 0), (1001, 0), (1, -1)):
            with self.assertRaises(SyncError):
                self.local.listing(Filters(), limit=limit, after_id=after)

    def test_stats_cover_all_matches_and_count_each_tag_once_per_card(self):
        result = self.local.stats(Filters())
        self.assertEqual(result['matched_count'], 3)
        self.assertEqual(result['unassigned_count'], 1)
        self.assertEqual(result['counts']['state'], {'Doing': 2, 'Done': 1})
        self.assertEqual(result['counts']['tag'], {'Urgent': 1, 'science': 2})
        self.assertEqual(result['counts']['assignee']['mira@example.invalid'], 1)
        self.assertEqual(self.local.stats(Filters(types=['Custom Request']))['matched_count'], 1)

    def test_show_keeps_fields_relations_and_reports_enrichment(self):
        result = self.local.show(3)
        self.assertEqual(result['card']['fields']['Custom.Priority'], 1)
        self.assertEqual(result['card']['parent_ids'], [99])
        self.assertEqual(result['card']['relations'], self.items[0]['relations'])
        self.assertEqual(result['enrichment']['comments'], 'not_fetched')
        with self.assertRaises(SyncError):
            self.local.show(99)

    def test_missing_corrupt_and_unsafe_snapshots_fail_without_creating_data(self):
        missing = self.root.parent / 'missing'
        with self.assertRaises(SyncError):
            LocalSnapshot(missing)
        self.assertFalse(missing.exists())
        with self.assertRaises(SyncError):
            LocalSnapshot(self.root, snapshot='../elsewhere')
        path = self.root / self.generation / 'cards/3.json'
        saved = json.loads(path.read_text())
        saved['id'] = 9
        path.write_text(json.dumps(saved))
        with self.assertRaises(SyncError):
            self.local.show(3)
        path.write_text('truncated')
        with self.assertRaises(SyncError):
            self.local.listing(Filters())

    def test_bundle_is_self_contained_and_preserves_agent_work_across_syncs(self):
        output = self.root.parent / 'agent-work'
        original = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = self.local.bundle(output, Filters(tag_values=['science']), limit=1)
        self.assertEqual(result['card_ids'], [3])
        self.assertTrue(result['has_more'])
        manifest = load_json(output / 'manifest.json')
        self.assertEqual(manifest['snapshot'], self.generation)
        self.assertEqual(manifest['enrichment']['comments'], 'not_fetched')
        self.assertEqual(load_json(output / 'cards/3.json'), self.local.show(3)['card'])
        self.assertTrue((output / '.gitignore').exists())
        self.assertEqual(original, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        (output / 'agent-notes.txt').write_text('Work in progress')
        self.publish([item(90)], number=2)
        with self.assertRaises(SyncError):
            self.local.bundle(output, Filters())
        self.assertEqual((output / 'agent-notes.txt').read_text(), 'Work in progress')
        self.assertEqual(load_json(output / 'cards/3.json')['id'], 3)

    def test_bundle_refuses_empty_selection_or_output_inside_sync_data(self):
        output = self.root.parent / 'agent-work'
        with self.assertRaises(SyncError):
            self.local.bundle(output, Filters(ids=[999]))
        self.assertFalse(output.exists())
        with self.assertRaises(SyncError):
            self.local.bundle(self.root / 'agent-work', Filters())

    def test_bundle_failure_publishes_nothing_and_cleans_its_staging(self):
        output = self.root.parent / 'agent-work'
        with patch('ado_board_sync.local.write_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.local.bundle(output, Filters())
        self.assertFalse(output.exists())
        self.assertFalse(list(output.parent.glob('.ado-bundle-*')))

    @unittest.skipUnless(shutil.which('git'), 'Git required to verify ignore behavior')
    def test_bundle_and_future_agent_files_are_git_ignored(self):
        repo = self.root.parent / 'repository'
        repo.mkdir()
        subprocess.run(['git', 'init', '--quiet', str(repo)], check=True)
        output = repo / 'agent-work'
        self.local.bundle(output, Filters())
        (output / 'agent-notes.txt').write_text('Invented notes')
        self.assertEqual(subprocess.check_output(
            ['git', '-C', str(repo), 'ls-files', '--others', '--exclude-standard'], text=True), '')

    def test_cli_local_commands_run_without_credentials_and_return_json(self):
        environment = {key: value for key, value in os.environ.items()
                       if key not in ('ADO_PAT', 'AZURE_DEVOPS_EXT_PAT')}
        environment['PYTHONPATH'] = str(REPO)
        for command in (['list', '--state', 'Doing', '--limit', '1'], ['show', '3'],
                        ['stats', '--tag', 'science'],
                        ['bundle', '--id', '3', '--output', str(self.root.parent / 'cli-bundle')]):
            with self.subTest(command=command[0]):
                result = subprocess.run([sys.executable, '-m', 'ado_board_sync', *command,
                                         '--data', str(self.root)], cwd=self.temp.name,
                                        env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)['snapshot'], self.generation)

    def test_cli_rejects_bad_filters_with_no_partial_stdout(self):
        result = subprocess.run([sys.executable, '-m', 'ado_board_sync', 'list',
                                 '--data', str(self.root), '--field', 'invalid'],
                                cwd=REPO, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertIn('--field requires', result.stderr)
