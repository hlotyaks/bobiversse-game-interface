# Secure Game Server Access Plan

Use two supported access methods:

1. **Recommended:** private overlay network such as Tailscale, with each friend authorized individually. This needs no public IP, no public game ports, and works even if Verizon uses CGNAT.
2. **Fallback:** direct public game hosting through tightly scoped port forwarding. This requires a public WAN IPv4 address on the Verizon CR1000B and forwards through both routers.

## Current topology

Internet → Verizon CR1000B → Netgear R7500 WAN `192.168.1.151` → Netgear LAN `10.0.0.0/24` → Ubuntu 26.04 game server via Ethernet.

The R7500 address of `192.168.1.151` is expected: it is private because the CR1000B is routing in front of it. It does **not** tell whether Verizon assigned the CR1000B a public Internet address.

## Phase 1 — Secure the server first

1. Reserve a fixed DHCP address for the server in the R7500, such as a stable `10.0.0.x` address. DONE
2. Apply Ubuntu security updates and enable unattended security updates. DONE
3. Use a non-root administration account. DONE.
4. Configure SSH with keys only; disable root login and password authentication. DONE (only disabled root login)
5. Run every game server in its own Docker Compose project and under a dedicated unprivileged Linux account/UID. DONE (account-creation automation is installed and tested; create an account when each game is deployed):
   - Create the service account with `create-game-account <game-name>`; do not add it to the `docker` group. Docker-group access is effectively root-equivalent on the host.
   - Run the container process with that account's numeric UID:GID (`user: "UID:GID"` in Compose) whenever the image supports it. Do not use `privileged: true`, host networking, host PID/IPC namespaces, extra Linux capabilities, or the Docker socket inside a game container.
   - Keep root-owned Compose files and systemd units separate from game data. Use systemd to start the Compose project rather than granting a service account Docker-daemon access.
   - Prefer rootless Docker or Docker user-namespace remapping where compatible with the game image; test game networking and volume permissions before relying on either.
6. Configure UFW or nftables: DONE (UFW baseline; add narrow game and Docker rules when a game is deployed)
   - Default deny for inbound traffic.
   - Allow established/related connections.
   - Allow game ports only when required.
   - Allow SSH only from the private overlay network or trusted internal LAN.
   - Never expose SSH, Docker’s API, remote desktop, Cockpit, game-management panels, or databases to the Internet.
   - Account for Docker's firewall rules. Published container ports can bypass ordinary UFW inbound rules, so enforce Docker-specific allow rules in the `DOCKER-USER` chain (or use an equivalent nftables policy) for the exact game ports and approved source networks. Do not rely on UFW alone for published container ports.
7. Store server worlds, configuration, and backups separately. Test restoring a world before relying on the backups. DONE (backup tooling and an extraction/restore workflow test are in place; repeat the restore test for every actual game world):
   - Use per-game bind mounts under `/srv/games/<game-name>/` for world data, configuration, and logs; do not keep the only copy in a disposable container layer.
   - Create root-owned, checksum-verified archives with `backup-game-data <game-name>` in `/srv/game-backups/<game-name>/`, then validate them with `verify-game-backup <game-name>`.
   - The current archive location is on the same host filesystem as game data. Copy verified archives to external or off-site storage for disaster recovery.
8. Treat this machine as a dedicated game host: do not keep personal documents, browser sessions, or reused passwords on it.

## Phase 2 — Recommended: private friend-only access

1. Install and authorize Tailscale on the Ubuntu host: DONE (host software is installed, authenticated, and running; complete the admin-console rename/tag and device-approval check before admitting other devices)
   - Install from Tailscale's current official Ubuntu instructions, then run `sudo tailscale up` and sign in using the administrator's tailnet account.
   - In the Tailscale admin console, rename/tag the device as the game host, confirm it is approved, and note its MagicDNS name and Tailscale IPv4 address.
   - Do not enable subnet routing, exit-node advertising, or SSH over Tailscale unless specifically needed later.
   - Update the host firewall to allow SSH only from the administrator's Tailscale device/IP and to allow no game ports until a game is deployed.

2. Test administrator access from the Windows computer: DONE (Windows device `groombridge34` is enrolled; SSH to `hlotyaks@bobiverse.tail40344b.ts.net` was verified over Tailscale)
   - Install the official Tailscale Windows client and sign in to the same tailnet account used for the Ubuntu host.
   - Confirm both the Windows device and game host appear as approved devices in the admin console.
   - From Windows PowerShell, test reachability with `tailscale ping <host-MagicDNS-name>`.
   - Test SSH using `ssh hlotyaks@<host-MagicDNS-name>` (or `cbrinton`), then confirm that a LAN-only address is not required. Use the administrator account password until SSH keys are adopted.
   - Confirm the SSH login works through Tailscale before removing the trusted-LAN SSH firewall rule; retain a local-console recovery path.

3. Invite and test one friend as a least-privilege pilot:
   - Invite a known friend from the Tailscale admin console. Require approval for their account/device before it joins the tailnet.
   - Have the friend install the official Tailscale client for their operating system, sign in using their own invited account, and send the device name for approval.
   - Create an ACL/grant that permits the friend's device/group to reach only the intended published game TCP/UDP ports on the game host. Do not grant SSH, all-host access, subnet routes, or exit-node access.
   - Ask the friend to run `tailscale ping <host-MagicDNS-name>`, then test the game client only after the game container and narrow Docker-aware firewall rules are in place.
   - Verify that the friend cannot connect to TCP port 22 or any unapproved port. Revoke the test device/account and confirm all access stops.

4. Use ACLs/grants so friends can reach only the specific game-server ports on this one host.
   - Match the ACL/grant ports to the Docker-published game ports, and restrict those published ports with the Docker-aware host firewall policy.
5. Restrict SSH to the administrator’s approved account/device, not all friends.
6. Share the server’s stable tailnet hostname with friends rather than an IP address.
7. Keep both routers free of game-related port forwards and disable UPnP.

This is the strongest practical model for a private group because the game services are unreachable from the general Internet. An attacker would first need an authorized device/account before reaching the game host.

**Alternative private option:** self-host WireGuard. It provides similar access control through one unique key per friend, but requires more key management and typically one public UDP router forward. Tailscale is the simpler starting point.

## Phase 3 — Determine whether direct public hosting is possible

1. In the CR1000B’s status page, find the **WAN/Internet IPv4** address; do not use its LAN address, DNS server, or the R7500’s WAN address.
2. Compare its category to an external-IP lookup from a device on the home network:
   - **Public IPv4:** direct port forwarding can work.
   - `10.x.x.x`, `172.16.x.x` through `172.31.x.x`, or `192.168.x.x`: upstream private NAT; direct forwarding cannot work.
   - `100.64.x.x` through `100.127.x.x`: CGNAT; direct forwarding cannot work.
3. If it is CGNAT/private, use Tailscale/WireGuard or request a public IPv4 option from Verizon.

## Phase 4 — Direct public access with the existing double NAT

Keep the current topology initially; it is the least disruptive option.

For each required game port and protocol:

1. Create a forwarding rule on the CR1000B:
   - External game port → R7500 WAN address `192.168.1.151`.
2. Create the matching forwarding rule on the R7500:
   - Same port/protocol → the reserved game-server `10.0.0.x` address.
3. Add a matching narrow inbound rule on the Ubuntu firewall.
4. Do not forward broad ranges “just in case.”
5. Do not enable UPnP.

Known examples to validate against current official dedicated-server documentation before configuring:

- Valheim commonly uses UDP `2456–2458`.
- Enshrouded commonly uses UDP `15636–15637`.
- Space Engineers 2 ports must be confirmed from its current dedicated-server documentation when it is deployed.

Each additional game should follow the same process: publisher documentation → exact TCP/UDP ports → rules on both routers → host firewall rule → off-site test.

## Phase 5 — Optional network simplification

If maintaining two forwarding rules per port becomes inconvenient, choose one of these only after confirming that the CR1000B does not have Verizon TV/MoCA or other routing dependencies:

- **Netgear access-point mode:** CR1000B becomes the only router, and game ports are forwarded only once to the server.
- **CR1000B router-only DMZ/IP-passthrough to the R7500:** the CR1000B sends unsolicited traffic to the R7500, while the R7500 still allows only explicit port forwards to the game server.

Do **not** configure the game server itself as a DMZ host. It would expose every listening service and turns any host-firewall error into an Internet-facing exposure.

## Phase 6 — Game and operational security

1. Use strong per-game server passwords, platform authentication, whitelists, player limits, and moderation roles where available.
2. Patch the OS, game server, mods, and container/runtime software promptly.
   - Patch Docker Engine, Docker Compose, and base/game images. Pin image versions or digests rather than using `latest`; regularly pull reviewed updates, recreate the affected container, and retain a rollback image until it is verified.
3. Prefer stable releases and vetted mods; mods are executable-risk dependencies.
4. Use dynamic DNS only if friends need a stable public hostname; share it only with the group.
5. Remove forwarding rules for inactive games.
6. Review router port-forward rules periodically.
7. Monitor disk usage, game logs, authentication failures, server restarts, and backup job results.
8. Revoke a former player’s Tailscale/WireGuard access immediately.

## Verification

1. Confirm the server retains its reserved address after reboot and DHCP renewal.
2. Confirm host firewall inspection shows only intended game listeners; public SSH/admin services must be absent.
3. Test Tailscale from an authorized off-site device and confirm the games connect.
4. Revoke a test friend device/account and confirm it can no longer reach the game server.
5. For direct access, test from cellular or another non-home network; local testing alone cannot validate double-NAT forwarding.
6. Verify only intended public ports respond externally.
7. Restore one test copy of a game world from backup.
8. Reboot the host and routers during a maintenance window and confirm recovery of services and access.

### Tailscale pilot verification

1. Confirm the Ubuntu host and Windows administrator device are approved in the same tailnet and resolve each other's MagicDNS names.
2. Confirm the Windows administrator can SSH to the host through Tailscale and retains a local-console recovery path.
3. Confirm the invited friend can reach only the approved game port after a game is deployed, and cannot reach SSH or unapproved ports.
4. Revoke the friend's test device/account and confirm its game-port access immediately stops.

## Decision

Start with Tailscale for Enshrouded, Space Engineers 2, Valheim, and future games. Add narrow direct port forwarding only when a specific game or player cannot use the private client. This avoids requiring a public IP for normal use and offers the best “friends only” boundary without exposing the server through DMZ.
