# Apple Reminders host bridge

This directory contains the macOS-side tlh bridge that exposes an authenticated POST JSON wrapper around `remindctl`.

## Run on macOS

Prefer a user-local token file so the bearer token does not appear in shell history or process listings:

```bash
python3 ./remindctl_bridge.py \
  --bind 127.0.0.1 \
  --port 8765 \
  --path /bridge \
  --token-file ~/.config/apple-reminders-bridge.token \
  --remindctl-path /opt/homebrew/bin/remindctl
```

## Reminders permission / TCC

Before running the bridge headlessly, grant Reminders access to `remindctl` from an interactive macOS login session:

```bash
/opt/homebrew/bin/remindctl authorize
/opt/homebrew/bin/remindctl doctor --for-agent --json
```

If you later launch the bridge with `launchd`, keep using the same macOS user account that granted access.

## Validation

Representative unit tests avoid live Reminders access by injecting a fake `remindctl` runner:

```bash
python3 -m unittest plugin-apple-reminders/host_bridge/test_remindctl_bridge.py
```
