# Feature backlog

Use this file to record proposed functionality and rollout work. Each item should state its scope, safety boundary, validation criteria, and whether it needs a maintenance window or an external test device. Do not record passwords, tokens, player data, or backup contents here.

## Phase 7 remaining rollout work

### Approved friend-device acceptance test

- **Scope:** From a separately managed, approved tailnet device, open `https://bobiverse.tail40344b.ts.net/`, view the catalog, use the dashboard connection address, and join only the intended Enshrouded world.
- **Boundary:** Use `enshrouded:secondary` for the lifecycle action and game-connectivity test; do not restart or modify `primary`.
- **Pass criteria:** HTTPS works through MagicDNS, the dashboard shows the secondary address `100.84.161.38:15640`, the friend can connect on the approved UDP port, and the controller audit log records the friend's `Tailscale-User-Login` after an allowed dashboard action.

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
