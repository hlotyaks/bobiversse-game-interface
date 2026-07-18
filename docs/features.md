# Feature backlog

Use this file to record proposed functionality and rollout work. Each item should state its scope, safety boundary, validation criteria, and whether it needs a maintenance window or an external test device. Do not record passwords, tokens, player data, or backup contents here.

## Page refresh

- **Status:** Implemented with browser-only hybrid polling.
- **Scope:** Refresh the catalog, instance status, and capacity view every 10 seconds while the visible dashboard is idle; use a 2-second cadence during a submitted start or restart operation. Show the latest update status and retain manual refresh.
- **Boundary:** Pause automatic requests in hidden tabs. Do not add a controller action, API route, WebSocket, server-sent-event stream, or automatic lifecycle retry.
- **Pass criteria:** Transitioning states become visible without manual intervention, no overlapping refresh requests occur, hidden tabs send no automatic requests, and showing the tab triggers an immediate refresh.

## Server console window

- **Status:** Implemented with redacted systemd-journal tails.
- **Scope:** Always show an expandable **Server logs** control for each registered instance. During startup/restart it refreshes at the existing two-second operation cadence; otherwise it reads logs only when expanded. Collapse successful startup output, retain failed-operation output for diagnosis, and do not continuously poll running-server logs.
- **Boundary:** The browser can read only the registered instance's allowlisted systemd unit through the controller. The controller limits output to 100 lines, redacts common secret-value formats, audits access, and does not expose raw exports, file-log adapters, WebSockets, or server-sent events. All dashboard-authorized users see the same redacted output.
- **Pass criteria:** Startup progress appears without overlapping requests; hidden tabs make no automatic requests; successful operations remove their startup console; failed operations retain final output; active-server log viewing fetches only on user request; log text cannot inject markup.
- **Maintenance:** No maintenance window or external test device is required for interface validation. Validate against a registered test instance before broad rollout.

## Add new game button

- **Status:** Initial administrator-only request export is implemented; catalog integration and provisioning remain a reviewed follow-up.
- **Scope:** An allowlisted Tailscale administrator can provide a Steam Store URL/app ID, catalog slug, and short purpose, then download a bounded request artifact. An operator runs `tools/fetch_steam_metadata.py` outside the deployed interface to produce advisory Steam metadata and a deliberately incomplete catalog draft for a Git pull request.
- **Boundary:** The browser, interface, and controller do not fetch Steam metadata, store a Steam API key, edit the catalog, select images/digests/ports/resources, create an adapter, provision an account, write secrets, change firewall rules, register an instance, or start a server. The controller audits only the request's safe identifiers and result, not its free-text purpose or Steam response.
- **Pass criteria:** The control is visible only to the root-configured Tailscale-login allowlist and the endpoint rejects every other actor; exported requests are canonical and bounded; the CLI strips HTML and fails closed on invalid Steam responses; generated drafts remain disabled and fail catalog validation until a reviewer completes all required deployment fields; a PR review is required before catalog deployment.
- **Maintenance:** No game-world outage is needed for request/export validation. A real game requires a separate maintenance and rollout plan covering renderer/provisioning support, backup/restore, firewall/Tailscale policy, capacity, lifecycle validation, and client connection testing.
## Phase 7 verified rollout work

### Approved friend-device acceptance test

- **Status:** Verified on 2026-07-17 with an approved friend device able to connect to the intended secondary Enshrouded world.
- **Scope:** From a separately managed, approved tailnet device, open `https://bobiverse.tail40344b.ts.net/`, view the catalog, use the dashboard connection address, and join only the intended Enshrouded world.
- **Boundary:** Use `enshrouded:secondary` for the lifecycle action and game-connectivity test; do not restart or modify `primary`.
- **Pass criteria:** HTTPS works through MagicDNS, the dashboard shows the secondary address `100.84.161.38:15640`, the friend can connect on the approved UDP port, and the controller audit log records the friend's `Tailscale-User-Login` after an allowed dashboard action.

## Phase 7 remaining rollout work

### Denied-access test

- **Scope:** Verify an unapproved tailnet device and a public-Internet client cannot reach the dashboard, SSH, Docker API, controller socket, databases, or game ports.
- **Boundary:** Do not weaken ACLs or publish Tailscale Funnel to run this test.
- **Pass criteria:** Access is denied for all unapproved paths; the approved dashboard and game paths remain private-tailnet-only.

### Test-device revocation

- **Scope:** Revoke a dedicated test friend/device in the Tailscale admin console after the approved-device test.
- **Boundary:** Revoke only the dedicated test device, not the host or the production pilot user.
- **Pass criteria:** The revoked device immediately loses dashboard and game access, while approved users retain access.

### Secondary restore rehearsal

- **Scope:** Restore `enshrouded:secondary` from its latest verified backup following a controlled lifecycle stop, then start and join it.
- **Boundary:** Stop and restore only the secondary world. Preserve the current backup until the restored world is verified. Do not alter `primary` data.
- **Pass criteria:** Backup verification succeeds before restore, the restored secondary starts through the controller, and an approved test device can join `100.84.161.38:15640`.
- **Maintenance:** Schedule a short test-world outage and notify any secondary-world players.

### Host reboot recovery test

- **Scope:** Reboot the host and verify Docker, controller, interface, Tailscale Serve, backup timer, and intended game units recover as documented.
- **Boundary:** This affects both instances and requires a maintenance window, verified backups, and player notification.
- **Pass criteria:** Services return healthy, the dashboard loads over private HTTPS, and the selected game worlds can be joined through their approved ports.

### Docker/Compose outage test

- **Scope:** In a maintenance window, simulate unavailable Docker/Compose and confirm the controller surfaces a safe failed startup and recovery after Docker is restored.
- **Boundary:** Docker is shared infrastructure and this test affects both game instances; do not perform it while the pilot world is in use.
- **Pass criteria:** No arbitrary command is run, failure is audited, the UI displays failure safely, and the affected test instance recovers through its allowlisted lifecycle path.

### Pilot rollout sign-off

- **Scope:** Launch with one pilot game and one approved friend after the external acceptance test and secondary restore rehearsal pass.
- **Pass criteria:** A documented owner confirms the approved-device, denied-access, revocation, and recovery checks; no additional games or workloads are enabled before that decision.

## Future features

Add new proposals below this heading. Include a concise problem statement, affected instances, security and capacity impact, deployment/rollback plan, and validation steps.
