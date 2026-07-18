# Game catalog operations

Phase 1 deploys a root-owned allowlist at `/etc/game-server-interface/catalog.yaml`. It contains only game metadata and preallocated instance slots; it must never contain server passwords, API keys, Docker credentials, or secret file contents.

## Initial allowlist

The catalog currently defines these enabled templates, each with `primary` and `secondary` independently deployable slots:

| Template | Image pin | Slots | UDP ports | Reservation per instance |
| --- | --- | --- | --- | --- |
| Valheim | `ghcr.io/community-valheim-tools/valheim-server@sha256:8e44cfce7b98d4460c3950ab85fa4ad8b82f02ab58b9bcd0b1d8a9620addd054` | 2 | 2456–2457; 2460–2461 | 2 CPU, 4096 MiB RAM, 12 GiB disk |
| Enshrouded | `sknnr/enshrouded-dedicated-server@sha256:269698c5ae61c4cbf01b9ea8473e84b4ff0b98c843842c60ee6a0a22fca0786e` | 2 | 15636–15637; 15640–15641 | 4 CPU, 6144 MiB RAM, 30 GiB disk |

The image pins target Linux/amd64 manifests resolved on 2026-07-12. They are reviewable starting points, not an approval to deploy a game automatically. Instance registration allocates only a listed slot and derives all paths, project names, unit names, service accounts, and backup identities from the catalog templates via `resolve_slot_from_catalog()` in [controller/game_controller.py](../controller/game_controller.py). The root-only steps that turn a registered slot into a running service — service account, data directories, secret env, Compose file, and systemd unit — are documented in [docs/instance-provisioning.md](instance-provisioning.md).

## Deploy or update the catalog

1. Edit the repository copy at [deploy/etc/game-server-interface/catalog.yaml](../deploy/etc/game-server-interface/catalog.yaml).
2. Validate it before installing:

        python3 tools/validate_catalog.py deploy/etc/game-server-interface/catalog.yaml

3. Install it as root-owned configuration:

        sudo install -d -o root -g root -m 0755 /etc/game-server-interface
        sudo install -o root -g root -m 0644 deploy/etc/game-server-interface/catalog.yaml /etc/game-server-interface/catalog.yaml
        sudo python3 tools/validate_catalog.py /etc/game-server-interface/catalog.yaml

Only root may modify the deployed catalog. The web interface and game service accounts must have read-only access if they ever need it; the controller remains the only component allowed to control a registered service.

## New-game request workflow

The dashboard's **Add new game** control does not add a catalog template or create a server. It is available only when `TRUSTED_ACTOR_HEADER=1` and the caller's exact `Tailscale-User-Login` value appears in the root-provided `GAME_INTERFACE_ADMIN_LOGINS` comma-separated allowlist. It exports a bounded JSON request containing a canonical Steam Store app URL, requested catalog slug, short purpose, requester, and timestamp.

Run the following from an administrator workstation or another controlled operator environment, never from the deployed interface container:

        python3 tools/fetch_steam_metadata.py --input game-request-example-123.json --output-dir review/example

The tool uses Steam's public Store app-details response only to prepare advisory metadata and a disabled, non-deployable YAML skeleton. It does not choose an image, digest, ports, resources, credentials, health check, persistent paths, or renderer. Do not commit the request's free-text purpose unless it is appropriate for repository history.

The resulting pull request must contain the completed catalog entry, a reviewed Linux/amd64 immutable image digest, a game-specific renderer adapter and tests, and updates to provisioning/backup/firewall documentation. Keep the template disabled until root provisioning and the controlled rollout checks succeed.

## Instance registration contract

A future controller must reject a registration unless all of the following are true:

- The template and instance ID exist in the catalog, the template is enabled, and the instance count has not reached its configured maximum.
- The slot has not already been registered.
- It derives `/srv/games/<template>-<instance>`, `/srv/game-backups/<template>-<instance>`, `game-<template>-<instance>`, `game-<template>-<instance>.service`, and `game-<template>-<instance>` service account names; callers cannot supply alternatives.
- Every host port is preallocated uniquely by the catalog validator.
- The service account is created with `create-game-account` or an equivalent root-only installer and receives neither `sudo` nor Docker-group membership.
- Secrets are written separately with root-only permissions and are not returned by the controller or copied to logs.

## Reviewed image update workflow

Before changing an image pin:

1. Review the upstream image, resolve the candidate Linux/amd64 digest, and record the source tag and review date in the catalog.
2. Validate and commit the catalog change for review.
3. For each affected running instance, verify a fresh backup using `verify-game-backup <template>-<instance>`.
4. Record that instance's current digest as the rollback candidate in root-owned instance state before replacing its Compose image reference.
5. Deploy the new digest, wait for the catalog health check, and retain the prior image and state until the server has been verified by an administrator.
6. If startup or health checks fail, restore the recorded digest and use the verified backup only if data recovery is required.

The later controller and deployment units will automate the state recording and enforcement steps; do not expose the update operation through the friend-facing interface.
