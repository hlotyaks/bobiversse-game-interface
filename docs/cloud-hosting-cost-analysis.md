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
