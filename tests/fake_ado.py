"""A localhost-only, contract-checking ADO server with a mutable synthetic board."""

import copy
import json
import re
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

COLUMN = 'WEF_ORBIT_Kanban.Column'
ROW = 'WEF_ORBIT_Kanban.Lane'
DONE = 'WEF_ORBIT_Kanban.Done'


def item(item_id, title='Observe a fictional nebula', column='Doing', state='Doing', kind='Issue'):
    return {'id': item_id, 'rev': 1, 'url': f'http://localhost/_apis/wit/workItems/{item_id}',
            'fields': {'System.Id': item_id, 'System.Title': title, 'System.WorkItemType': kind,
                       'System.State': state, 'System.AreaPath': 'OrbitLab',
                       'System.IterationPath': 'OrbitLab', 'System.Description': '<p>Invented.</p>',
                       'System.AssignedTo': {'displayName': 'Mira Fern', 'id': 'fictional-person'},
                       'System.BoardColumn': 'Another team column', COLUMN: column,
                       ROW: 'Expedite', DONE: False}, 'relations': []}


class FakeADO:
    def __init__(self):
        self.items = {i: item(i) for i in (1, 2, 3)}
        self.calls = []
        self.snapshots = {}
        self.fail_path = None
        self.fail_code = 400
        self.partial_batch = False
        self.batch_calls = 0
        self.fail_batch = None
        self.change_config_on_batch = False
        self.after_first_query = None
        self.ambiguous_create = False
        self.invalid_json = False
        self.truncate_body = False
        self.query_mode = None
        self.next_id = 100
        self.board = {'id': 'orbit-board', 'name': 'Issues', 'isValid': True, 'revision': 1,
                      'fields': {'columnField': {'referenceName': COLUMN},
                                 'rowField': {'referenceName': ROW},
                                 'doneField': {'referenceName': DONE}},
                      'columns': [
                          {'name': name, 'columnType': stage, 'stateMappings': {'Issue': state}}
                          for name, stage, state in [('To Do', 'incoming', 'To Do'),
                                                     ('Doing', 'inProgress', 'Doing'),
                                                     ('Review', 'inProgress', 'Doing'),
                                                     ('Done', 'outgoing', 'Done')]],
                      'rows': [{'id': 'normal', 'name': ''}, {'id': 'rush', 'name': 'Expedite'}]}
        self.areas = {'field': {'referenceName': 'System.AreaPath'}, 'defaultValue': 'OrbitLab',
                      'values': [{'value': 'OrbitLab', 'includeChildren': True}]}
        self.settings = {'backlogIteration': {'id': 'orbit-iteration', 'path': 'OrbitLab'}}
        self.types = [{'name': kind, 'states': [{'name': state} for state in ('To Do', 'Doing', 'Done')]}
                      for kind in ('Epic', 'Issue', 'Task')]

    def respond(self, method, path, params, body, content_type):
        self.calls.append((method, path, params, copy.deepcopy(body)))
        assert params['api-version'] == ['7.1'], params
        if self.fail_path and self.fail_path in path:
            return self.fail_code, {'message': 'invented-test-token'}, {}
        if self.invalid_json:
            return 200, b'not json invented-test-token', {}
        if method == 'GET':
            if '/_apis/projects/' in path:
                return 200, {'defaultTeam': {'id': 'orbit-team'}}, {}
            if path.endswith('/boards'):
                return 200, {'count': 1, 'value': [{'id': 'orbit-board', 'name': 'Issues'}]}, {}
            if path.endswith('/backlogs'):
                return 200, {'count': 1, 'value': [{'name': 'Issues', 'type': 'requirement'}]}, {}
            if path.endswith('/boards/orbit-board'):
                return 200, self.board, {}
            if path.endswith('/teamsettings/teamfieldvalues'):
                return 200, self.areas, {}
            if path.endswith('/teamsettings'):
                return 200, self.settings, {}
            if path.endswith('/workitemtypes'):
                return 200, {'value': self.types}, {}
        if method == 'POST' and path.endswith('/wiql'):
            query = body['query']
            assert '[System.TeamProject]' in query and 'ORDER BY [System.Id] ASC' in query
            assert params.get('timePrecision') == ['true']
            asof_match = re.search(r" ASOF '([^']+)'$", query)
            if asof_match:
                as_of = asof_match[1]
                snapshot = self.snapshots[as_of]
            else:
                as_of = f'2026-10-05T12:00:00.{len(self.snapshots) + 1:07d}Z'
                snapshot = copy.deepcopy(self.items)
                self.snapshots[as_of] = snapshot
            ids = sorted(snapshot)
            if '[System.WorkItemType]' in query:
                # This fake enforces the default team's type/state, area and iteration scope.
                assert '[System.AreaPath] UNDER' in query
                assert '[System.IterationPath] UNDER' in query
                ids = [i for i in ids if snapshot[i]['fields']['System.WorkItemType'] == 'Issue'
                       and snapshot[i]['fields']['System.State'] in {'To Do', 'Doing', 'Done'}
                       and snapshot[i]['fields']['System.AreaPath'].startswith('OrbitLab')
                       and snapshot[i]['fields']['System.IterationPath'].startswith('OrbitLab')]
            last = int(re.search(r'\[System.Id\] > (\d+)', query)[1])
            ids = [i for i in ids if i > last][:int(params['$top'][0])]
            if self.query_mode == 'duplicate' and ids:
                ids.append(ids[0])
            if self.query_mode == 'reverse':
                ids.reverse()
            if self.after_first_query and not asof_match:
                hook, self.after_first_query = self.after_first_query, None
                hook(self)
            result = {'asOf': as_of, 'workItems': [{'id': i} for i in ids]}
            if self.query_mode == 'missing_time':
                del result['asOf']
            return 200, result, {}
        if method == 'POST' and path.endswith('/workitemsbatch'):
            self.batch_calls += 1
            if self.fail_batch == self.batch_calls:
                return 500, {'message': 'partial transfer'}, {}
            assert len(body['ids']) <= 200
            assert body['$expand'] == 'all' and body['errorPolicy'] == 'fail'
            snapshot = self.snapshots[body['asOf']]
            results = [copy.deepcopy(snapshot[i]) for i in body['ids']]
            if self.partial_batch:
                results = results[:-1]
            if self.change_config_on_batch:
                self.board['revision'] += 1
            return 200, {'count': len(results), 'value': results}, {}
        if method == 'POST' and '/workitems/$' in path:
            assert content_type == 'application/json-patch+json'
            kind = path.rsplit('$', 1)[-1]
            assert kind in {t['name'] for t in self.types}
            fields = {op['path'].removeprefix('/fields/'): op['value']
                      for op in body if op['path'].startswith('/fields/')}
            states = next(t['states'] for t in self.types if t['name'] == kind)
            assert fields['System.State'] in {s['name'] for s in states}
            assert 'System.AssignedTo' not in fields  # invented personas are not ADO identities
            relations = [op['value'] for op in body if op['path'] == '/relations/-']
            for relation in relations:
                assert int(relation['url'].rsplit('/', 1)[-1]) in self.items
            created = {'id': self.next_id, 'rev': 1, 'fields': {
                **fields, 'System.WorkItemType': kind}, 'relations': relations}
            self.items[self.next_id] = copy.deepcopy(created)
            self.next_id += 1
            if self.ambiguous_create:
                self.ambiguous_create = False
                return 503, {'message': 'creation committed but response lost'}, {}
            return 200, created, {}
        if method == 'PATCH' and '/workitems/' in path:
            assert content_type == 'application/json-patch+json'
            target = self.items[int(path.rsplit('/', 1)[-1])]
            for op in body:
                target['fields'][op['path'].removeprefix('/fields/')] = op['value']
            target['rev'] += 1
            return 200, target, {}
        return 404, {'message': 'unknown fake route'}, {}


@contextmanager
def server(fake=None):
    fake = fake or FakeADO()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def do_PATCH(self):
            self.handle_request()

        def handle_request(self):
            parsed = urlsplit(self.path)
            data = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            body = json.loads(data) if data else None
            try:
                status, result, headers = fake.respond(
                    self.command, unquote(parsed.path), parse_qs(parsed.query), body,
                    self.headers.get('Content-Type'))
            except Exception as error:
                fake.calls.append(('CONTRACT_ERROR', type(error).__name__))
                status, result, headers = 500, {'message': 'fake contract failed'}, {}
            encoded = result if isinstance(result, bytes) else json.dumps(result).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded) + (10 if fake.truncate_body else 0)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(encoded)

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake, f'http://127.0.0.1:{httpd.server_port}'
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()
