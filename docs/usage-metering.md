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

The meter splits the question into **how many** and **who**, and reads both from the game itself.

- **How many** connected clients there are comes from Enshrouded's per-machine `Session` block,
  logged every ~30s (`m#N(...) … OperatingNormally`, the server's own entry excluded), read via
  `docker logs`. Authoritative, needs no tuning.
- **Who** they are comes from the game's `[online]` events, which name every peer:

      [online] Added peer 0(23) (steamid:76561190000000005)
      [online] Removed peer 0(23)          /   [online] Timeout for peer 0(23)

  Replaying add/drop events over a window (`--identity-window`, default 24h) gives exactly who is
  connected. Peer handles are unique per session, so there is no reuse ambiguity.

Steam IDs are translated via the admin-maintained
`/var/lib/game-server-interface/player-identities.json`, re-read every cycle so an edit applies
within a minute with **no restart**:

```json
"identities": {
  "76561190000000001": {"name": "SomeCharacter", "login": "someone@example.com"},
  "76561190000000002": {"name": "OtherCharacter", "login": ""}
}
```

Steam IDs identify a person across *every* Steam game, so an entry written for one game already
works for the next one — mapping a player is a one-time job, not a per-game one. An entry for
someone who has never played is inert: the meter only ever looks up IDs the game actually reports.

**`name` is the billing identity** — the in-game name the group knows each other by, and what
appears on the bill. **`login`** is that person's tailnet login, carried only so the dashboard can
tell which line belongs to the viewer: it identifies people from the `Tailscale-User-Login` header,
so without this it cannot match a line keyed by game name. It is optional — a player with no login
is billed normally but sees no personal line on the Billing page. (A bare string value is accepted
as a name with no login.) A player whose ID is not in that map is still counted by the game but is not named:
their share is reported as **UNATTRIBUTED** rather than guessed at. List what needs mapping with:

    sudo /usr/local/sbin/gsi-diagnose identities

For each ID that report shows the mapped login (or that it is unmapped), the Steam profile URL,
session count and last-seen time, and — usually the quickest way to recognise someone — the
**character names** they played under. The game logs `Sending Character Savegame '<name>'`, which
the report attributes only when exactly one player was connected, so the name is unambiguous.

### Why not the network layer

Everything before 2026-09-09 tried to infer identity from network traffic, and all of it failed,
because **players never touch the tailnet to play**. Enshrouded uses Steam's relay network: the
server connects *outbound* to Steam and clients arrive through Steam's relays. A packet capture on
`tailscale0` with a client connected and in the world caught **zero** packets on the game port,
while SSH to the same host was plainly visible in the same window.

So there was never a `client → game-port` flow to see — not in conntrack, not on the wire. Three
successive heuristics chased it anyway:

| Signal | Why it failed |
| --- | --- |
| `conntrack` flows to the game port | No such flow exists; traffic goes via Steam relays |
| Peer byte rate (`RxBytes`/`TxBytes`) | Populated only for peers with a **direct** path; DERP-relayed peers read 0 |
| Peer `LastWrite` | Same direct-path-only limitation |

The two tailnet heuristics are preserved as `--attribution byte-rate` and `--attribution last-write`
for a future deployment where players *do* connect over the tailnet, and the observer shadow-logs
both. Full history in
[presence-source-conntrack-findings.md](presence-source-conntrack-findings.md).

### Exclusions

Some logins are never players of a given game — an admin who runs the server but does not play it.
Two mechanisms, both applied after identity resolution so a game's slots go to actual players:

- **Per-game (preferred), admin-managed at runtime.** An administrator edits the exclusion list for
  each game on the dashboard's **Exclusions** page. This writes the controller-managed
  `/var/lib/game-server-interface/presence-exclusions.json` (`{template_id: [logins]}`); the meter
  re-reads it every cycle, so a change takes effect within a minute with **no restart**. Under the
  hood: interface `GET`/`POST /api/exclusions` (admin-gated) → controller `list_exclusions` /
  `set_exclusions` (validated, atomic, audited).
- **Global.** `--exclude-login <login>` (repeatable) drops a login from *every* game.

With game-log identity these matter far less than they did — attribution no longer mistakes an
admin's SSH traffic for play — but they remain the way to say "this person runs the server and
does not play this game". An exclusion matches either the player's tailnet login or their in-game
name; the dashboard only accepts logins, so that is the usual form.

### Rebuilding a bad stretch from the game log

The meter samples live, so a window where it was stopped or misattributing leaves a ledger no
recalculation can fix. The game log can fix it, within its retention: it records every connect and
disconnect with a timestamp, so replaying it reconstructs exactly who was present when.

    sudo tools/backfill_presence.py --dry-run          # what would change
    sudo tools/backfill_presence.py                    # splice it in

It writes the same record shape the live meter writes, taking `count` from the game's own `Session`
block rather than from how many names it recovered — so the UNATTRIBUTED property is preserved
rather than papered over. Only the named instance's samples inside the rebuilt window are replaced;
other instances and other times are left untouched.

**Order matters** when a bad stretch is wider than the log's retention. Clear the month first, then
backfill, so the part the log cannot reach stays honestly meter-blind instead of keeping bad data:

    sudo .../ledger_admin.py --clear-month 2026-09 --instance enshrouded-primary
    sudo .../backfill_presence.py --instance enshrouded-primary

The meter appends while this runs; the rewrite is atomic, so at worst one concurrent sample is lost.

Correcting a bad capture after the fact:
`sudo /usr/local/libexec/game-server-interface/ledger_admin.py --remove-login <login>` (add
`--dry-run` first), or `--clear-month YYYY-MM --instance <id>` to retire a month whose capture is
not trustworthy.

Where a game names players only by their character, record those names so the meter can resolve
them:

```json
"76561190000000001": {"name": "Rhadamanthus", "login": "someone@example.com",
                      "characters": {"valheim": ["Rhad"], "enshrouded": ["Rhadamanthus"]}}
```

`sudo /usr/local/sbin/gsi-diagnose identities` lists, per game, the character names it has observed
each player using and flags any not yet recorded, so filling this in is a copy-paste job.

Valheim's readers are the second worked example, and they show the shape is not always the same.
Valheim names a player as they arrive but **not** as they leave, so identity is carried across
three lines — the Steam ID on connect, the ZDO owner id of their character, and the running
`now N player(s)` total — and a departure is matched only by that owner id reappearing. That arrival-to-character
gap is wide — ~20s measured, since it spans the client loading the world — so two people starting a
session together interleave as a matter of course rather than as an edge case.

**Recorded character names resolve that outright**: the character line names the player directly,
however simultaneously they joined, and recognising one player narrows the field for the next.
Only where a character is unrecognised *and* more than one arrival is outstanding does the reader
decline to name anyone, rather than pairing by arrival order and risking a transposition; the
game's own count still reports them, so they reach the bill as UNATTRIBUTED. A `now 0 player(s)`
line clears the set outright, which makes the reader self-correcting: drift cannot outlive a
session.

Adding a new game means writing two small functions keyed by template in
[tools/presence_meter.py](../tools/presence_meter.py): an `OCCUPANCY_READERS` entry for the count
and an `IDENTITY_READERS` entry for the identities. Games that log neither fall back to the
`--min-kbps` tailnet heuristic, with all the caveats above.

## Components

| Piece | File | Role |
| --- | --- | --- |
| Presence meter | [tools/presence_meter.py](../tools/presence_meter.py) | Root systemd service. Each cycle reads the game's own client count and connected-player identities from its log, maps them to tailnet logins, and appends an occupancy sample to the ledger. |
| Player identity map | `/var/lib/game-server-interface/player-identities.json` | Admin-maintained `{in-game ID: tailnet login}`. Root-owned `0600`. Unmapped players are counted but not named. |
| Live observer | [scripts/observe-presence.py](../scripts/observe-presence.py) | Read-only. Run as root during a real session to watch the count, every peer's byte deltas/EWMA, what the meter would attribute, and the conntrack flows side by side. The tool for diagnosing a *who* failure. |
| Presence ledger | `/var/lib/game-server-interface/presence.jsonl` | Append-only JSONL, one line per instance per cycle: `{"ts","instance","present":[logins],"count":N}`. `count` is the game's own client count and `present` is only who the meter could name, so `count >= len(present)`; `"count": null` means the occupancy read failed (unknown, *not* nobody). Root-owned, `0600` — it is playtime metadata (who played when); treat it as private, like the audit log. |
| Billing config | [deploy/etc/game-server-interface/billing.yaml](../deploy/etc/game-server-interface/billing.yaml) | Nominal per-instance run-cost and the group-size multiplier schedule `m(n)`. No secrets. |
| Ledger backfill | [tools/backfill_presence.py](../tools/backfill_presence.py) | Replays the game log to rebuild presence samples for a past window, splicing them over whatever the meter recorded. |
| Billing calculator | [tools/billing.py](../tools/billing.py) | Pure calculator over the ledger. Produces per-user hours, solo/group split, sessions, and the dry-run bill (text or `--json`). |

## Counting and naming are separate problems

The meter answers two questions per cycle and they fail independently:

- **How many** — the game's own count. Reliable.
- **Who** — the identities the game logs for itself. Exact, and from the same source as the count.
  This was the weak half for two months, through three failed network heuristics, because nobody
  checked whether the game named its players. It does.

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

How far back the meter reads when asking a game its player count depends on how that game reports
it. Enshrouded prints a periodic snapshot, so the last two minutes always contain one. Valheim
prints a *running total* only when someone joins or leaves, so a quiet server has nothing in a
short window and would read as **unknown** — billing that as meter-blind, and accruing unbilled
blind hours for a server that is simply empty. Games like that are read over a long window
(`OCCUPANCY_WINDOWS`), where the most recent total is still current however long ago it was
printed.

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

## Several games at once

The ledger is keyed by instance, so playtime is already separated per game with no extra
configuration — every configured slot is sampled every cycle whether or not it is running. A
combined bill breaks each player's hours out per game and sums the charges:

    sudo /usr/local/sbin/gsi-diagnose all-games
    # or: billing.py --all-games [--month YYYY-MM] [--json]

    player                enshrouded-primary     valheim-primary     total       bill
    Gronk                               2.00                1.00      3.00   USD 0.28
    Michala                             0.00                1.75      1.75   USD 0.26

Each game is costed on **its own** rate and group-size multiplier, then summed per player: soloing
one game while three people play another are different prices, and a combined-hours figure could
not express that. Games that saw no play in the month are left out.

### Adding a game

Two things gate whether a new game bills correctly, and neither fails loudly:

1. **A rate in `billing.yaml`.** A slot with no `run_cost_per_hour` accrues playtime and bills
   **zero** — a working-looking bill that happens to be free. The combined report prints a WARNING
   naming any such game, and a test asserts every catalog slot is priced.
2. **An occupancy reader and an identity reader** keyed by template in
   [tools/presence_meter.py](../tools/presence_meter.py) (`OCCUPANCY_READERS`, `IDENTITY_READERS`).
   Without them the meter falls back to the `--min-kbps` tailnet heuristic, which on this host
   identifies **nobody**: players reach Steam-relayed games over Steam's network, so the tailnet
   carries no game traffic to measure. The symptom is a game whose hours are all UNATTRIBUTED.

Writing the two readers needs a sample of that game's server log with players connected — what it
prints when someone joins, leaves, and while they are on. Both are small functions; Enshrouded's are
the worked example.

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
  The viewer is matched to their line through the `login` field of the identity map, since lines are
  keyed by in-game name; a player with no login mapped sees no personal line.
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
