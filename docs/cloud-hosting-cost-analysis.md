# Cloud hosting cost analysis (elastic, Terraform-managed)

Proposed 2026-07-18. Decision document for review — no infrastructure is created by
merging this. It evaluates moving some or all game hosting off the home `bobiverse`
host onto a cloud provider, with the goal of **paying (near) nothing when nobody is
playing** while supporting **multiple games and instances** beyond what one 8-core box
can hold. All vendor prices are approximate and must be re-verified with each provider's
live calculator before ratification; they move, and Hetzner in particular moved sharply
in mid-2026 (see below).

## Framing: what problem is this actually solving?

The 2026-07-18 Enshrouded combat-lag incident was a **self-imposed 2-core container cap**
(now raised to 4; see the audit log and `docs/features.md`), not a hardware ceiling.
`bobiverse` is 8 logical cores at a typical load of ~1.4, so it is not out of headroom for
today's single world.

The real drivers for a cloud design are:

1. **Horizontal scale past one box.** The catalog admission limit already reserves 6 of
   `bobiverse`'s 8 cores. One more CPU-heavy game (or a second concurrent Enshrouded raid)
   saturates the host. Cloud lets each game own its own compute.
2. **Not running compute 24/7 for a few evenings of play.** A home box is a sunk cost, but
   a cloud VM that only bills while friends are online can cost less than the electricity to
   idle a dedicated machine — *if* it is designed for elasticity.

If neither driver is compelling yet, the cheapest correct answer is "keep running on
`bobiverse` and revisit when a second heavy game is actually requested." This document
assumes we do want to plan for cloud.

## The one insight that decides the design

"Pay nothing when idle" depends entirely on **whether a provider stops billing when a VM is
powered off:**

| Behavior | Providers | Elastic strategy |
| --- | --- | --- |
| **Stops compute billing when stopped** (pay only for disk while idle) | AWS EC2, GCP Compute Engine, Azure VMs | Simple stop/start; disk persists; ~1–2 min wake |
| **Bills as long as the VM exists** (power-off saves nothing) | Hetzner, DigitalOcean, Vultr, Linode | Must destroy + recreate from a snapshot to save money; ~3–4 min wake, more automation |

Genuine "only pay when playing" therefore points at a **hyperscaler with idle-shutdown**,
not the flat-rate budget VPS providers. Two properties of our existing setup make that
cheap:

- **Tailscale**: access is already gated through the tailnet, so a cloud VM needs **no public
  IPv4** (saves the IPv4 rent and avoids public Steam-browser exposure). Players connect over
  the tailnet exactly as they do to `bobiverse` today.
- **`bobiverse` stays the always-on control plane**: the controller, Tailscale coordinator,
  and the "wake my server" trigger keep running on the home box (already a sunk cost). Cloud
  pays *only* for active game compute.

## Vendor comparison — always-on baseline

Reference workload: an Enshrouded-class world at **4 vCPU / 16 GB**, ~30 GB persistent disk,
~45 GB/mo egress, **no public IP** (Tailscale). USD per month. All have first-party or
well-maintained Terraform providers.

| Vendor | Instance | Compute/mo | All-in/mo | Terraform provider | Stops billing when off? |
| --- | --- | --- | --- | --- | --- |
| **GCP** | e2-standard-4 | ~$98 | **~$106** | `hashicorp/google` | Yes |
| AWS | m7i.xlarge | ~$147 | ~$154 | `hashicorp/aws` | Yes |
| Azure | D4as_v5 | ~$140 | ~$148 | `hashicorp/azurerm` | Yes |
| Vultr | 4 vCPU / 16 GB | ~$96 | ~$96 | `vultr/vultr` | No (bills powered-off) |
| DigitalOcean | GP 4 vCPU / 16 GB | ~$126 | ~$126 | `digitalocean/digitalocean` | No (bills powered-off) |
| Hetzner | CCX23 (dedicated) | ~$102 | ~$102 | `hetznercloud/hcloud` | No (bills on existence) |
| Hetzner | CPX32 (4 vCPU / **8 GB**, shared) | ~$42 | ~$42 | `hetznercloud/hcloud` | No (bills on existence) |

**Hetzner caveat.** Hetzner was historically the runaway price winner (~$27/mo for this
tier). A **June 15, 2026 price adjustment raised new-order dedicated-vCPU plans ~110–175%**
(CCX23 ≈ €86 ≈ ~$102/mo); existing customers are grandfathered but a new deployment pays the
new rate. Its shared-vCPU CPX32 at ~$42/mo is still cheap, but shared vCPU + 8 GB reintroduces
exactly the CPU-contention profile we are trying to escape for combat. Hetzner still bundles
generous disk and 20 TB egress.

## Recommended design: elastic (stop-on-idle) on GCP

Fixed cost while nobody plays = **just the persistent disk, ~$3/mo per game**. Compute accrues
only during active hours. With Tailscale (no IP) and `bobiverse` as the control plane:

**Enshrouded (4 vCPU / 16 GB), GCP `e2-standard-4` @ ~$0.134/hr:**

| Monthly play | Compute | + disk + egress | Total/mo |
| --- | --- | --- | --- |
| Light (~40 hr) | ~$5 | +$8 | **~$11** |
| Moderate (~100 hr) | ~$13 | +$8 | **~$21** |
| Heavy (~200 hr) | ~$27 | +$9 | **~$36** |
| Moderate on **Spot/preemptible** (~$0.04/hr) | ~$4 | +$8 | **~$12** |

Spot cuts compute ~70% but the VM can be reclaimed mid-session (it auto-restarts). Fine for
casual play, not for a scheduled raid night.

**Two games, elastic, moderate use:** Enshrouded (~$21) + Valheim (2 vCPU / 8 GB,
`e2-standard-2`, ~60 hr → ~$9) = **~$30/mo**, each sleeping independently. The always-on
equivalent on GCP would be **~$160/mo**, so elasticity saves roughly 80%.

**"When nobody is logged in" cost:** not literally $0 — the world save lives on a persistent
disk (~$3/mo/game). To get closer to zero, snapshot-and-destroy on idle drops that to
**~$0.40/mo/game**, at the cost of a slower (~3–4 min) cold start and more automation.

## How it maps onto the existing architecture

This is an evolution of what the repo already does, not a rewrite:

- **Terraform module per game VM** (instance + persistent disk + firewall + Tailscale join),
  driven by the same catalog concept already in `deploy/etc/game-server-interface/catalog.yaml`.
  Directly mirrors the reusable per-instance provisioning tooling already built for local
  Docker (`tools/render_instance.py`, `scripts/provision-game-instance.sh`).
- **Packer** (also HashiCorp) bakes the game server pre-installed into the machine image, so
  waking a VM skips Enshrouded's ~8.8 GB SteamCMD re-download → cold start ~60–120 s instead of
  minutes.
- **Wake / sleep control** reuses the controller model: an idle-watcher on the VM reads player
  count from the server query port and triggers shutdown after ~15 min empty; a "start" button
  on the `bobiverse` dashboard (or a Discord bot) wakes it. This is the one honest friction
  point — *someone must trigger the first wake out-of-band*, because nobody is connected to
  auto-trigger it.
- **Access** stays Tailscale-gated: the cloud VM joins the tailnet; no public game ports, same
  private-by-default model as today.

## Recommendation

- **Cheapest elastic with genuine scale-to-zero:** **GCP** (AWS as a close second),
  **~$20–35/mo** for Enshrouded at realistic usage and **~$30–45/mo for two games**, falling to
  a few dollars when idle. Requires the idle-shutdown automation described above.
- **Cheapest flat / simplest (no automation):** **Hetzner CCX23 ~$102/mo per heavy game**
  (post-hike), or **CPX32 ~$42/mo** if shared-vCPU / 8 GB is acceptable — but with no real idle
  savings.
- **Do-nothing option:** keep the current single world on `bobiverse` (already fixed) and only
  build the cloud path when a second heavy game is actually requested.

Suggested path if ratified: start with **one game (Enshrouded) on GCP, elastic, Tailscale-gated,
`bobiverse` as the control plane, Terraform + Packer**, validate the wake/sleep UX and a real
monthly bill, then extend the module to additional games.

## Optional add-on: usage-based cost sharing (Stripe)

A proposed billing layer that attributes the real hosting cost across the friend group by
**who is actually present**, so that solo play carries the full fare and group play splits it.
The explicit goal is **not to make money** (it recovers cost, no more) but to **price in the
incentive to play together**.

### The attribution model

Charge the server's run-cost at each moment split evenly among the players present at that
moment, integrated over the session:

```
player_i_charge = ∫ ( C(t) / n(t) ) · present_i(t) dt
```

where `C(t)` is the instance's run-cost rate ($/hr) while it is up, `n(t)` is the number of
players connected at time `t`, and `present_i(t)` is 1 while player *i* is connected. In
practice we sample occupancy per minute and divide.

Properties that make it fair and cost-exact:

- **Sums to the real bill.** As long as at least one player is present, the players' charges
  for that interval add up to exactly `C(t)` — full cost recovery, zero profit or loss.
- **Solo pays full, groups split.** Illustrative at a headline `$1/hr` run-cost:

  | Players present | Each pays | 
  | --- | --- |
  | 1 | $1.00/hr |
  | 2 | $0.50/hr |
  | 3 | $0.33/hr |
  | 4 | $0.25/hr |

- **Reflects who showed up when.** If Alice plays 8–10 pm and Bob joins only 9–10, Alice pays
  the full rate 8–9 and they split 9–10 — Bob's arrival lowers Alice's second hour.
- **No perverse incentive.** Every additional body only lowers everyone's share, so there is
  nothing to game; even an AFK player helps the others.

**Reality check on the numbers.** With the recommended GCP-elastic design the run-cost is not
$1/hr — `e2-standard-4` is ~$0.134/hr compute plus a small egress allowance, so the real fare
is roughly **$0.15–0.20/hr solo, ~$0.05/hr each in a group of four.** A month of moderate solo
play is ~$15–20; the same play in a group is ~$5. The dollars are small, which is central to
the design decisions below.

### The three things that do not map cleanly to "active play"

1. **Cold start and idle grace.** The ~1–2 min wake and the ~15 min idle-shutdown tail burn
   compute with zero or few players. Proposed rule: **the player who wakes the server pays the
   cold-start**, and the idle-grace tail is charged to whoever was last present (or socialized
   as a tiny fixed per-session fee). Keep this small and explicit on the bill.
2. **Fixed monthly cost.** The persistent disk (~$3/mo/game) accrues whether or not anyone
   plays and cannot be usage-attributed. Proposed: a small flat **monthly membership** per
   enrolled member covers disk + always-on overhead; everything else is usage-based.
3. **After-the-fact variability.** The true cloud invoice (especially egress) is only known at
   month end. Charge a **published per-game hourly rate** (compute + egress buffer) during the
   month, then **reconcile against the actual invoice** and carry the difference forward as a
   credit/debit. Publish the reconciliation so the group can see it nets to zero.

### Stripe mechanics (the fee problem forces the shape)

Stripe costs ~2.9% + $0.30 **per charge**. Charging a $0.25 session would lose money to fees,
so the system **must not charge per session**:

- **Meter continuously, charge monthly.** Accumulate each member's per-minute charges into an
  append-only ledger (same discipline as the existing root-only `audit.jsonl`), then create
  **one Stripe invoice per member per month** (or when a balance crosses, say, $10). This is
  Stripe Billing's metered/usage model and keeps fees at ~3%.
- **Card on file, minimal PCI scope.** Members enroll once via **Stripe Checkout / Elements**
  (Stripe-hosted), so card data never touches our servers (PCI SAQ A). We store only the Stripe
  customer/payment-method IDs.
- **Itemized receipts.** Each monthly bill shows every session: date, duration, who else was
  present, the rate, and the resulting share — transparency is the anti-dispute mechanism.
- **Secrets and webhooks.** The Stripe secret key is a root-only secret handled like
  `SERVER_PASSWORD` (never in the catalog, logs, or the friend-facing interface); webhook events
  are signature-verified.

### Data backbone and architecture

- **Presence events.** The meter needs reliable per-player join/leave timestamps. Source them
  from the game server's connect/disconnect log events (Enshrouded/Valheim) or the query/RCON
  port, keyed to a stable identity — the **Tailscale login** we already attribute actions by,
  or the Steam ID.
- **Where it runs.** The metering + monthly billing job live on the `bobiverse` control plane
  alongside the controller; they read presence events and the instance's run-rate, write the
  member ledger, and call Stripe. No new always-on cloud cost.
- **Consent.** Charging real money requires explicit opt-in, saved-payment authorization, and
  simple written terms (what is charged, how reconciliation works, refunds/disputes).

### Honest tradeoff before building this

At a total infra cost of ~$30–45/mo for two games, the amounts moved per person are small
(single-digit to low-double-digit dollars). A full Stripe integration adds a **payments + PII +
secrets + reconciliation surface** whose fees and maintenance may exceed what it recovers for a
handful of friends. Lighter options that keep most of the incentive:

- **Transparent ledger + manual settle-up.** Do all the metering and itemized monthly
  statements, but settle via Venmo/PayPal/"just square up" instead of automated card charges.
  Near-zero fees, no PCI/consent surface, same group-play incentive.
- **Flat monthly membership.** Everyone enrolled pays an equal share of the total bill.
  Dead simple, but loses the per-usage incentive that is the whole point here.

**Recommendation:** build the **metering + itemized transparent statements first** (that is
where the incentive and the fairness live), and treat **automated Stripe charging as a second
phase** the group opts into only if manual settle-up becomes annoying. This delivers the
group-play incentive immediately without standing up a payment processor for lunch-money sums.

### Billing open questions

- Automated Stripe charges, or metered statements + manual settle-up to start?
- Who eats the Stripe fee and any un-recovered rounding — socialize it onto each bill, or does
  the admin absorb it?
- Membership fee for fixed costs, or fold fixed cost into a slightly higher hourly rate?
- Does making solo play cost 4× risk the world simply going unplayed when no group forms — and
  is that acceptable, or do we want a solo cap?

## Open questions for review

- Is the ~1–2 min "click to wake" cold start acceptable UX, or do we want a small always-on
  warm tier (raising the idle floor) for instant joins?
- On-demand only, or allow Spot/preemptible for non-critical worlds to cut compute ~70%?
- Keep `bobiverse` as the permanent control plane, or move the controller/interface into the
  cloud too (adds a small always-on cost but removes the home-box dependency)?
- Per-game VM (independent elasticity, more disks) vs. one shared VM running multiple
  containers (cheaper idle floor, all-or-nothing sleep)?

## Cost sources (re-verify before ratification)

- AWS m7i.xlarge — https://www.economize.cloud/resources/aws/pricing/ec2/m7i.xlarge/
- AWS public IPv4 charge — https://aws.amazon.com/blogs/aws/new-aws-public-ipv4-address-charge-public-ip-insights/
- AWS EBS pricing — https://aws.amazon.com/ebs/pricing/
- GCP e2-standard-4 — https://www.economize.cloud/resources/gcp/pricing/compute-engine/e2-standard-4/
- Hetzner June 2026 price increase — https://webhosting.today/2026/06/18/hetzners-price-increases-reached-209-the-30-headline-applied-to-a-different-tier/
- Hetzner pricing calculator — https://costgoat.com/pricing/hetzner
- DigitalOcean / Vultr — https://checkthat.ai/brands/digitalocean/pricing
