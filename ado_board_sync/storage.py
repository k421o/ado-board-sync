"""Immutable generations and one atomic pointer, including on Windows."""

import json
import os
import re
import tempfile
from contextlib import contextmanager
from collections.abc import Mapping
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


def ignore_export(root):
    """Exclude all generated data, even in a custom output folder inside a repo."""
    path = root / '.gitignore'
    contents = '# Generated ADO data may contain private work items.\n*\n'
    if path.is_symlink():
        raise SyncError('Export .gitignore must not be a symlink.')
    if path.exists() and path.read_text(encoding='utf-8') == contents:
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=root,
                                         prefix='.gitignore-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(root)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
        ignore_export(root)
        snapshots = root / 'snapshots'
        if snapshots.is_symlink():
            raise SyncError('Snapshots directory must not be a symlink.')
        snapshots.mkdir(exist_ok=True)
        yield root
    finally:
        lock.unlink()


class SnapshotCards(Mapping):
    """Read one previous card at a time instead of retaining its contents."""

    def __init__(self, folder, ids):
        self.folder, self.ids = folder, set(ids)

    def __iter__(self):
        return iter(self.ids)

    def __len__(self):
        return len(self.ids)

    def __getitem__(self, item_id):
        if item_id not in self.ids:
            raise KeyError(item_id)
        return load_json(self.folder / f"{item_id}.json")


def current_snapshot(root, *, lazy=False):
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
    ids = manifest['card_ids']
    if (any(type(i) is not int or i <= 0 for i in ids)
            or ids != sorted(set(ids))):
        raise SyncError('Invalid local card IDs.')
    cards = SnapshotCards(path / 'cards', ids)
    return manifest, cards if lazy else dict(cards)
