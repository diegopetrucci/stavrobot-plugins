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
    completed = optional_bool(params, 'completed')
    dry_run = optional_bool(params, 'dry_run')
    completed_value = True if completed is None else completed
    dry_run_value = False if dry_run is None else dry_run
    if not completed_value and dry_run_value:
        raise ToolError('dry_run is supported only when completed is true')

    payload: dict[str, object] = {'reminder_ids': reminder_ids}
    if completed is not None:
        payload['completed'] = completed_value
    return bridge_request('reminders.bulk_complete', payload, dry_run=dry_run_value if dry_run_value else None)


if __name__ == '__main__':
    raise SystemExit(run_tool(handle))
