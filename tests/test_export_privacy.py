import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ado_board_sync import SCHEMA_VERSION
from ado_board_sync.storage import locked_folder
from ado_board_sync.sync import sync


class ExportPrivacyTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('git'), 'Git required to verify ignore behavior')
    def test_custom_output_and_older_exports_are_ignored_by_git(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(['git', 'init', '--quiet', str(repo)], check=True)
            for existing in (False, True):
                with self.subTest(existing=existing):
                    output = repo / ('older-export' if existing else 'custom-pull')
                    if existing:
                        output.mkdir()
                        (output / '.ado-board-sync.json').write_text(
                            json.dumps({'schema_version': SCHEMA_VERSION}))
                        (output / 'current.json').write_text('{}')
                        # An existing exception must not expose private data.
                        (output / '.gitignore').write_text('*\n!current.json\n')
                    with locked_folder(output):
                        paths = ['current.json', 'snapshots/example/cards/1.json',
                                 'snapshots/example/manifest.json',
                                 '.in-progress/cards/2.json', '.in-progress/checkpoint.json']
                        for relative in paths:
                            path = output / relative
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_text('{}')
                        ignored = subprocess.run(
                            ['git', '-C', str(repo), 'check-ignore', '--stdin'],
                            input='\n'.join(str(path.relative_to(repo))
                                            for path in output.rglob('*') if path.is_file()),
                            capture_output=True, text=True, check=True)
                        self.assertEqual(len(ignored.stdout.splitlines()),
                                         sum(path.is_file() for path in output.rglob('*')))
                    visible = subprocess.check_output(
                        ['git', '-C', str(repo), 'ls-files', '--others', '--exclude-standard'],
                        text=True)
                    self.assertEqual(visible, '')

    def test_ignore_failure_stops_before_network_or_cards_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'custom-pull'
            with patch('ado_board_sync.storage.os.replace', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    # No client is touched until the output folder is protected.
                    sync(None, root)
            self.assertFalse((root / '.sync-lock').exists())
            self.assertFalse((root / 'snapshots').exists())
            self.assertFalse(list(root.glob('.gitignore-*')))
            with locked_folder(root):
                self.assertTrue((root / '.gitignore').is_file())
