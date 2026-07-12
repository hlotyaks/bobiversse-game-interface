# Plan: Private Game Server Interface

Build a tailnet-only HTTPS web interface that lists an explicit catalog of supported game containers, shows their status and resource requirements, and lets any approved tailnet member start or restart a game. Keep privileged Docker and systemd control outside the web containers: the UI calls a narrow, allowlisted host controller that can operate only registered game services.

**Scope**
- Included: game discovery from a curated metadata catalog, status display, start and restart actions, multi-game and multi-instance resource admission controls, audit logs, Tailscale-only HTTPS access, and operational documentation.
- Excluded: Internet exposure, router port forwarding, a generic Docker dashboard, arbitrary image execution, game-specific server configuration editors, automated empty-server shutdown, and Docker socket access from the UI.

## Phase 1 — Define the game contract

1. Create a root-owned game catalog in a standard host configuration location. Each game template is identified by a stable lowercase ID and is explicitly registered; the interface must never enumerate and expose every local Docker image as a runnable game. Define separately managed instances using a stable instance ID scoped to a template (for example, `minecraft:survival-1`); do not use a container ID as an instance identity.
2. Define metadata for each catalog entry:
   - Display name, description, icon/banner, supported player count, game connection hostname and ports, server-password guidance, and optional documentation link.
   - Pinned container image reference, image digest or approved version, Compose project and systemd service identifiers, and a dedicated service-account UID/GID.
   - Host data location under the existing per-game storage convention, required persistent volumes, backup status, health-check method, expected startup timeout, and log location.
   - CPU, memory, disk, and optional player-capacity estimates used for scheduling.
   - Dependencies, incompatibilities, and an enabled/disabled flag.
   - Per-template instance policy: whether multiple instances are allowed, the maximum instance count, the naming rules, and defaults for instance-specific data paths, Compose project names, systemd unit names, connection ports, and display labels.
3. Require every instance to have unique persistent data, Compose project and systemd unit identifiers, published game ports, log paths, and backup identity. Validate these values when an instance is registered so one instance can never overwrite, control, or connect players to another instance.
4. Standardize each game instance deployment as a root-owned Compose project paired with a root-owned systemd unit. The game service account owns only that instance's data but does not gain Docker-group membership or administrative permissions.
5. Require pinned images rather than `latest`. Add a reviewed update workflow that records the running image digest, retains a rollback candidate, and verifies backup health before an update.

## Phase 2 — Build the secure control boundary

1. Implement a minimal host-side controller as a restricted systemd service. It accepts only a fixed set of operations: list catalog and registered instances, read instance status, start a registered instance, restart a registered instance, read an approved health summary, and read a bounded log tail.
2. Keep the controller’s allowlist in the root-owned catalog. It must map a submitted template and instance ID to a predefined systemd service name and must not accept arbitrary shell arguments, Compose file paths, image names, container IDs, or Docker API payloads. Instance creation or registration must be a separately allowlisted operation that validates the template's instance policy and generates or selects only predeclared safe values.
3. Have the controller operate named systemd units rather than expose the Docker socket to the website. Systemd units remain responsible for Compose lifecycle, start ordering, restart policy, and per-instance service accounts.
4. Use a Unix-domain socket or loopback-only authenticated API between the web/API containers and the controller. Restrict socket ownership and service credentials so only the deployed API service can invoke controller actions.
5. Return operation IDs and states such as `queued`, `starting`, `healthy`, `failed`, `restarting`, and `already-running`; do not block a browser request while a large game image starts.
6. Record immutable, root-readable audit events for every control action: timestamp, game template and instance ID, requested action, initiating Tailscale identity when available, source tailnet address/device, result, controller error, and operation duration.
7. Bound log access to a small recent tail and redact known secret fields. Never expose environment files, Compose files containing secrets, backup archives, host logs, or the controller’s credentials.

## Phase 3 — Containerized web interface

1. Deploy the unprivileged frontend and API as separate containers or a single unprivileged application container, with a read-only root filesystem where practical, no privileged mode, no Docker socket mount, no host networking, and minimal capabilities.
2. Provide a catalog page showing:
   - Available game templates, their registered instances, and whether each template and instance is enabled.
   - Current lifecycle state, health, current image version, last start/restart outcome, and recent status message for each instance.
   - Resource request and available capacity summary.
   - Instance-specific connection instructions using the existing MagicDNS host name and that instance's allocated game ports.
3. Provide start and restart controls for every approved tailnet user, matching the agreed access model. Where the template policy permits it, provide a controlled create/register-instance flow that assigns a valid label and the prevalidated instance configuration; do not expose free-form Compose, image, volume, or port fields. Display a clear confirmation that starting an instance consumes shared server capacity and that restart disconnects that instance's active players.
4. Disable actions when an instance is already transitioning, disabled, unhealthy in a way that restart cannot fix, or rejected by the resource or instance policy. Show a human-readable reason and audit the rejected request.
5. Design the catalog and API so a future stop action, scheduled maintenance window, or admin-only controls can be added without changing the basic security boundary.

## Phase 4 — Resource and concurrency policy

1. Configure a global capacity policy using conservative CPU, memory, disk-space, and optional swap thresholds derived from the host’s actual resources.
2. At each instance creation and start request, calculate whether the requested instance’s catalog reservation plus all running instances remains within configured limits. Reject the request when it would exceed a safety reserve for the operating system and management services.
3. Support multiple games and multiple instances of a game running concurrently only when their declared resource reservations fit, their ports and storage locations are unique, and the template’s instance limit permits it. This fulfills the multi-game goal while retaining a simple, predictable initial policy.
4. Use Docker Compose resource limits and per-instance systemd limits where compatible with each image. Treat metadata reservations as admission control, not a substitute for enforced limits.
5. Surface running-instance resource usage and the reason for each blocked creation or start. Do not automatically stop games in the first release; lifecycle remains manual as selected.
6. Add disk-pressure checks before startup, including sufficient room for persistent game data, logs, image layers, and a verified backup. Block launches before disk exhaustion can affect existing worlds.

## Phase 5 — Tailscale-only publishing and authorization

1. Bind the web application only to loopback. Publish it through Tailscale Serve using HTTPS on the game host’s MagicDNS name; do not enable Funnel and do not create router port forwards.
2. Update the tailnet ACL/grant policy so the friend group can reach the game host’s HTTPS interface and only the intended game ports. Retain the existing restriction that SSH is available only to approved administrator devices.
3. Confirm that the Tailscale Serve routing configuration is persistent across reboot and is managed by documented host configuration, not manually recreated after each restart.
4. Treat tailnet membership as authorization for controls, as chosen. Capture Tailscale-provided user headers or perform a tailnet identity lookup where safely available, but do not depend on browser-supplied identity headers.
5. Ensure the application accepts traffic only from the local Tailscale Serve proxy path. Reject direct network exposure and strip or overwrite identity-related headers at the trusted boundary to prevent spoofed audit identities.
6. Add UFW and Docker-aware firewall validation so neither the interface nor future published game containers become reachable outside the tailnet-approved paths.

## Phase 6 — Reliability, backups, and operations

1. Configure restart behavior separately for the interface, controller, and each game instance. An instance restart requested by the UI must invoke that instance's service unit’s defined lifecycle rather than directly killing arbitrary containers.
2. Reuse the established per-instance backup process before updates and before high-risk maintenance. Surface the latest backup timestamp and verification result for each instance in the UI, but keep archive contents and restore operations outside the friend-facing interface.
3. Add controller and interface health checks, structured logs, bounded log retention, and monitoring for failed starts, repeated crashes, storage pressure, memory pressure, backup failures, and failed health checks.
4. Define a crash policy: systemd/Compose may automatically restart a crashed game under bounded retry limits; the UI’s manual restart remains available when retries are exhausted. Display crash-loop status instead of repeatedly retrying indefinitely.
5. Document runbooks for registering a game template and an instance, allocating and retiring instance ports and storage, updating an image across instances, rollback, backup verification, recovery after host reboot, revoking a Tailscale member, and disabling a compromised or unstable instance or template.
6. Ensure game-specific passwords and secrets are stored outside the catalog metadata and are never returned by the API, rendered in the UI, or copied to audit logs.

## Phase 7 — Testing and rollout

1. Create a non-production sample game template with at least two instances to exercise catalog parsing, instance registration, service control, status transitions, action authorization, audit records, and failure handling without risking a real world.
2. Test all lifecycle paths: disabled instance, successful start, already-running start, healthy restart, failed startup, crash-loop, controller restart, interface restart, host reboot, and unavailable Docker/Compose service.
3. Test concurrency admission with synthetic metadata and controlled limits: one game within budget, multiple games within budget, multiple instances of one template within budget, rejection at a template's instance limit, and a rejected request that would exceed CPU, memory, disk, port, or storage isolation constraints.
4. From an approved friend device, verify HTTPS access through MagicDNS, visibility of the catalog, permitted start/restart behavior, correct audit attribution, and game connectivity on only approved ports.
5. From an unapproved device and from the public Internet, verify that the interface, SSH, Docker API, databases, controller socket, and unapproved game ports are inaccessible.
6. Revoke a friend’s tailnet device or account and verify immediate loss of interface and game access.
7. Restore a representative game world from a verified backup after a controlled game lifecycle test, then verify that the restored server can be started and joined.
8. Launch with one pilot game and one friend before registering additional games or enabling concurrent workloads.

**Key decisions**
- The runnable game list is curated metadata, not a raw list of installed images. Local images may be incomplete, outdated, unsafe, or unsuitable for public control.
- The website does not receive Docker socket access. A narrow host-side allowlist controller preserves the existing least-privilege design.
- All approved tailnet members can start and restart catalog games; actions are audited.
- HTTPS is tailnet-only through Tailscale Serve. No Tailscale Funnel, public forwarding, UPnP, or Internet-facing management panel is introduced.
- Multiple simultaneous games, including multiple independent instances of the same game template, are supported only when the template instance policy, configured resource reservations, unique per-instance ports and data paths, and host safety thresholds permit them.
- The first release is manual lifecycle management: no player-empty detection and no automatic idle shutdown.
