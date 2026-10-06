"""Bounded-memory staging and batch checkpoints for project and board exports."""

import re
import shutil
import os
import uuid
from datetime import datetime, timezone

from . import SCHEMA_VERSION
from .client import SyncError
from .storage import load_json, sync_directory, write_json


class Export:
    def __init__(self, root, source, configuration, previous, *, resume=False):
        self.root, self.source, self.configuration = root, source, configuration
        self.previous = previous
        self.stage = root / '.in-progress'
        if self.stage.is_symlink():
            raise SyncError('Export staging directory must not be a symlink.')
        if self.stage.exists() and not resume:
            shutil.rmtree(self.stage)
        if self.stage.exists():
            checkpoint = load_json(self.stage / 'checkpoint.json')
            if (checkpoint.get('schema_version') != SCHEMA_VERSION
                    or checkpoint.get('source') != source
                    or checkpoint.get('configuration') != configuration):
                raise SyncError('Interrupted export source or configuration differs; restart without --resume.')
            self.as_of = checkpoint.get('as_of')
            self.last_id, self.count = checkpoint.get('last_id'), checkpoint.get('count')
            if (type(self.last_id) is not int or type(self.count) is not int
                    or self.last_id < 0 or self.count < 0
                    or (self.count == 0) != (self.last_id == 0)):
                raise SyncError('Invalid export checkpoint; restart without --resume.')
            if self.as_of is not None:
                if not isinstance(self.as_of, str) or not re.fullmatch(r'[0-9TZ:.+\-]+', self.as_of):
                    raise SyncError('Invalid export timestamp; restart without --resume.')
                try:
                    datetime.fromisoformat(self.as_of.replace('Z', '+00:00'))
                except ValueError:
                    raise SyncError('Invalid export timestamp; restart without --resume.') from None
            elif self.count:
                raise SyncError('Missing export timestamp; restart without --resume.')
            committed = []
            for path in (self.stage / 'cards').iterdir():
                if path.is_symlink() or not re.fullmatch(r'[1-9][0-9]*\.json', path.name):
                    raise SyncError('Unexpected file in export staging directory.')
                item_id = int(path.stem)
                if item_id > self.last_id:
                    # A crash may leave files from the batch whose checkpoint never committed.
                    path.unlink()
                else:
                    card = load_json(path)
                    if card.get('id') != item_id:
                        raise SyncError('Staged card ID does not match its filename.')
                    committed.append(item_id)
            if len(committed) != self.count or (committed and max(committed) != self.last_id):
                raise SyncError('Interrupted export is incomplete; restart without --resume.')
        else:
            (self.stage / 'cards').mkdir(parents=True)
            self.as_of, self.last_id, self.count = None, 0, 0
            write_json(self.stage / 'checkpoint.json', self.checkpoint())

    def checkpoint(self):
        return {'schema_version': SCHEMA_VERSION, 'source': self.source,
                'configuration': self.configuration, 'as_of': self.as_of,
                'last_id': self.last_id, 'count': self.count}

    def add(self, cards, as_of):
        if self.as_of is not None and self.as_of != as_of:
            raise SyncError('Export timestamp changed; refusing inconsistent results.')
        cards = sorted(cards, key=lambda card: card['id'])
        ids = [card['id'] for card in cards]
        if any(type(i) is not int or i <= self.last_id for i in ids) or len(ids) != len(set(ids)):
            raise SyncError('Export IDs did not advance; refusing incomplete results.')
        for card in cards:
            write_json(self.stage / 'cards' / f"{card['id']}.json", card)
        sync_directory(self.stage / 'cards')
        self.as_of = as_of
        if ids:
            self.last_id = ids[-1]
        self.count += len(ids)
        temporary = self.stage / 'checkpoint.tmp'
        temporary.unlink(missing_ok=True)
        write_json(temporary, self.checkpoint())
        os.replace(temporary, self.stage / 'checkpoint.json')
        sync_directory(self.stage)

    def finish(self):
        ids = sorted(int(p.stem) for p in (self.stage / 'cards').glob('*.json'))
        if len(ids) != self.count or not self.as_of:
            raise SyncError('Export is incomplete; previous snapshot retained.')
        old_ids, new_ids = set(self.previous), set(ids)
        updated, moved = [], []
        for item_id in sorted(old_ids & new_ids):
            current = load_json(self.stage / 'cards' / f'{item_id}.json')
            previous = self.previous[item_id]
            if current != previous:
                updated.append(item_id)
                if any(current[key] != previous[key] for key in ('column', 'column_done', 'lane')):
                    moved.append(item_id)
        changes = {'added': sorted(new_ids - old_ids), 'updated': updated, 'moved': moved,
                   'removed': sorted(old_ids - new_ids),
                   'unchanged': len(old_ids & new_ids) - len(updated)}
        manifest = {'schema_version': SCHEMA_VERSION, 'source': self.source, 'as_of': self.as_of,
                    'synced_at': datetime.now(timezone.utc).isoformat(), 'card_ids': ids,
                    'card_count': self.count, 'changes': changes, 'configuration': self.configuration,
                    'enrichment': {'comments': 'not_fetched', 'attachments': 'links_only',
                                   'revision_history': 'not_fetched'}}
        (self.stage / 'manifest.json').unlink(missing_ok=True)
        write_json(self.stage / 'manifest.json', manifest)
        sync_directory(self.stage)
        generation = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex
        destination = self.root / 'snapshots' / generation
        self.stage.rename(destination)
        sync_directory(self.root / 'snapshots')
        pointer = self.root / ('.current-' + generation)
        try:
            write_json(pointer, {'schema_version': SCHEMA_VERSION,
                                'snapshot': 'snapshots/' + generation,
                                'cards': 'snapshots/' + generation + '/cards',
                                'manifest': 'snapshots/' + generation + '/manifest.json'})
            os.replace(pointer, self.root / 'current.json')
            sync_directory(self.root)
        finally:
            pointer.unlink(missing_ok=True)
        return manifest
