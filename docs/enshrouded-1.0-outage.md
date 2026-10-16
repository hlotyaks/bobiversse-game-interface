# Enshrouded 1.0 outage (2026-10-15 to 2026-10-16): the server could not start, and why

**Status: resolved 2026-10-16** by migrating to `mornedhels/enshrouded-server` 1.8.0-proton, which
was released that evening with the upstream fix (Ubuntu 26 base, GE-Proton 11-5, `libvulkan1`). The
diagnosis below is kept because it is the reason the image changed, and so none of it is
re-diagnosed if a future game update looks similar. See
[Resolution](#resolution-2026-10-16) at the end.

## Symptom

A restart appears to hang on "updating", then:

    ERROR: Timed out waiting for server to be up and listening on UDP port 15636

That message is misleading twice over. It is emitted after a fixed 60-second wait
(`for i in {1..30}` with `sleep 2` in the image's entrypoint), and it is printed whether the server
is slow, crashed, or never launched. Here the server dies about one second in, so the remaining 59
seconds are the entrypoint waiting on something already dead.

## Actual cause

    [app] start creation step Init_Steamworks
    [steam] Failed to initialize Steamworks system...Version Mismatch
    [steam] Message from Steam: No SteamUtils011
    [app] Failed to create Steamworks.
    [app] start destruction step Init

The game is a Windows binary run under Proton. Enshrouded 1.0 requires the `SteamUtils011`
Steamworks interface, which **GE-Proton 10-26 cannot bridge**. Upstream's fix is Proton 11-x.

## Why it cannot be fixed here

- **The image we were on is abandoned.** `sknnr/enshrouded-dedicated-server` was last built **2026-01-02**;
  its newest tag (`proton-v2.3.2` = `proton-latest` = `latest`, digest `c410b742`) dates from then,
  and its GitHub repository returns 404. The pinned digest and `latest` are the *same build*:
  identical entrypoint, identical example config, identical bundled `steamclient.so`, identical
  `created` timestamp. There is no newer image to approve.
- **Dropping in Proton 11-9 does not work.** The entrypoint resolves
  `compatibilitytools.d/GE-Proton${GE_PROTON_VERSION}`, so a newer Proton can be mounted in and
  selected by env. Tried; it fails earlier:

      OSError: libvulkan.so.1: cannot open shared object file: No such file or directory

  GE-Proton 11-x links libvulkan, which this headless image does not ship. Fixing it needs a
  rebuilt image, not a mount — which is precisely what upstream is doing.
- **The maintained alternative has the same bug today.**
  [mornedhels/enshrouded-server](https://github.com/mornedhels/enshrouded-server) (active, pushed
  2026-10-15) carries issue **#254** — "Enshrouded 1.0 fails to start on latest: Steamworks Version
  Mismatch / No SteamUtils011", the identical error — with fix PR **#255** *open and unmerged*, and
  commits today bumping proton-ge to 11-9 and repairing the Docker build.

## Things ruled out along the way

Each of these looked plausible and was tested rather than assumed:

| Theory | Verdict |
| --- | --- |
| steamcmd self-update wipes `compatdata/<appid>` | No — the directory survives a self-update |
| The install was corrupt from an interrupted download | No — steamcmd reports `already up to date` |
| The bundled Linux `steamclient.so` is stale | Refreshed by steamcmd (Dec 2025 → Oct 2026), and irrelevant: the game is a Windows binary and loads `steamclient64.dll` |
| `STEAM_COMPAT_CLIENT_INSTALL_PATH` points somewhere without a Windows Steam client | Repointed at the install dir, which has a current `steamclient64.dll`; same failure |
| A newer image of the same project would fix it | `latest` is the same build as the pinned digest |
| Proton rebuilds its prefix slowly, exhausting the 60s | No — an empty prefix is 8KB; a bind mount there *broke* it instead (see instance-provisioning.md) |

## Resolution (2026-10-16)

Upstream released **1.8.0** at 19:46 UTC, closing issue #254. The fix is in
[PR #252](https://github.com/mornedhels/enshrouded-server/pull/252): Ubuntu 26 base image,
GE-Proton **11-5**, and `libvulkan1` — the package whose absence made mounting Proton 11 into the
old image fail. We migrated to `mornedhels/enshrouded-server:1.8.0-proton`
(`sha256:689dcb30…`) rather than waiting for a sknnr rebuild that is never coming.

Verified on this host before the catalog digest was approved, with the real install directory
mounted and an isolated copy of the world:

    [app] start creation step Init_Steamworks
    Game Version (SVN): 1269579
    [online] Server connected to Steam successfully
    [online] Server SteamId: 90295057295641613
    [Session] 'HostOnline' (up)!

About 20 seconds from container start to `HostOnline`, under the same narrow capability set the
rendered Compose file uses.

**The 1.0 build converts the savegame, and it did so on the live world.** The EA-era save was
picked up (`[savedata2-migration] Found file '3ad85aea'…`), deserialized in 2.98s, and rewritten in
the new format:

    savegame/world0.ksd                 the live world (1.6MB)
    savegame/migrated/3ad85aea*         the EA files, moved aside intact
    savegame/backup/world0.ksd.backup-N the game's own rolling backups, now of world0.ksd

Nothing is lost — the old files are preserved under `migrated/` — but the conversion is **one-way**:
once `world0.ksd` exists, the EA-era server cannot read the world's current state, so rolling back
to the old image means rolling back to the pre-migration save and losing any play since. That
tarball is at `/srv/games/enshrouded-primary/savegame-pre-1.0-migration.tar.gz`.

The rolling-save *file names* therefore changed, which matters only for restores: nothing in this
repo keys on them. The usage meter reads the game's log through `docker logs`, not the save, and
backups archive the directory.

One migration trap, found by testing rather than reasoning: the **old image left a
`logs/enshrouded_server.log` symlink to `/proc/1/fd/1`** in the persisted install directory. It
exists so the game's file log lands on container stdout. Under the new image the game runs as
uid 10000 while PID 1 is root, so opening it is denied and the server exits one second in with

    [enshrouded] Could not open log file, error no permission

which looks exactly like a fresh image bug and is not one. Deleting the stale symlink fixes it; the
new image lets the game write a real log file and still carries the same lines on stdout, which is
what the usage meter reads. A future instance provisioned from scratch never has the symlink — but
**the old image recreates it on every start**, so if it ever runs again it has to be deleted again
before the new image will start. That happened during this very migration: the instance was
re-provisioned before the new catalog had reached the host, so the old image started once, put the
symlink back, and the first start on the new image failed on it.

Metering was re-verified against the new image's output rather than assumed: the game's
`Session`/`Machines:` block and its `[online] Added peer … (steamid:…)` lines pass through the new
image's syslog prefix intact, and `tools/presence_meter.py` parses a complete block from it
unchanged. See [usage-metering.md](usage-metering.md).

### Operational difference worth knowing

Under the old image, a server that failed to launch took the container down with it. The new image
runs supervisord, which restarts the server a few times and then parks it in `FATAL` state — so a
**dead server leaves the container running**. Watch for `entered FATAL state, too many start
retries too quickly` in the logs; the dashboard's own health check, not container liveness, is what
tells you the game is down.

## What was safe throughout

The world was never at risk: `/srv/games/enshrouded-primary/savegame`, 7.6MB with the game's own
rolling saves. Nothing in the outage or the migration touched it — the migration was tested against
a copy, and a tarball snapshot was taken first at
`/srv/games/enshrouded-primary/savegame-pre-1.0-migration.tar.gz`. The install directory persists
across restarts, so recovery costs no re-download.
