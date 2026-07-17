# Instance provisioning

The catalog defines *which* games and slots are allowed; the controller *registers* a slot
and *starts* its systemd unit. This document covers the step in between: turning a registered
slot into a running service by creating its service account, data directories, secret env,
Compose file, and systemd unit. These are root-only actions performed out of band, exactly as
`docs/game-catalog.md` requires (the controller never creates accounts, writes secrets, or
generates units).

All generated values are derived from the root-owned catalog, so provisioning cannot invent
ports, images, paths, or resource limits. The shared source of truth is
`resolve_slot_from_catalog()` in [controller/game_controller.py](../controller/game_controller.py),
reused by both the controller and the renderer.

## Tools

- [tools/render_instance.py](../tools/render_instance.py) — pure renderer. Resolves a
  `(template, instance)` slot and emits `compose.yaml` plus `game-<template>-<instance>.service`.
  No privileged side effects; run it with no `--output-dir` to preview the files. Per-game
  differences live in small adapters (the `enshrouded` adapter is implemented).
- [scripts/provision-game-instance.sh](../scripts/provision-game-instance.sh) — root
  orchestrator. Validates the catalog, creates the service account (`create-game-account`),
  makes the savegame bind-mount owned by the container UID, writes the root-only secret env,
  renders + installs the Compose file and unit, validates the Compose, and enables (but does
  **not** start) the unit.
- [scripts/game-firewall.sh](../scripts/game-firewall.sh) — root. Verifies that the published
  game UDP ports are exposed only on the host's Tailscale IP (never `0.0.0.0`) and removes the
  obsolete DOCKER-USER source-filter rules if a prior version installed them. It does not add
  DOCKER-USER filters: Docker's userland proxy rewrites inbound source addresses to the bridge
  gateway, so a tailnet-source filter would drop all game traffic while protecting nothing. The
  real control is the tailnet-IP port binding that `render_instance.py` writes into the Compose
  `ports`. It never opens a UFW "Anywhere" game rule.

## Workflow

```bash
# 1. Provision (prompts for server name / slots / password; secret stays root-only).
sudo ./scripts/provision-game-instance.sh enshrouded primary

# 2. Register the slot with the controller (write action, API user only).
sudo -u game-interface-api /usr/local/libexec/game-server-interface/controller_client.py \
  '{"action":"register_instance","template_id":"enshrouded","instance_id":"primary","actor":"<you>"}'

# 3. Place any migrated world data, then fix ownership to the container UID.
sudo cp -a <save-files> /srv/games/enshrouded-primary/savegame/
sudo chown -R 10000:10000 /srv/games/enshrouded-primary/savegame

# 4. Verify the game ports are exposed only on the tailnet IP (and clean up any legacy rules).
sudo ./scripts/game-firewall.sh 15636 15637

# 5. Start via the controller (re-checks admission and audits), or systemctl.
sudo -u game-interface-api /usr/local/libexec/game-server-interface/controller_client.py \
  '{"action":"start","template_id":"enshrouded","instance_id":"primary","actor":"<you>"}'
```

## Security model notes

- **Container UID.** `sknnr/enshrouded-dedicated-server` runs as its built-in non-root
  UID/GID `10000:10000` and cannot take an arbitrary UID without rebuilding (which would break
  the pinned digest). The instance therefore runs as `10000:10000` with the savegame directory
  chowned to match. Isolation is enforced through `cap_drop: [ALL]`, `no-new-privileges`,
  Compose/systemd CPU and memory limits, `pids_limit`, and tailnet-only port publishing rather
  than a per-game host UID. Do not enable daemon-wide userns-remap on this live host without a
  planned maintenance window; it affects the already-running interface container.
- **Secrets.** `SERVER_PASSWORD` (and the operator-chosen `SERVER_NAME`/`SERVER_SLOTS`) live
  only in `/etc/game-server-interface/instances/<instance>/<template>.env`, `0600 root:root`,
  referenced by the Compose `env_file`. They never enter the catalog, the rendered Compose,
  or logs (the controller's log reader also redacts secret-looking lines).
- **Ports.** Current Enshrouded uses a single game/connect port (the lower reserved port, set
  via `PORT`); the server binary reads it from the config's `queryPort`. Steam's server-browser
  query port (fixed 27015) is intentionally not published on the tailnet, so friends join by
  direct-connect to `<host>:<port>` rather than the Steam server browser. The reserved ports are
  published only on the host's Tailscale IP.
- **Access control.** With `EXTERNAL_CONFIG=0` the image sets every user group's password to the
  single `SERVER_PASSWORD`, and the shipped config lists the Admin group first, so any player who
  joins with that password receives Admin rights. That is acceptable when the tailnet is the real
  access boundary; to give friends non-admin access again, switch the instance to `EXTERNAL_CONFIG=1`
  with a root-only `enshrouded_server.json` carrying distinct group passwords.
- **Backups.** After the world is verified in place, run
  `sudo backup-game-data enshrouded-primary && sudo verify-game-backup enshrouded-primary`.
