"""Invented sandbox cards, with server-side markers for interrupted-run recovery."""

import html

from .board import configuration, read_items, resolve_board, values
from .client import SyncError, segment

MARKER = 'ado-board-sync-seed-v1'
PROCESSES = {
    'Basic': ('Issue', ('To Do', 'Doing', 'Done'), ('To Do', 'Doing', 'Done')),
    'Agile': ('User Story', ('New', 'Active', 'Closed'), ('New', 'Active', 'Closed')),
    'Scrum': ('Product Backlog Item', ('New', 'Committed', 'Done'), ('To Do', 'In Progress', 'Done')),
    'CMMI': ('Requirement', ('Proposed', 'Active', 'Closed'), ('Proposed', 'Active', 'Closed')),
}
PERSONAS = ('Mira Fern', 'Oren Finch', 'Lena Vale', 'Tavi Moss')
IDEAS = (
    ('Add a constellation search', 'Visitors can find constellations by season and hemisphere.'),
    ('Save a favorite observing site', 'Keep an invented observing location for the next visit.'),
    ('Show a clear-sky checklist', 'Display a short checklist before planning an evening outside.'),
    ('Improve the telescope catalog', 'Group fictional instruments by aperture and portability.'),
    ('Handle an empty observation log', 'Offer a helpful prompt when no observations exist.'),
    ('Build a moon-phase calendar', 'Show a readable monthly calendar for a fictional sky guide.'),
    ('Make chart labels accessible', 'Provide keyboard focus and descriptive chart labels.'),
    ('Add a night-mode preview', 'Let readers preview a dim display before starting a session.'),
    ('Fix duplicate bookmark labels', 'Keep labels unique when a reader saves the same chart twice.'),
    ('Export a sample observing list', 'Produce a small invented list suitable for printing.'),
)


def plan(process='Basic', count=24):
    if process not in PROCESSES or not 12 <= count <= 500:
        raise SyncError('Seed requires a supported process and 12–500 cards.')
    kind, states, task_states = PROCESSES[process]
    cards = []
    latest_story = None
    for index in range(count):
        key = f'orbit-{index + 1:03d}'
        stage = (index + index // 3) % 3
        parent = None
        if index < 2:
            card_type = 'Epic'
            title = ('Explore the evening sky', 'Keep an observation journal')[index]
            description = 'A fictional astronomy guide for an entirely invented sandbox.'
            # Epic workflows differ from the board's requirement workflow.
            state = {'Basic': 'Doing', 'Agile': 'Active',
                     'Scrum': 'In Progress', 'CMMI': 'Active'}[process]
            column = None
        elif index % 3 == 1:
            card_type, state = 'Task', task_states[(index // 3) % 3]
            title = 'Validate ' + cards[-1]['title'].split('] ', 1)[1].lower()
            description = 'Check the fictional feature with a small synthetic example.'
            parent, column = latest_story, None
        else:
            card_type, state = kind, states[stage]
            idea_title, description = IDEAS[(index - 2) % len(IDEAS)]
            title = idea_title + (f' (sample {index + 1})' if index >= 12 else '')
            parent = 'orbit-001' if index % 2 == 0 else 'orbit-002'
            latest_story = key
            column = ('To Do', 'Doing', 'Done')[stage]
        cards.append({'key': key, 'title': f'[{key}] {title}', 'type': card_type,
                      'state': state, 'column': column, 'stage': stage,
                      'assignee': {'display_name': PERSONAS[index % len(PERSONAS)],
                                   'mode': 'fictional persona in description'},
                      'parent_key': parent, 'description': description,
                      'tags': ['synthetic', MARKER, key, f'orbit-seed-{process}-{count}']})
    return {'schema_version': 1, 'process': process, 'count': count, 'cards': cards,
            'assignee_policy': 'Invented personas are stored in descriptions. '
                               'ADO AssignedTo remains empty; no users are created.',
            'column_policy': 'Live seed uses the selected board’s incoming, in-progress '
                             'and outgoing columns and their state mappings.'}


def description(card):
    return ('<p>Synthetic sandbox card (' + MARKER + ').</p><p>'
            + html.escape(card['description']) + '</p><p>Synthetic assignee: '
            + html.escape(card['assignee']['display_name']) + ' (fictional persona).</p>')


def seed(client, process='Basic', count=24, team=None, board=None):
    planned = plan(process, count)
    route, board_id = resolve_board(client, team, board)
    config = configuration(client, route, board_id)
    primary = PROCESSES[process][0]
    columns = config['board']['columns']
    stage_columns = []
    for column_type in ('incoming', 'inProgress', 'outgoing'):
        matches = [c for c in columns if c.get('columnType') == column_type
                   and primary in c['stateMappings']]
        if not matches:
            raise SyncError('Selected board does not match --process or lacks a required column.')
        stage_columns.append(matches[0])
    types = values(client.request('GET', client.project_route + '/_apis/wit/workitemtypes'))
    available = {t['name']: {s['name'] for s in t.get('states', [])} for t in types}
    for card in planned['cards']:
        if card['type'] == primary:
            column = stage_columns[card['stage']]
            card['column'], card['state'] = column['name'], column['stateMappings'][primary]
        if card['state'] not in available.get(card['type'], set()):
            raise SyncError('Project work item types/states do not match --process; no cards created.')
    query = 'SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project'
    existing, _ = read_items(client, query)
    keyed = {c['key']: c for c in planned['cards']}
    saved = {}
    for item in existing:
        fields = item.get('fields', {})
        matches = [c for c in keyed.values() if fields.get('System.Title') == c['title']]
        if len(matches) != 1:
            raise SyncError('Seed requires an empty project or only this exact synthetic seed.')
        card = matches[0]
        tags = {tag.strip() for tag in fields.get('System.Tags', '').split(';')}
        if (card['key'] in saved or tags != set(card['tags'])
                or fields.get('System.WorkItemType') != card['type']
                or fields.get('System.Description') != description(card)
                or fields.get('System.AssignedTo')):
            raise SyncError('Seed found unrelated, edited or duplicate cards; refusing writes.')
        saved[card['key']] = item
    for key, item in saved.items():
        card = keyed[key]
        fields = item['fields']
        parent = card['parent_key']
        expected_parent = saved.get(parent, {}).get('id') if parent else None
        actual_parents = [r.get('url', '').rsplit('/', 1)[-1]
                          for r in item.get('relations', [])
                          if r.get('rel') == 'System.LinkTypes.Hierarchy-Reverse']
        if (fields.get('System.State') != card['state']
                or fields.get('System.AreaPath') != config['team_field_values']['defaultValue']
                or fields.get('System.IterationPath') != config['backlog_iteration']['path']
                or actual_parents != ([str(expected_parent)] if expected_parent else [])):
            raise SyncError('Synthetic seed cards were edited or have inconsistent parents; refusing writes.')
    created = 0
    for card in planned['cards']:
        key = card['key']
        parent_key = card['parent_key']
        parent_id = saved[parent_key]['id'] if parent_key else None
        if key not in saved:
            fields = {'System.Title': card['title'], 'System.State': card['state'],
                      'System.Description': description(card),
                      'System.AreaPath': config['team_field_values']['defaultValue'],
                      'System.IterationPath': config['backlog_iteration']['path'],
                      'System.Tags': '; '.join(card['tags'])}
            body = [{'op': 'add', 'path': '/fields/' + field, 'value': value}
                    for field, value in fields.items()]
            if parent_id:
                body.append({'op': 'add', 'path': '/relations/-', 'value': {
                    'rel': 'System.LinkTypes.Hierarchy-Reverse',
                    'url': client.root + '/_apis/wit/workItems/' + str(parent_id),
                }})
            # Creation is never automatically retried: a timeout may hide a successful write.
            item = client.request('POST', client.project_route + '/_apis/wit/workitems/$'
                                  + segment(card['type']), body, patch=True, retry=False)
            if type(item.get('id')) is not int:
                raise SyncError('Seed create response was incomplete; rerun the same command.')
            saved[key] = item
            created += 1
        if card['column'] is not None:
            column_field = config['board']['fields']['columnField']['referenceName']
            if saved[key].get('fields', {}).get(column_field) == card['column']:
                continue
            item_id = saved[key]['id']
            # Also repairs a run interrupted immediately after creation. All cards were
            # validated as our own seed before any writes; this does not edit other cards.
            client.request('PATCH', client.project_route + '/_apis/wit/workitems/' + str(item_id),
                           [{'op': 'add', 'path': '/fields/' + column_field,
                             'value': card['column']}], patch=True, retry=False)
    return {'created': created, 'total': len(saved), 'board': config['board']['name'],
            'cards': [{'key': c['key'], 'id': saved[c['key']]['id']} for c in planned['cards']],
            'assignee_policy': planned['assignee_policy']}
