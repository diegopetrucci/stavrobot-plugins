#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared'))
from tool_runtime import (  # noqa: E402
    ToolError,
    bridge_request,
    optional_bool,
    parse_id_list,
    require_string,
    run_tool,
)


def handle(params: dict[str, object]) -> dict[str, object]:
    reminder_ids = parse_id_list(require_string(params, 'reminder_ids'))
    force = optional_bool(params, 'force')
    dry_run = optional_bool(params, 'dry_run')
    dry_run_value = False if dry_run is None else dry_run
    if not dry_run_value and force is not True:
        raise ToolError('force=true is required for non-dry-run reminder deletion')

    payload: dict[str, object] = {'reminder_ids': reminder_ids}
    if force is True:
        payload['force'] = True
    return bridge_request('reminders.bulk_delete', payload, dry_run=dry_run_value if dry_run_value else None)


if __name__ == '__main__':
    raise SystemExit(run_tool(handle))
