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
    ensure_exactly_one,
    expect_list,
    optional_string,
    run_tool,
)


def handle(params: dict[str, object]) -> dict[str, object]:
    filters: dict[str, object] = {}
    filter_value = optional_string(params, 'filter')
    date_value = optional_string(params, 'date')
    list_value = optional_string(params, 'list')
    list_id_value = optional_string(params, 'list_id')

    if filter_value is not None:
        filters['filter'] = filter_value
    if date_value is not None:
        filters['date'] = date_value
    if list_value is not None:
        filters['list'] = list_value
    if list_id_value is not None:
        filters['list_id'] = list_id_value
    ensure_exactly_one(filters, 'list', 'list_id')
    if date_value is not None and filter_value != 'date':
        raise ToolError('date may be used only with filter=date')
    if filter_value == 'date' and date_value is None:
        raise ToolError('date is required when filter=date')

    payload = {'filters': filters} if filters else {}
    result = bridge_request('reminders.list', payload)
    items = expect_list(result.get('items'), 'items')
    return {'count': len(items), 'items': items}


if __name__ == '__main__':
    raise SystemExit(run_tool(handle))
