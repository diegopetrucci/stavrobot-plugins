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
    optional_number,
    optional_string,
    require_string,
    run_tool,
)


STRING_KEYS = ('notes', 'url', 'due', 'alarm', 'repeat', 'priority', 'list', 'list_id')


def handle(params: dict[str, object]) -> dict[str, object]:
    payload: dict[str, object] = {'title': require_string(params, 'title')}
    for key in STRING_KEYS:
        value = optional_string(params, key)
        if value is not None:
            payload[key] = value
    ensure_exactly_one(payload, 'list', 'list_id')

    location = optional_string(params, 'location')
    leaving = optional_bool(params, 'leaving')
    radius = optional_number(params, 'radius_meters')
    if location is None and (leaving is not None or radius is not None):
        raise ToolError('location is required when leaving or radius_meters is provided')
    if location is not None:
        trigger: dict[str, object] = {'location': location}
        if leaving is True:
            trigger['leaving'] = True
        if radius is not None:
            trigger['radius_meters'] = radius
        payload['location_trigger'] = trigger

    return bridge_request('reminders.create', payload)


if __name__ == '__main__':
    raise SystemExit(run_tool(handle))
