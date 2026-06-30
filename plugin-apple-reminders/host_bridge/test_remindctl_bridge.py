from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

MODULE_PATH = Path(__file__).resolve().parent / "remindctl_bridge.py"
SPEC = importlib.util.spec_from_file_location("apple_reminders_bridge", MODULE_PATH)
bridge = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.lists = [
            {"id": "L1", "title": "Work", "reminderCount": 1, "overdueCount": 0},
            {"id": "L2", "title": "Home", "reminderCount": 1, "overdueCount": 0},
        ]
        self.reminders = [
            {
                "id": "R1",
                "title": "Buy milk",
                "listID": "L2",
                "listName": "Home",
                "isCompleted": False,
                "priority": "medium",
                "dueDate": "2026-07-01T09:00:00Z",
                "alarmDate": None,
                "notes": "2%",
                "url": None,
                "recurrenceRule": None,
            },
            {
                "id": "R2",
                "title": "Draft report",
                "listID": "L1",
                "listName": "Work",
                "isCompleted": True,
                "priority": "high",
                "dueDate": "2026-07-02T09:00:00Z",
                "alarmDate": None,
                "notes": "Quarterly",
                "url": "https://example.test/report",
                "recurrenceRule": "weekly",
            },
        ]
        self.next_list_id = 3
        self.next_reminder_id = 3

    def __call__(
        self,
        argv: list[str],
        timeout_seconds: float,
        env: dict[str, str],
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> bridge.CommandResult:
        del timeout_seconds, max_stdout_bytes, max_stderr_bytes
        self.calls.append((argv, env))
        self._assert_sanitized_env(env)
        command = argv[1]
        args = self._strip_runtime_flags(argv[2:])
        if command == "version":
            raise AssertionError("status.get must use remindctl --version, not a version subcommand")
        if command == "--version":
            return self._text("9.9.9\n")
        if command == "status":
            return self._json({"authorized": True, "status": "full-access"})
        if command == "doctor":
            return self._json(
                {
                    "authorization": {"authorized": True, "status": "full-access"},
                    "agentNotes": ["Permission already granted"],
                    "richRead": {"readable": True, "tableCounts": {"sections": 2}},
                }
            )
        if command == "list":
            return self._handle_list(args)
        if command == "show":
            return self._json(self._filter_show(args))
        if command == "search":
            return self._json(self._search(args))
        if command == "info":
            return self._json(self._resolve_reminder(args[0]))
        if command == "export":
            return self._handle_export(args)
        if command == "add":
            return self._handle_add(args)
        if command == "edit":
            return self._handle_edit(args)
        if command == "complete":
            return self._handle_complete(args)
        if command == "delete":
            return self._handle_delete(args)
        raise AssertionError(f"Unexpected command: {argv}")

    def _assert_sanitized_env(self, env: dict[str, str]) -> None:
        self._ensure_keys(env, {"HOME", "LANG", "LC_ALL", "PATH"})

    def _handle_list(self, args: list[str]) -> bridge.CommandResult:
        names, options, flags = self._parse_args(args)
        if "create" in flags:
            name = names[0]
            created = {"id": f"L{self.next_list_id}", "title": name, "reminderCount": 0, "overdueCount": 0}
            self.next_list_id += 1
            self.lists.append(created)
            return self._json([created])
        target = self._resolve_list(names[0] if names else None, options.get("list-id")) if names or options.get("list-id") else None
        if "rename" in options:
            assert target is not None
            target["title"] = options["rename"]
            for reminder in self.reminders:
                if reminder["listID"] == target["id"]:
                    reminder["listName"] = target["title"]
            return self._text("")
        if "delete" in flags:
            assert target is not None
            self.lists = [item for item in self.lists if item["id"] != target["id"]]
            self.reminders = [item for item in self.reminders if item["listID"] != target["id"]]
            return self._text("")
        return self._json(self.lists)

    def _handle_export(self, args: list[str]) -> bridge.CommandResult:
        _, options, _ = self._parse_args(args)
        reminders = self._filter_by_list(self.reminders, options.get("list"), options.get("list-id"))
        export_format = options.get("export-format", "json")
        if export_format == "csv":
            rows = ["id,title,list,completed,priority,dueDate,notes,url"]
            for reminder in reminders:
                rows.append(
                    ",".join(
                        [
                            reminder["id"],
                            reminder["title"],
                            reminder["listName"],
                            "1" if reminder["isCompleted"] else "0",
                            reminder["priority"],
                            reminder["dueDate"] or "",
                            reminder["notes"] or "",
                            reminder["url"] or "",
                        ]
                    )
                )
            return self._text("\n".join(rows) + "\n")
        return self._json(reminders)

    def _handle_add(self, args: list[str]) -> bridge.CommandResult:
        _, options, flags = self._parse_args(args)
        target = self._resolve_list(options.get("list"), options.get("list-id")) or self.lists[0]
        reminder = {
            "id": f"R{self.next_reminder_id}",
            "title": options["title"],
            "listID": target["id"],
            "listName": target["title"],
            "isCompleted": False,
            "priority": options.get("priority", "none"),
            "dueDate": options.get("due"),
            "alarmDate": options.get("alarm"),
            "notes": options.get("notes"),
            "url": options.get("url"),
            "recurrenceRule": options.get("repeat"),
        }
        if "location" in options:
            reminder["locationTrigger"] = {
                "address": options["location"],
                "radius": float(options.get("radius", "100")),
                "proximity": "leaving" if "leaving" in flags else "arriving",
            }
        self.next_reminder_id += 1
        self.reminders.append(reminder)
        return self._json(reminder)

    def _handle_edit(self, args: list[str]) -> bridge.CommandResult:
        reminder_id = args[0]
        _, options, flags = self._parse_args(args[1:])
        reminder = self._resolve_reminder(reminder_id)
        if "title" in options:
            reminder["title"] = options["title"]
        if "list" in options or "list-id" in options:
            target = self._resolve_list(options.get("list"), options.get("list-id"))
            assert target is not None
            reminder["listID"] = target["id"]
            reminder["listName"] = target["title"]
        if "due" in options:
            reminder["dueDate"] = options["due"]
        if "alarm" in options:
            reminder["alarmDate"] = options["alarm"]
        if "notes" in options:
            reminder["notes"] = options["notes"]
        if "url" in options:
            reminder["url"] = options["url"]
        if "repeat" in options:
            reminder["recurrenceRule"] = options["repeat"]
        if "priority" in options:
            reminder["priority"] = options["priority"]
        if "clear-due" in flags:
            reminder["dueDate"] = None
        if "clear-alarm" in flags:
            reminder["alarmDate"] = None
        if "clear-url" in flags:
            reminder["url"] = None
        if "no-repeat" in flags:
            reminder["recurrenceRule"] = None
        if "complete" in flags:
            reminder["isCompleted"] = True
        if "incomplete" in flags:
            reminder["isCompleted"] = False
        return self._json(reminder)

    def _handle_complete(self, args: list[str]) -> bridge.CommandResult:
        ids, _, flags = self._parse_args(args)
        reminders = [self._resolve_reminder(item) for item in ids]
        if "dry-run" not in flags:
            for reminder in reminders:
                reminder["isCompleted"] = True
        return self._json(reminders)

    def _handle_delete(self, args: list[str]) -> bridge.CommandResult:
        ids, _, flags = self._parse_args(args)
        reminders = [self._resolve_reminder(item) for item in ids]
        if "dry-run" in flags:
            return self._json(reminders)
        deleted_ids = {item["id"] for item in reminders}
        self.reminders = [item for item in self.reminders if item["id"] not in deleted_ids]
        return self._json({"deleted": len(deleted_ids)})

    def _filter_show(self, args: list[str]) -> list[dict[str, object]]:
        positional, options, _ = self._parse_args(args)
        reminders = self._filter_by_list(self.reminders, options.get("list"), options.get("list-id"))
        if not positional:
            return [item for item in reminders if not item["isCompleted"]]
        filter_value = positional[0]
        if filter_value == "completed":
            return [item for item in reminders if item["isCompleted"]]
        if filter_value == "all":
            return reminders
        return [item for item in reminders if item.get("dueDate", "").startswith(filter_value)]

    def _search(self, args: list[str]) -> list[dict[str, object]]:
        positional, options, _ = self._parse_args(args)
        query = positional[0].lower()
        reminders = self._filter_by_list(self.reminders, options.get("list"), options.get("list-id"))
        if "completed" not in options:
            reminders = [item for item in reminders if not item["isCompleted"]]
        matches = []
        for reminder in reminders:
            haystack = " ".join(
                [
                    reminder["title"],
                    reminder.get("notes") or "",
                    reminder.get("url") or "",
                ]
            ).lower()
            if query in haystack:
                matches.append(reminder)
        return matches

    def _resolve_list(self, name: str | None, list_id: str | None) -> dict[str, object] | None:
        if name is None and list_id is None:
            return None
        matches = self._filter_by_list(self.lists, name, list_id)
        assert len(matches) == 1
        return matches[0]

    def _resolve_reminder(self, prefix: str) -> dict[str, object]:
        matches = [item for item in self.reminders if item["id"].startswith(prefix)]
        assert len(matches) == 1
        return matches[0]

    def _filter_by_list(self, items: list[dict[str, object]], name: str | None, list_id: str | None) -> list[dict[str, object]]:
        if name:
            return [
                item
                for item in items
                if item.get("listName") == name or item.get("title") == name
            ]
        if list_id:
            return [
                item
                for item in items
                if str(item["listID"] if "listID" in item else item["id"]).startswith(list_id)
            ]
        return list(items)

    def _parse_args(self, args: list[str]) -> tuple[list[str], dict[str, str], set[str]]:
        positional: list[str] = []
        options: dict[str, str] = {}
        flags: set[str] = set()
        index = 0
        while index < len(args):
            item = args[index]
            if item.startswith("--"):
                name = item[2:]
                if index + 1 < len(args) and not args[index + 1].startswith("--") and name not in {"delete", "create", "force", "json", "no-color", "no-input", "dry-run", "leaving", "complete", "incomplete", "clear-due", "clear-alarm", "clear-url", "no-repeat", "completed", "for-agent"}:
                    options[name] = args[index + 1]
                    index += 2
                else:
                    flags.add(name)
                    if name == "completed":
                        options[name] = "1"
                    index += 1
            else:
                positional.append(item)
                index += 1
        return positional, options, flags

    def _strip_runtime_flags(self, args: list[str]) -> list[str]:
        return [item for item in args if item not in {"--json", "--no-color", "--no-input"}]

    def _json(self, payload: object) -> bridge.CommandResult:
        return bridge.CommandResult(0, json.dumps(payload).encode("utf-8"), b"")

    def _text(self, payload: str) -> bridge.CommandResult:
        return bridge.CommandResult(0, payload.encode("utf-8"), b"")

    def _ensure_keys(self, env: dict[str, str], expected: set[str]) -> None:
        assert set(env) == expected, env

    def last_call(self, command: str) -> list[str]:
        for argv, _env in reversed(self.calls):
            if len(argv) > 1 and argv[1] == command:
                return argv
        raise AssertionError(f"Command not called: {command}")


class BridgeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        fake_executable = Path(self.tempdir.name) / "remindctl"
        fake_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_executable.chmod(0o755)
        self.runner = FakeRunner()
        self.config = bridge.BridgeConfig(
            path="/bridge",
            token="test-token",
            remindctl_path=fake_executable,
        )
        self.app = bridge.make_app(self.config, runner=self.runner)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_http_status_get_requires_auth_and_returns_bridge_status(self) -> None:
        server = bridge.BridgeHTTPServer(("127.0.0.1", 0), bridge.BridgeRequestHandler, self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/bridge"
            request = Request(
                url,
                data=json.dumps(
                    {
                        "contract_version": "2026-06-30",
                        "operation": "status.get",
                        "payload": {},
                        "request_id": "req-1",
                    }
                ).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer test-token",
                },
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["request_id"], "req-1")
            self.assertEqual(payload["result"]["remindctl_version"], "9.9.9")
            self.assertIn("status.doctor", payload["result"]["capabilities"])
            self.assertEqual(self.runner.calls[0][0][1], "--version")

            bad_request = Request(
                url,
                data=b"{}",
                headers={"Content-Type": "application/json", "Authorization": "Bearer wrong"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as exc_info:
                urlopen(bad_request, timeout=5)
            self.assertEqual(exc_info.exception.code, 401)
            exc_info.exception.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_request_body_read_is_capped_at_bridge_limit_plus_one(self) -> None:
        class ReadRecorder:
            def __init__(self) -> None:
                self.read_size: int | None = None

            def read(self, size: int) -> bytes:
                self.read_size = size
                return b"x" * size

        config = bridge.BridgeConfig(
            path="/bridge",
            token="test-token",
            remindctl_path=self.config.remindctl_path,
            max_body_bytes=7,
        )
        app = bridge.make_app(config, runner=self.runner)
        handler = object.__new__(bridge.BridgeRequestHandler)
        handler.headers = {"Content-Length": "999999"}
        handler.server = type("DummyServer", (), {"app": app})()
        handler.rfile = ReadRecorder()

        body = handler._read_request_body()

        self.assertEqual(handler.rfile.read_size, 8)
        self.assertEqual(len(body), 8)

    def test_default_runner_caps_streams_during_collection(self) -> None:
        for stream_name in ("stdout", "stderr"):
            with self.subTest(stream=stream_name):
                writer = "sys.stdout.buffer" if stream_name == "stdout" else "sys.stderr.buffer"
                script = (
                    "import sys, time; "
                    f"{writer}.write(b'x' * 131072); "
                    f"{writer}.flush(); "
                    "time.sleep(5)"
                )
                started = time.monotonic()
                result = bridge.default_runner(
                    [sys.executable, "-c", script],
                    timeout_seconds=5,
                    env={"PATH": os.environ.get("PATH", "")},
                    max_stdout_bytes=1024,
                    max_stderr_bytes=1024,
                )
                elapsed = time.monotonic() - started

                self.assertLess(elapsed, 4)
                self.assertNotEqual(result.returncode, 0)
                if stream_name == "stdout":
                    self.assertEqual(len(result.stdout), 1025)
                    self.assertEqual(result.stderr, b"")
                else:
                    self.assertEqual(result.stdout, b"")
                    self.assertEqual(len(result.stderr), 1025)

    def test_rejects_unknown_top_level_and_payload_fields(self) -> None:
        with self.assertRaises(bridge.SchemaValidationError):
            self.app.process_envelope(
                {
                    "contract_version": "2026-06-30",
                    "operation": "status.get",
                    "payload": {},
                    "extra": True,
                }
            )
        with self.assertRaises(bridge.SchemaValidationError):
            self.app.process_envelope(
                {
                    "contract_version": "2026-06-30",
                    "operation": "reminders.update",
                    "payload": {"reminder_id": "R1", "patch": {"location_trigger": {"location": "x"}}},
                }
            )
        with self.assertRaises(bridge.SchemaValidationError):
            self.app.process_envelope(
                {
                    "contract_version": "2026-06-30",
                    "operation": "reminders.bulk_update",
                    "payload": {"reminder_ids": ["R1", ""], "patch": {"priority": "low"}},
                }
            )

    def test_doctor_and_reminder_info_are_allowlisted(self) -> None:
        doctor = self.app.process_envelope(
            {
                "contract_version": "2026-06-30",
                "operation": "status.doctor",
                "payload": {},
            }
        )
        info = self.app.process_envelope(
            {
                "contract_version": "2026-06-30",
                "operation": "reminders.info",
                "payload": {"reminder_id": "R1"},
            }
        )
        self.assertEqual(doctor["result"]["doctor"]["agentNotes"], ["Permission already granted"])
        self.assertEqual(info["result"]["reminder"]["id"], "R1")

    def test_export_json_and_csv_are_wrapped_in_bridge_results(self) -> None:
        json_result = self.app.process_envelope(
            {
                "contract_version": "2026-06-30",
                "operation": "reminders.export",
                "payload": {"format": "json", "list": "Home"},
            }
        )
        csv_result = self.app.process_envelope(
            {
                "contract_version": "2026-06-30",
                "operation": "reminders.export",
                "payload": {"format": "csv", "list_id": "L2"},
            }
        )
        self.assertEqual(json_result["result"]["format"], "json")
        self.assertEqual(len(json_result["result"]["items"]), 1)
        self.assertEqual(csv_result["result"]["format"], "csv")
        self.assertIn("id,title,list", csv_result["result"]["content"])

    def test_create_update_complete_uncomplete_and_delete_reminders(self) -> None:
        created = self.app.process_envelope(
            {
                "contract_version": "2026-06-30",
                "operation": "reminders.create",
                "payload": {
                    "title": "Check mailbox",
                    "list": "Home",
                    "repeat": "daily",
                    "location_trigger": {"location": "1 Apple Park Way", "leaving": True, "radius_meters": 150},
                },
            }
        )
        reminder_id = created["result"]["reminder"]["id"]
        updated = self.app.process_envelope(
            {
                "contract_version": "2026-06-30",
                "operation": "reminders.update",
                "payload": {
                    "reminder_id": reminder_id,
                    "patch": {"title": "Check mailbox now", "clear_url": True, "incomplete": True},
                },
            }
        )
        completed = self.app.process_envelope(
            {
                "contract_version": "2026-06-30",
                "operation": "reminders.complete",
                "payload": {"reminder_id": reminder_id},
                "dry_run": True,
            }
        )
        uncompleted = self.app.process_envelope(
            {
                "contract_version": "2026-06-30",
                "operation": "reminders.uncomplete",
                "payload": {"reminder_id": reminder_id},
            }
        )
        deleted = self.app.process_envelope(
            {
                "contract_version": "2026-06-30",
                "operation": "reminders.delete",
                "payload": {"reminder_id": reminder_id, "force": True},
            }
        )
        self.assertEqual(updated["result"]["reminder"]["title"], "Check mailbox now")
        self.assertTrue(completed["result"]["dry_run"])
        self.assertFalse(uncompleted["result"]["reminder"]["isCompleted"])
        self.assertEqual(deleted["result"]["deleted"], 1)

    def test_delete_operations_require_force_for_non_dry_run_and_pass_force_flag(self) -> None:
        with self.assertRaisesRegex(bridge.SchemaValidationError, "payload.force must be true"):
            self.app.process_envelope(
                {
                    "contract_version": "2026-06-30",
                    "operation": "lists.delete",
                    "payload": {"list": "Home"},
                }
            )
        with self.assertRaisesRegex(bridge.SchemaValidationError, "payload.force must be true"):
            self.app.process_envelope(
                {
                    "contract_version": "2026-06-30",
                    "operation": "reminders.delete",
                    "payload": {"reminder_id": "R1"},
                }
            )
        with self.assertRaisesRegex(bridge.SchemaValidationError, "payload.force must be true"):
            self.app.process_envelope(
                {
                    "contract_version": "2026-06-30",
                    "operation": "reminders.bulk_delete",
                    "payload": {"reminder_ids": ["R1", "R2"]},
                }
            )

        deleted_reminder = self.app.process_envelope(
            {
                "contract_version": "2026-06-30",
                "operation": "reminders.delete",
                "payload": {"reminder_id": "R1", "force": True},
            }
        )
        bulk_deleted = self.app.process_envelope(
            {
                "contract_version": "2026-06-30",
                "operation": "reminders.bulk_delete",
                "payload": {"reminder_ids": ["R2"], "force": True},
            }
        )
        deleted_list = self.app.process_envelope(
            {
                "contract_version": "2026-06-30",
                "operation": "lists.delete",
                "payload": {"list": "Home", "force": True},
            }
        )

        self.assertEqual(deleted_reminder["result"]["deleted"], 1)
        self.assertEqual(bulk_deleted["result"]["deleted"], 1)
        self.assertEqual(deleted_list["result"]["deleted"], 1)
        self.assertIn("--force", self.runner.last_call("list"))
        self.assertIn("--force", self.runner.last_call("delete"))

    def test_list_and_bulk_mutations_work_without_real_reminders_access(self) -> None:
        created_list = self.app.process_envelope(
            {
                "contract_version": "2026-06-30",
                "operation": "lists.create",
                "payload": {"title": "Errands"},
            }
        )
        renamed_list = self.app.process_envelope(
            {
                "contract_version": "2026-06-30",
                "operation": "lists.update",
                "payload": {"list": "Errands", "rename": "Weekend"},
            }
        )
        moved = self.app.process_envelope(
            {
                "contract_version": "2026-06-30",
                "operation": "reminders.bulk_move",
                "payload": {"reminder_ids": ["R1", "R2"], "target_list": "Weekend"},
            }
        )
        bulk_updated = self.app.process_envelope(
            {
                "contract_version": "2026-06-30",
                "operation": "reminders.bulk_update",
                "payload": {"reminder_ids": ["R1", "R2"], "patch": {"priority": "low", "no_repeat": True}},
            }
        )
        bulk_completed = self.app.process_envelope(
            {
                "contract_version": "2026-06-30",
                "operation": "reminders.bulk_complete",
                "payload": {"reminder_ids": ["R1", "R2"], "completed": False},
            }
        )
        bulk_deleted_preview = self.app.process_envelope(
            {
                "contract_version": "2026-06-30",
                "operation": "reminders.bulk_delete",
                "payload": {"reminder_ids": ["R1", "R2"]},
                "dry_run": True,
            }
        )
        deleted_list = self.app.process_envelope(
            {
                "contract_version": "2026-06-30",
                "operation": "lists.delete",
                "payload": {"list_id": created_list["result"]["list"]["id"], "force": True},
            }
        )
        self.assertEqual(renamed_list["result"]["list"]["title"], "Weekend")
        self.assertEqual(moved["result"]["updated"], 2)
        self.assertEqual(bulk_updated["result"]["items"][0]["priority"], "low")
        self.assertFalse(bulk_completed["result"]["completed"])
        self.assertTrue(bulk_deleted_preview["result"]["dry_run"])
        self.assertEqual(deleted_list["result"]["deleted"], 1)


if __name__ == "__main__":
    unittest.main()
