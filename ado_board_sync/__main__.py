import argparse
import json
import sys

from .client import Client, SyncError, credentials
from .hierarchy import text_tree
from .local import Filters, LocalSnapshot
from .seed import PROCESSES, plan, seed
from .sync import sync


def parser():
    root = argparse.ArgumentParser(description='Sync Azure DevOps work items into local JSON cards.')
    commands = root.add_subparsers(dest='command', required=True)
    for name, help_text in [('list', 'Filter and page through local card summaries.'),
                            ('show', 'Read one complete local card.'),
                            ('tree', 'Build the full local parent/child hierarchy across all types.'),
                            ('stats', 'Count local cards by state, type, assignee and tag.'),
                            ('bundle', 'Package matching cards for an agent, separate from sync data.')]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument('--data', default='board-data', help='Existing sync output folder; no auth needed.')
        command.add_argument('--snapshot', help='Pin a returned snapshots/<generation> path across pages.')
        if name == 'tree':
            selection = command.add_mutually_exclusive_group()
            selection.add_argument('--root', type=int, help='Show this item and all its descendants.')
            selection.add_argument('--path', type=int, help='Show the full ancestor chain ending at this item.')
            command.add_argument('--format', choices=('json', 'text'), default='json',
                                 help='JSON nodes with parent/child IDs and depth (default), or indented text.')
            command.epilog = ('Uses recorded links, not fixed type names. Missing items are placeholders; '
                              'cycles/conflicting parents fail. Includes every node without pagination. '
                              'Use a project-scope sync for the broadest available hierarchy.')
            continue
        if name == 'show':
            command.add_argument('id', type=int, help='Work item ID.')
            continue
        command.add_argument('--id', type=int, action='append', default=[], help='Exact ID; repeatable.')
        command.add_argument('--state', action='append', default=[], help='Exact state; repeatable, case-insensitive.')
        command.add_argument('--type', action='append', default=[], help='Exact work item type; repeatable.')
        owner = command.add_mutually_exclusive_group()
        owner.add_argument('--assignee', action='append', default=[], help='Exact name, email or identity ID; repeatable.')
        owner.add_argument('--unassigned', action='store_true')
        command.add_argument('--tag', action='append', default=[], help='Exact tag; repeatable.')
        command.add_argument('--area', action='append', default=[], help='Area path including descendants; repeatable.')
        command.add_argument('--iteration', action='append', default=[], help='Iteration path including descendants; repeatable.')
        command.add_argument('--text', help='Case-insensitive text in title or HTML-decoded description.')
        command.add_argument('--field', action='append', default=[], metavar='REFERENCE=VALUE',
                             help='Exact raw field match; JSON value or unquoted text. Repeatable.')
        command.epilog = ('Different filters combine with AND. Repeated values for one filter or field '
                          'combine with OR. Names/tags/paths ignore case; raw fields use exact JSON values.')
        if name in {'list', 'bundle'}:
            command.add_argument('--limit', type=int, default=50, help='Maximum cards (1–1000; default 50).')
            command.add_argument('--after-id', type=int, default=0, help='Continue after this ID; use --snapshot to pin results.')
        if name == 'bundle':
            command.add_argument('--output', required=True, help='New agent folder outside the sync output; Git-ignored automatically.')
    for name, help_text in [('sync', 'Read a project into an atomic local snapshot.'),
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
            command.add_argument('--scope', choices=('project', 'board'), default='project',
                                 help='All accessible project items (default), or one board.')
            command.add_argument('--resume', action='store_true',
                                 help='Continue a matching interrupted export at its original timestamp.')
            command.add_argument('--progress', action='store_true', help='Print batch progress to stderr.')
        else:
            command.add_argument('--process', choices=tuple(PROCESSES), default='Basic')
            command.add_argument('--count', type=int, default=24, help='Total synthetic cards (12–500).')
            command.add_argument('--dry-run', action='store_true', help='Print the invented plan without auth or network.')
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command in {'list', 'show', 'stats', 'bundle', 'tree'}:
            snapshot = LocalSnapshot(args.data, args.snapshot)
            if args.command == 'tree':
                result = snapshot.tree(root=args.root, path=args.path)
            elif args.command == 'show':
                result = snapshot.show(args.id)
            else:
                filters = Filters(ids=args.id, states=args.state, types=args.type,
                                  assignees=args.assignee, unassigned=args.unassigned,
                                  tag_values=args.tag, areas=args.area, iterations=args.iteration,
                                  text=args.text, fields=args.field)
                if args.command == 'stats':
                    result = snapshot.stats(filters)
                elif args.command == 'list':
                    result = snapshot.listing(filters, args.limit, args.after_id)
                else:
                    result = snapshot.bundle(args.output, filters, args.limit, args.after_id)
        elif not 1 <= args.timeout <= 300:
            raise SyncError('--timeout must be between 1 and 300 seconds.')
        elif args.command == 'seed' and args.dry_run:
            result = plan(args.process, args.count)
        else:
            client = Client(args.organization, args.project,
                            credentials(args.auth, args.organization), args.base_url, args.timeout)
            if args.command == 'sync':
                progress = ((lambda count: print(f'Fetched {count} work items', file=sys.stderr))
                            if args.progress else None)
                manifest = sync(client, args.output, args.team, args.board, scope=args.scope,
                                resume=args.resume, progress=progress)
                result = {'card_count': manifest['card_count'], 'as_of': manifest['as_of'],
                          'changes': manifest['changes'], 'output': args.output,
                          'read_first': args.output.rstrip('/\\') + '/current.json'}
            else:
                result = seed(client, args.process, args.count, args.team, args.board)
        if args.command == 'tree' and args.format == 'text':
            for line in text_tree(result):
                print(line)
        elif args.command == 'tree':
            json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
            print()
        else:
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
