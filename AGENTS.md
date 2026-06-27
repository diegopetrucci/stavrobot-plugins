# AGENTS.md

This repository hosts custom Stavrobot plugins.

## Misc

- To interface with GitHub, use the `gh` CLI. If `gh repo view` requires authentication for public data, use public REST reads through `gh api`.
- Use `python3`, not `python`.
- Prefix shell commands with `rtk` when supported. If RTK cannot parse a command, use `rtk proxy <cmd>` or follow RTK's explicit fallback instruction.
- Check `git status --short --branch` before editing and preserve unrelated user changes.

## Stavrobot references

- Upstream app: `https://github.com/skorokithakis/stavrobot`
- Published docs: `https://stavrobot.stavros.io`
- Plugin authoring source of truth: upstream `coder/PLUGIN.md`.
- First-party plugin examples: `https://github.com/orgs/stavrobot/repositories`
- When this file and upstream plugin docs disagree, follow upstream and update this file.

Useful upstream read:

```bash
rtk gh api 'repos/skorokithakis/stavrobot/contents/coder/PLUGIN.md?ref=master' --jq '.content' | base64 --decode
```

## Repository shape

- Treat this as a multi-plugin workspace, not the Stavrobot application repo.
- Put each plugin in a top-level `plugin-<name>/` directory unless the user asks for a different layout.
- Each `plugin-<name>/` directory should be self-contained and look like an installable Stavrobot plugin root: `manifest.json`, `README.md`, `.gitignore`, and one subdirectory per tool.
- Do not add a repository-root `manifest.json` unless the whole repository is intentionally a single Stavrobot plugin.
- Stavrobot's documented Git install flow expects a plugin manifest at the Git repo root. If a plugin in this workspace must be installed directly by Git URL, confirm the install strategy before assuming subdirectory installs work.

## Plugin contract

- Root plugin manifest is `manifest.json`.
- Root manifest `name` must use lowercase letters, digits, and hyphens only.
- Root manifest `description` should be short and user-facing.
- Declare required user configuration in the root manifest `config` object.
- Do not manage the `permissions` key. Stavrobot adds and owns it in runtime `config.json`.
- Each tool lives in its own directory with its own `manifest.json` and executable entrypoint.
- Tool manifests declare `name`, `description`, `entrypoint`, optional `async`, and `parameters`.
- Prefer `snake_case` tool names, matching first-party plugins.
- Tools receive a JSON object on stdin and must write a JSON object to stdout.
- The tool working directory is the tool directory, one level below the plugin root.
- Return compact, useful data. Do not blindly pass through full upstream API responses.
- Use non-zero exit codes for failures. Stderr is returned as failure detail, so keep it diagnostic and secret-free.
- Only set `async: true` for tools that normally need more than 30 seconds.
- Produced files go in `/tmp/<plugin_name>/`; reference them by filename in the JSON result.

## Python tools

- Use a `uv` shebang for executable Python plugin tools:

```python
#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///
```

- Read params with `json.load(sys.stdin)` and write results with `json.dump(..., sys.stdout)`.
- Add network timeouts for external API calls.
- The plugin runner validates parameter names and primitive types. Tool code may still check required values or domain constraints when useful.
- Make entrypoints executable with `chmod +x`.
- For local helper commands outside plugin entrypoints, use `python3`.

## Node tools

- Read stdin from file descriptor `0`, not `/dev/stdin`.
- Do not rely on global npm installs. Use `npx` for CLI packages or a local `npm install` from an init script.

## Config and secrets

- `config.json` is runtime-only and must be gitignored.
- Agents must not open, print, summarize, or commit local `config.json` files.
- Tool code may read `../config.json` at runtime for declared config keys.
- Never log API keys, access tokens, cookies, chat IDs, or other secrets. Report only whether required keys are present or missing.
- Prefer narrowly scoped API tokens and service integrations over broad host access or filesystem mounts.
- Do not add tools that expose arbitrary host files, plugin directories, or runtime secrets to the LLM.

## Testing

- Validate changed JSON manifests with `python3 -m json.tool <path>`.
- Test changed tools from their tool directory, because that is the runtime working directory:

```bash
printf '%s\n' '{"query":"test"}' | ./run.py
```

- Use fake or sample config for tests. Do not use real secret-bearing `config.json` values.
- For file-producing tools, verify files are written under `/tmp/<plugin_name>/` and keep total output under Stavrobot's 25 MB transport limit.
- If a tool depends on a network service, test success and a representative failure path when practical.
- In the final response, report which checks ran and which were skipped.

## Documentation

- Every plugin needs a root `README.md`.
- Each README should explain what the plugin does, how to install or copy it into Stavrobot, required config values, available tools, and known limitations.
- Keep setup instructions practical and secret-free.
- Do not commit a copied `PLUGIN.md` into plugins; use the upstream authoring guide as the reference.
