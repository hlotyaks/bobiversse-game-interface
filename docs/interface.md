# Phase 3 web interface

The Phase 3 interface is a single unprivileged application container. It serves the catalog page and a narrowly scoped HTTP API, while the root-owned Phase 2 controller remains the only process that can request lifecycle changes from `systemd`.

## Security boundary

The Compose deployment in [deploy/opt/game-server-interface/compose.yaml](../deploy/opt/game-server-interface/compose.yaml) enforces these initial constraints:

- Runs as the existing `game-interface-api` numeric UID/GID (`995:983`), the only UID accepted by the controller socket.
- Uses a read-only root filesystem, drops every Linux capability, enables `no-new-privileges`, and uses a small `noexec,nosuid` temporary filesystem.
- Has no Docker socket, privileged mode, host networking, host PID/IPC namespaces, or host filesystem mount beyond the read-only controller socket directory.
- Uses a dedicated non-host Docker bridge network and publishes HTTP solely as `127.0.0.1:8080`.
- Is supervised by the root-owned [deploy/etc/systemd/system/game-server-interface.service](../deploy/etc/systemd/system/game-server-interface.service) unit.

It must not be exposed directly on the LAN or Internet. Phase 5 will add the Tailscale Serve publishing configuration and trusted identity boundary.

## Install and verify

Install after Phase 2 is active:

    sudo bash ./scripts/install-phase3.sh

The installer copies root-owned assets to `/opt/game-server-interface`, builds the pinned Python base image, enables the service, and leaves the interface bound only to loopback.

Verify locally from the host:

    systemctl status game-server-interface.service
    curl --fail http://127.0.0.1:8080/healthz
    curl --fail http://127.0.0.1:8080/api/catalog

The UI is intentionally not reachable through Tailscale yet. Do not add a firewall exception, router forwarding rule, or Tailscale Funnel configuration for port `8080`.

## API and controls

The browser only reaches these same-origin routes:

| Route | Function |
| --- | --- |
| `GET /api/catalog` | Safe template summary. |
| `GET /api/instances` | Registered slots with live `systemd` status. |
| `POST /api/instances` | Registers a catalog-defined slot only. |
| `POST /api/actions/start` | Queues an asynchronous start. |
| `POST /api/actions/restart` | Queues an asynchronous restart. |
| `GET /api/operations/<id>` | Reads asynchronous operation state. |

The UI shows registered and unregistered slots separately, exposes only catalog-provided ports, disables lifecycle actions while services transition, and asks for confirmation before registration, start, or restart. A registered slot remains `pending-provisioning` until a later root-reviewed workflow creates its exact service unit and secret file; the controller safely rejects a start of an absent unit.

## Identity until Phase 5

By default the API records the actor as `local-loopback` and ignores every browser-supplied actor header. This avoids accepting spoofed audit identity while the service is only locally available. Phase 5 may set `TRUSTED_ACTOR_HEADER=1` only after Tailscale Serve is configured to remove incoming identity headers and set the trusted user identity header itself.
