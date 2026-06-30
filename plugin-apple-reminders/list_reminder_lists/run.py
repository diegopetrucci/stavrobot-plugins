#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared'))
from tool_runtime import bridge_request, expect_list, run_tool  # noqa: E402


def handle(params: dict[str, object]) -> dict[str, object]:
    del params
    result = bridge_request('lists.list', {})
    lists = expect_list(result.get('lists'), 'lists')
    return {'count': len(lists), 'lists': lists}


if __name__ == '__main__':
    raise SystemExit(run_tool(handle))
