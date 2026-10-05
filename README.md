# ado-board-sync

Read an Azure DevOps Kanban board into a folder of JSON cards. Run the same
command again to pick up additions, edits, column/lane moves and removals.
The sync command only reads Azure DevOps. A separate seed command creates
invented cards in an empty sandbox project.

Python 3.11 or newer is the only runtime requirement. There are no package
dependencies, package installs, token files or configuration files. All example
organizations, projects, titles and people below are invented. The repository
remains private until its publication review is complete.

## Quickstart

Clone and run the offline checks from a fresh clone (macOS or Linux):

```sh
git clone https://github.com/k421o/ado-board-sync.git
cd ado-board-sync
python3 --version
./check.sh
./ado-board-sync seed --organization orbit-grove --project OrbitLab --dry-run
```

On Windows, use Python 3.11+ and run `python -m unittest discover -s tests -v`
and `python -m ado_board_sync ...` from the cloned repository instead of the
shell launchers. Snapshot publication uses an atomic file replacement rather
than symlinks and does not require Windows symlink privileges.

Create a PAT for the target organization in Azure DevOps User settings →
Personal access tokens. Select a short expiry and these scopes; the identity
also needs access to the project and its work items:

| Command | PAT scopes |
| --- | --- |
| `sync` | Work Items: **Read** (`vso.work`) |
| `seed` | Work Items: **Read & write** (`vso.work_write`, includes read) |
| Either command without `--team` | Also Project and Team: **Read** (`vso.project`) and User Profile: **Read** (`vso.profile`) for default-team discovery |
| `seed --dry-run` | None; no credentials or network calls |

Passing `--team` skips the project-discovery API and needs only the Work Items
scope. Neither command needs project creation, user management, code, build or
full-access scopes. The REST scopes follow the official
[Work API](https://github.com/MicrosoftDocs/vsts-rest-api-specs/blob/master/specification/work/7.1/work.json),
[Work Item Tracking API](https://github.com/MicrosoftDocs/vsts-rest-api-specs/blob/master/specification/wit/7.1/workItemTracking.json)
and [Core API](https://github.com/MicrosoftDocs/vsts-rest-api-specs/blob/master/specification/core/7.1/core.json)
contracts.

For a Bash or Zsh terminal, enter the token at the hidden prompt so its value
is never included in a command or shell history:

```sh
printf 'Azure DevOps PAT: '
read -r -s ADO_PAT
printf '\n'
export ADO_PAT
./ado-board-sync sync --organization orbit-grove --project OrbitLab --output ./board-data
unset ADO_PAT
```

Replace the organization and project with your own. The organization is its
short name, not its URL. Project and team arguments accept names or IDs. The
default selection is the project's default team and its requirement-level
board (for example, Issues in Basic or Stories in Agile). To select a different
team or a portfolio board:

```sh
./ado-board-sync sync --organization orbit-grove --project OrbitLab \
  --team 'Starlight Team' --board 'Issues' --output ./board-data
```

Inject `ADO_PAT` again before subsequent commands, or have your secret manager
provide it in the process environment. `AZURE_DEVOPS_EXT_PAT` is also accepted.
Do not put a PAT in a command argument, file, `.env`, README or exported card.
The client holds credentials in memory, refuses redirects, and omits server
error bodies from diagnostics. `--timeout 60` increases each request's timeout
from the default 30 seconds. Read requests have bounded retries for transient
network errors and throttling.

On macOS, Keychain can supply the PAT without an environment variable: use
Keychain Access to create a generic password item with name/service
`ado-board-sync`, account equal to your organization short name, and the PAT
as its password. Add `--auth keychain` to either live command. This tool only
reads that item, never creates or updates it; Keychain may prompt for access.

## Read the local cards

The sync prints a JSON summary with added, updated, moved and removed IDs.
Always read `board-data/current.json` first, then follow its relative paths:

```json
{
  "schema_version": 1,
  "snapshot": "snapshots/20261005T120000Z-0123456789abcdef0123456789abcdef",
  "cards": "snapshots/20261005T120000Z-0123456789abcdef0123456789abcdef/cards",
  "manifest": "snapshots/20261005T120000Z-0123456789abcdef0123456789abcdef/manifest.json"
}
```

For example, an agent or a small script can read the active snapshot like this:

```python
import json
from pathlib import Path

root = Path("board-data")
pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
manifest = json.loads((root / pointer["manifest"]).read_text(encoding="utf-8"))
cards = [json.loads((root / pointer["cards"] / f"{i}.json").read_text(encoding="utf-8"))
         for i in manifest["card_ids"]]
```

Each `<id>.json` contains the stable work item ID and revision, title, type,
state, selected team's column, split-column done flag, lane, assignee identity,
area/iteration paths, description, parent IDs, raw fields and relations.
Descriptions retain ADO's HTML. Raw data preserves custom fields and links for
later comparison or publishing tools. Parent links may point to cards on a
different board; only the selected board's cards are exported. Comments,
attachments and revision history are not fetched in this round.

The manifest records the source, board configuration, server `as_of` time,
card IDs and changes since the previous successful sync. Membership uses the
team's area scope, backlog iteration and the board's type/state mappings.
This includes completed cards and ignores temporary browser search, assignee
and date filters; it exports the accessible board membership rather than a
screenshot of the current web view. Cards deleted, moved outside that scope,
or moved to a state absent from the board disappear from the new snapshot.
Positions use the selected team's board-specific fields, not another team's
global column. Newly created cards without those fields use their state’s
first mapped column until ADO supplies a position.

## Failure and recovery

All pages and batches use one server timestamp. Missing items, invalid data,
HTTP failures or a board-configuration change abort publication. Files are
written into a new immutable generation; only after it is complete does
`current.json` switch atomically. An interrupted or failed run leaves the
last published cards readable. An initial failed run has no `current.json`.
Concurrent writers are refused by `.sync-lock`.

Keep using the last pointer after a failure and rerun the same command. After
a process is forcibly killed, verify that no sync is running before removing
`board-data/.sync-lock`. Complete older generations remain available for
readers and recovery; delete unneeded generations manually only after readers
finish, keeping the generation named in `current.json`. A crash may also leave
an unreferenced generation or `.tmp-*` folder, which can be removed at that time.
Do not recursively read every generation as the current board: older folders
intentionally contain cards that have since been removed.

Use a dedicated empty output folder. The tool refuses unrelated folders and
refuses to repoint an existing snapshot folder to another source. Local edits
to a generation are not published to ADO; the next successful sync writes a
fresh generation from the board. Store any agent notes outside snapshot
generations. Keep the local data in your own environment; it can contain
private board content even though this repository uses only invented data.

## Seed an empty sandbox

Create your personal Azure DevOps organization and an empty **private** project
yourself. Choose the **Basic** process for the simplest setup. No organization
is provisioned by this tool. Preview the 24 invented astronomy-guide cards:

```sh
./ado-board-sync seed --organization orbit-grove --project OrbitLab --dry-run
```

The preview lists Epics, Issues and Tasks, a mix of states and board columns,
fictional assignees, descriptions, tags and parent keys. After injecting a PAT
with the seed scopes above, create those cards and read the resulting board:

```sh
./ado-board-sync seed --organization orbit-grove --project OrbitLab
./ado-board-sync sync --organization orbit-grove --project OrbitLab --output ./board-data
unset ADO_PAT
```

Use `--count 60` for a larger sample (12–500 total cards). `--process Agile`,
`--process Scrum` and `--process CMMI` generate the corresponding work item
types and states. Live seed validates these against the project's process and
the selected board before writing; customized workflows may be rejected.
The seed uses existing incoming, in-progress and outgoing columns and their
state mappings, and the team's default area and backlog iteration. It does
not change board settings or create users. Fictional assignees are personas
stored in descriptions; `System.AssignedTo` stays empty because ADO requires
real identities there. Epics and Tasks are linked to their children/parents
but do not appear as cards on the requirement board.

Seed refuses a nonempty project unless every existing card belongs to this
exact unedited synthetic seed. Interrupted creates are reconciled using stable
keys and tags on the server. Rerun with the same process, count, team and board;
already-created cards are not duplicated. Ambiguous create failures are never
automatically retried. This is not a project reset: do not rerun seed after
experimenting with edits or adding other cards. Use another empty project.

## Offline verification

`./check.sh` uses the standard-library test runner and ephemeral HTTP servers
bound to `127.0.0.1`; it needs no ADO account, token, dependency install or
external network. Tests cover the command-line workflow, repeated sync,
team-specific positions, scope removals, historical paging beyond 1,000 cards,
200-item batches, failed/partial transfer and disk publication, output guards,
all four seed processes and interrupted-seed recovery. The test servers and
temporary output folders are torn down after each test.

`--base-url http://127.0.0.1:PORT` is available for a localhost fake server;
it cannot redirect the client to a remote test host. Live commands normally
use `https://dev.azure.com/<organization>` with REST API version 7.1. Live ADO
integration and publication review are separate from this offline proof.
