# Phase 7 validation

## Test boundary

All destructive lifecycle validation targets only `enshrouded:secondary` (`game-enshrouded-secondary.service`, UDP `15640–15641`, and `/srv/games/enshrouded-secondary`). The active `enshrouded:primary` instance is treated as a production pilot and is checked only for continued availability.

## Completed host validation — 2026-07-17

The following checks passed using `enshrouded:secondary` only:

- An active-instance `start` request returned `already-running`.
- A controller-managed `restart` completed healthy.
- A temporary runtime-only failed-start condition produced failed startup and the configured systemd start limit. The controller reported `crash_loop: true` after three recorded automatic restarts.
- A controller-managed manual retry reset the secondary unit's failed state and completed healthy.
- Restarting the controller preserved the completed operation record; restarting the interface restored its protected Unix-socket health endpoint.
- A fresh scheduled backup for the secondary verified successfully at `2026-07-17T20:55:57Z`.
- UDP `15640` and `15641` passed tailnet-only bind validation.
- The audit log includes the accepted and healthy lifecycle records under the `phase7-*` actors.
- `enshrouded:primary` remained active throughout each test.

The source suite includes synthetic tests for disabled templates, a template instance limit, and memory/disk admission rejection. These tests do not modify a deployed game.

## Automated secondary lifecycle test

Run `sudo bash ./scripts/test-phase7-secondary.sh` from the repository root. It requires both Enshrouded instances to be initially active and performs these checks:

1. Start an already-running secondary instance and require the controller's `already-running` response.
2. Restart the secondary through the restricted controller and require a healthy completion.
3. Apply a temporary runtime-only failing `ExecStartPre` to the secondary unit. Systemd must stop at its five-start limit; it records three automatic restarts before rejecting further starts.
4. Confirm the controller reports `crash_loop: true`, remove the runtime drop-in, and return the secondary to healthy. Systemd may resume its pending restart after the configuration correction; otherwise the script uses the controller's manual retry path.
5. Run the scheduled backup service and require a verified `enshrouded:secondary` backup status.
6. Confirm UDP `15640–15641` bind only to the host Tailscale IP.
7. Confirm `enshrouded:primary` remains active throughout.

The script removes the temporary failure injection through an exit trap. If it is interrupted, remove `/run/systemd/system/game-enshrouded-secondary.service.d/phase7-failed-start.conf`, run `sudo systemctl daemon-reload`, and start the secondary through the dashboard.

## Infrastructure restart checks

The controller and interface can be restarted without touching either game unit:

1. Restart `game-server-interface-controller.service`, then inspect secondary status through the dashboard or controller client.
2. Restart `game-server-interface.service`, then verify `https://bobiverse.tail40344b.ts.net/` loads and shows both instances.
3. Confirm completed lifecycle operations remain queryable after controller restart for up to 24 hours.

## Tests requiring a separate approved friend device

The following cannot be proven from the host and remain a manual rollout checklist:

- Connect an approved friend to the private HTTPS dashboard over MagicDNS and confirm their Tailscale identity is present in the audit log after an allowed action.
- Connect that friend to `100.84.161.38:15640`; confirm no connection is possible to unrelated host ports.
- Verify an unapproved tailnet device and a public Internet client cannot reach the dashboard, controller socket, SSH, Docker API, or game ports.
- Revoke a dedicated test friend/device in Tailscale and confirm immediate loss of access.

## Deferred tests

Do not stop Docker, reboot the host, disable the Enshrouded template, or perform a restore test while `enshrouded:primary` is the production pilot. Those tests affect more than the isolated secondary boundary. Schedule them in a maintenance window after a verified primary backup and player notification.
