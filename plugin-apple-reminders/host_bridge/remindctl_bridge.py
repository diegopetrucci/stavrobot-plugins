#!/usr/bin/env python3
"""Authenticated macOS host bridge for Apple Reminders via remindctl.

Launch on macOS from a user session that already has Reminders permission for
`remindctl`, for example:

  ./remindctl_bridge.py \
    --bind 127.0.0.1 \
    --port 8765 \
    --path /bridge \
    --token-file ~/.config/apple-reminders-bridge.token \
    --remindctl-path /opt/homebrew/bin/remindctl

Before using the bridge for the first time, grant Reminders access to
`remindctl` in an interactive macOS login session:

  /opt/homebrew/bin/remindctl authorize
  /opt/homebrew/bin/remindctl doctor --for-agent --json

If you later launch this bridge under launchd, do the permission-granting step
first while signed in to the same macOS user account.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import secrets
import selectors
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

BRIDGE_VERSION = "0.1.0"
DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_PATH = "/"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_BODY_BYTES = 64 * 1024
DEFAULT_MAX_STDOUT_BYTES = 512 * 1024
DEFAULT_MAX_STDERR_BYTES = 32 * 1024
SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"


@dataclass(frozen=True)
class BridgeConfig:
    path: str
    token: str
    remindctl_path: Path
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    max_stdout_bytes: int = DEFAULT_MAX_STDOUT_BYTES
    max_stderr_bytes: int = DEFAULT_MAX_STDERR_BYTES


@dataclass(frozen=True)
class ServerConfig:
    bind: str
    port: int
    bridge: BridgeConfig


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class BridgeError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: HTTPStatus = HTTPStatus.BAD_REQUEST,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}


class SchemaValidationError(BridgeError):
    def __init__(self, message: str) -> None:
        super().__init__("validation_error", message, http_status=HTTPStatus.BAD_REQUEST)


class SchemaValidator:
    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document

    def validate(self, schema: dict[str, Any], value: Any, path: str = "request") -> None:
        if "$ref" in schema:
            self.validate(self.resolve_ref(schema["$ref"]), value, path)
            return

        if "anyOf" in schema:
            errors: list[str] = []
            for option in schema["anyOf"]:
                try:
                    self.validate(option, value, path)
                    break
                except SchemaValidationError as exc:
                    errors.append(exc.message)
            else:
                raise SchemaValidationError(errors[0] if errors else f"{path} did not match any allowed shape")

        expected_type = schema.get("type")
        if expected_type:
            self._validate_type(expected_type, value, path)

        if "const" in schema and value != schema["const"]:
            raise SchemaValidationError(f"{path} must equal {schema['const']!r}")

        if "enum" in schema and value not in schema["enum"]:
            raise SchemaValidationError(f"{path} must be one of {', '.join(repr(item) for item in schema['enum'])}")

        if isinstance(value, dict):
            self._validate_object(schema, value, path)
        elif isinstance(value, list):
            self._validate_array(schema, value, path)
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
            self._validate_scalar(schema, value, path)

    def resolve_ref(self, ref: str) -> dict[str, Any]:
        if not ref.startswith("#/"):
            raise ValueError(f"Unsupported $ref: {ref}")
        node: Any = self.document
        for segment in ref[2:].split("/"):
            node = node[segment]
        if not isinstance(node, dict):
            raise ValueError(f"Unsupported non-object $ref target: {ref}")
        return node

    def _validate_type(self, expected_type: str, value: Any, path: str) -> None:
        type_checks: dict[str, Callable[[Any], bool]] = {
            "object": lambda item: isinstance(item, dict),
            "string": lambda item: isinstance(item, str),
            "boolean": lambda item: isinstance(item, bool),
            "array": lambda item: isinstance(item, list),
            "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        }
        checker = type_checks.get(expected_type)
        if checker is None:
            raise ValueError(f"Unsupported schema type: {expected_type}")
        if not checker(value):
            raise SchemaValidationError(f"{path} must be a {expected_type}")

    def _validate_object(self, schema: dict[str, Any], value: dict[str, Any], path: str) -> None:
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        additional = schema.get("additionalProperties", True)

        for name in required:
            if name not in value:
                raise SchemaValidationError(f"{path}.{name} is required")

        if additional is False:
            for key in value:
                if key not in properties:
                    raise SchemaValidationError(f"{path}.{key} is not allowed")

        for key, item in value.items():
            child_schema = properties.get(key)
            if child_schema:
                self.validate(child_schema, item, f"{path}.{key}")

    def _validate_array(self, schema: dict[str, Any], value: list[Any], path: str) -> None:
        min_items = schema.get("minItems")
        if min_items is not None and len(value) < min_items:
            raise SchemaValidationError(f"{path} must contain at least {min_items} item(s)")

        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                self.validate(item_schema, item, f"{path}[{index}]")

    def _validate_scalar(self, schema: dict[str, Any], value: Any, path: str) -> None:
        minimum = schema.get("minimum")
        if minimum is not None and value < minimum:
            raise SchemaValidationError(f"{path} must be >= {minimum}")

        pattern = schema.get("pattern")
        if pattern is not None and isinstance(value, str) and re.fullmatch(pattern, value) is None:
            raise SchemaValidationError(f"{path} has an invalid format")


Runner = Callable[[list[str], float, dict[str, str], int, int], CommandResult]


def default_runner(
    argv: list[str],
    timeout_seconds: float,
    env: dict[str, str],
    max_stdout_bytes: int,
    max_stderr_bytes: int,
) -> CommandResult:
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            shell=False,
        )
    except OSError as exc:
        raise BridgeError(
            "upstream_command_failed",
            "failed to launch remindctl",
            http_status=HTTPStatus.BAD_GATEWAY,
            details={"command": safe_command_name(argv)},
        ) from exc

    stdout_limit = max_stdout_bytes + 1
    stderr_limit = max_stderr_bytes + 1
    stdout = bytearray()
    stderr = bytearray()
    selector = selectors.DefaultSelector()
    try:
        if process.stdout is None or process.stderr is None:
            raise BridgeError(
                "internal_error",
                "internal bridge error",
                http_status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        selector.register(process.stdout, selectors.EVENT_READ, (stdout, stdout_limit))
        selector.register(process.stderr, selectors.EVENT_READ, (stderr, stderr_limit))
        deadline = time.monotonic() + timeout_seconds

        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                raise BridgeError(
                    "timeout",
                    "remindctl timed out",
                    http_status=HTTPStatus.GATEWAY_TIMEOUT,
                    details={"timeout_seconds": timeout_seconds, "command": safe_command_name(argv)},
                )

            events = selector.select(remaining)
            if not events:
                continue

            for key, _ in events:
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue

                buffer, buffer_limit = key.data
                if len(buffer) < buffer_limit:
                    buffer.extend(chunk[: buffer_limit - len(buffer)])
                if len(buffer) >= buffer_limit and process.poll() is None:
                    process.kill()

        return CommandResult(
            returncode=process.wait(),
            stdout=bytes(stdout),
            stderr=bytes(stderr),
        )
    finally:
        selector.close()
        if process.stdout is not None and not process.stdout.closed:
            process.stdout.close()
        if process.stderr is not None and not process.stderr.closed:
            process.stderr.close()


class BridgeApp:
    def __init__(self, config: BridgeConfig, contract: dict[str, Any], runner: Runner | None = None) -> None:
        self.config = config
        self.contract = contract
        self.validator = SchemaValidator(contract)
        self.runner = runner or default_runner
        self.command_lock = threading.Lock()
        self.contract_version = self._read_contract_version()
        self.operations = {entry["name"]: entry for entry in contract["operations"]}
        self.handlers: dict[str, Callable[[dict[str, Any], bool], dict[str, Any]]] = {
            "status.get": self.handle_status_get,
            "status.doctor": self.handle_status_doctor,
            "lists.list": self.handle_lists_list,
            "lists.create": self.handle_lists_create,
            "lists.update": self.handle_lists_update,
            "lists.delete": self.handle_lists_delete,
            "reminders.list": self.handle_reminders_list,
            "reminders.search": self.handle_reminders_search,
            "reminders.info": self.handle_reminders_info,
            "reminders.export": self.handle_reminders_export,
            "reminders.create": self.handle_reminders_create,
            "reminders.update": self.handle_reminders_update,
            "reminders.complete": self.handle_reminders_complete,
            "reminders.uncomplete": self.handle_reminders_uncomplete,
            "reminders.delete": self.handle_reminders_delete,
            "reminders.bulk_update": self.handle_reminders_bulk_update,
            "reminders.bulk_complete": self.handle_reminders_bulk_complete,
            "reminders.bulk_delete": self.handle_reminders_bulk_delete,
            "reminders.bulk_move": self.handle_reminders_bulk_move,
        }
        if set(self.handlers) != set(self.operations):
            missing = sorted(set(self.operations) - set(self.handlers))
            extra = sorted(set(self.handlers) - set(self.operations))
            raise ValueError(f"Bridge operation mapping mismatch. Missing={missing} extra={extra}")

    def handle_http_request(
        self,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, Any]]:
        operation = ""
        request_id: str | None = None
        try:
            self._validate_http_request(method=method, path=path, headers=headers, body=body)
            self._authenticate(headers.get("Authorization", ""))
            envelope = self._parse_json_body(body)
            if isinstance(envelope, dict):
                operation = str(envelope.get("operation", "") or "")
                request_id_value = envelope.get("request_id")
                request_id = request_id_value if isinstance(request_id_value, str) else None
            return HTTPStatus.OK, self.process_envelope(envelope)
        except BridgeError as exc:
            return int(exc.http_status), self._error_response(operation, request_id, exc)
        except Exception:
            unexpected = BridgeError(
                "internal_error",
                "internal bridge error",
                http_status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return int(unexpected.http_status), self._error_response(operation, request_id, unexpected)

    def process_envelope(self, envelope: Any) -> dict[str, Any]:
        if not isinstance(envelope, dict):
            raise SchemaValidationError("request must be a JSON object")

        self.validator.validate(self.contract["request_envelope"], envelope)
        operation = envelope["operation"]
        payload = envelope["payload"]
        dry_run = bool(envelope.get("dry_run", False))
        request_id = envelope.get("request_id")

        payload_model_name = self.operations[operation]["payload_model"]
        payload_model = self.contract["shared_payload_models"][payload_model_name]
        self.validator.validate(payload_model, payload, "payload")
        self._validate_business_rules(operation, payload, dry_run)

        result = self.handlers[operation](payload, dry_run)
        response: dict[str, Any] = {
            "ok": True,
            "operation": operation,
            "result": result,
        }
        if request_id is not None:
            response["request_id"] = request_id
        return response

    def handle_status_get(self, payload: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        del payload, dry_run
        remindctl_version = self._run_text_command(["--version"]).strip()
        authorization = self._run_json_command(["status"])
        return {
            "bridge_version": BRIDGE_VERSION,
            "contract_version": self.contract_version,
            "host": platform.node(),
            "capabilities": sorted(self.operations),
            "remindctl_path": str(self.config.remindctl_path),
            "remindctl_version": remindctl_version,
            "authorization": authorization,
        }

    def handle_status_doctor(self, payload: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        del payload, dry_run
        report = self._run_json_command(["doctor", "--for-agent"])
        return {"doctor": report}

    def handle_lists_list(self, payload: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        del payload, dry_run
        return {"lists": self._list_summaries()}

    def handle_lists_create(self, payload: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        self._reject_unsupported_dry_run("lists.create", dry_run)
        title = canonical_list_title(payload)
        created = self._run_json_command(["list", title, "--create"])
        record = first_array_item(created, "lists.create")
        return {"list": record}

    def handle_lists_update(self, payload: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        self._reject_unsupported_dry_run("lists.update", dry_run)
        target = self._resolve_list_summary(payload)
        args = ["list"]
        if payload.get("list"):
            args.append(payload["list"])
        elif payload.get("list_id"):
            args.extend(["--list-id", payload["list_id"]])
        args.extend(["--rename", payload["rename"]])
        self._run_empty_command(args)
        updated = self._resolve_list_summary({"list_id": target["id"]})
        return {"list": updated}

    def handle_lists_delete(self, payload: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        self._reject_unsupported_dry_run("lists.delete", dry_run)
        target = self._resolve_list_summary(payload)
        args = ["list"]
        if payload.get("list"):
            args.append(payload["list"])
        elif payload.get("list_id"):
            args.extend(["--list-id", payload["list_id"]])
        args.append("--delete")
        if payload.get("force"):
            args.append("--force")
        self._run_empty_command(args)
        return {
            "deleted": 1,
            "list": target["title"],
            "list_id": target["id"],
        }

    def handle_reminders_list(self, payload: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        del dry_run
        filters = payload.get("filters", {})
        args = ["show"]
        filter_value = filters.get("filter")
        if filter_value == "date":
            args.append(filters["date"])
        elif filter_value:
            args.append(filter_value)
        self._append_list_filter(args, filters)
        items = self._run_json_command(args)
        return {"items": items}

    def handle_reminders_search(self, payload: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        del dry_run
        args = ["search", payload["query"]]
        if payload.get("completed"):
            args.append("--completed")
        self._append_list_filter(args, payload)
        items = self._run_json_command(args)
        return {"items": items}

    def handle_reminders_info(self, payload: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        del dry_run
        reminder = self._run_json_command(["info", payload["reminder_id"]])
        return {"reminder": reminder}

    def handle_reminders_export(self, payload: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        del dry_run
        export_format = payload["format"]
        args = ["export"]
        self._append_list_filter(args, payload)
        if export_format == "csv":
            args.extend(["--export-format", "csv"])
            content = self._run_text_command(args)
            return {"format": "csv", "content": content}
        args.extend(["--export-format", "json"])
        items = self._run_json_command(args)
        return {"format": "json", "items": items}

    def handle_reminders_create(self, payload: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        self._reject_unsupported_dry_run("reminders.create", dry_run)
        args = ["add", "--title", payload["title"]]
        self._append_creation_fields(args, payload)
        reminder = self._run_json_command(args)
        return {"reminder": reminder}

    def handle_reminders_update(self, payload: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        self._reject_unsupported_dry_run("reminders.update", dry_run)
        args = ["edit", payload["reminder_id"]]
        self._append_patch_fields(args, payload["patch"])
        reminder = self._run_json_command(args)
        return {"reminder": reminder}

    def handle_reminders_complete(self, payload: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        args = ["complete", payload["reminder_id"]]
        if dry_run:
            args.append("--dry-run")
        items = self._run_json_command(args)
        return {"reminder": first_array_item(items, "reminders.complete"), "dry_run": dry_run}

    def handle_reminders_uncomplete(self, payload: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        self._reject_unsupported_dry_run("reminders.uncomplete", dry_run)
        reminder = self._run_json_command(["edit", payload["reminder_id"], "--incomplete"])
        return {"reminder": reminder}

    def handle_reminders_delete(self, payload: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        args = ["delete", payload["reminder_id"]]
        if dry_run:
            args.append("--dry-run")
            items = self._run_json_command(args)
            return {
                "deleted": len(items),
                "dry_run": True,
                "items": items,
            }
        args.append("--force")
        receipt = self._run_json_command(args)
        return {"deleted": int(receipt.get("deleted", 0)), "dry_run": False}

    def handle_reminders_bulk_update(self, payload: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        self._reject_unsupported_dry_run("reminders.bulk_update", dry_run)
        items = [self._update_one_reminder(reminder_id, payload["patch"]) for reminder_id in payload["reminder_ids"]]
        return {"updated": len(items), "items": items}

    def handle_reminders_bulk_complete(self, payload: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        completed = payload.get("completed", True)
        reminder_ids = payload["reminder_ids"]
        if completed:
            args = ["complete", *reminder_ids]
            if dry_run:
                args.append("--dry-run")
            items = self._run_json_command(args)
            return {
                "updated": len(items),
                "completed": True,
                "dry_run": dry_run,
                "items": items,
            }
        self._reject_unsupported_dry_run("reminders.bulk_complete(completed=false)", dry_run)
        items = [self._run_json_command(["edit", reminder_id, "--incomplete"]) for reminder_id in reminder_ids]
        return {
            "updated": len(items),
            "completed": False,
            "dry_run": False,
            "items": items,
        }

    def handle_reminders_bulk_delete(self, payload: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        reminder_ids = payload["reminder_ids"]
        args = ["delete", *reminder_ids]
        if dry_run:
            args.append("--dry-run")
            items = self._run_json_command(args)
            return {
                "deleted": len(items),
                "dry_run": True,
                "items": items,
            }
        args.append("--force")
        receipt = self._run_json_command(args)
        return {
            "deleted": int(receipt.get("deleted", 0)),
            "dry_run": False,
            "reminder_ids": reminder_ids,
        }

    def handle_reminders_bulk_move(self, payload: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        self._reject_unsupported_dry_run("reminders.bulk_move", dry_run)
        patch: dict[str, Any] = {}
        if payload.get("target_list"):
            patch["list"] = payload["target_list"]
        if payload.get("target_list_id"):
            patch["list_id"] = payload["target_list_id"]
        items = [self._update_one_reminder(reminder_id, patch) for reminder_id in payload["reminder_ids"]]
        return {"updated": len(items), "items": items}

    def _update_one_reminder(self, reminder_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        args = ["edit", reminder_id]
        self._append_patch_fields(args, patch)
        result = self._run_json_command(args)
        if not isinstance(result, dict):
            raise BridgeError("upstream_invalid_json", "remindctl returned an unexpected response")
        return result

    def _validate_http_request(self, *, method: str, path: str, headers: dict[str, str], body: bytes) -> None:
        if urlsplit(path).path != self.config.path:
            raise BridgeError("not_found", "unknown endpoint", http_status=HTTPStatus.NOT_FOUND)
        if method != "POST":
            raise BridgeError("method_not_allowed", "POST required", http_status=HTTPStatus.METHOD_NOT_ALLOWED)
        content_type = headers.get("Content-Type", "")
        if not content_type.lower().startswith("application/json"):
            raise BridgeError(
                "unsupported_media_type",
                "Content-Type must be application/json",
                http_status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            )
        if len(body) > self.config.max_body_bytes:
            raise BridgeError(
                "request_too_large",
                "request body too large",
                http_status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )

    def _authenticate(self, authorization_header: str) -> None:
        if not authorization_header.startswith("Bearer "):
            raise BridgeError("unauthorized", "invalid bearer token", http_status=HTTPStatus.UNAUTHORIZED)
        presented = authorization_header[len("Bearer "):]
        if not secrets.compare_digest(presented.encode("utf-8"), self.config.token.encode("utf-8")):
            raise BridgeError("unauthorized", "invalid bearer token", http_status=HTTPStatus.UNAUTHORIZED)

    def _parse_json_body(self, body: bytes) -> Any:
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeError("invalid_json", "request body must be valid UTF-8 JSON") from exc

    def _validate_business_rules(self, operation: str, payload: dict[str, Any], dry_run: bool) -> None:
        if operation == "lists.create":
            title = payload.get("title")
            name = payload.get("name")
            if title and name and title != name:
                raise SchemaValidationError("payload.title and payload.name must match when both are provided")
            self._require_non_empty(canonical_list_title(payload), "payload.title")
        elif operation == "lists.update":
            self._validate_exactly_one(payload, "list", "list_id")
            self._require_non_empty(payload["rename"], "payload.rename")
        elif operation == "lists.delete":
            self._validate_exactly_one(payload, "list", "list_id")
            if not dry_run:
                self._require_delete_force(payload, path="payload")
        elif operation == "reminders.list":
            filters = payload.get("filters", {})
            self._validate_optional_list_target(filters, path="payload.filters")
            self._validate_filter_payload(filters)
        elif operation == "reminders.search":
            self._validate_optional_list_target(payload, path="payload")
            self._require_non_empty(payload["query"], "payload.query")
        elif operation == "reminders.info":
            self._require_non_empty(payload["reminder_id"], "payload.reminder_id")
        elif operation == "reminders.export":
            self._validate_optional_list_target(payload, path="payload")
        elif operation == "reminders.create":
            self._require_non_empty(payload["title"], "payload.title")
            self._validate_optional_list_target(payload, path="payload")
            self._validate_location_trigger(payload.get("location_trigger"), path="payload.location_trigger")
        elif operation == "reminders.update":
            self._require_non_empty(payload["reminder_id"], "payload.reminder_id")
            self._validate_patch(payload["patch"])
        elif operation in {"reminders.complete", "reminders.uncomplete", "reminders.delete"}:
            self._require_non_empty(payload["reminder_id"], "payload.reminder_id")
            if operation == "reminders.delete" and not dry_run:
                self._require_delete_force(payload, path="payload")
        elif operation == "reminders.bulk_update":
            self._validate_ids(payload["reminder_ids"])
            self._validate_patch(payload["patch"])
        elif operation == "reminders.bulk_complete":
            self._validate_ids(payload["reminder_ids"])
        elif operation == "reminders.bulk_delete":
            self._validate_ids(payload["reminder_ids"])
            if not dry_run:
                self._require_delete_force(payload, path="payload")
        elif operation == "reminders.bulk_move":
            self._validate_ids(payload["reminder_ids"])
            self._validate_exactly_one(payload, "target_list", "target_list_id")

    def _validate_patch(self, patch: dict[str, Any]) -> None:
        if not patch:
            raise SchemaValidationError("payload.patch must not be empty")
        self._validate_optional_list_target(patch, path="payload.patch")
        self._validate_flag_value(patch, "no_repeat")
        self._validate_flag_value(patch, "clear_due")
        self._validate_flag_value(patch, "clear_alarm")
        self._validate_flag_value(patch, "clear_url")
        self._validate_flag_value(patch, "complete")
        self._validate_flag_value(patch, "incomplete")
        self._reject_conflict(patch, "due", "clear_due")
        self._reject_conflict(patch, "alarm", "clear_alarm")
        self._reject_conflict(patch, "url", "clear_url")
        self._reject_conflict(patch, "repeat", "no_repeat")
        self._reject_conflict(patch, "complete", "incomplete")
        if all(not self._has_meaningful_value(value) for value in patch.values()):
            raise SchemaValidationError("payload.patch must include at least one change")

    def _validate_filter_payload(self, filters: dict[str, Any]) -> None:
        filter_name = filters.get("filter")
        date_value = filters.get("date")
        if filter_name == "date" and not date_value:
            raise SchemaValidationError("payload.filters.date is required when payload.filters.filter is 'date'")
        if filter_name != "date" and date_value is not None:
            raise SchemaValidationError("payload.filters.date is only allowed when payload.filters.filter is 'date'")

    def _validate_location_trigger(self, trigger: dict[str, Any] | None, *, path: str) -> None:
        if trigger is None:
            return
        self._require_non_empty(trigger["location"], f"{path}.location")

    def _validate_ids(self, reminder_ids: list[str]) -> None:
        for index, reminder_id in enumerate(reminder_ids):
            self._require_non_empty(reminder_id, f"payload.reminder_ids[{index}]")

    def _validate_optional_list_target(self, payload: dict[str, Any], *, path: str) -> None:
        if payload.get("list") and payload.get("list_id"):
            raise SchemaValidationError(f"{path}.list and {path}.list_id are mutually exclusive")

    def _validate_exactly_one(self, payload: dict[str, Any], field_a: str, field_b: str) -> None:
        has_a = bool(payload.get(field_a))
        has_b = bool(payload.get(field_b))
        if has_a == has_b:
            raise SchemaValidationError(f"Exactly one of payload.{field_a} or payload.{field_b} is required")

    def _validate_flag_value(self, payload: dict[str, Any], field: str) -> None:
        if field in payload and payload[field] is not True:
            raise SchemaValidationError(f"payload.patch.{field} must be true when provided")

    def _require_delete_force(self, payload: dict[str, Any], *, path: str) -> None:
        if payload.get("force") is not True:
            raise SchemaValidationError(f"{path}.force must be true for non-dry-run delete operations")

    def _reject_conflict(self, payload: dict[str, Any], value_field: str, flag_field: str) -> None:
        if value_field in payload and payload.get(flag_field):
            raise SchemaValidationError(f"payload.patch.{value_field} conflicts with payload.patch.{flag_field}")

    def _require_non_empty(self, value: str, path: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise SchemaValidationError(f"{path} must be a non-empty string")

    def _reject_unsupported_dry_run(self, operation: str, dry_run: bool) -> None:
        if dry_run:
            raise BridgeError("unsupported_option", f"dry_run is not supported for {operation}")

    def _has_meaningful_value(self, value: Any) -> bool:
        if value is True:
            return True
        if isinstance(value, str):
            return bool(value.strip())
        return value is not None and value is not False

    def _read_contract_version(self) -> str:
        version = self.contract.get("contract_version")
        if not isinstance(version, str) or not version:
            raise ValueError("Missing contract_version in bridge contract")
        return version

    def _list_summaries(self) -> list[dict[str, Any]]:
        result = self._run_json_command(["list"])
        if not isinstance(result, list):
            raise BridgeError("upstream_invalid_json", "remindctl list returned an unexpected response")
        return result

    def _resolve_list_summary(self, payload: dict[str, Any]) -> dict[str, Any]:
        lists = self._list_summaries()
        if payload.get("list"):
            matches = [item for item in lists if item.get("title") == payload["list"]]
        else:
            target_prefix = payload["list_id"]
            matches = [item for item in lists if isinstance(item.get("id"), str) and item["id"].startswith(target_prefix)]
        if not matches:
            raise BridgeError("not_found", "list not found", http_status=HTTPStatus.NOT_FOUND)
        if len(matches) > 1:
            raise BridgeError("ambiguous_target", "list target is ambiguous")
        return matches[0]

    def _append_list_filter(self, args: list[str], payload: dict[str, Any]) -> None:
        if payload.get("list"):
            args.extend(["--list", payload["list"]])
        elif payload.get("list_id"):
            args.extend(["--list-id", payload["list_id"]])

    def _append_creation_fields(self, args: list[str], payload: dict[str, Any]) -> None:
        if payload.get("notes"):
            args.extend(["--notes", payload["notes"]])
        if payload.get("url"):
            args.extend(["--url", payload["url"]])
        if payload.get("due"):
            args.extend(["--due", payload["due"]])
        if payload.get("alarm"):
            args.extend(["--alarm", payload["alarm"]])
        if payload.get("repeat"):
            args.extend(["--repeat", payload["repeat"]])
        if payload.get("priority"):
            args.extend(["--priority", payload["priority"]])
        self._append_list_filter(args, payload)
        trigger = payload.get("location_trigger")
        if trigger:
            args.extend(["--location", trigger["location"]])
            if "radius_meters" in trigger:
                args.extend(["--radius", format_radius(trigger["radius_meters"])])
            if trigger.get("leaving"):
                args.append("--leaving")

    def _append_patch_fields(self, args: list[str], patch: dict[str, Any]) -> None:
        if patch.get("title"):
            args.extend(["--title", patch["title"]])
        if patch.get("notes"):
            args.extend(["--notes", patch["notes"]])
        if patch.get("url"):
            args.extend(["--url", patch["url"]])
        if patch.get("due"):
            args.extend(["--due", patch["due"]])
        if patch.get("alarm"):
            args.extend(["--alarm", patch["alarm"]])
        if patch.get("repeat"):
            args.extend(["--repeat", patch["repeat"]])
        if patch.get("priority"):
            args.extend(["--priority", patch["priority"]])
        self._append_list_filter(args, patch)
        if patch.get("no_repeat"):
            args.append("--no-repeat")
        if patch.get("clear_due"):
            args.append("--clear-due")
        if patch.get("clear_alarm"):
            args.append("--clear-alarm")
        if patch.get("clear_url"):
            args.append("--clear-url")
        if patch.get("complete"):
            args.append("--complete")
        if patch.get("incomplete"):
            args.append("--incomplete")

    def _run_json_command(self, args: list[str]) -> Any:
        payload = self._run_command(args, mode="json")
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeError(
                "upstream_invalid_json",
                "remindctl returned invalid JSON",
                http_status=HTTPStatus.BAD_GATEWAY,
                details={"command": safe_command_name(args)},
            ) from exc

    def _run_text_command(self, args: list[str]) -> str:
        payload = self._run_command(args, mode="text")
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BridgeError(
                "upstream_invalid_output",
                "remindctl returned non-UTF-8 output",
                http_status=HTTPStatus.BAD_GATEWAY,
                details={"command": safe_command_name(args)},
            ) from exc

    def _run_empty_command(self, args: list[str]) -> None:
        self._run_command(args, mode="empty")

    def _run_command(self, args: list[str], *, mode: str) -> bytes:
        argv = [str(self.config.remindctl_path), *args, "--no-color", "--no-input"]
        if mode == "json":
            argv.append("--json")
        with self.command_lock:
            result = self.runner(
                argv,
                self.config.timeout_seconds,
                build_sanitized_env(self.config.remindctl_path),
                self.config.max_stdout_bytes,
                self.config.max_stderr_bytes,
            )

        if len(result.stdout) > self.config.max_stdout_bytes:
            raise BridgeError(
                "output_limit",
                "remindctl stdout exceeded the bridge limit",
                http_status=HTTPStatus.BAD_GATEWAY,
                details={"command": safe_command_name(args)},
            )
        if len(result.stderr) > self.config.max_stderr_bytes:
            raise BridgeError(
                "output_limit",
                "remindctl stderr exceeded the bridge limit",
                http_status=HTTPStatus.BAD_GATEWAY,
                details={"command": safe_command_name(args)},
            )
        if result.returncode != 0:
            raise BridgeError(
                "upstream_command_failed",
                "remindctl command failed",
                http_status=HTTPStatus.BAD_GATEWAY,
                details={
                    "command": safe_command_name(args),
                    "exit_code": result.returncode,
                    "stderr_present": bool(result.stderr.strip()),
                },
            )
        if mode == "empty":
            return b""
        return result.stdout

    def _error_response(self, operation: str, request_id: str | None, exc: BridgeError) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "operation": operation,
            "error": {
                "code": exc.code,
                "message": exc.message,
            },
        }
        if request_id is not None:
            payload["request_id"] = request_id
        if exc.details:
            payload["error"]["details"] = exc.details
        return payload


def safe_command_name(args: list[str]) -> str:
    return args[0] if args else "remindctl"


def canonical_list_title(payload: dict[str, Any]) -> str:
    return str(payload.get("title") or payload.get("name") or "")


def first_array_item(value: Any, operation: str) -> dict[str, Any]:
    if not isinstance(value, list) or not value or not isinstance(value[0], dict):
        raise BridgeError(
            "upstream_invalid_json",
            f"remindctl returned an unexpected response for {operation}",
            http_status=HTTPStatus.BAD_GATEWAY,
        )
    return value[0]


def format_radius(value: float) -> str:
    if isinstance(value, int) or value.is_integer():
        return str(int(value))
    return format(value, "g")


def build_sanitized_env(remindctl_path: Path) -> dict[str, str]:
    home = os.path.expanduser("~")
    path_entries = [str(remindctl_path.parent)] + SAFE_PATH.split(":")
    deduped_path = ":".join(dict.fromkeys(path_entries))
    return {
        "HOME": home,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": deduped_path,
    }


def load_contract() -> dict[str, Any]:
    contract_path = Path(__file__).resolve().parent.parent / "shared" / "bridge-operations.json"
    with contract_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def parse_args(argv: list[str]) -> ServerConfig:
    parser = argparse.ArgumentParser(
        description="Run the Apple Reminders macOS host bridge for tlh.",
        epilog=(
            "Prefer --token-file so the bearer token does not appear in your shell history or process list. "
            "Grant Reminders access to remindctl first with `remindctl authorize`, then verify with "
            "`remindctl doctor --for-agent --json` before running this server headlessly."
        ),
    )
    parser.add_argument("--bind", default=DEFAULT_BIND, help=f"Bind address (default: {DEFAULT_BIND})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"TCP port (default: {DEFAULT_PORT})")
    parser.add_argument("--path", default=DEFAULT_PATH, help=f"Exact POST path to serve (default: {DEFAULT_PATH})")
    parser.add_argument(
        "--token",
        help="Bearer token for incoming requests. Prefer --token-file instead to avoid exposing it in argv.",
    )
    parser.add_argument("--token-file", help="Read the bearer token from this file.")
    parser.add_argument(
        "--remindctl-path",
        default="/opt/homebrew/bin/remindctl",
        help="Absolute path to remindctl (default: /opt/homebrew/bin/remindctl)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Per-command timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS})",
    )
    args = parser.parse_args(argv)

    token = resolve_token(args.token, args.token_file)
    path = normalize_path(args.path)
    remindctl_path = resolve_remindctl_path(args.remindctl_path)

    bridge = BridgeConfig(
        path=path,
        token=token,
        remindctl_path=remindctl_path,
        timeout_seconds=args.timeout_seconds,
    )
    return ServerConfig(bind=args.bind, port=args.port, bridge=bridge)


def resolve_token(token: str | None, token_file: str | None) -> str:
    if token and token_file:
        raise SystemExit("Use either --token or --token-file, not both")
    if token_file:
        content = Path(token_file).read_text(encoding="utf-8").strip()
        if not content:
            raise SystemExit("Token file is empty")
        return content
    if token:
        if not token.strip():
            raise SystemExit("Token must not be empty")
        return token
    env_token = os.environ.get("APPLE_REMINDERS_BRIDGE_TOKEN", "").strip()
    if env_token:
        return env_token
    raise SystemExit("Provide --token, --token-file, or APPLE_REMINDERS_BRIDGE_TOKEN")


def normalize_path(path: str) -> str:
    if not path:
        raise SystemExit("--path must not be empty")
    normalized = path if path.startswith("/") else f"/{path}"
    if normalized != "/" and normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    return normalized or "/"


def resolve_remindctl_path(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        raise SystemExit("--remindctl-path must be absolute")
    if not path.exists():
        raise SystemExit(f"remindctl not found at {path}")
    if not os.access(path, os.X_OK):
        raise SystemExit(f"remindctl is not executable: {path}")
    return path.resolve()


class BridgeHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler], app: BridgeApp):
        super().__init__(server_address, handler_class)
        self.app = app


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server: BridgeHTTPServer

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        del format, args

    def _handle(self) -> None:
        body = self._read_request_body()
        status_code, payload = self.server.app.handle_http_request(
            method=self.command,
            path=self.path,
            headers={key: value for key, value in self.headers.items()},
            body=body,
        )
        response = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status_code)
        if status_code == HTTPStatus.METHOD_NOT_ALLOWED:
            self.send_header("Allow", "POST")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def _read_request_body(self) -> bytes:
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            return b""
        try:
            expected = int(content_length)
        except ValueError:
            expected = 0
        if expected <= 0:
            return b""
        max_read = self.server.app.config.max_body_bytes + 1
        return self.rfile.read(min(expected, max_read))


def make_app(config: BridgeConfig, runner: Runner | None = None) -> BridgeApp:
    return BridgeApp(config=config, contract=load_contract(), runner=runner)


def main(argv: list[str] | None = None) -> int:
    server_config = parse_args(argv or sys.argv[1:])
    app = make_app(server_config.bridge)
    server = BridgeHTTPServer((server_config.bind, server_config.port), BridgeRequestHandler, app)
    try:
        print(
            f"Listening on http://{server.server_address[0]}:{server.server_address[1]}{server_config.bridge.path}",
            file=sys.stderr,
        )
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
