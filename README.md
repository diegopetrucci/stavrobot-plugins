# Stavrobot plugins workspace

This repository is a workspace for Stavrobot plugin submodules. It is **not** an installable Stavrobot plugin root and should not be added to Stavrobot directly.

## Layout

Each top-level `plugin-*` directory is its own plugin repository checked out as a git submodule.

Current submodules:

- `plugin-apple-reminders/`

## Clone with submodules

```bash
git clone --recurse-submodules https://github.com/diegopetrucci/stavrobot-plugins.git
cd stavrobot-plugins
```

If you already cloned the workspace without submodules, initialize and update them with:

```bash
git submodule update --init --recursive
```

To pull the latest submodule commits recorded by this workspace later:

```bash
git submodule update --recursive --remote
```

## Installing plugins

Install individual plugins from their standalone repositories or by copying a checked-out submodule directory into your local Stavrobot plugins directory.

For Apple Reminders, use the standalone plugin repository:

- https://github.com/diegopetrucci/stavrobot-apple-reminders-plugin
