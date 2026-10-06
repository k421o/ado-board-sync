"""Immutable generations and one atomic pointer, including on Windows."""

import json
import os
import re
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION
from .client import SyncError


def load_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise SyncError('Local snapshot is unreadable; restore it before syncing.') from None


def write_json(path, data):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def sync_directory(path):
    if os.name == 'posix':
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@contextmanager
def locked_folder(output):
    root = Path(output).absolute()
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise SyncError('Output folder must not be a symlink.')
    marker = root / '.ado-board-sync.json'
    if not marker.exists() and any(root.iterdir()):
        raise SyncError('Output folder is not an empty or owned snapshot folder.')
    lock = root / '.sync-lock'
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SyncError('Another sync holds .sync-lock. After a crash, confirm no sync '
                        'is running before removing that file.') from None
    os.close(descriptor)
    try:
        if not marker.exists():
            write_json(marker, {'schema_version': SCHEMA_VERSION})
        elif load_json(marker) != {'schema_version': SCHEMA_VERSION}:
            raise SyncError('Unsupported snapshot folder format.')
        snapshots = root / 'snapshots'
        if snapshots.is_symlink():
            raise SyncError('Snapshots directory must not be a symlink.')
        snapshots.mkdir(exist_ok=True)
        yield root
    finally:
        lock.unlink()


def current_snapshot(root):
    pointer = root / 'current.json'
    if not pointer.exists():
        return None, {}
    data = load_json(pointer)
    snapshot = data.get('snapshot', '')
    if (data.get('schema_version') != SCHEMA_VERSION or not isinstance(snapshot, str)
            or not re.fullmatch(r'snapshots/[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}', snapshot)):
        raise SyncError('Invalid current snapshot pointer.')
    path = root / snapshot
    if path.parent != root / 'snapshots' or path.is_symlink():
        raise SyncError('Invalid current snapshot path.')
    manifest = load_json(path / 'manifest.json')
    if (manifest.get('schema_version') != SCHEMA_VERSION
            or not isinstance(manifest.get('card_ids'), list)
            or manifest.get('card_count') != len(manifest['card_ids'])):
        raise SyncError('Invalid local snapshot manifest.')
    cards = {}
    for item_id in manifest['card_ids']:
        if type(item_id) is not int or item_id <= 0:
            raise SyncError('Invalid local card ID.')
        cards[item_id] = load_json(path / 'cards' / f'{item_id}.json')
    return manifest, cards


def publish(root, source, config, cards, as_of, previous):
    generation = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex
    staging = root / 'snapshots' / ('.tmp-' + generation)
    destination = root / 'snapshots' / generation
    pointer = root / ('.current-' + generation)
    new = {card['id']: card for card in cards}
    changed = sorted(i for i in new.keys() & previous.keys() if new[i] != previous[i])
    changes = {'added': sorted(new.keys() - previous.keys()), 'updated': changed,
               'moved': [i for i in changed if any(new[i][key] != previous[i][key]
                                                   for key in ('column', 'column_done', 'lane'))],
               'removed': sorted(previous.keys() - new.keys()),
               'unchanged': len(new.keys() & previous.keys()) - len(changed)}
    manifest = {'schema_version': SCHEMA_VERSION, 'source': source, 'as_of': as_of,
                'synced_at': datetime.now(timezone.utc).isoformat(),
                'card_ids': sorted(new), 'card_count': len(new), 'changes': changes,
                'configuration': config}
    try:
        (staging / 'cards').mkdir(parents=True)
        for item_id, card in new.items():
            write_json(staging / 'cards' / f'{item_id}.json', card)
        write_json(staging / 'manifest.json', manifest)
        sync_directory(staging / 'cards')
        sync_directory(staging)
        staging.rename(destination)
        sync_directory(root / 'snapshots')
        write_json(pointer, {'schema_version': SCHEMA_VERSION,
                             'snapshot': 'snapshots/' + generation,
                             'cards': 'snapshots/' + generation + '/cards',
                             'manifest': 'snapshots/' + generation + '/manifest.json'})
        # The only commit point. Every referenced file is already complete and durable.
        os.replace(pointer, root / 'current.json')
        sync_directory(root)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        pointer.unlink(missing_ok=True)
    return manifest
