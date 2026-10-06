"""Reconstruct a type-independent hierarchy from one local snapshot."""

import json
import re
from urllib.parse import urlsplit

from .client import SyncError

PARENT = 'System.LinkTypes.Hierarchy-Reverse'
CHILD = 'System.LinkTypes.Hierarchy-Forward'


def work_item_id(value):
    if type(value) is not int or value <= 0:
        raise SyncError('Invalid hierarchy work item ID; refusing an incomplete tree.')
    return value


def link_id(relation):
    url = relation.get('url')
    if not isinstance(url, str):
        raise SyncError('Hierarchy link has no valid work item URL.')
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise SyncError('Hierarchy link has no valid work item URL.') from None
    match = re.search(r'/_apis/wit/workitems/([1-9][0-9]*)/?$', parsed.path, re.I)
    if not match or parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise SyncError('Hierarchy link has no valid work item URL.')
    return int(match[1])


def build_tree(snapshot, *, root=None, path=None):
    if root is not None and path is not None:
        raise SyncError('Choose either --root or --path.')
    nodes, parents = {}, {}

    def edge(parent, child):
        parent, child = work_item_id(parent), work_item_id(child)
        previous = parents.get(child)
        if previous is not None and previous != parent:
            raise SyncError(f'Card {child} has conflicting parents {previous} and {parent}; '
                            'repair the local data or resync before generating a tree.')
        parents[child] = parent

    # Only compact summaries/edges stay in memory; descriptions and raw fields do not.
    for item_id in sorted(snapshot.cards):
        card = snapshot.card(item_id)
        nodes[item_id] = {key: card[key] for key in ('id', 'rev', 'title', 'type', 'state')}
        nodes[item_id]['missing'] = False
        cached = card.get('parent_ids', [])
        relations = card.get('relations', [])
        if not isinstance(cached, list) or not isinstance(relations, list):
            raise SyncError('Invalid local hierarchy relationships.')
        for parent in cached:
            edge(parent, item_id)
        field_parent = card['fields'].get('System.Parent')
        if field_parent is not None:
            if type(field_parent) is not int or field_parent < 0:
                raise SyncError('Invalid System.Parent value in local card.')
            if field_parent:
                edge(field_parent, item_id)
        for relation in relations:
            if not isinstance(relation, dict):
                raise SyncError('Invalid local hierarchy relationship.')
            kind = relation.get('rel')
            if kind == PARENT:
                edge(link_id(relation), item_id)
            elif kind == CHILD:
                edge(item_id, link_id(relation))

    missing = sorted((set(parents) | set(parents.values())) - nodes.keys())
    for item_id in missing:
        nodes[item_id] = {'id': item_id, 'rev': None, 'title': None, 'type': None,
                          'state': None, 'missing': True}

    # Iterative walks handle arbitrarily deep chains without Python recursion or
    # storing an O(depth) ancestor list on every node.
    visited = set()
    for start in sorted(nodes):
        cursor, trail, positions = start, [], {}
        while cursor is not None and cursor not in visited:
            if cursor in positions:
                cycle = trail[positions[cursor]:] + [cursor]
                detail = ' -> '.join(str(i) for i in cycle[:20])
                if len(cycle) > 20:
                    detail += ' -> ...'
                raise SyncError('Hierarchy cycle detected: ' + detail)
            positions[cursor] = len(trail)
            trail.append(cursor)
            cursor = parents.get(cursor)
        visited.update(trail)

    children = {item_id: [] for item_id in nodes}
    for child, parent in parents.items():
        children[parent].append(child)
    for values in children.values():
        values.sort()
    roots = sorted(item_id for item_id in nodes if item_id not in parents)
    selected = root if root is not None else path
    ancestor_ids = []
    if selected is not None:
        work_item_id(selected)
        if selected not in nodes:
            raise SyncError(f'Card {selected} is not present or referenced in this snapshot.')
        cursor = parents.get(selected)
        while cursor is not None:
            ancestor_ids.append(cursor)
            cursor = parents.get(cursor)
        ancestor_ids.reverse()

    if path is not None:
        ordered = [(item_id, depth) for depth, item_id in enumerate(ancestor_ids + [path])]
        roots = [ordered[0][0]]
    else:
        if root is not None:
            roots = [root]
        ordered, stack = [], [(item_id, 0) for item_id in reversed(roots)]
        while stack:
            item_id, depth = stack.pop()
            ordered.append((item_id, depth))
            stack.extend((child, depth + 1) for child in reversed(children[item_id]))

    included = {item_id for item_id, _ in ordered}
    rows = [{**nodes[item_id], 'parent_id': parents.get(item_id),
             'child_ids': [child for child in children[item_id] if child in included],
             'depth': depth} for item_id, depth in ordered]
    return {**snapshot.metadata(), 'selection': 'path' if path is not None else
            'subtree' if root is not None else 'full',
            'coverage': 'Known parent/child links in this local snapshot only.',
            'snapshot_card_count': len(snapshot.cards), 'snapshot_missing_ids': missing,
            'root_ids': roots, 'node_count': len(rows),
            'card_count': sum(not row['missing'] for row in rows),
            'missing_ids': sorted(item_id for item_id in included if nodes[item_id]['missing']),
            'ancestor_ids': ancestor_ids if root is not None else [],
            'path_ids': [item_id for item_id, _ in ordered] if path is not None else None,
            'max_depth': max((depth for _, depth in ordered), default=None), 'nodes': rows}


def text_tree(result):
    """Render preorder rows without recursive traversal or unbounded indentation."""
    def label(value):
        return json.dumps(value, ensure_ascii=False)[1:-1].replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')

    yield f"Snapshot: {result['snapshot']} (as of {result['as_of']})"
    yield f"{result['card_count']} cards, {len(result['missing_ids'])} missing references; local snapshot only."
    if result['snapshot_missing_ids']:
        yield f"Snapshot has {len(result['snapshot_missing_ids'])} referenced items without local details."
    if result['ancestor_ids']:
        yield 'Ancestors above selected root: ' + ' -> '.join(map(str, result['ancestor_ids']))
    for node in result['nodes']:
        depth = node['depth']
        prefix = '  ' * min(depth, 20)
        if depth > 20:
            prefix += f"[depth {depth}, parent #{node['parent_id']}] "
        if node['missing']:
            yield prefix + f"- #{node['id']} [missing from snapshot]"
        else:
            yield prefix + f"- #{node['id']} [{label(node['type'])}] {label(node['title'])} ({label(node['state'])})"
