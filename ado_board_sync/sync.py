from .board import card_from_item, configuration, membership_query, read_items, resolve_board
from .client import SyncError
from .storage import current_snapshot, locked_folder, publish


def sync(client, output, team=None, board=None):
    with locked_folder(output) as root:
        route, board_id = resolve_board(client, team, board)
        source = {'endpoint': client.root, 'project': client.project,
                  'team_route': route, 'board_id': board_id}
        old_manifest, previous = current_snapshot(root)
        if old_manifest and old_manifest['source'] != source:
            raise SyncError('Output belongs to a different project/team/board; use another folder.')
        config = configuration(client, route, board_id)
        items, as_of = read_items(client, membership_query(client, config))
        cards = [card_from_item(item, config['board']) for item in items]
        if configuration(client, route, board_id) != config:
            raise SyncError('Board settings changed during sync; rerun to get a consistent snapshot.')
        return publish(root, source, config, cards, as_of, previous)
