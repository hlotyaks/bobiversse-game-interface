# Presence source: why conntrack is blind under Tailscale (and when to switch back)

The usage meter ([tools/presence_meter.py](../tools/presence_meter.py)) needs to know **which
tailnet identities are playing each game**. It supports two sources — `tailscale` (default) and
`conntrack` — because the right choice depends on how players reach the server. This documents the
2026-07-18 investigation that drove the default to `tailscale`, so a future move to cloud hosting
can re-evaluate deliberately.

## The original design (conntrack) and why it was reasonable

Game servers here (Enshrouded in particular) do **not** log player identity — the server stdout
only prints an anonymous connected-machine count (`Machines: … m#1 … remote N`). The first design
therefore read identity from the network: enumerate the live UDP flows to the game port with
`conntrack -L -p udp --dport <port>`, keep the tailnet (`100.64.0.0/10`) sources, and map each IP
to a login via `tailscale status --json`. Clean, game-agnostic, per-instance.

## What we observed on bobiverse (2026-07-18)

With a friend actively in the Enshrouded world:

- **The server confirmed a live player.** Its log showed `m#1(129): … remote 54, ping 44 ms,
  OperatingNormally` — a real client exchanging packets.
- **conntrack saw nothing on the game port.** `conntrack -L -p udp --dport 15636` → `0 flow
  entries`. A full `conntrack -L -p udp` dump (67 flows) contained **no `100.x` source on any
  port** and nothing on 15636. Every flow was one of:
  - Tailscale coordination/STUN — `src=<host-LAN> sport=41641 dport=3478`,
  - WireGuard tunnels — `sport=41641 dport=41641` between the host and peers,
  - the game container reaching Steam — `src=172.19.0.2 … dport=270xx`,
  - LAN/DNS/SSDP noise.

## Why conntrack cannot see it

Players connect over the tailnet, so their game packets travel **inside the encrypted WireGuard
tunnel** (the `41641↔41641` UDP flows are all conntrack sees). `tailscaled` decrypts them and
delivers them to the game port via the `tailscale0` interface / userspace path, which does **not**
produce a kernel conntrack entry of the form `client → 100.84.161.38:15636`. The Docker userland
proxy compounds it: even inside the container the client source is rewritten to the bridge gateway
(`172.19.0.1`), so the container can't identify players either. There is no game port to watch at
the conntrack layer — the traffic simply never appears there as a trackable client→port flow.

This is not fixable by changing the watched port or filter. It is a property of Tailscale-tunnelled
delivery.

## The working source (tailscale)

`tailscale status --json` sees exactly what conntrack cannot. During the same session:

| Peer | login | Active | Rx+Tx |
| --- | --- | --- | --- |
| 9950X3D (the player) | cbrinton@… | **true** | ~18.9 MB |
| Groombridge34 (dashboard only) | hlotyaks@… | true | ~3.3 MB |
| others | … | false | 0 |

So the meter's default source reads `tailscale status --json` and treats a peer as **playing** when
it is `Active` **and** its traffic rate since the last sample exceeds a threshold
(`--min-kbps`, default 25). The rate is essential: the `Active` flag alone over-counts — a peer with
the dashboard open is "active" but not playing, and the server confirmed only one in-game client.
Presence is attributed to instances whose systemd unit is active. Identity is the Tailscale login,
the same identity the dashboard already attributes by.

Limitations (acceptable for a dry-run, friend-group Stage 1): a one-cycle startup lag (a rate needs
two samples); the threshold may need tuning per game; and if two games run at once, a player's
traffic can't be attributed to a specific one (it counts toward each running instance). These are
documented in [usage-metering.md](usage-metering.md).

## When to switch back to conntrack

`conntrack` becomes the right source **only if players connect directly to a published UDP port,
i.e. not through a WireGuard tunnel** — for example a cloud host that exposes a public game port and
drops Tailscale (see [cloud-hosting-cost-analysis.md](cloud-hosting-cost-analysis.md)). Then
`conntrack -L` shows real `client → game-port` flows again and `--source conntrack` works as
originally designed.

**Caveat for that scenario:** the conntrack source still resolves IP→identity via
`tailscale status`. A Tailscale-less cloud has no such map, so it would yield client *public IPs*,
not logins — identity attribution would need a new mechanism (per-player reserved addresses, an
account/login system, or keeping Tailscale purely for identity while exposing the game port). Decide
that as part of the cloud migration; the conntrack code path and its tests are preserved so the
networking half is ready.

Switching sources is a one-line change to the unit's `ExecStart` (`--source conntrack`), which also
uses the `CAP_NET_ADMIN` already granted in the unit and the `conntrack` package.
