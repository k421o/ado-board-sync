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


def worker(url, output, count):
    started = time.monotonic()
    manifest = sync(Client('orbit-lab', 'OrbitLab', 'invented-test-token', url), output)
    elapsed = time.monotonic() - started
    assert manifest['card_ids'] == list(range(3, count * 3 + 1, 3))
    _, cards = current_snapshot(Path(output), lazy=True)
    for item_id in (3, (count // 2) * 3, count * 3):
        assert cards[item_id]['fields'] == synthetic(item_id)['fields']
        assert cards[item_id]['relations'] == synthetic(item_id)['relations']
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_mib = peak / (1024 * 1024 if sys.platform == 'darwin' else 1024)
    except ImportError:
        peak_mib = None
    print(json.dumps({'items': count, 'seconds': round(elapsed, 3),
                      'peak_rss_mib': round(peak_mib, 2) if peak_mib is not None else None,
                      'added': len(manifest['changes']['added']),
                      'unchanged': manifest['changes']['unchanged']}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--counts', type=int, nargs='+', default=[10000, 50000])
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--url', help=argparse.SUPPRESS)
    parser.add_argument('--output', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if any(count < 2 for count in args.counts):
        parser.error('counts must be at least 2')
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


if __name__ == '__main__':
    main()
