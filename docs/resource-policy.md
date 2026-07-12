# Phase 4 resource and concurrency policy

The root-owned catalog now carries a host-specific `capacity_policy`. On 2026-07-12, bobiverse had 8 logical CPUs, 30 GiB RAM, 8 GiB swap, and approximately 80 GiB free disk. The configured admission budget reserves capacity for Ubuntu, Docker, Tailscale, the controller, backups, and management services:

| Resource | Admission limit | Safety reserve |
| --- | ---: | ---: |
| CPU | 6 cores | Remaining host cores |
| Memory | 24,576 MiB | Remaining host memory |
| Disk | Per-instance reservation | 20 GiB free on each relevant filesystem |
| Swap | n/a | At least 1,024 MiB free |

The controller evaluates this policy when an instance is registered and again before a start is queued and executed. It counts only instances with `active`, `activating`, or `reloading` systemd states as running reservations. Requests that exceed CPU, memory, disk, or swap thresholds are rejected with an auditable reason.

## Multiple games and instances

Concurrent instances are permitted only when all conditions are satisfied:

1. The template allows the selected slot and has not reached its instance limit.
2. The catalog’s preallocated port, data, backup, Compose, unit, and service-account names are unique.
3. The sum of active reservations plus the requested reservation is inside the CPU and memory admission budgets.
4. Every configured disk path retains the configured free-space safety reserve after the projected disk reservation.
5. The host has the minimum free swap capacity.

The present reservations mean that one Enshrouded instance plus up to two Valheim instances fits the six-core CPU budget, subject to actual disk and swap checks. Two Enshrouded instances plus two Valheim instances do not fit and are rejected before any `systemd` start request.

## Enforced limits during provisioning

When an instance is registered, the controller persists catalog-derived limit values alongside its state. A root-reviewed provisioning workflow must apply them to both the game Compose service and its systemd unit:

- Compose: `cpus` and `mem_limit` from `resource_limits.compose`.
- Systemd: `CPUQuota` and `MemoryMax` from `resource_limits.systemd`.

These are enforced limits; the admission reservation is a separate, conservative scheduling decision. Do not substitute one for the other. The initial slots remain `pending-provisioning`, so no game unit exists yet and no game can bypass this requirement.

## Operational visibility

`capacity` is a controller read operation and is exposed at `GET /api/capacity`. The Phase 3 UI displays current CPU and memory reservations, disk headroom, and the disk safety reserve. Registered instance cards display observed systemd memory usage and cumulative CPU time when available. The UI disables register/start controls when it can determine that the candidate cannot fit and names the limiting CPU, memory, disk, or swap threshold; the controller remains authoritative and rechecks immediately before launch.

Review and adjust the policy in [deploy/etc/game-server-interface/catalog.yaml](../deploy/etc/game-server-interface/catalog.yaml) after hardware changes, before registering a more demanding game, or when disk layout changes. Validate and deploy catalog changes using the workflow in [docs/game-catalog.md](game-catalog.md); the root-owned deployed catalog must match the repository source.
