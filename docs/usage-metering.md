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
count. So identity comes from the tailnet instead: every player reaches the tailnet-bound game
port from their own Tailscale node, so the meter reads the live UDP flows to the game port and
resolves each peer's tailnet IP to its Tailscale login. This reuses the same identity the
dashboard already attributes actions by (`Tailscale-User-Login`), needs **no separate login
system**, and is game-agnostic (it will work for Valheim or any future UDP game unchanged).

## Components

| Piece | File | Role |
| --- | --- | --- |
| Presence meter | [tools/presence_meter.py](../tools/presence_meter.py) | Root systemd service. Each cycle lists tailnet UDP flows per game port (`conntrack`), maps IPs to logins (`tailscale status --json`), and appends an occupancy sample to the ledger. |
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
`game-presence-meter.service`. Runtime prerequisites: `conntrack` and the `tailscale` CLI.

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

The parsing and billing logic are covered by unit tests, but the conntrack-based presence capture
can only be confirmed with a real session (an idle server shows no flows). On first use, with at
least one player connected, check that the meter sees them:

    sudo conntrack -L -p udp --dport 15636 | grep -c 'src=100\.'   # nonzero while someone plays
    tail -f /var/lib/game-server-interface/presence.jsonl          # samples should list logins

If Docker's userland proxy is masking the real client source in conntrack (see the networking
note in the provisioning docs), the fallback is to sample on the `tailscale0` ingress instead;
capture a real session's `conntrack -L` output and adjust `parse_conntrack_peers` accordingly.

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
