# bobiverse machine and network configuration

## Network

Reserved IP: 10.0.0.200

## Note

    Reserved via MAC address (70:85:c2:a3:ff:65) in R7500DHCP settings.

## Tailscale

https://login.tailscale.com/admin/machines

Github Account

- Tailscale version `1.98.8` is installed from Tailscale's official Ubuntu
    repository. The `tailscaled` service is enabled and active.
- The host is authenticated to the tailnet as `bobiverse`; its MagicDNS name is
    `bobiverse.tail40344b.ts.net` and its Tailscale IPv4 address is
    `100.84.161.38`.
- Tailscale SSH, subnet-route advertising, and exit-node advertising are not
    enabled. Private Tailscale Serve HTTPS publishes the game interface at
    `https://bobiverse.tail40344b.ts.net/` and proxies only to its protected
    local Unix socket. Tailscale Funnel is not enabled. No game ports are
    currently published.
- The approved Windows administrator device is `groombridge34`, with Tailscale
    IPv4 address `100.126.180.63`. UFW allows TCP port 22 only from this device
    over Tailscale. Tailscale connectivity was verified in both directions on
    2026-07-12, and SSH to `hlotyaks@bobiverse.tail40344b.ts.net` was verified
    successfully. The trusted-LAN SSH rule remains as a local recovery path.
- The approved `cbrinton` administrator device is `9950x3d`, with Tailscale
    IPv4 address `100.93.220.3`. UFW allows TCP port 22 only from this device
    over Tailscale. Tailscale reachability was verified on 2026-07-12; confirm
    SSH using `cbrinton@bobiverse.tail40344b.ts.net`.
- Rename/tag and approve this machine in the Tailscale admin console as the
    game host. Record its final MagicDNS hostname here after that is complete.
- UPnP is disabled on the Netgear R7500 and was verified with `tailscale
    netcheck` on 2026-07-12: no automatic port mapping is available. Tailscale
    works through outbound NAT traversal/relays and does not require game-port
    forwarding.

### Friend setup instructions

These instructions let friends join the private game network. They do **not**
grant access to the server's desktop, SSH, administrator accounts, or home
network.

1. **Before creating or signing in to Tailscale**, ask the server administrator
    to send an invitation to the private `hlotyaks.github` Tailscale network.
    Open and accept that invitation using an account owned by the player. This
    ensures the device joins the correct private network instead of creating a
    separate personal network by default. Do not use or request the
    administrator's GitHub, Microsoft, or Tailscale password.
2. Open [Tailscale's download page](https://tailscale.com/download) and choose
    the download for the computer being used to play.
3. Install Tailscale. The default installer choices are fine.
4. On Windows, restart the computer after installation if Tailscale will not
    finish starting, appears to hang while signing in, or does not show a
    **Log in** option. Restarting can complete its background-service setup.
5. Open Tailscale from the Start menu (Windows), Applications folder (Mac), or
    app launcher (Linux). Select **Log in** and use the personal account that
    accepted the administrator's invitation. Complete any browser prompts, then
    return to Tailscale.
6. Send the server administrator the device name shown in the Tailscale app
    and confirm that it shows the `hlotyaks.github` network.
    Wait for confirmation that the device has been approved.
7. Leave Tailscale connected while playing. It may stay connected in the
    background; it does not require changing router settings or opening ports.
8. When the administrator says the game is ready, use the server address
    `bobiverse.tail40344b.ts.net` in the game client, along with the game port
    or server-browser instructions provided for that game.

If it does not work, first check that the Tailscale app shows **Connected**.
Then send the administrator a screenshot of the Tailscale app showing the
device name and connection status. Do not send passwords, recovery codes, or
private keys.

## Host firewall

- UFW is enabled and starts automatically at boot.
- Default policy: deny incoming traffic and allow outgoing traffic.
- Explicit inbound SSH rules allow TCP port 22 from the trusted Netgear LAN,
    `10.0.0.0/24`, and from the approved Windows Tailscale administrator
    devices: `100.126.180.63` (`groombridge34`) and `100.93.220.3` (`9950x3d`,
    `cbrinton`).
- No game ports are currently allowed or published. The stale Internet-wide
    Enshrouded rules were removed before the interface was published. Add only
    the documented TCP/UDP ports for an active game when it is deployed and
    restrict them with Tailscale-aware Docker firewall rules.
- Docker Engine `29.1.3` and Docker Compose `2.40.3` are installed. Published
    container ports must be restricted in Docker's `DOCKER-USER` chain (or
    equivalent nftables policy); UFW's ordinary inbound rule alone is
    insufficient.

To inspect the active policy, use:

        sudo ufw status verbose

## Game server service accounts

Create one dedicated, unprivileged Linux account for each game server. Either
`hlotyaks` or `cbrinton` can create an account with:

    sudo create-game-account <game-name>

For example:

    sudo create-game-account valheim

The command creates a system account with a locked password and a `nologin`
shell. It does not grant `sudo`, Docker, `adm`, or other administrative-group
access. Its game data is isolated under `/srv/games/<game-name>/` with private
`server`, `worlds`, `config`, `backups`, and `logs` directories owned by that
account.
It also prints the numeric Compose `user: "UID:GID"` value for the account;
use that value in the matching game's Compose configuration when the image
supports running as a non-root user.

When installing a game, run its systemd service with the matching account as
both `User=` and `Group=`. Do not run game servers as `root`, `hlotyaks`, or
`cbrinton`.

## Game backup and restore process

Game data is stored in `/srv/games/<game-name>/`. Root-owned backup archives
are stored separately in `/srv/game-backups/<game-name>/`, which has `750`
permissions. The game-service account cannot alter the archived copies.

Before creating a backup, stop the game container or use that game's supported
save/snapshot operation so world files are consistent. Then create and verify
an archive with:

    sudo backup-game-data <game-name>
    sudo verify-game-backup <game-name>

The backup utility archives the complete per-game data directory, verifies that
the archive can be read, and writes a SHA-256 checksum. The verification utility
checks the checksum and extracts the archive to a temporary location.

The backup-and-extraction workflow was tested with temporary world and
configuration data on 2026-07-12. Repeat a full restore test using each actual
game's world before relying on its backups.

Both the game data and archive location are currently on the same host/root
filesystem. This protects against accidental deletion or a bad container
update, but not disk failure, theft, fire, or host compromise. Copy verified
archives to separate external or off-site storage before treating them as a
disaster-recovery backup.
