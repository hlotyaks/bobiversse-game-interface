# Operations runbook

These procedures are root-only unless stated otherwise. Keep the catalog, deployed files, and tailnet policy under version control; do not place passwords, tokens, or save archives in the repository.

## Daily reliability checks

1. Confirm the controller, interface, backup timer, and a game unit are active:
   `systemctl is-active game-server-interface-controller game-server-interface game-server-interface-backup.timer game-enshrouded-primary`.
2. Inspect private interface health through its Unix socket:
   `curl --unix-socket /run/game-server-interface/web/interface.sock --fail http://localhost/healthz`.
3. Review the backup timer and last run:
   `systemctl list-timers game-server-interface-backup.timer` and `journalctl -u game-server-interface-backup.service -n 50 --no-pager`.
4. In the UI, confirm each live instance shows a recent verified backup. A missing or failed backup requires operator review before an image update.

## Backup and recovery

Scheduled backups run daily at 02:00 local time with up to 15 minutes of random delay. They invoke the existing root-owned `backup-game-data <template>-<instance>` and `verify-game-backup <template>-<instance>` commands. The scheduler stores only timestamp, verification result, and a bounded error summary in `/var/lib/game-server-interface/backup_status.json`; it never stores game secrets.

To run an immediate backup for Enshrouded without changing the running world:

1. Run `sudo systemctl start game-server-interface-backup.service`.
2. Confirm success with `sudo systemctl status game-server-interface-backup.service --no-pager`.
3. Confirm archive integrity with `sudo verify-game-backup enshrouded-primary`.
4. Keep restore operations root-only. Stop the game, restore following the installed backup tool's documented procedure, fix ownership of the game data directory if required, and start the unit through the UI. Never expose restore endpoints in the web interface.

## Crash loop and manual retry

Game units allow at most five starts in five minutes. When the limit is reached, systemd stops automatic restart and the UI displays `CRASH LOOP` with a **Manual retry** action.

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

## Tailnet member revocation

Remove the person or their device in the Tailscale admin console, review access-control rules, and verify they can no longer reach HTTPS 443 or any approved game port. Tailscale identity remains in append-only audit history; do not edit audit records.
