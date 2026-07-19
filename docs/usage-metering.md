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

- **`tailscale` (default).** Players reach the game over the tailnet, so their packets travel
  inside the WireGuard tunnel and the kernel's conntrack never sees a `client → game-port` flow
  (this was verified on bobiverse; full evidence in
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
- **`conntrack`.** Watches `conntrack -L` for direct `client → game-port` flows. This is blind under
  Tailscale (above) but is the right source for a future cloud/public-IP deployment without the
  WireGuard tunnel. Preserved and tested; switch with `--source conntrack`.

Known limits of the default source (fine for a dry-run Stage 1): a one-cycle startup lag (the
identity rate needs two samples); attribution assumes the game's connected clients are the busiest
tailnet peers, so a non-player peer generating *sustained* heavy tailnet traffic could still be
mis-ranked into a player's place (per-peer rates are EWMA-smoothed so a single-cycle burst or a
player's transient tailscale counter reset no longer flips a slot — this was observed crediting a
solo player's time to a bystander before smoothing); and if two games run at once a player's traffic
counts toward each running instance (it can't be split between them). Adding an occupancy reader for
another game is a small function keyed by template in
[tools/presence_meter.py](../tools/presence_meter.py) (`OCCUPANCY_READERS`).

## Components

| Piece | File | Role |
| --- | --- | --- |
| Presence meter | [tools/presence_meter.py](../tools/presence_meter.py) | Root systemd service. Each cycle reads `tailscale status --json`, marks Active peers over the traffic-rate threshold as playing, attributes them to active game units, and appends an occupancy sample to the ledger. (`--source conntrack` swaps in the direct-flow source for non-Tailscale deployments.) |
| Presence ledger | `/var/lib/game-server-interface/presence.jsonl` | Append-only JSONL, one line per instance per cycle: `{"ts","instance","present":[logins]}`. Root-owned, `0600` — it is playtime metadata (who played when); treat it as private, like the audit log. |
| Billing config | [deploy/etc/game-server-interface/billing.yaml](../deploy/etc/game-server-interface/billing.yaml) | Nominal per-instance run-cost and the group-size multiplier schedule `m(n)`. No secrets. |
| Billing calculator | [tools/billing.py](../tools/billing.py) | Pure calculator over the ledger. Produces per-user hours, solo/group split, sessions, and the dry-run bill (text or `--json`). |

## The bill model

For each sample interval of duration `d` with `n` players present, each present user accrues
`rate_per_second * d * m(n) / n`. `m(1) > 1` makes solo a premium; `m(n) < 1` for larger groups
subsidizes group play. Charges therefore do **not** sum to the raw server cost per interval — the
difference is the shared **kitty**, reported so the group can confirm it nets out over time. See
the cost-analysis doc for the rationale and the tuning discussion.

Occupancy is a step function between samples; a sample's duration is the gap to the next sample,
capped at `max_gap_seconds` so meter downtime is never billed as continuous play.

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

The dashboard shows a **Your bill** panel with a world selector and a **month selector** (the
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
