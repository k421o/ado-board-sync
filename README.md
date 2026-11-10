# ado-board-sync

Export Azure DevOps projects to local snapshots. It filters work items, builds hierarchy trees, and packages matching cards into bundles for agents. It is a Python command-line tool.

## Run

Sync reads a token from the `ADO_PAT` or `AZURE_DEVOPS_EXT_PAT` environment variable, or from the keychain with `--auth keychain`.

```sh
./ado-board-sync sync --organization <org> --project <project> --output ./board-data
./ado-board-sync list --data ./board-data
./ado-board-sync tree --data ./board-data --format text
./ado-board-sync bundle --data ./board-data --output ./bundle
./ado-board-sync seed --organization <org> --project <project> --dry-run
./check.sh
```

The other commands are `show` and `stats`. `check.sh` runs the unit tests in `tests/`.

## Layout

- `ado-board-sync`: shell launcher for the `ado_board_sync` package.
- `ado_board_sync/`: the client, sync, local snapshot filters, hierarchy and seed code.
- `tests/`: unit tests with a fake Azure DevOps server.
- `scripts/check-scale.py`: runs the exporter against generated local data.
