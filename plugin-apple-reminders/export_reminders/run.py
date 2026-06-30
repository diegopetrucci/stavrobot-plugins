#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared'))
from tool_runtime import (  # noqa: E402
    ToolError,
    bridge_request,
    ensure_exactly_one,
    expect_list,
    optional_string,
    require_string,
    run_tool,
    write_output_file,
)


def handle(params: dict[str, object]) -> dict[str, object]:
    export_format = require_string(params, 'format').lower()
    if export_format not in {'json', 'csv'}:
        raise ToolError('format must be json or csv')

    payload: dict[str, object] = {'format': export_format}
    list_value = optional_string(params, 'list')
    list_id_value = optional_string(params, 'list_id')
    if list_value is not None:
        payload['list'] = list_value
    if list_id_value is not None:
        payload['list_id'] = list_id_value
    ensure_exactly_one(payload, 'list', 'list_id')

    result = bridge_request('reminders.export', payload)
    if export_format == 'json':
        items = expect_list(result.get('items'), 'items')
        filename, size_bytes = write_output_file('export-reminders.json', json.dumps(items, separators=(',', ':'), ensure_ascii=False))
        return {'format': 'json', 'file': filename, 'item_count': len(items), 'size_bytes': size_bytes}

    content = result.get('content')
    if not isinstance(content, str):
        raise ToolError('bridge returned invalid export content')
    filename, size_bytes = write_output_file('export-reminders.csv', content)
    lines = [line for line in content.splitlines() if line.strip()]
    item_count = max(len(lines) - 1, 0)
    return {'format': 'csv', 'file': filename, 'item_count': item_count, 'size_bytes': size_bytes}


if __name__ == '__main__':
    raise SystemExit(run_tool(handle))
