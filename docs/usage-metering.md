# Usage metering (Stage 1: playtime + dry-run cost sharing)

This is the first of two stages toward the cost-sharing feature described in
[docs/cloud-hosting-cost-analysis.md](cloud-hosting-cost-analysis.md).

- **Stage 1 (this doc):** measure who plays, when, and with how many others, and compute a
  *hypothetical* cost-share bill. **No money is moved and no payment credentials exist.** It runs
  against the current free `bobiverse` host so the group can test-fly the model — see the real
  hours, the solo-vs-group split, and how each person's share would fall — before committing to
  cloud infrastructure or charging anyone.
- **Stage 2 (later):** money handling (Stripe enrollment, monthly invoicing, reconciliation).
  Not started; it builds on the ledger this stage produces.

## How players are identified

Enshrouded does not log player identity — its stdout only reports an anonymous connected-machine
count — so identity comes from the network layer. The meter has two interchangeable sources:

- **`tailscale` (default).** Players reach the game over the tailnet. A 2026-07-18 test found no
  `client → game-port` flow in conntrack and concluded the tunnel hid it; that conclusion was
  **over-generalised and is now partly retracted** — tailnet traffic *is* conntrack-tracked, and
  whether the game's UDP flows appear is an open question with a live re-test pending (see
  [presence-source-conntrack-findings.md](presence-source-conntrack-findings.md)). The meter splits
  the question into **how many** and **who**:
  - **How many** connected clients there are comes from the *game itself*. Enshrouded logs a
    per-machine block every ~30s (`m#N(...) … OperatingNormally`, the server's own entry excluded);
    the meter reads it via `docker logs`. This is authoritative and needs no tuning.
  - **Who** they are comes from `tailscale status --json`: the reported client count is attributed
    to the busiest tailnet peers by traffic rate. Identity is the Tailscale login, the same one the
    dashboard uses, so there is **no separate login system**.

  This replaced an earlier "peer is `Active` and above `--min-kbps`" heuristic that silently
  undercounted: real per-client Enshrouded traffic (~single-digit kbps) sits far below any usable
  bandwidth threshold, so genuine players were dropped while a host with ambient non-game tailnet
  traffic leaked through. The game's own count is the reliable player/idle discriminator. Games
  **without** an occupancy reader still fall back to the `--min-kbps` traffic-rate heuristic.
  Presence is attributed only to instances whose systemd unit is active.
- **`conntrack`.** Watches `conntrack -L` for direct `client → game-port` flows, naming each client
  exactly rather than ranking by bandwidth. Believed blind under Tailscale (above) — a belief now
  under re-test — and the right source for a future cloud/public-IP deployment without the
  WireGuard tunnel. Preserved and tested; switch with `--source conntrack`.

Attribution assumes the game's connected clients are the busiest tailnet peers. Two mechanisms keep
that honest:

- **Smoothing.** Per-peer rates are EWMA-smoothed, so a single-cycle burst or a player's transient
  tailscale counter reset no longer flips a slot to the wrong person (observed crediting a solo
  player's time to a bystander before smoothing).
- **Exclusions.** Some tailnet peers are *never* players — a server admin or dashboard-only user
  whose HTTPS/SSH traffic to the host is indistinguishable by volume from game traffic. There are
  two ways to exclude them, both applied before attribution so a game's slots go to actual players:
  - **Per-game (preferred), admin-managed at runtime.** An administrator edits the exclusion list
    for each game on the dashboard's **Exclusions** page (a login excluded from Enshrouded can still
    be metered as a player of other games). This writes the controller-managed
    `/var/lib/game-server-interface/presence-exclusions.json` (`{template_id: [logins]}`); the meter
    re-reads it every cycle, so a change takes effect within a minute with **no restart**. Seeded on
    install with `enshrouded → hlotyaks@github` (the non-playing admin); the installer never
    overwrites the live file. Under the hood: interface `GET`/`POST /api/exclusions` (admin-gated) →
    controller `list_exclusions` / `set_exclusions` (validated, atomic, audited).
  - **Global.** `--exclude-login <login>` (repeatable) in the meter's unit drops a login from *every*
    game — use it only for an account that is never a player of anything (e.g. a monitoring bot).

Correcting a bad capture after the fact (e.g. a mis-attributed login from before an exclusion was
added): `sudo /usr/local/libexec/game-server-interface/ledger_admin.py --remove-login <login>`
(add `--dry-run` first). It strips the login from every sample's `present` list, keeping emptied
samples as `present: []`, atomically and at mode `0600`.

Other known limits of the default source (fine for a dry-run Stage 1): a one-cycle startup lag (the
identity rate needs two samples); a non-player peer generating *sustained* heavy game-like traffic
that isn't on the exclusion list could still be mis-ranked; and if two games run at once a player's
traffic counts toward each running instance (it can't be split between them). Adding an occupancy
reader for another game is a small function keyed by template in
[tools/presence_meter.py](../tools/presence_meter.py) (`OCCUPANCY_READERS`).

## Components

| Piece | File | Role |
| --- | --- | --- |
| Presence meter | [tools/presence_meter.py](../tools/presence_meter.py) | Root systemd service. Each cycle reads the game's client count and `tailscale status --json`, attributes that count to the busiest tailnet peers of active game units, and appends an occupancy sample to the ledger. (`--source conntrack` swaps in the direct-flow source for non-Tailscale deployments.) |
| Live observer | [scripts/observe-presence.py](../scripts/observe-presence.py) | Read-only. Run as root during a real session to watch the count, every peer's byte deltas/EWMA, what the meter would attribute, and the conntrack flows side by side. The tool for diagnosing a *who* failure. |
| Presence ledger | `/var/lib/game-server-interface/presence.jsonl` | Append-only JSONL, one line per instance per cycle: `{"ts","instance","present":[logins],"count":N}`. `count` is the game's own client count and `present` is only who the meter could name, so `count >= len(present)`; `"count": null` means the occupancy read failed (unknown, *not* nobody). Root-owned, `0600` — it is playtime metadata (who played when); treat it as private, like the audit log. |
| Billing config | [deploy/etc/game-server-interface/billing.yaml](../deploy/etc/game-server-interface/billing.yaml) | Nominal per-instance run-cost and the group-size multiplier schedule `m(n)`. No secrets. |
| Billing calculator | [tools/billing.py](../tools/billing.py) | Pure calculator over the ledger. Produces per-user hours, solo/group split, sessions, and the dry-run bill (text or `--json`). |

## Counting and naming are separate problems

The meter answers two questions per cycle and they fail independently:

- **How many** — the game's own count. Reliable.
- **Who** — bandwidth ranking over tailnet peers. **This is the weak half**, and it has now failed
  three times (the original `--min-kbps` undercount, the transient-burst misattribution that EWMA
  smoothing addressed, and the 2026-08 blackout below).

The ledger therefore records **both** numbers. When the game says three clients and the meter can
only name one, that is written down as `{"present": ["a@ex"], "count": 3}` and the bill charges the
group rate `m(3)/3` for the person it named, reports the other two player-hours as
**UNATTRIBUTED**, and leaves their share unbilled. It does *not* silently read a one-name list as
solo play — which is exactly what produced the bad August bill:

> **2026-08 incident.** Enshrouded logged three connected clients continuously from 19:11 to 21:44
> on 2026-08-23; the ledger recorded `present: []` for that entire window, and no peer's
> `tailscale status --json` byte counters moved enough to clear the 1.0 kbps attribution floor.
> Separately, whenever the occupancy read failed the meter fell back to `--min-kbps 25` and
> credited the top talker — an idle SSH session to this host measures ~24 kbps, so an administrator
> was repeatedly billed for solo play the game never saw. August's report read
> "players: 2, solo share 100%" for an evening that had four people in it.

The `count` field fixes the *billing* consequence. It does not fix the *naming* half. For that,
`game-presence-observer.service` runs continuously alongside the meter, recording what attribution
*saw* rather than only what it concluded — the game's client count, every peer's byte counters and
derived kbps/EWMA, who would be named, and the conntrack flows on the game port. Read it with:

    sudo /usr/local/sbin/gsi-diagnose observer

It logs only cycles that carry information (someone connected, the occupancy read failed, a client
went unnamed) plus a heartbeat every 20th cycle, so an idle server writes ~150 records a day into
`/var/lib/game-server-interface/presence-observer.jsonl` (root-owned `0600`, rotated weekly, 16
weeks kept). A *single* player logging in is a useful test on its own: if the log shows
`count=1 unnamed=1`, the naming failure reproduces with one person and no coordination. See also
[presence-source-conntrack-findings.md](presence-source-conntrack-findings.md), whose central
claim (that conntrack cannot see tailnet traffic) has been shown to be wrong.

**A game that reports its own occupancy never falls back to bandwidth ranking.** A failed read is
recorded as unknown and billed as nothing. The `--min-kbps` fallback now applies only to games with
no entry in `OCCUPANCY_READERS`.

## The bill model

For each sample interval of duration `d` with `n` players present, each present user accrues
`rate_per_second * d * m(n) / n`. `m(1) > 1` makes solo a premium; `m(n) < 1` for larger groups
subsidizes group play. Charges therefore do **not** sum to the raw server cost per interval — the
difference is the shared **kitty**, reported so the group can confirm it nets out over time. See
the cost-analysis doc for the rationale and the tuning discussion.

Here `n` is the **game's** reported client count, not how many of those clients the meter managed to
identify. Occupancy is a step function between samples; a sample's duration is the gap to the next
sample, capped at `max_gap_seconds` so meter downtime is never billed as continuous play. Samples
with an unknown count are billed as nothing and totalled separately as `meter_blind_hours`.

## Install (root)

    sudo scripts/install-usage-metering.sh

This installs the meter and calculator to `/usr/local/libexec/game-server-interface/`, installs
the billing config (without overwriting an edited copy) and the systemd unit, and starts
`game-presence-meter.service`. Runtime prerequisite: the `tailscale` CLI (the default source);
`conntrack` is only needed if you switch to `--source conntrack`.

## Read a report

    sudo /usr/local/libexec/game-server-interface/billing.py --instance enshrouded-primary
    # or from the repo, against any ledger/config:
    python3 tools/billing.py --ledger /var/lib/game-server-interface/presence.jsonl \
      --config /etc/game-server-interface/billing.yaml --instance enshrouded-primary --json

Example (synthetic session — alice solos, then bob and cara join):

    Usage report for enshrouded-primary
      server up: 1.83h   players: 3   solo share of playtime: 8.3%

      user            hours    solo   group  solo%      bill
      alice@ex         1.83    0.33    1.50    18%  USD 0.22
      bob@ex           1.17    0.00    1.17     0%  USD 0.09
      cara@ex          1.00    0.00    1.00     0%  USD 0.08
      actual cost: USD 0.33   charged: USD 0.39   kitty: USD 0.06

alice played the same 1.83h envelope as the session but owes the most because her solo stretch
carries the premium — the incentive is legible directly on the bill.

## Validating the presence meter live

The parsing and billing logic are covered by unit tests, but the live capture can only be confirmed
with a real session. With at least one player connected (and after ~2 sample intervals, since a
traffic rate needs two samples), check that the meter sees them:

    sudo tail -f /var/lib/game-server-interface/presence.jsonl     # your instance line should list logins

Cross-check identity and rate against the source the meter reads:

    tailscale status                                               # the player shows as active with traffic

If a real player is `Active` but never appears in the ledger, the `--min-kbps` threshold is likely
too high for that game — lower it in the unit's `ExecStart`. If a non-player (dashboard viewer) is
wrongly counted, raise it. Background on why this replaced the conntrack source is in
[presence-source-conntrack-findings.md](presence-source-conntrack-findings.md).

## Web dashboard

The dashboard is organised as a left **sidebar** with pages: **Controls** (the game catalog,
capacity, and start/register/restart actions), **Billing**, and — for administrators only —
**Exclusions** (the per-game non-player list described above; the nav item is hidden for non-admins
and the `/api/exclusions` routes reject non-admins server-side regardless).

The **Billing** page shows a **Your bill** panel with a world selector and a **month selector** (the
current month "to date" plus any past months present in the ledger). Data flows
`presence ledger -> billing.py -> controller "billing" read action -> interface /api/billing ->
UI`, keyed to the viewer's Tailscale login:

- **Every player** sees only their own line — hours, solo/group split, and their dry-run share.
- **Administrators** (the `is_game_administrator` gate: `TRUSTED_ACTOR_HEADER=1` and the login in
  `GAME_INTERFACE_ADMIN_LOGINS`) additionally see the full per-user table and the aggregate
  totals (server-up hours, actual cost, charged, kitty). Non-admins never receive other players'
  data — the interface filters the controller's full report down to the caller's own line before
  it reaches the browser.

The controller reads the ledger and `billing.yaml` (both root-owned) and calls the sibling
`billing.py`; the read is audited by actor like every other controller action. No new install
step beyond `install-usage-metering.sh` (which places `billing.py` next to the controller); the
admin view requires the interface's admin env, same as the other admin features.

## Not in this stage

No payments. Stripe money-handling (enrollment, monthly invoicing, reconciliation) is Stage 2 and
builds on this same ledger and calculator.
