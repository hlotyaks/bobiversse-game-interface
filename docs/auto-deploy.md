# Auto-deploy (pull-based GitOps)

bobiverse reconciles itself to `origin/main`. Merge a PR and, within ~2 minutes, the host runs the
installers for whatever changed — no manual `sudo` deploy commands. This is the pull model (like
Flux/Argo, shrunk to one host): nothing inbound is exposed, and no GitHub credentials live on the
server as root.

## How it works

```
game-autodeploy.timer (every 2 min)
  └─ game-autodeploy.service (oneshot, root)
       └─ /usr/local/sbin/game-autodeploy         # stable, root-owned, NOT from the checkout
            ├─ flock (no overlapping runs)
            ├─ git fetch  (as the deploy user, who holds the repo credential)
            ├─ verify origin/main is a fast-forward of the deployed commit  (else refuse)
            ├─ if unchanged → exit quietly
            ├─ reset the deploy clone to origin/main
            ├─ scripts/deploy.sh <old> <new>       # from the checkout: run only what changed
            ├─ health-check controller + interface + meter
            └─ on failure → roll back to the previous commit, reinstall, re-check
```

- **Deploy clone** — a dedicated clone at `/srv/game-autodeploy/repo`, owned by the deploy user and
  separate from your dev checkout, so auto-deploy never touches in-progress branch work. The clone
  only ever tracks `origin/main`.
- **Credentials** — the timer runs as root but drops to the deploy user (`sudo -u`) for every git
  operation, so the private-repo fetch uses that user's existing credential. Root needs no GitHub
  token.
- **What runs** — [scripts/deploy.sh](../scripts/deploy.sh) diffs the two commits and runs the
  minimal installers: `install-phase6.sh` (controller, which also reinstalls the interface),
  `install-phase3.sh` (interface only), `install-usage-metering.sh` + meter restart, and/or a
  catalog install. A docs-only merge deploys nothing. Running game instances are never touched.
- **The stable wrapper** (`/usr/local/sbin/game-autodeploy`) is installed out-of-band and is *not*
  part of the pulled tree, so a commit cannot rewrite the git-trust/rollback logic itself. The
  per-change installer logic *is* pulled — that is what a deploy is — which is why the branch must be
  protected (below).

## Safety

- **Fast-forward only + verify remote.** If `origin/main` is not a fast-forward of the deployed
  commit (a force-push or rewritten history), the deploy is refused and logged; nothing changes.
- **Health-check + auto-rollback.** After applying, it checks the controller and meter are active
  and the interface answers `/healthz`. If not, it resets to the previous commit, reinstalls, and
  re-checks. A bad commit lands on `origin/main` but does **not** stay running on the host.
- **Branch protection is required.** Auto-deploy means anything merged to `main` runs as root here,
  unattended. Protect `main` on GitHub (require a PR review) so a deploy is always something a human
  approved. `main` should be the reviewed line, not a scratch branch.

## Install (once, root)

```
sudo scripts/install-autodeploy.sh
```

Creates the deploy clone, installs the wrapper + `autodeploy.conf` + the systemd timer, and starts
polling. Override defaults with env vars if needed: `DEPLOY_USER`, `BRANCH`, `CLONE`, `ORIGIN_URL`
(e.g. `ORIGIN_URL=... sudo -E scripts/install-autodeploy.sh`).

## Operate

```
systemctl status game-autodeploy.timer                          # is it scheduled?
journalctl -u game-autodeploy.service -f                        # watch a deploy
sudo systemctl start game-autodeploy.service                    # force a reconcile now
sudo cat /var/lib/game-server-interface/autodeploy-status.json  # last result (deployed/rolled_back/…)
scripts/deploy.sh --dry-run <old-ref> <new-ref>                 # preview a plan (no root, no changes)
```

To pause auto-deploy (e.g. during manual maintenance): `sudo systemctl stop game-autodeploy.timer`;
re-enable with `sudo systemctl start game-autodeploy.timer`.
