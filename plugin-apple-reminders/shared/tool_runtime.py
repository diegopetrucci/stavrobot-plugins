from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = Path('/tmp/apple-reminders')
REQUEST_TIMEOUT_SECONDS = 20


class ToolError(Exception):
    """Expected tool failure with a secret-free message."""


def run_tool(handler: Callable[[dict[str, Any]], Any]) -> int:
    try:
        params = read_params()
        result = handler(params)
        json.dump(result, sys.stdout, separators=(',', ':'), ensure_ascii=False)
        sys.stdout.write('\n')
        return 0
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def read_params() -> dict[str, Any]:
    try:
        params = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise ToolError('stdin must be valid JSON') from exc
    if not isinstance(params, dict):
        raise ToolError('stdin JSON must be an object')
    return params


def get_contract_version() -> str:
    contract_path = PLUGIN_ROOT / 'shared' / 'bridge-operations.json'
    try:
        with contract_path.open('r', encoding='utf-8') as handle:
            contract = json.load(handle)
    except FileNotFoundError as exc:
        raise ToolError('shared bridge contract is unavailable') from exc
    except json.JSONDecodeError as exc:
        raise ToolError('shared bridge contract is invalid') from exc
    version = contract.get('contract_version')
    if not isinstance(version, str) or not version:
        raise ToolError('shared bridge contract is invalid')
    return version


def load_config() -> tuple[str, str]:
    config_path = PLUGIN_ROOT / 'config.json'
    try:
        with config_path.open('r', encoding='utf-8') as handle:
            config = json.load(handle)
    except FileNotFoundError as exc:
        raise ToolError('config.json is missing; configure bridge_url and bridge_token') from exc
    except json.JSONDecodeError as exc:
        raise ToolError('config.json must contain valid JSON') from exc

    if not isinstance(config, dict):
        raise ToolError('config.json must be a JSON object')

    bridge_url = config.get('bridge_url')
    bridge_token = config.get('bridge_token')
    if not isinstance(bridge_url, str) or not bridge_url.strip():
        raise ToolError('bridge_url config is missing or empty')
    parsed = urlparse(bridge_url.strip())
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise ToolError('bridge_url must be an HTTP or HTTPS URL')
    if not isinstance(bridge_token, str) or not bridge_token.strip():
        raise ToolError('bridge_token config is missing or empty')
    return bridge_url.strip(), bridge_token.strip()


def bridge_request(operation: str, payload: dict[str, Any], *, dry_run: bool | None = None) -> dict[str, Any]:
    bridge_url, bridge_token = load_config()
    envelope: dict[str, Any] = {
        'contract_version': get_contract_version(),
        'operation': operation,
        'payload': payload,
    }
    if dry_run is not None:
        envelope['dry_run'] = dry_run

    request = Request(
        bridge_url,
        data=json.dumps(envelope, separators=(',', ':'), ensure_ascii=False).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {bridge_token}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )

    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read()
    except HTTPError as exc:
        body = exc.read()
        message = _bridge_error_message(operation, body)
        if message:
            raise ToolError(message) from exc
        if exc.code in {401, 403}:
            raise ToolError('bridge authentication failed') from exc
        raise ToolError(f'bridge request failed with HTTP {exc.code}') from exc
    except URLError as exc:
        raise ToolError('bridge request failed') from exc

    result = _decode_bridge_response(operation, body)
    if not isinstance(result, dict):
        raise ToolError('bridge returned an invalid result')
    return result


def _decode_bridge_response(expected_operation: str, body: bytes) -> Any:
    try:
        decoded = json.loads(body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolError('bridge returned invalid JSON') from exc

    if not isinstance(decoded, dict):
        raise ToolError('bridge returned an invalid response')

    operation = decoded.get('operation')
    if operation != expected_operation:
        raise ToolError('bridge returned a mismatched operation')

    if decoded.get('ok') is True:
        return decoded.get('result')

    message = _bridge_error_message(expected_operation, body)
    if message:
        raise ToolError(message)
    raise ToolError('bridge reported an error')


def _bridge_error_message(expected_operation: str, body: bytes) -> str | None:
    try:
        decoded = json.loads(body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    if decoded.get('operation') not in {None, expected_operation}:
        return None
    error = decoded.get('error')
    if not isinstance(error, dict):
        return None
    code = error.get('code')
    message = error.get('message')
    if isinstance(code, str) and code and isinstance(message, str) and message:
        return f'bridge {code}: {message}'
    if isinstance(message, str) and message:
        return f'bridge error: {message}'
    return None


def require_string(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f'{key} is required')
    return value.strip()


def optional_string(params: dict[str, Any], key: str) -> str | None:
    if key not in params:
        return None
    value = params[key]
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f'{key} must be a non-empty string')
    return value.strip()


def optional_bool(params: dict[str, Any], key: str) -> bool | None:
    if key not in params:
        return None
    value = params[key]
    if not isinstance(value, bool):
        raise ToolError(f'{key} must be a boolean')
    return value


def optional_number(params: dict[str, Any], key: str) -> float | int | None:
    if key not in params:
        return None
    value = params[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f'{key} must be a number')
    return value


def ensure_exactly_one(payload: dict[str, Any], first: str, second: str) -> None:
    first_present = first in payload and payload[first] not in {None, ''}
    second_present = second in payload and payload[second] not in {None, ''}
    if first_present and second_present:
        raise ToolError(f'use only one of {first} or {second}')


def require_one_of(payload: dict[str, Any], first: str, second: str) -> None:
    ensure_exactly_one(payload, first, second)
    first_present = first in payload and payload[first] not in {None, ''}
    second_present = second in payload and payload[second] not in {None, ''}
    if not first_present and not second_present:
        raise ToolError(f'provide {first} or {second}')


def parse_id_list(raw_value: str) -> list[str]:
    text = raw_value.strip()
    if not text:
        raise ToolError('reminder_ids is required')

    ids: list[str]
    if text.startswith('['):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ToolError('reminder_ids must be a JSON array string or a comma/newline-separated string') from exc
        if not isinstance(parsed, list) or not parsed:
            raise ToolError('reminder_ids must contain at least one id')
        ids = []
        for item in parsed:
            if not isinstance(item, str) or not item.strip():
                raise ToolError('reminder_ids JSON array items must be non-empty strings')
            ids.append(item.strip())
    else:
        ids = []
        for line in text.replace('\r', '\n').split('\n'):
            for piece in line.split(','):
                candidate = piece.strip()
                if candidate:
                    ids.append(candidate)
        if not ids:
            raise ToolError('reminder_ids must contain at least one id')

    return list(dict.fromkeys(ids))


def expect_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ToolError(f'bridge returned invalid {label}')
    return value


def expect_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolError(f'bridge returned invalid {label}')
    return value


def write_output_file(filename: str, content: str | bytes) -> tuple[str, int]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / filename
    data = content.encode('utf-8') if isinstance(content, str) else content
    path.write_bytes(data)
    return filename, len(data)
