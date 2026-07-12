# Phase 2 controller

The controller is a root-owned, systemd-managed local control boundary. Its only purpose is to map a catalog template and instance ID to an allowlisted service unit. It never accepts a shell command, image reference, container ID, Compose filename, volume path, systemd unit name, or Docker API request from a caller.

## Install

From the repository root, install the controller once the source is reviewed:

    sudo ./scripts/install-phase2.sh

The installer validates the catalog and then creates the `game-interface-api` system user, installs root-owned executable files under `/usr/local/libexec/game-server-interface/`, creates root-only state and audit directories, enables `game-server-interface-controller.service`, and marks the audit file append-only when `chattr` is available.

The service listens only on `/run/game-server-interface/controller.sock`. The directory is `root:game-interface-api` mode `0750`, and the socket is `root:game-interface-api` mode `0660`. The controller authenticates the connecting process with Linux `SO_PEERCRED` and rejects every UID other than `game-interface-api`. Phase 3 must run its API process as this account; no web container receives Docker-socket access.

## Protocol

The socket protocol is one JSON object per line, with a maximum request size of 16 KiB. Responses are one JSON object per line. The supported actions are:

| Action | Required fields | Behavior |
| --- | --- | --- |
| `list_catalog` | none | Returns the safe public template summary. |
| `list_instances` | none | Returns registered instance metadata only. |
| `register_instance` | `template_id`, `instance_id` | Registers a predeclared catalog slot and derives all names and paths. |
| `status` | `template_id`, `instance_id` | Returns `systemd` lifecycle status for a registered instance. |
| `start` | `template_id`, `instance_id` | Queues a non-blocking `systemctl start` operation. |
| `restart` | `template_id`, `instance_id` | Queues a non-blocking `systemctl restart` operation. |
| `operation_status` | `operation_id` | Returns the in-memory operation state. |
| `health` | none | Returns status summaries for all registered instances. |
| `logs` | `template_id`, `instance_id`, optional `tail` | Returns at most 100 recent journal lines with common secret assignments redacted. |

The optional `actor` field is limited to 256 characters and is recorded as supplied by the future authenticated API. It is not a browser identity mechanism; Phase 3 must set it only from Tailscale's trusted proxy boundary.

## Instance lifecycle boundary

`register_instance` validates the template, slot, limit, and catalog-generated names before persisting root-only instance state. It does **not** create a service account, Compose file, password, persistent world, or systemd unit. It marks the registration `pending-provisioning`.

Consequently, `start` and `restart` can only request the unit derived from the catalog, but they fail safely until a later, root-reviewed provisioning workflow creates that exact unit and its secret file. This prevents the controller from becoming a general-purpose host or Docker execution interface.

## Operations and audit records

Start and restart return an operation ID immediately with `queued`, then transition through `starting` or `restarting` to `healthy` or `failed`. The controller records completion duration and outcome. Operation state is intentionally in-memory in this phase; callers must treat it as unavailable after a controller restart and query `status` instead.

Audit records are JSON lines in `/var/log/game-server-interface/audit.jsonl`, mode `0600`, owned by root. The installer applies the filesystem append-only flag when supported. Every accepted request and lifecycle completion includes a timestamp, template and instance ID, requested action, API-provided actor, peer UID, result, and applicable duration. Audit failure causes the request to fail rather than silently continuing.

## Verification

After installation:

    systemctl status game-server-interface-controller.service
    sudo ls -l /run/game-server-interface/controller.sock
    sudo lsattr /var/log/game-server-interface/audit.jsonl

The diagnostic client at [controller/controller_client.py](../controller/controller_client.py) is intended to run only as the `game-interface-api` account after Phase 3 supplies its trusted identity context. A normal user must receive `unauthorized local caller` or lack filesystem permission to connect.
