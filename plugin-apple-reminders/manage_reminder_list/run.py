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
    optional_string,
    require_one_of,
    require_string,
    run_tool,
)


def handle(params: dict[str, object]) -> dict[str, object]:
    action = require_string(params, 'action').lower()
    if action == 'create':
        result = bridge_request('lists.create', {'title': require_string(params, 'title')})
        result['action'] = 'create'
        return result

    payload: dict[str, object] = {}
    list_value = optional_string(params, 'list')
    list_id_value = optional_string(params, 'list_id')
    if list_value is not None:
        payload['list'] = list_value
    if list_id_value is not None:
        payload['list_id'] = list_id_value
    require_one_of(payload, 'list', 'list_id')

    if action in {'rename', 'update'}:
        payload['rename'] = require_string(params, 'rename')
        result = bridge_request('lists.update', payload)
        result['action'] = 'rename'
        return result

    if action == 'delete':
        if optional_bool(params, 'force') is not True:
            raise ToolError('force=true is required for list deletion')
        payload['force'] = True
        result = bridge_request('lists.delete', payload)
        result['action'] = 'delete'
        return result

    raise ToolError('action must be create, rename, update, or delete')


if __name__ == '__main__':
    raise SystemExit(run_tool(handle))
