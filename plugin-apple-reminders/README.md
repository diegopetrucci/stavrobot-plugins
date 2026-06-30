# Apple Reminders plugin

This tlh plugin lets Stavrobot read and manage Apple Reminders through a small authenticated bridge running on a macOS host. The bridge shells out to [`remindctl`](https://github.com/steipete/remindctl), so Stavrobot never gets arbitrary shell or filesystem access on the Mac: it can only call the allowlisted reminder/list operations exposed by the bridge.

## What this plugin does

- Lists reminder lists and reminders.
- Searches reminders and fetches full reminder details by stable ID.
- Creates, edits, completes, uncompletes, deletes, and exports reminders.
- Creates, renames, and deletes reminder lists.
- Requires explicit IDs for reminder mutations and `force=true` for destructive deletes.

## Plugin root and copy strategy

This repository is a **multi-plugin workspace**. The repository root is **not** the Apple Reminders plugin root.

Use `plugin-apple-reminders/` itself as the installable plugin root so its `manifest.json` stays at the top level:

```text
plugin-apple-reminders/
├── manifest.json
├── README.md
├── host_bridge/
└── ...tool directories...
```

Install by copying or symlinking `plugin-apple-reminders/` into your Stavrobot plugins directory. Do **not** copy the whole repository as though it were one plugin.

## Prerequisites

- A macOS host with access to the Reminders app/data.
- `remindctl` installed on that macOS host.
- Network reachability from Stavrobot to the macOS host bridge.
- Local plugin config with `bridge_url` and `bridge_token`.

## 1) Install `remindctl` on the macOS host

A practical Homebrew install is:

```bash
brew install steipete/tap/remindctl
command -v remindctl
remindctl --version
```

The bridge requires an **absolute** `remindctl` path. `command -v remindctl` usually returns one of these:

- Apple Silicon Homebrew: `/opt/homebrew/bin/remindctl`
- Intel Homebrew: `/usr/local/bin/remindctl`

## 2) Generate and store a bridge token without printing it

Prefer a local token file with restrictive permissions instead of passing secrets on the command line.

```bash
mkdir -p ~/.config
python3 - <<'PY'
from pathlib import Path
import secrets

path = Path.home() / '.config' / 'apple-reminders-bridge.token'
path.write_text(secrets.token_urlsafe(32) + '\n', encoding='utf-8')
path.chmod(0o600)
print(f'wrote {path}')
PY
```

This writes the token to `~/.config/apple-reminders-bridge.token` without printing the secret value.

Use that same token value for the plugin's `bridge_token` config, but keep it out of git, logs, shell history, screenshots, and tickets. If you need to paste it into local config, copy it from the file in a local editor or clipboard tool rather than `echo`-ing it to the terminal.

## 3) Grant macOS Reminders permission (TCC)

Grant access from an **interactive macOS login session** before running the bridge headlessly:

```bash
"$(command -v remindctl)" authorize
"$(command -v remindctl)" doctor --for-agent --json
```

Notes:

- The process that actually touches Reminders is `remindctl`, invoked by the bridge.
- If macOS prompts for Reminders access, allow it.
- If there is no prompt or access was denied earlier, check **System Settings → Privacy & Security → Reminders** and re-run the commands above.
- Keep using the **same macOS user account** when you later run the bridge (including `launchd` or other headless launches).
- If you reinstall or move `remindctl`, you may need to grant access again.

## 4) Run the macOS host bridge

From the macOS host, start the bridge from `plugin-apple-reminders/host_bridge/`:

```bash
python3 ./remindctl_bridge.py \
  --bind 127.0.0.1 \
  --port 8765 \
  --path /bridge \
  --token-file ~/.config/apple-reminders-bridge.token \
  --remindctl-path "$(command -v remindctl)"
```

Important details:

- `--token-file` is safer than `--token` because it keeps the bearer token out of argv.
- `--path` must match the path used in `bridge_url` exactly.
- The bridge serves **POST** requests only.
- Run it as the same macOS user that authorized `remindctl`.

## 5) Configure the plugin

Create the plugin's runtime `config.json` locally in the plugin root and do **not** commit it.

Required config keys:

- `bridge_url`: full HTTP/HTTPS bridge endpoint
- `bridge_token`: bearer token shared with the bridge

Example config for Docker Desktop:

```json
{
  "bridge_url": "http://host.docker.internal:8765/bridge",
  "bridge_token": "<same token stored in ~/.config/apple-reminders-bridge.token>"
}
```

If Stavrobot runs directly on the same Mac instead of in a container, `bridge_url` can usually be:

```json
{
  "bridge_url": "http://127.0.0.1:8765/bridge",
  "bridge_token": "<same token stored in ~/.config/apple-reminders-bridge.token>"
}
```

## Docker Desktop and other runtime networking

### Docker Desktop on macOS

Use `host.docker.internal` in `bridge_url`:

- `http://host.docker.internal:8765/bridge`

That is the normal copy strategy when Stavrobot runs in Docker Desktop and the bridge runs on the Mac host.

### Other container/VM runtimes

Some runtimes do **not** provide `host.docker.internal`. In that case you may need to:

1. Bind the bridge to a host-reachable address instead of `127.0.0.1`.
2. Point `bridge_url` at that reachable host IP or hostname.
3. Adjust host firewall rules carefully.

Be careful here:

- Binding to `0.0.0.0` or a LAN IP exposes the bridge beyond loopback.
- Keep the token strong and secret.
- Prefer a private interface or tightly scoped firewall rules.
- Only allow trusted networks/clients.
- macOS may prompt about incoming connections when you bind non-loopback addresses.

## Available tools

| Tool | What it does | Key parameters / caveats |
| --- | --- | --- |
| `reminders_status` | Checks bridge reachability and advertised capabilities. | No parameters. Good first connectivity check. |
| `reminders_doctor` | Returns agent-focused setup diagnostics from the host side. | No parameters. Use when permissions or host setup look wrong. |
| `list_reminder_lists` | Lists available reminder lists. | No parameters. |
| `list_reminders` | Lists reminders using remindctl-style filters. | `filter` may be `today`, `tomorrow`, `week`, `overdue`, `upcoming`, `open`, `completed`, `all`, or `date`. `date` is required only with `filter=date`. Optional `list` or `list_id` (use one, not both). |
| `search_reminders` | Searches reminders by text query. | `query` required. Optional `completed=true` and optional `list` or `list_id`. |
| `get_reminder_info` | Fetches full details for one reminder. | `reminder_id` required. Stable full IDs or accepted ID prefixes work; numeric indexes do not. |
| `add_reminder` | Creates a reminder. | `title` required. Optional `notes`, `url`, `due`, `alarm`, `repeat`, `priority`, `list`/`list_id`, and location trigger fields `location`, `leaving`, `radius_meters`. `location` is required if you set `leaving` or `radius_meters`. |
| `edit_reminder` | Updates one reminder. | `reminder_id` required plus at least one patch field. Supports `title`, `notes`, `url`, `due`, `alarm`, `repeat`, `no_repeat`, `priority`, `list`/`list_id`, `complete`, `incomplete`, `clear_due`, `clear_alarm`, `clear_url`. |
| `complete_reminders` | Completes or uncompletes one or more reminders. | `reminder_ids` is a single string containing either a JSON array string or a comma/newline-separated list of IDs. Use `completed=false` to mark reminders incomplete. `dry_run=true` is only supported for completion previews, not uncomplete previews. |
| `delete_reminders` | Deletes one or more reminders. | `reminder_ids` uses the same string format as `complete_reminders`. Non-dry-run deletion requires `force=true`. Use `dry_run=true` first when you want a preview. |
| `manage_reminder_list` | Creates, renames, updates, or deletes a reminder list. | `action` must be `create`, `rename`, `update`, or `delete`. Create uses `title`. Rename/update uses `list` or `list_id` plus `rename`. Delete uses `list` or `list_id` and requires `force=true`. |
| `export_reminders` | Exports reminders to a file. | `format` must be `json` or `csv`. Optional `list` or `list_id`. Files are written under `/tmp/apple-reminders` and the tool returns the filename, item count, and size. |

## Safety model and destructive-operation caveats

- The bridge is deny-by-default and exposes only documented reminder/list operations.
- Tools use stable reminder IDs or accepted ID prefixes, **not** list positions.
- Bulk reminder operations require explicit reminder IDs; destructive deletes are never filter-based.
- `force=true` is required for:
  - `delete_reminders` real execution
  - `manage_reminder_list` with `action=delete`
- `dry_run=true` is supported for reminder deletion previews and completion previews, but not for every mutation.

## Export behavior

`export_reminders` writes files under:

```text
/tmp/apple-reminders
```

The returned JSON includes the **filename** plus metadata such as `item_count` and `size_bytes`. Treat exports as local temporary artifacts in the environment where the tool ran.

## Known limitations

- A macOS host is required for live Reminders access.
- The plugin does not talk directly to Apple Reminders from Linux/Windows; it always goes through the macOS bridge.
- `bridge_url` must be the full endpoint; tools do not append `/bridge` or other paths for you.
- Export supports `json` and `csv` only.
- Export narrowing is by `list` or `list_id`; it does not expose the full read-filter matrix.
- Due/alarm/repeat parsing follows whatever date/time and recurrence formats `remindctl` accepts.

## Troubleshooting

- Start with `reminders_status` to verify reachability, auth, and capabilities.
- Use `reminders_doctor` when the bridge is up but host setup or permissions still look wrong.
- If you get authentication failures, verify that:
  - `bridge_token` matches the token file contents exactly
  - `bridge_url` matches the bridge `--path`
  - the bridge was restarted after token changes
- If the bridge is unreachable from Docker Desktop, confirm `bridge_url` uses `host.docker.internal` and that the bridge is listening on the expected port/path.
- If you are on another runtime, confirm you changed both the bind address and firewall rules appropriately.
- If Reminders access fails, rerun `remindctl authorize` and `remindctl doctor --for-agent --json` from an interactive session as the same macOS user that runs the bridge.
- If the bridge says `remindctl` is missing, pass an absolute path from `command -v remindctl`.
