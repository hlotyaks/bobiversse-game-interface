# Enshrouded 1.0 outage (2026-10-15): the server cannot start, and why

**Status: blocked upstream.** No available image runs Enshrouded 1.0. This is not a configuration
problem with this deployment and cannot be fixed here. Written up so it is not re-diagnosed.

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

- **Our image is abandoned.** `sknnr/enshrouded-dedicated-server` was last built **2026-01-02**;
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

## Options

1. **Wait for mornedhels to release.** Their fix is in progress today. Lowest effort; unknown but
   probably short timeline.
2. **Migrate to mornedhels/enshrouded-server once released.** Worth doing regardless of this
   outage: our current image is abandoned with its source deleted, so the next game update breaks
   us again with nobody to fix it. Needs a new catalog entry (image, digest, `approved_at`), a new
   Compose adapter, and a savegame migration — their layout is `/opt/enshrouded/savegame`.
3. **Build our own image.** Full control, ongoing maintenance burden.

## What is safe

The world is intact: `/srv/games/enshrouded-primary/savegame`, 7.6MB with rolling backups. Nothing
in this outage touched it, and the install directory now persists across restarts, so recovering
costs no re-download.
