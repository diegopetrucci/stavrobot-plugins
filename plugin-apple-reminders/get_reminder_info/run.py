#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared'))
from tool_runtime import bridge_request, run_tool, require_string  # noqa: E402


def handle(params: dict[str, object]) -> dict[str, object]:
    return bridge_request('reminders.info', {'reminder_id': require_string(params, 'reminder_id')})


if __name__ == '__main__':
    raise SystemExit(run_tool(handle))
