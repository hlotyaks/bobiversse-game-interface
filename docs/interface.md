# Phase 3 web interface

The Phase 3 interface is a single unprivileged application container. It serves the catalog page and a narrowly scoped HTTP API, while the root-owned Phase 2 controller remains the only process that can request lifecycle changes from `systemd`.

## Security boundary

The Compose deployment in [deploy/opt/game-server-interface/compose.yaml](../deploy/opt/game-server-interface/compose.yaml) enforces these initial constraints:

- Runs as the existing `game-interface-api` numeric UID/GID (`995:983`), the only UID accepted by the controller socket.
- Uses a read-only root filesystem, drops every Linux capability, enables `no-new-privileges`, and uses a small `noexec,nosuid` temporary filesystem.
- Has no Docker socket, privileged mode, host networking, host PID/IPC namespaces, or host filesystem mount beyond the read-only controller socket directory.
- Uses a dedicated non-host Docker bridge network and accepts HTTP only through the protected host Unix socket used by Tailscale Serve.
- Is supervised by the root-owned [deploy/etc/systemd/system/game-server-interface.service](../deploy/etc/systemd/system/game-server-interface.service) unit.

It is not exposed directly on the LAN or Internet. Phase 5 publishes it through private Tailscale Serve HTTPS only; Tailscale Funnel remains disabled.

## Install and verify

Install after Phase 2 is active:

    sudo bash ./scripts/install-phase3.sh

The installer copies root-owned assets to `/opt/game-server-interface`, builds the pinned Python base image, enables the service, and leaves the interface behind its protected Unix socket.

Verify locally from the host:

    systemctl status game-server-interface.service
    sudo curl --unix-socket /run/game-server-interface/web/interface.sock --fail http://localhost/healthz
    sudo curl --unix-socket /run/game-server-interface/web/interface.sock --fail http://localhost/api/catalog

The private dashboard is available through `https://bobiverse.tail40344b.ts.net/` to approved tailnet members. Do not add a firewall exception, router forwarding rule, or Tailscale Funnel configuration.

## API and controls

The browser only reaches these same-origin routes:

| Route | Function |
| --- | --- |
| `GET /api/catalog` | Safe template summary. |
| `GET /api/capacity` | Current capacity reservations and admission limits. |
| `GET /api/backup-status` | Latest backup verification summary. |
| `GET /api/instances` | Registered slots with live `systemd` status. |
| `GET /api/logs?template_id=<id>&instance_id=<id>&tail=<1-100>` | Redacted tail of the registered instance's systemd-unit journal; `tail` is optional and defaults to 50. |
| `POST /api/instances` | Registers a catalog-defined slot only. |
| `POST /api/actions/start` | Queues an asynchronous start. |
| `POST /api/actions/restart` | Queues an asynchronous restart. |
| `GET /api/operations/<id>` | Reads asynchronous operation state. |

The UI shows registered and unregistered slots separately, exposes only catalog-provided ports, displays a copyable catalog-derived connection address for a registered game, and shows backup status and observed resource use. It disables lifecycle actions while services transition and asks for confirmation before registration, start, or restart. A registered slot remains `pending-provisioning` until a later root-reviewed workflow creates its exact service unit and secret file; the controller safely rejects a start of an absent unit.

## Dashboard refresh behavior

The browser refreshes the full catalog, instance-status, and capacity view every 10 seconds while the dashboard is visible. During a submitted start or restart operation, it checks the operation and refreshes the displayed state every 2 seconds for up to 60 checks; after a terminal result or timeout it returns to the 10-second cadence. A status label shows the last successful update or a retry condition, and **Refresh status** remains available for an immediate manual update.

Automatic requests pause while the tab is hidden and the dashboard performs an immediate refresh when it becomes visible again. This is browser-only polling over the scoped API: it does not add a controller capability, WebSocket, or server-sent-event connection, and it never retries lifecycle actions.

## Server console

Each registered instance always displays an expandable **Server logs** control. While a submitted start or restart is in progress, the dashboard reads a small recent tail from the instance's systemd journal at the existing two-second operation-refresh cadence. A successful operation collapses its startup console; a failed operation retains its final output for diagnosis. Outside a lifecycle operation, opening the control fetches up to 100 lines once; running instances are not continuously polled for logs.

The controller, not the browser, resolves the registered instance to its allowlisted systemd unit, limits output to 100 lines, applies its secret-value redaction, and records the request in the audit log. The output is a unit-journal tail only: it is not a raw export, historical search facility, or adapter for game-specific file logs.

## Tailnet identity

By default the API records the actor as `local-loopback` and ignores every browser-supplied actor header. In the deployed private Serve configuration, Phase 5 sets `TRUSTED_ACTOR_HEADER=1` only after Tailscale Serve is configured to remove incoming identity headers and set `Tailscale-User-Login` itself.
