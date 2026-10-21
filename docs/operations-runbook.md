# Phase 6+ operations runbook

> **Status: deployed.** These procedures describe the currently installed reliability, backup, and operational controls.

These procedures are root-only unless stated otherwise. Keep the catalog, deployed files, and tailnet policy under version control; do not place passwords, tokens, or save archives in the repository.

## Daily reliability checks

1. Confirm the controller, interface, backup timer, and a game unit are active:
   `systemctl is-active game-server-interface-controller game-server-interface game-server-interface-backup.timer game-enshrouded-primary`.
2. Inspect private interface health through its Unix socket:
   `curl --unix-socket /run/game-server-interface/web/interface.sock --fail http://localhost/healthz`.
3. Review the backup timer and last run:
   `systemctl list-timers game-server-interface-backup.timer` and `journalctl -u game-server-interface-backup.service -n 50 --no-pager`.
4. In the UI, confirm each live instance shows a recent verified backup. A missing or failed backup requires operator review before an image update.

## A game server that looks stuck on "updating"

Enshrouded fetches its server build from Steam at startup rather than shipping it in the image, so
a start that appears hung is usually a download in progress. Check before intervening:

    docker logs -f game-enshrouded-primary 2>&1 | grep -E "progress:|Success! App|ERROR"

A line like `Update state (0x61) downloading, progress: 12.75 (1033108728 / 8102038331)` is healthy
— it is working through 8.1GB. Stopping the unit at that point discards the partial download, and
because `ExecStop` removes the container, the next start begins again from zero. Let it finish.

With the install directory persisted (see
[instance-provisioning.md](instance-provisioning.md)) a restart hash-validates local files instead,
which takes minutes rather than a full transfer. A start that shows *no* progress lines for several
minutes, or an `ERROR` from steamcmd, is a genuine failure worth investigating.

A healthy Enshrouded start ends with these lines, and is the thing to look for:

    [online] Server connected to Steam successfully
    [Session] 'HostOnline' (up)!

**A dead Enshrouded server leaves its container running.** The image runs supervisord, which
restarts the game a few times and then parks it:

    INFO gave up: enshrouded-server entered FATAL state, too many start retries too quickly

The container stays up with nothing serving, so container liveness is not a health signal. Judge it
by `HostOnline` in the log, or by the dashboard's health check.

If the log shows `Failed to initialize Steamworks system...Version Mismatch` / `No SteamUtils011`,
that is the Enshrouded 1.0 Proton incompatibility that took the server down on 2026-10-15 and was
resolved on 2026-10-16 by migrating images. Seeing it again means the running image regressed to an
older Proton — check the digest against the catalog before investigating anything else, and read
[enshrouded-1.0-outage.md](enshrouded-1.0-outage.md).

If the server exits about a second after launch with

    [enshrouded] Could not open log file, error no permission

a stale `logs/enshrouded_server.log` symlink from the old image is in the install directory; delete
it. Details in [instance-provisioning.md](instance-provisioning.md).

**A failed game start currently reports success to systemd.** `docker compose up` without
`--abort-on-container-exit` returns 0 when the container stops, whatever the container's own exit
code, so the unit records `Result=success` and `Restart=on-failure` never fires. With this image a
failed game start usually does not stop the container at all (see above), so the unit looks healthy
either way. Judge a start by the container log, not by the unit's result.

**A server that is up but reports "needs updating" to players is a different fault.** The server
logs `HostOnline` and looks entirely healthy; it is simply serving an old build that updated
clients refuse to join. Compare the installed build with Steam's rather than reading the log:

    docker exec game-enshrouded-primary grep -m1 buildid \
      /opt/enshrouded/server/steamapps/appmanifest_2278520.acf
    curl -s https://api.steamcmd.net/v1/info/2278520 \
      | jq -r '.data."2278520".depots.branches.public.buildid'

If they differ, the start's update attempt hit a steamcmd race and installed nothing while exiting
0 — `Success! App '2278520' already up to date.` in the log with a newer build available is the
signature. Restarting retries the same coin flip, so it fixes it only sometimes. Force it:

    docker exec game-enshrouded-primary /usr/local/etc/enshrouded/enshrouded-force-update

Check nobody is connected first: the image's own player check cannot protect them on this build.
Full diagnosis, including why this is not the image's version check, in
[game-build-updates.md](game-build-updates.md).

## Backup and recovery

Scheduled backups run daily at 02:00 local time with up to 15 minutes of random delay. They invoke the existing root-owned `backup-game-data <template>-<instance>` and `verify-game-backup <template>-<instance>` commands. The scheduler stores only timestamp, verification result, and a bounded error summary in `/var/lib/game-server-interface/backup_status.json`; it never stores game secrets.

To run an immediate backup for Enshrouded without changing the running world:

1. Run `sudo systemctl start game-server-interface-backup.service`.
2. Confirm success with `sudo systemctl status game-server-interface-backup.service --no-pager`.
3. Confirm archive integrity with `sudo verify-game-backup enshrouded-primary`.
4. Keep restore operations root-only. Stop the game, restore following the installed backup tool's documented procedure, fix ownership of the game data directory if required, and start the unit through the UI. Never expose restore endpoints in the web interface.

## Crash loop and manual retry

Game units allow at most five starts in five minutes. When the limit is reached, systemd stops automatic restart and the UI displays `CRASH LOOP` with a **Manual retry** action. Systemd records three automatic restarts before rejecting the final starts at this limit.

1. Preserve evidence first: `sudo journalctl -u game-enshrouded-primary.service -n 100 --no-pager`.
2. Check disk, memory, and the most recent verified backup in the UI.
3. Correct the fault; do not repeatedly retry an unknown failure.
4. Select **Manual retry** in the UI. The controller resets only systemd's failed-limit state, then submits the catalog-allowlisted start operation.
5. If the instance fails again, disable it until an operator has reviewed the image, configuration, and game logs.

## Image update and rollback

1. Announce downtime and confirm a recent verified backup.
2. Review and pin the new immutable image digest in the catalog. Do not use mutable tags.
3. Validate the catalog, deploy it, then restart the affected game via the UI.
4. Confirm active status, game connectivity from an authorized tailnet client, and the audit trail.
5. To roll back, restore the previously reviewed digest from Git, validate and deploy it, then restart the instance. Restore save data only when the game update altered it and the verified backup is required.

## Reboot recovery

After a host reboot, verify Docker, controller, interface, Serve, backup timer, and each intended game unit. Use `systemctl --failed` to find failures. The controller persists completed lifecycle operations for 24 hours; operations interrupted by a controller restart are marked failed rather than silently resumed.

## Register, provision, disable, and retire

- **New template:** review the image and container privileges; define immutable digest, resource reservation, slots, ports, backup/update policy, and renderer support; validate the catalog and test a non-production slot before deployment.
- **New instance:** reserve only an allowlisted slot in the UI, then use the root provisioning script. Check game-port binding is tailnet-only, take and verify a backup, and start through the UI.
- **Disable an unstable instance:** stop its systemd unit, retain its latest verified backup, and remove or disable its catalog slot through a reviewed catalog change. Do not delete data until the retention decision is recorded.
- **Retire an instance:** stop it, verify and retain the final backup, remove its systemd unit and firewall publication, then remove its catalog slot only after the retention period.

## New game requests

Configure `GAME_INTERFACE_ADMIN_LOGINS` as a root-owned, comma-separated list of exact Tailscale login values alongside `TRUSTED_ACTOR_HEADER=1` before exposing the dashboard request control. An empty allowlist denies every request. Both live in `/etc/game-server-interface/interface.env`, which `install-phase5.sh` reconciles in place -- it will not discard settings it did not write. (Before 2026-09-13 it truncated the file on every run, silently emptying the allowlist and demoting every administrator; the only symptom was admin controls quietly vanishing from the dashboard.) After changing either value, redeploy the interface configuration and verify an allowlisted administrator sees **Add new game** while an ordinary dashboard user does not.

The button downloads a review request; it does not alter host state. Move that file to a controlled operator workstation and run `tools/fetch_steam_metadata.py` to create the Steam review package. Review the generated disabled skeleton, implement the required adapter and tests, and open a Git pull request. Discard an unwanted request or branch to roll back: no catalog, game data, account, secret, firewall, or service state has changed before the normal reviewed catalog deployment and root provisioning steps.

## Tailnet member revocation

Remove the person or their device in the Tailscale admin console, review access-control rules, and verify they can no longer reach HTTPS 443 or any approved game port. Tailscale identity remains in append-only audit history; do not edit audit records.
