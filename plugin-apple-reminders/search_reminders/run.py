#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared'))
from tool_runtime import (  # noqa: E402
    bridge_request,
    ensure_exactly_one,
    expect_list,
    optional_bool,
    optional_string,
    require_string,
    run_tool,
)


def handle(params: dict[str, object]) -> dict[str, object]:
    payload: dict[str, object] = {'query': require_string(params, 'query')}
    list_value = optional_string(params, 'list')
    list_id_value = optional_string(params, 'list_id')
    if list_value is not None:
        payload['list'] = list_value
    if list_id_value is not None:
        payload['list_id'] = list_id_value
    ensure_exactly_one(payload, 'list', 'list_id')

    completed = optional_bool(params, 'completed')
    if completed is True:
        payload['completed'] = True

    result = bridge_request('reminders.search', payload)
    items = expect_list(result.get('items'), 'items')
    return {'count': len(items), 'items': items}


if __name__ == '__main__':
    raise SystemExit(run_tool(handle))
