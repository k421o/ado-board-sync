#!/usr/bin/env python3
"""Exercise the real HTTP exporter on generated localhost data; never contacts ADO."""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / 'tests')]

from ado_board_sync.client import Client
from ado_board_sync.local import Filters, LocalSnapshot
from ado_board_sync.storage import current_snapshot
from ado_board_sync.sync import sync
from fake_ado import FakeADO, item, server


def synthetic(item_id):
    value = item(item_id, kind=('Epic', 'Task', 'Issue', 'Custom Approval')[item_id % 4],
                 state=('Closed', 'Awaiting review', 'Active')[item_id % 3])
    value['fields']['System.Description'] = '<p>' + ('Invented astronomy observation. ' * 34) + '</p>'
    value['fields']['Custom.Structured'] = {'sample': item_id, 'labels': ['星', 'synthetic']}
    value['relations'] = ([{'rel': 'System.LinkTypes.Hierarchy-Reverse',
                           'url': f'http://localhost/_apis/wit/workItems/{item_id - 3}'}]
                         if item_id > 3 else [])
    return value


class ScaleADO(FakeADO):
    def __init__(self, count):
        super().__init__()
        self.count, self.queries, self.batches = count, 0, 0
        self.timestamp = '2026-10-06T12:00:00Z'

    def respond(self, method, path, params, body, content_type):
        assert method == 'POST', 'Project export must not need board metadata'
        assert params['api-version'] == ['7.1']
        if path.endswith('/wiql'):
            self.queries += 1
            query = body['query']
            assert '[System.TeamProject] = @project' in query
            assert 'ORDER BY [System.Id] ASC' in query
            assert all(f not in query for f in ('System.WorkItemType', 'System.State', 'System.AreaPath'))
            assert params['timePrecision'] == ['true']
            historical = re.search(r" ASOF '([^']+)'$", query)
            if historical:
                assert historical[1] == self.timestamp
            after = int(re.search(r'\[System.Id\] > (\d+)', query)[1])
            top = int(params['$top'][0])
            assert top <= 1000
            # Nonconsecutive IDs prove that the cursor is an ID, not a row offset.
            start = after // 3 + 1
            ids = [n * 3 for n in range(start, min(start + top, self.count + 1))]
            return 200, {'asOf': self.timestamp, 'workItems': [{'id': i} for i in ids]}, {}
        if path.endswith('/workitemsbatch'):
            self.batches += 1
            assert 0 < len(body['ids']) <= 200
            assert body['asOf'] == self.timestamp
            assert body['$expand'] == 'all' and body['errorPolicy'] == 'fail'
            assert all(i % 3 == 0 and 3 <= i <= self.count * 3 for i in body['ids'])
            return 200, {'value': [synthetic(i) for i in body['ids']]}, {}
        raise AssertionError('Unexpected endpoint')


def peak_rss():
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(peak / (1024 * 1024 if sys.platform == 'darwin' else 1024), 2)
    except ImportError:
        return None


def local_worker(output, count):
    started = time.monotonic()
    local = LocalSnapshot(output)
    result = local.stats(Filters())
    assert result['matched_count'] == count
    assert sum(result['counts']['type'].values()) == count
    stats_seconds = time.monotonic() - started
    started = time.monotonic()
    filters = Filters(types=['Custom Approval'])
    expected = [i for i in range(3, count * 3 + 1, 3) if i % 4 == 3]
    seen, after = [], 0
    while True:
        page = local.listing(filters, limit=1000, after_id=after)
        seen.extend(card['id'] for card in page['cards'])
        if not page['has_more']:
            break
        after = page['next_after_id']
    assert seen == expected
    filter_seconds = time.monotonic() - started
    with tempfile.TemporaryDirectory(prefix='ado-agent-scale-') as directory:
        bundle = local.bundle(Path(directory) / 'work', filters, limit=50)
        assert bundle['card_ids'] == expected[:50]
    assert local.show(count * 3)['card']['fields'] == synthetic(count * 3)['fields']
    started = time.monotonic()
    tree = local.tree()
    tree_seconds = time.monotonic() - started
    assert tree['card_count'] == count and tree['missing_ids'] == []
    assert tree['root_ids'] == [3] and tree['max_depth'] == count - 1
    assert [node['id'] for node in tree['nodes']] == list(range(3, count * 3 + 1, 3))
    assert tree['nodes'][-1]['parent_id'] == (count - 1) * 3
    # Exercise full JSON output for a chain far beyond Python's recursion limit.
    with tempfile.TemporaryDirectory(prefix='ado-tree-scale-') as directory:
        with (Path(directory) / 'tree.json').open('w', encoding='utf-8') as stream:
            json.dump(tree, stream, indent=2, ensure_ascii=False)
    print(json.dumps({'items': count, 'phase': 'local_queries', 'traffic': 'none',
                      'stats_seconds': round(stats_seconds, 3),
                      'filter_all_pages_seconds': round(filter_seconds, 3),
                      'filtered_count': len(seen), 'bundle_count': len(bundle['card_ids']),
                      'tree_seconds': round(tree_seconds, 3), 'tree_depth': tree['max_depth'],
                      'peak_rss_mib': peak_rss()}))


def worker(url, output, count):
    started = time.monotonic()
    manifest = sync(Client('orbit-lab', 'OrbitLab', 'invented-test-token', url), output)
    elapsed = time.monotonic() - started
    assert manifest['card_ids'] == list(range(3, count * 3 + 1, 3))
    _, cards = current_snapshot(Path(output), lazy=True)
    for item_id in (3, (count // 2) * 3, count * 3):
        assert cards[item_id]['fields'] == synthetic(item_id)['fields']
        assert cards[item_id]['relations'] == synthetic(item_id)['relations']
    print(json.dumps({'items': count, 'seconds': round(elapsed, 3),
                      'peak_rss_mib': peak_rss(),
                      'added': len(manifest['changes']['added']),
                      'unchanged': manifest['changes']['unchanged']}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--counts', type=int, nargs='+', default=[10000, 50000])
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--local-worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--url', help=argparse.SUPPRESS)
    parser.add_argument('--output', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if any(count < 2 for count in args.counts):
        parser.error('counts must be at least 2')
    if args.local_worker:
        local_worker(args.output, args.counts[0])
        return
    if args.worker:
        worker(args.url, args.output, args.counts[0])
        return
    for count in args.counts:
        fake = ScaleADO(count)
        with server(fake) as (_, url), tempfile.TemporaryDirectory(prefix='ado-scale-') as directory:
            for phase in ('first', 'repeat'):
                before = fake.queries, fake.batches
                child = subprocess.run([sys.executable, __file__, '--worker', '--counts', str(count),
                                        '--url', url, '--output', directory],
                                       capture_output=True, text=True, timeout=600)
                if child.returncode:
                    raise RuntimeError(child.stderr)
                result = json.loads(child.stdout)
                queries, batches = fake.queries - before[0], fake.batches - before[1]
                assert queries == count // 1000 + 1
                assert batches == (count + 199) // 200
                assert result['added' if phase == 'first' else 'unchanged'] == count
                print(json.dumps({**result, 'phase': phase, 'id_queries': queries,
                                  'detail_batches': batches, 'traffic': 'localhost_only'}), flush=True)
            child = subprocess.run([sys.executable, __file__, '--local-worker', '--counts', str(count),
                                    '--output', directory], capture_output=True, text=True, timeout=600)
            if child.returncode:
                raise RuntimeError(child.stderr)
            print(child.stdout.strip(), flush=True)


if __name__ == '__main__':
    main()
