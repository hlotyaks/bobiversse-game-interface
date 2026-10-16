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
  differences live in small adapters (`enshrouded` and `valheim` are implemented).
  `--describe <template>` prints that template's provisioning facts as JSON — the directories to
  create, the uid:gid that must own them, and the secret env keys to prompt for. The provisioning
  script consumes it, so adding a game is one edit here rather than two files that can drift.
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

## Adding a game

1. Add a Compose adapter and a `PROVISIONING` entry to `tools/render_instance.py`. Read the
   image's real contract — its entrypoint, the paths it persists, the env it reads, whether it
   needs to start as root — rather than assuming; `docker run --entrypoint /bin/sh <image>` is
   the fastest way to check.
2. Give every slot a `run_cost_per_hour` in `billing.yaml`, or its playtime bills as zero.
3. Add an `OCCUPANCY_READERS` and an `IDENTITY_READERS` entry in `tools/presence_meter.py`, which
   needs a sample of that game's server log with players connected. Without them the meter falls
   back to a tailnet heuristic that identifies nobody for a Steam-relayed game; see
   [usage-metering.md](usage-metering.md).

## Enshrouded notes

The image is `mornedhels/enshrouded-server` (migrated 2026-10-16). The previous image,
`sknnr/enshrouded-dedicated-server`, is abandoned — last built 2026-01-02, source repository
deleted — and Enshrouded 1.0 does not run under the GE-Proton it bundles at all; see
[enshrouded-1.0-outage.md](enshrouded-1.0-outage.md).

- **The install directory must be persisted.** The image ships **no** game install and its
  entrypoint runs `steamcmd +app_update <id> validate` on every start. Unmounted, that lands in the
  container's writable layer, which the unit's `ExecStop` (`docker compose down`) destroys, so
  **every restart re-downloaded 8.1GB** (measured 2026-10-15). It is bind-mounted from
  `/srv/games/enshrouded-<instance>/server` to the image's own `install_path`,
  `/opt/enshrouded/server`.

  The symptom was a restart that looked hung: the server appears to "stick on updating", because it
  is two minutes into an 8GB transfer. Interrupting it looks like curing a hang and is actually
  interrupting progress.

- **The savegame mount nests inside the install mount** on purpose, so the world survives wiping
  the install directory to force a clean re-download. Docker mounts parents before children, so it
  layers on top rather than being hidden.

- **It starts as root on purpose.** The bootstrap runs `groupmod`/`usermod`, rewrites
  `/etc/passwd`, and `chown -R`s its data tree before supervisord drops to `PUID:PGID`. A blanket
  `cap_drop: [ALL]` breaks it at startup, so the adapter drops everything and adds back exactly
  `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID`, `SETUID`. A `user:` override would also break it.
  `PUID:PGID` is `10000:10000` — arbitrary to the image, but it owns the existing 7.5GB data tree,
  so keeping it means the migration needed no chown.

- **`SERVER_QUERYPORT`, not `PORT` or `SERVER_PORT`.** Enshrouded has a single port, the config's
  `queryPort`, which is also the port players connect to. This image reads it from
  `SERVER_QUERYPORT`; its `SERVER_PORT` is deprecated and only warns, and the old image's `PORT` is
  not read at all. The player cap comes from the catalog as `SERVER_SLOT_COUNT`, not from the
  secret env file (the old `SERVER_SLOTS` key is unread — a stale copy is harmless).

- **Delete any `logs/enshrouded_server.log` symlink left by the old image.** The old image created
  it pointing at `/proc/1/fd/1` so the game's file log reached container stdout. It persists in the
  install directory, and under this image the game runs as uid 10000 while PID 1 is root, so
  opening it is denied and the server exits one second in with

      [enshrouded] Could not open log file, error no permission

  Removing the symlink is the whole fix; the game then writes a real log file and still prints the
  same lines to stdout, which is what the usage meter reads. Instances provisioned from scratch
  never have it — but the old image recreates it on every start, so after any start on that image
  (a rollback, or a stale Compose file) it has to be deleted again.

- **The world is in the 1.0 format now.** The 1.0 build converted it on first start: the live world
  is `savegame/world0.ksd`, the EA-era rolling files were moved intact to `savegame/migrated/`, and
  the game keeps its own rolling backups in `savegame/backup/`. The conversion is one-way, so a
  rollback to the old image also means rolling back the world to
  `/srv/games/enshrouded-primary/savegame-pre-1.0-migration.tar.gz`.

- **Do not mount anything over the Proton prefix.** The server is a Windows binary run under
  Proton, whose prefix lives at `<install_path>/steamapps/compatdata/<appid>` — already inside the
  install mount, so it persists without a mount of its own. Giving it one is what broke the server
  on 2026-10-15: a bind mount at `compatdata` hid the directory Proton creates `pfx.lock` in, and
  Proton died with

      FileNotFoundError: [Errno 2] No such file or directory: '.../compatdata/2278520/pfx.lock'

  which surfaced as a generic startup timeout. Tried and reverted, on the mistaken theory that
  building the prefix was what exhausted the old image's 60-second budget; an empty prefix is 8KB
  and building it is not the bottleneck.

- **A start is never instant, even persisted.** `STEAMCMD_ARGS` defaults to `public validate`, so
  every start re-hashes the install rather than trusting it. That is local disk work measured in
  tens of seconds to minutes instead of an 8GB transfer, and a game release becomes a delta, but it
  is not zero. Do not read a few minutes of "updating" as a hang. Measured 2026-10-16 on a
  persisted install: container start to `HostOnline` in about 20 seconds.

- **A dead server no longer stops the container.** supervisord restarts the server a few times and
  then parks it in `FATAL` state, so the container stays up with nothing serving. Look for
  `entered FATAL state, too many start retries too quickly`; container liveness is not a health
  signal for this image.

## Valheim notes

- **It starts as root on purpose.** The bootstrap runs `groupmod`, rewrites `/etc/passwd`, and
  `chown -R`s the data tree before supervisord drops to `PUID:PGID`. A blanket `cap_drop: [ALL]`
  as used for Enshrouded kills it at startup, so the adapter drops everything and adds back only
  `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID`, `SETUID` — taken from that script, not assumed.
  `no-new-privileges` still applies, ports are still tailnet-only, and caps are still a deny-list
  of one.
- **`CROSSPLAY=true`, joined by code.** This is forced by the deployment rather than chosen.
  The game ports are published only on the host's tailnet IP, so Steam's master servers cannot
  query the server from the public internet and will never list it — `SERVER_PUBLIC=1` registers a
  server nobody can find, which is exactly what happened on the first attempt. Valheim also carries
  gameplay over a relay rather than the tailnet (a capture on `tailscale0` during a join attempt
  caught nothing on either port), so the tailnet address is not a usable connection path either.

  Crossplay needs no inbound reachability: the server dials out to PlayFab's relay and issues a
  **join code**, printed in the server log on startup. Players use Join Game → Join with code.
  Entry remains gated by `SERVER_PASS`.

  The only alternative is publishing the ports on `0.0.0.0` with router port-forwarding, which this
  project's firewall policy deliberately refuses (`game-firewall.sh` never opens an "Anywhere"
  rule). That would be a real change in exposure and should be a deliberate decision, not a default.
- **The dashboard shows a join code, not an address.** Because the ports carry no gameplay, the
  controller reads the current code from the running server and the UI shows it in place of the
  host:port it prints for address-joined games. The code is reissued on every restart, so it is
  read live rather than stored, and it appears only while the instance is active.
- **The published UDP ports carry no gameplay.** They are kept because the A2S query and the
  image's own status check use them, and because a future non-relayed deployment would need them.
  Do not read the tailnet-only binding as the access control; the password is.
- **Two consecutive UDP ports.** The server derives its query port as `SERVER_PORT + 1` and cannot
  be told otherwise, so the adapter refuses a gapped catalog reservation rather than publishing a
  port nothing listens on.
- **`WORLDS_FILE_PERMISSIONS=0755`** is required, not cosmetic. The image chmods every entry in
  `worlds_local` with that value, which defaults to `0644`; modern Valheim stores each world as a
  *directory*, and `0644` on a directory strips the execute bit that makes it traversable. The
  server can then list the world but not open the files inside, so it spins on `The WorldGenerator
  instance was null` and never binds its UDP sockets. It presents as a hang with no permissions
  error anywhere, and `supervisorctl status` shows everything RUNNING.
- **Two persistent paths**: `/config` (worlds, backups, configuration) and `/opt/valheim` (the
  downloaded server build). Both are bind mounts under `/srv/games/valheim-<instance>/`.

## Security model notes

- **Container UID.** Both game images start as root and drop to `PUID:PGID` themselves, so the
  Compose services set no `user:`. Enshrouded keeps `10000:10000` — the UID its previous image
  hardcoded, and the owner of the existing instance data — and Valheim uses the same, with the
  data directories chowned to match. Isolation is enforced through `cap_drop: [ALL]` plus the
  narrow add-back each image's bootstrap needs, `no-new-privileges`,
  Compose/systemd CPU and memory limits, `pids_limit`, and tailnet-only port publishing rather
  than a per-game host UID. Do not enable daemon-wide userns-remap on this live host without a
  planned maintenance window; it affects the already-running interface container.
- **Secrets.** `SERVER_PASSWORD` (and the operator-chosen `SERVER_NAME`) live
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
