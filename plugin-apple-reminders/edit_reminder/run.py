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
    optional_bool,
    optional_string,
    require_string,
    run_tool,
)


STRING_KEYS = ('title', 'notes', 'url', 'due', 'alarm', 'repeat', 'priority', 'list', 'list_id')
TRUE_ONLY_FLAGS = ('no_repeat', 'complete', 'incomplete', 'clear_due', 'clear_alarm', 'clear_url')


def handle(params: dict[str, object]) -> dict[str, object]:
    patch: dict[str, object] = {}
    for key in STRING_KEYS:
        value = optional_string(params, key)
        if value is not None:
            patch[key] = value
    ensure_exactly_one(patch, 'list', 'list_id')

    complete = optional_bool(params, 'complete')
    incomplete = optional_bool(params, 'incomplete')
    if complete is True and incomplete is True:
        raise ToolError('complete and incomplete cannot both be true')

    for key in TRUE_ONLY_FLAGS:
        value = optional_bool(params, key)
        if value is True:
            patch[key] = True

    if not patch:
        raise ToolError('provide at least one patch field to edit')

    payload = {
        'reminder_id': require_string(params, 'reminder_id'),
        'patch': patch,
    }
    return bridge_request('reminders.update', payload)


if __name__ == '__main__':
    raise SystemExit(run_tool(handle))
