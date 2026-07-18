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

## Not in this stage

No web-UI display yet, and no payments. The **per-user bill in the dashboard** (each user sees
their own; admins see everyone's, via the existing admin gate) is the next increment and reads
this same calculator output. Stripe money-handling is Stage 2.
