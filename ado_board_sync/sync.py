from .board import card_from_item, configuration, item_batches, membership_query, resolve_board
from .client import SyncError
from .export import Export
from .storage import current_snapshot, locked_folder


def sync(client, output, team=None, board=None, *, scope='project', resume=False, progress=None):
    if scope not in {'project', 'board'}:
        raise SyncError('Scope must be project or board.')
    if scope == 'project' and (team or board):
        raise SyncError('--team and --board require --scope board.')
    with locked_folder(output) as root:
        if scope == 'board':
            route, board_id = resolve_board(client, team, board)
            source = {'endpoint': client.root, 'project': client.project,
                      'team_route': route, 'board_id': board_id}
            config = configuration(client, route, board_id)
            query = membership_query(client, config)
        else:
            source = {'endpoint': client.root, 'project': client.project, 'scope': 'project'}
            config = {'scope': 'project'}
            query = 'SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project'
        old_manifest, previous = current_snapshot(root, lazy=True)
        if old_manifest and old_manifest['source'] != source:
            raise SyncError('Output belongs to a different project/scope/team/board; use another folder.')
        export = Export(root, source, config, previous, resume=resume)
        for items, as_of in item_batches(client, query, after_id=export.last_id, as_of=export.as_of):
            export.add([card_from_item(item, config.get('board')) for item in items], as_of)
            if progress:
                progress(export.count)
        if scope == 'board' and configuration(client, route, board_id) != config:
            raise SyncError('Board settings changed during sync; rerun to get a consistent snapshot.')
        return export.finish()
