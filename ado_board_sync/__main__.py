import argparse
import json
import sys

from .client import Client, SyncError, credentials
from .seed import PROCESSES, plan, seed
from .sync import sync


def parser():
    root = argparse.ArgumentParser(description='Sync an Azure DevOps Kanban board into local JSON cards.')
    commands = root.add_subparsers(dest='command', required=True)
    for name, help_text in [('sync', 'Read a board into an atomic local snapshot.'),
                            ('seed', 'Fill an empty sandbox project with invented cards.')]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument('--organization', required=True, help='Organization short name, not a URL.')
        command.add_argument('--project', required=True, help='Project name or ID.')
        command.add_argument('--team', help='Team name or ID; default: project default team.')
        command.add_argument('--board', help='Board name or ID; default: requirement board.')
        command.add_argument('--auth', choices=('env', 'keychain'), default='env')
        command.add_argument('--base-url', help='HTTP localhost fake-server override, for testing only.')
        command.add_argument('--timeout', type=float, default=30, help='Request timeout in seconds (1–300).')
        if name == 'sync':
            command.add_argument('--output', default='board-data', help='Dedicated local folder.')
        else:
            command.add_argument('--process', choices=tuple(PROCESSES), default='Basic')
            command.add_argument('--count', type=int, default=24, help='Total synthetic cards (12–500).')
            command.add_argument('--dry-run', action='store_true', help='Print the invented plan without auth or network.')
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if not 1 <= args.timeout <= 300:
            raise SyncError('--timeout must be between 1 and 300 seconds.')
        if args.command == 'seed' and args.dry_run:
            result = plan(args.process, args.count)
        else:
            client = Client(args.organization, args.project,
                            credentials(args.auth, args.organization), args.base_url, args.timeout)
            if args.command == 'sync':
                manifest = sync(client, args.output, args.team, args.board)
                result = {'card_count': manifest['card_count'], 'as_of': manifest['as_of'],
                          'changes': manifest['changes'], 'output': args.output,
                          'read_first': args.output.rstrip('/\\') + '/current.json'}
            else:
                result = seed(client, args.process, args.count, args.team, args.board)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except SyncError as error:
        print('error: ' + str(error), file=sys.stderr)
        return 1
    except OSError:
        print('error: Local filesystem operation failed. Inspect permissions/free space; '
              'read current.json for the last published snapshot.', file=sys.stderr)
        return 1
    except (KeyError, TypeError, ValueError, AttributeError):
        print('error: Unexpected data format; refusing incomplete results.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('error: Interrupted. Sync retains the last published snapshot; '
              'rerun an interrupted seed with the same options.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
