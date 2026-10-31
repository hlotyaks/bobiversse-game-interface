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

**Enshrouded and Valheim can no longer run at the same time.** Enshrouded's reservation was raised from four cores to six on 2026-10-31 (see below), which consumes the entire six-core admission budget on its own. Starting either game while the other is active is refused before any `systemd` start request, with the reason `CPU reservation 8 exceeds 6 core admission limit` and the running instance named so it is obvious what has to be stopped first. Two Enshrouded instances, or two Valheim instances alongside Enshrouded, are likewise rejected. This is a deliberate trade: a playable Enshrouded in exchange for taking turns between games. Raising the admission limit above six would hand the games the cores the host itself needs for Docker, Tailscale, the controller, backups and metering.

## Why Enshrouded reserves six cores

Four cores produced the same failure that two cores had produced in July, and the symptom was not "the server is at 100% CPU" — it was that the server stalled in bursts. Measured on 2026-10-31 with three players connected and roughly 25,000 world entities:

| Measure | Value |
| --- | ---: |
| CPU used vs. quota | 392% of 400% |
| Scheduling periods throttled | 34,532 of 36,781 (93.9%) |
| Accumulated stall | ~81 minutes |
| Simulation tick | 27–38 ms average, 18–22 updates/s |
| Memory used | 1.08 GiB of 6 GiB |

Memory was never the constraint. The game runs eight compute threads — one `keen GameServer` plus seven `keen Task-Default` workers — and sizes that pool to the host's eight CPUs, not to the container's quota. A pool that wide against a four-core quota spends each 100 ms period's budget early and then freezes for the remainder, so the cost is paid in latency rather than in throughput. That is why restarting never helped and why the server looked healthy: `HostOnline`, sensible memory, no errors in the log.

Demand was at least 4.3 cores *while throttled*, so six clears it with headroom. Read the throttle counters rather than CPU percentage when judging this:

    cid=$(docker inspect -f '{{.Id}}' game-enshrouded-primary)
    cat /sys/fs/cgroup/system.slice/docker-$cid.scope/cpu.stat

`nr_throttled` approaching `nr_periods` is the signal. If it returns there with six cores, the next lever is the world's entity count, not the quota — the catalog cannot buy its way out of a base that is too expensive to simulate.

## Enforced limits during provisioning

When an instance is registered, the controller persists catalog-derived limit values alongside its state. A root-reviewed provisioning workflow must apply them to both the game Compose service and its systemd unit:

- Compose: `cpus` and `mem_limit` from `resource_limits.compose`.
- Systemd: `CPUQuota` and `MemoryMax` from `resource_limits.systemd`.

These are enforced limits; the admission reservation is a separate, conservative scheduling decision. Do not substitute one for the other. The initial slots remain `pending-provisioning`, so no game unit exists yet and no game can bypass this requirement.

## Operational visibility

`capacity` is a controller read operation and is exposed at `GET /api/capacity`. The Phase 3 UI displays current CPU and memory reservations, disk headroom, and the disk safety reserve. Registered instance cards display observed systemd memory usage and cumulative CPU time when available. The UI disables register/start controls when it can determine that the candidate cannot fit and names the limiting CPU, memory, disk, or swap threshold; the controller remains authoritative and rechecks immediately before launch.

Review and adjust the policy in [deploy/etc/game-server-interface/catalog.yaml](../deploy/etc/game-server-interface/catalog.yaml) after hardware changes, before registering a more demanding game, or when disk layout changes. Validate and deploy catalog changes using the workflow in [docs/game-catalog.md](game-catalog.md); the root-owned deployed catalog must match the repository source.
