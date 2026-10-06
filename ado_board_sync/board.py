"""Resolve a team's Kanban board and capture its work items at one server time."""

import re
from datetime import datetime

from . import SCHEMA_VERSION
from .client import SyncError, literal, segment


def values(data):
    items = data.get('value')
    if not isinstance(items, list) or data.get('count', len(items)) != len(items):
        raise SyncError('Incomplete ADO list response.')
    return items


def resolve_board(client, team=None, board=None):
    if not team:
        project = client.request('GET', '/_apis/projects/' + segment(client.project))
        team = project.get('defaultTeam', {}).get('id')
        if not team:
            raise SyncError('Project has no default team; pass --team.')
    route = client.project_route + '/' + segment(team) + '/_apis/work'
    boards = values(client.request('GET', route + '/boards'))
    if not board:
        backlogs = values(client.request('GET', route + '/backlogs'))
        requirements = [b['name'] for b in backlogs if b.get('type') == 'requirement']
        if len(requirements) != 1:
            raise SyncError('Cannot identify the requirement board; pass --board.')
        board = requirements[0]
    matches = [b for b in boards if board in {b.get('id'), b.get('name')}]
    if len(matches) != 1:
        raise SyncError('Board not found or ambiguous. Available boards: '
                        + ', '.join(str(b.get('name', '?')) for b in boards))
    board_id = matches[0]['id']
    return route, board_id


def configuration(client, route, board_id):
    board = client.request('GET', route + '/boards/' + segment(board_id))
    areas = client.request('GET', route + '/teamsettings/teamfieldvalues')
    settings = client.request('GET', route + '/teamsettings')
    try:
        if board.get('isValid') is False:
            raise ValueError()
        columns = board['columns']
        if not columns or not all(c['name'] and c['stateMappings'] for c in columns):
            raise ValueError()
        for key in ('columnField', 'rowField', 'doneField'):
            field = board['fields'][key]['referenceName']
            if not re.fullmatch(r'[A-Za-z0-9_.-]+', field):
                raise ValueError()
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', areas['field']['referenceName']):
            raise ValueError()
        if not areas['values'] or any(not a['value'] for a in areas['values']):
            raise ValueError()
        iteration = settings['backlogIteration']
        path = iteration['path']
        if not isinstance(path, str):
            raise ValueError()
        if not path or path.startswith('\\'):
            # Team settings use project-relative paths, including '' for the root.
            # Resolve the canonical project name even when --project is a GUID.
            node_route = client.project_route + '/_apis/wit/classificationNodes/Iterations'
            root = client.request('GET', node_route)
            parts = path.lstrip('\\').split('\\') if path else []
            if any(not part or part in {'.', '..'} for part in parts):
                raise ValueError()
            node = (client.request('GET', node_route + '/' + '/'.join(map(segment, parts)))
                    if parts else root)
            if (not isinstance(root.get('name'), str) or not root['name']
                    or root.get('structureType') != 'iteration'
                    or node.get('structureType') != 'iteration'
                    or not iteration.get('id') or node.get('identifier') != iteration['id']):
                raise ValueError()
            iteration = {**iteration, 'path': '\\'.join([root['name'], *parts])}
    except (KeyError, TypeError, ValueError):
        raise SyncError('Board configuration is incomplete; refusing an unsafe export.') from None
    # Keep only membership/position configuration; unrelated settings need not abort a sync.
    return {'board': board, 'team_field_values': areas,
            'backlog_iteration': iteration}


def membership_query(client, config):
    mappings = set()
    for column in config['board']['columns']:
        mappings.update(column['stateMappings'].items())
    types = ' OR '.join('([System.WorkItemType] = ' + literal(kind)
                        + ' AND [System.State] = ' + literal(state) + ')'
                        for kind, state in sorted(mappings))
    team = config['team_field_values']
    areas = ' OR '.join('[' + team['field']['referenceName'] + '] '
                        + ('UNDER ' if area.get('includeChildren') else '= ')
                        + literal(area['value']) for area in team['values'])
    return ('SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project'
            + ' AND (' + types + ') AND (' + areas + ')'
            + ' AND [System.IterationPath] UNDER ' + literal(config['backlog_iteration']['path']))


def read_items(client, query, page_size=1000):
    last_id, as_of, result = 0, None, []
    while True:
        paged = query + f' AND [System.Id] > {last_id} ORDER BY [System.Id] ASC'
        if as_of:
            paged += ' ASOF ' + literal(as_of)
        data = client.query(paged, page_size)
        ids = data.get('workItems')
        try:
            server_time = data['asOf']
            datetime.fromisoformat(server_time.replace('Z', '+00:00'))
            if not re.fullmatch(r'[0-9TZ:.+\-]+', server_time):
                raise ValueError()
            if as_of is not None and server_time != as_of:
                raise ValueError()
            if not isinstance(ids, list) or len(ids) > page_size:
                raise ValueError()
            ids = [item['id'] for item in ids]
            if any(type(i) is not int or i <= last_id for i in ids):
                raise ValueError()
            if ids != sorted(set(ids)):
                raise ValueError()
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SyncError('Invalid or inconsistent WIQL page; refusing partial results.') from None
        as_of = server_time
        for start in range(0, len(ids), 200):
            result.extend(client.hydrate(ids[start:start + 200], as_of))
        if len(ids) < page_size:
            return result, as_of
        last_id = ids[-1]


def card_from_item(item, board):
    fields = item.get('fields')
    if (not isinstance(fields, dict) or type(item.get('rev')) is not int
            or any(key not in fields for key in
                   ('System.Title', 'System.WorkItemType', 'System.State'))):
        raise SyncError('Incomplete card fields; refusing partial results.')
    refs = {k: v['referenceName'] for k, v in board['fields'].items()}
    column = fields.get(refs['columnField'])
    if not column:
        # A newly created card may not yet have a board-specific position field.
        matches = [c['name'] for c in board['columns']
                   if c['stateMappings'].get(fields['System.WorkItemType']) == fields['System.State']]
        column = matches[0] if matches else None
    assigned = fields.get('System.AssignedTo')
    relations = item.get('relations', [])
    if not isinstance(relations, list):
        raise SyncError('Invalid work item relations.')
    parents = []
    for relation in relations:
        if relation.get('rel') == 'System.LinkTypes.Hierarchy-Reverse':
            match = re.search(r'/workItems/(\d+)$', relation.get('url', ''), re.I)
            if match:
                parents.append(int(match[1]))
    return {
        'schema_version': SCHEMA_VERSION, 'id': item['id'], 'rev': item['rev'],
        'url': item.get('url'), 'title': fields['System.Title'],
        'type': fields['System.WorkItemType'], 'state': fields['System.State'],
        'column': column, 'column_done': fields.get(refs['doneField'], False),
        'lane': fields.get(refs['rowField'], ''), 'assignee': assigned,
        'area_path': fields.get('System.AreaPath'),
        'iteration_path': fields.get('System.IterationPath'),
        'description': fields.get('System.Description', ''),
        'parent_ids': parents, 'fields': fields, 'relations': relations,
    }
