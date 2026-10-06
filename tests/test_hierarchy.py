import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ado_board_sync.board import card_from_item
from ado_board_sync.client import Client, SyncError
from ado_board_sync.hierarchy import CHILD, PARENT, build_tree, text_tree
from ado_board_sync.sync import sync
from fake_ado import FakeADO, item, server

REPO = Path(__file__).resolve().parents[1]


def relation(kind, target):
    return {'rel': kind, 'url': f'http://localhost/_apis/wit/workItems/{target}'}


def card(item_id, kind='Task', parent=None, children=()):
    value = item(item_id, title=f'Invented {kind} {item_id}', kind=kind)
    value['relations'] = ([relation(PARENT, parent)] if parent is not None else [])
    value['relations'].extend(relation(CHILD, child) for child in children)
    return card_from_item(value)


class MemorySnapshot:
    def __init__(self, cards):
        self.cards = {saved['id']: saved for saved in cards}
        self.reads = 0

    def metadata(self):
        return {'schema_version': 1, 'snapshot': 'snapshots/invented',
                'as_of': '2026-10-06T12:00:00Z', 'source': {'scope': 'project'}}

    def card(self, item_id):
        self.reads += 1
        return self.cards[item_id]


class HierarchyTests(unittest.TestCase):
    def snapshot(self):
        return MemorySnapshot([card(1, 'Epic', children=[2]), card(2, 'Feature', 1),
                               card(3, 'User Story', 2), card(4, 'Task', 3),
                               card(5, 'Custom Approval', 2), card(6, 'Issue'),
                               card(7, 'Task', 1)])

    def test_full_forest_covers_all_types_and_skipped_levels(self):
        snapshot = self.snapshot()
        result = build_tree(snapshot)
        self.assertEqual(result['root_ids'], [1, 6])
        self.assertEqual([node['id'] for node in result['nodes']], [1, 2, 3, 4, 5, 7, 6])
        self.assertEqual([node['depth'] for node in result['nodes']], [0, 1, 2, 3, 2, 1, 0])
        self.assertEqual(result['max_depth'], 3)
        self.assertEqual(result['card_count'], 7)
        self.assertEqual(result['missing_ids'], [])
        self.assertEqual(snapshot.reads, 7)
        self.assertEqual(result['nodes'][1]['child_ids'], [3, 5])
        self.assertTrue(all('fields' not in node and 'description' not in node for node in result['nodes']))

    def test_reciprocal_and_cached_edges_are_deduplicated(self):
        parent, child = card(1, children=[2, 2]), card(2, parent=1)
        child['fields']['System.Parent'] = 1
        child['parent_ids'].append(1)
        result = build_tree(MemorySnapshot([parent, child]))
        self.assertEqual(result['node_count'], 2)
        self.assertEqual(result['nodes'][0]['child_ids'], [2])

    def test_forward_links_and_parent_field_work_without_reverse_links(self):
        parent, child, grandchild = card(1, children=[2]), card(2), card(3)
        parent['fields']['System.Parent'] = 0
        grandchild['fields']['System.Parent'] = 2
        result = build_tree(MemorySnapshot([parent, child, grandchild]))
        self.assertEqual(result['root_ids'], [1])
        self.assertEqual([n['parent_id'] for n in result['nodes']], [None, 1, 2])

    def test_unrelated_links_do_not_create_hierarchy_edges(self):
        saved = card(1)
        saved['relations'] = [relation('System.LinkTypes.Related', 99),
                               relation('System.LinkTypes.Dependency-Forward', 88),
                               {'rel': 'AttachedFile', 'url': 'not-a-work-item'}]
        result = build_tree(MemorySnapshot([saved]))
        self.assertEqual(result['node_count'], 1)
        self.assertEqual(result['missing_ids'], [])

    def test_missing_parents_and_children_are_explicit_placeholders(self):
        result = build_tree(MemorySnapshot([card(2, parent=1, children=[3])]))
        self.assertEqual(result['root_ids'], [1])
        self.assertEqual(result['card_count'], 1)
        self.assertEqual(result['missing_ids'], [1, 3])
        self.assertEqual([n['id'] for n in result['nodes']], [1, 2, 3])
        self.assertTrue(result['nodes'][0]['missing'])
        self.assertIsNone(result['nodes'][0]['title'])
        self.assertIn('#1 [missing from snapshot]', '\n'.join(text_tree(result)))
        self.assertEqual(build_tree(MemorySnapshot([card(2, parent=1)]), path=2)['path_ids'], [1, 2])

    def test_subtree_and_ancestor_path_preserve_context(self):
        result = build_tree(self.snapshot(), root=2)
        self.assertEqual(result['selection'], 'subtree')
        self.assertEqual(result['root_ids'], [2])
        self.assertEqual(result['ancestor_ids'], [1])
        self.assertEqual([n['id'] for n in result['nodes']], [2, 3, 4, 5])
        self.assertEqual(result['nodes'][0]['depth'], 0)
        result = build_tree(self.snapshot(), path=4)
        self.assertEqual(result['selection'], 'path')
        self.assertEqual(result['path_ids'], [1, 2, 3, 4])
        self.assertEqual([n['child_ids'] for n in result['nodes']], [[2], [3], [4], []])

    def test_empty_snapshot_has_an_empty_forest(self):
        result = build_tree(MemorySnapshot([]))
        self.assertEqual(result['root_ids'], [])
        self.assertEqual(result['nodes'], [])
        self.assertIsNone(result['max_depth'])

    def test_cycles_and_self_links_fail_instead_of_omitting_nodes(self):
        for cards in ([card(1, parent=1)],
                      [card(1, parent=3), card(2, parent=1), card(3, parent=2)],
                      [card(1), card(2, parent=3), card(3, parent=2)]):
            with self.subTest(ids=[c['id'] for c in cards]), self.assertRaisesRegex(SyncError, 'cycle'):
                build_tree(MemorySnapshot(cards))

    def test_conflicting_parents_are_never_arbitrarily_chosen(self):
        cards = [card(1, children=[3]), card(2), card(3, parent=2)]
        with self.assertRaisesRegex(SyncError, 'conflicting parents'):
            build_tree(MemorySnapshot(cards))
        child = card(3, parent=1)
        child['fields']['System.Parent'] = 2
        with self.assertRaisesRegex(SyncError, 'conflicting parents'):
            build_tree(MemorySnapshot([child]))

    def test_invalid_relationships_and_selections_fail(self):
        for change in ({'parent_ids': [True]}, {'parent_ids': [-1]}, {'relations': None},
                       {'relations': [None]}, {'relations': [{'rel': PARENT, 'url': 'broken'}]},
                       {'fields': {'System.Parent': '1'}}):
            saved = card(1)
            saved.update(change)
            with self.subTest(change=change), self.assertRaises(SyncError):
                build_tree(MemorySnapshot([saved]))
        for selection in ({'root': 999}, {'path': 0}, {'root': 1, 'path': 4}):
            with self.subTest(selection=selection), self.assertRaises(SyncError):
                build_tree(self.snapshot(), **selection)

    def test_valid_relation_urls_allow_query_and_trailing_slash(self):
        saved = card(2)
        saved['relations'] = [{'rel': PARENT,
                               'url': 'https://dev.azure.com/orbit/_apis/wit/workitems/1/?api-version=7.1'}]
        self.assertEqual(build_tree(MemorySnapshot([saved]))['root_ids'], [1])

    def test_deep_chains_do_not_recurse_or_duplicate_ancestor_arrays(self):
        count = 2500
        snapshot = MemorySnapshot(card(i, parent=i + 1 if i < count else None)
                                  for i in range(1, count + 1))
        result = build_tree(snapshot)
        self.assertEqual(result['node_count'], count)
        self.assertEqual(result['max_depth'], count - 1)
        self.assertEqual(result['root_ids'], [count])
        json.dumps(result)  # A deeply nested object would hit Python's recursion limit.
        lines = list(text_tree(result))
        self.assertIn('[depth 2499, parent #2]', lines[-1])
        self.assertLess(max(map(len, lines)), 250)
        self.assertEqual(build_tree(snapshot, path=1)['path_ids'], list(range(count, 0, -1)))

    def test_text_is_readable_and_escapes_terminal_controls(self):
        snapshot = self.snapshot()
        snapshot.cards[1]['title'] = 'Invented\nforged line\x1b[31m'
        lines = list(text_tree(build_tree(snapshot)))
        self.assertIn('  - #2 [Feature] Invented Feature 2 (Doing)', lines)
        self.assertIn('      - #4 [Task] Invented Task 4 (Doing)', lines)
        self.assertFalse(any('\x1b' in line or '\n' in line for line in lines))
        self.assertIn('\\nforged line\\u001b', lines[2])

    def test_cli_reads_real_export_offline_and_pins_old_snapshot(self):
        fake = FakeADO()
        fake.items = {1: item(1, kind='Epic'), 2: item(2, kind='Feature'), 3: item(3, kind='User Story')}
        fake.items[2]['relations'] = [relation(PARENT, 1)]
        fake.items[3]['relations'] = [relation(PARENT, 2)]
        with tempfile.TemporaryDirectory() as directory:
            with server(fake) as (_, url):
                sync(Client('orbit-lab', 'OrbitLab', 'invented-test-token', url), directory)
                previous = json.loads((Path(directory) / 'current.json').read_text())['snapshot']
                fake.items = {4: item(4)}
                sync(Client('orbit-lab', 'OrbitLab', 'invented-test-token', url), directory)
            environment = {k: v for k, v in os.environ.items() if k not in ('ADO_PAT', 'AZURE_DEVOPS_EXT_PAT')}
            command = [sys.executable, '-m', 'ado_board_sync', 'tree', '--data', directory,
                       '--snapshot', previous]
            before = {str(p): p.read_bytes() for p in Path(directory).rglob('*') if p.is_file()}
            for flags in ([], ['--format', 'text'], ['--path', '3'], ['--root', '2']):
                result = subprocess.run(command + flags, cwd=REPO, env=environment,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                if '--format' in flags:
                    self.assertIn('    - #3 [User Story]', result.stdout)
                else:
                    self.assertEqual(json.loads(result.stdout)['snapshot'], previous)
            self.assertEqual(before, {str(p): p.read_bytes() for p in Path(directory).rglob('*') if p.is_file()})
            result = subprocess.run(command + ['--root', '999'], cwd=REPO, env=environment,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, '')
