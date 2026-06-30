# Apple Reminders host bridge contract

This file defines the initial allowlisted API surface between future Docker-side Stavrobot tools and a macOS-side `remindctl` bridge.

## Goals

- Keep the plugin root self-contained while bridge and tool tickets land later.
- Model `remindctl` capabilities rather than generic Apple Reminders/EventKit features.
- Allow only explicit Apple Reminders operations.
- Forbid arbitrary host command execution, shell passthrough, or general filesystem access.

## Transport

- Endpoint: `POST` to the full HTTP or HTTPS URL stored in plugin config key `bridge_url`
- Authentication: `Authorization: Bearer <bridge_token>` using plugin config key `bridge_token`
- Content type: `application/json`
- Contract version: `2026-06-30`

`bridge_url` is expected to point at the concrete bridge endpoint. Future tools should not synthesize extra paths on their own. Local Docker Desktop setups may use an `http://` localhost or host-gateway URL.

## Request envelope

```json
{
  "contract_version": "2026-06-30",
  "operation": "status.get",
  "payload": {},
  "request_id": "optional-caller-generated-id",
  "dry_run": false
}
```

Rules:

- `operation` must be one of the allowlisted names in `shared/bridge-operations.json`.
- `payload` must match the shared model for that operation.
- `request_id` is optional and should be echoed by the bridge when present.
- `dry_run` is optional. The host bridge supports preview behavior for `reminders.complete`, `reminders.delete`, `reminders.bulk_complete` when `completed=true`, and `reminders.bulk_delete`. Read-only operations ignore it and other mutations reject it.
- Unknown top-level fields should be rejected.

## Response envelope

Success shape:

```json
{
  "ok": true,
  "operation": "status.get",
  "request_id": "optional-caller-generated-id",
  "result": {
    "bridge_version": "0.0.0",
    "host": "macos-host",
    "capabilities": ["status.get"]
  }
}
```

Failure shape:

```json
{
  "ok": false,
  "operation": "reminders.create",
  "request_id": "optional-caller-generated-id",
  "error": {
    "code": "validation_error",
    "message": "title is required"
  }
}
```

Rules:

- `ok` is always present.
- Exactly one of `result` or `error` should be present.
- `error.code` should be stable and machine-readable.
- Failures must never expose secrets.

## Allowlisted operations

The bridge must expose only the following operation families:

| Family | Operations |
| --- | --- |
| status | `status.get`, `status.doctor` |
| list read/mutation | `lists.list`, `lists.create`, `lists.update`, `lists.delete` |
| reminder read/search/export | `reminders.list`, `reminders.search`, `reminders.info`, `reminders.export` |
| reminder mutation | `reminders.create`, `reminders.update`, `reminders.complete`, `reminders.uncomplete`, `reminders.delete` |
| recurrence and location triggers | modeled through remindctl-style `repeat`, `no_repeat`, and create-time `location_trigger` payload fields |
| bulk operations | `reminders.bulk_update`, `reminders.bulk_complete`, `reminders.bulk_delete`, `reminders.bulk_move` with explicit reminder id lists only |

The machine-readable source of truth is `shared/bridge-operations.json`.

## Shared payload model expectations

Future implementations should reuse these shared objects from `shared/bridge-operations.json`:

- `recurrence_rule`: a remindctl repeat string: `daily`, `weekly`, `biweekly`, `monthly`, `yearly`, or `every N days/weeks/months/years`
- `location_trigger`: remindctl create-time location arguments with a location address string plus optional `leaving` and `radius_meters`
- `reminder_patch`: mutable remindctl-supported reminder fields including title, notes, URL, due, alarm, repeat/no-repeat, priority, list/list_id, complete/incomplete, and clear flags
- `reminder_filters`: remindctl-style read filters: today, tomorrow, week, overdue, upcoming, open, completed, all, or date, with optional list/list_id narrowing
- `reminder_export_query`: remindctl export format plus optional list/list_id narrowing; export does not support today/open/date-style filters
- `bulk_id_selection`: explicit reminder ids for bulk actions; destructive bulk operations must not be filter-based

## Safety boundaries

The bridge contract explicitly excludes:

- arbitrary host command execution
- shell fragments or free-form scripts in request payloads
- raw EventKit object introspection
- unrestricted file reads or writes
- dynamic operations outside the allowlisted catalog
- unsupported generic Apple Reminders fields outside the remindctl-backed models in `shared/bridge-operations.json`

Unknown operations or payloads outside the documented models should fail closed.
