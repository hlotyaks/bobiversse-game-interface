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

### Administrator operations page

- **Status:** Proposed 2026-07-18. Motivated by the Enshrouded primary CPU-throttling incident, whose fix (stop the secondary world, raise primary from 2 to 4 cores) required editing the root-owned catalog, re-rendering the Compose file and unit, `daemon-reload`, and a manual restart — none of which is reachable from the dashboard today.
- **Problem statement:** A tailnet administrator (currently `cbrinton` and `hlotyaks`) cannot change an instance's CPU/RAM, stop a running world, control boot-start, or update a pinned image from the interface. These all require root config edits plus a re-render, which the controller deliberately does not perform.
- **Scope (one-click privileged applier).** Extend the existing Phase 7 admin gate (`is_game_administrator`, `TRUSTED_ACTOR_HEADER` + `GAME_INTERFACE_ADMIN_LOGINS` on `Tailscale-User-Login`) with admin-only controls and endpoints for four actions:
  1. **Change resources** — per-instance CPU cores and memory, validated against the `admission_limits` ceiling and projected running total.
  2. **Stop a running server** — new controller `stop` verb (only `start`/`restart` exist today).
  3. **Disable/enable at boot** — `systemctl enable`/`disable` on the allowlisted unit.
  4. **Image/version update** — change a per-instance pinned image digest, guarded by a verified backup and automatic rollback on failed health check.
- **Design.**
  - **Per-instance override file** at `/etc/game-server-interface/instances/<t>-<i>/overrides.yaml` (root-owned, `0644`), holding optional `resources` and `image.digest`. The catalog stays the human-reviewed default and ceiling; `resolve_slot_from_catalog()` and `render_instance.py` apply the override on top so the effective values feed both the render and the controller's admission math. This also fixes the current per-*template* limitation (today one Enshrouded resource change hits both slots).
  - **One new root helper** `/usr/local/libexec/game-server-interface/game-instance-admin` (`0755 root`), invoked by the controller with an allowlisted absolute path and bounded, re-validated arguments — the same trust pattern as the controller's existing `/usr/bin/systemctl` calls. Subcommands: `set-resources` and `set-image` (write override → `render_instance.py` → install Compose + unit → `daemon-reload`), plus `set-image --rollback`. It restarts nothing; the controller applies the change through its existing audited `restart` lifecycle.
  - **Controller:** add `stop`, `set_enabled`, `set_resources`, `set_image` to `WRITE_ACTIONS`. `stop`/`set_enabled` are direct `systemctl` calls on the allowlisted unit. `set_resources`/`set_image` validate bounds and admission, invoke the helper, update stored instance resources, then restart via the existing lifecycle. All audited with actor and old→new values.
  - **Interface:** new admin-gated routes (`403` for non-admins, mirroring `/api/game-requests`): `POST /api/actions/{stop,set-enabled,set-resources,set-image}`, with numeric range / digest-format validation before the controller call. Extend the policy endpoint so the UI renders admin controls and current admission headroom.
  - **UI:** a per-instance Admin section (rendered only when the policy allows) with resource inputs showing headroom, stop/start/restart, a boot-start toggle, and a digest field gated behind a "backup verified" indicator. Reuse the existing async operation-polling UX; show an explicit downtime warning because any resource or image change recreates the container (SteamCMD re-download).
- **Security & capacity impact.** Adds exactly one bounded root executable and four numeric/enum-only controller actions; no free-form input reaches root, every action stays capped by `admission_limits` and audited by `Tailscale-User-Login`. Resource changes cannot exceed the admission ceiling. Requires root to set `TRUSTED_ACTOR_HEADER=1` and `GAME_INTERFACE_ADMIN_LOGINS=<cbrinton-login>,<hlotyaks-login>` in the interface env.
- **Deployment/rollback.** Install the helper and `overrides.yaml` support; set the admin env; deploy interface + controller. Rollback: remove the admin env (controls vanish, endpoints 403) and the helper; override files can be deleted to revert an instance to catalog defaults. Image updates record the prior digest and auto-roll-back on health-check failure.
- **Validation.** Unit tests for override precedence in the renderer, controller bounds/admission rejection, image-update backup requirement + rollback, and interface admin-gating (`403` for a non-admin actor). Exercise end-to-end on `enshrouded:secondary` (never `primary`) before rollout; confirm a non-admin friend sees no controls and is refused; verify audit entries carry the actor and old→new values.
- **Maintenance.** Resource and image changes restart the target world (a few minutes of downtime); schedule a window and notify players. Suggested build phasing: Phase A = override plumbing + resources + stop + boot-toggle (delivers the incident fix from the UI); Phase B = image/version update with backup + rollback.
