#!/usr/bin/env bash
# Apply the code between two git refs to the running host by running only the installers whose
# inputs changed. Idempotent and non-interactive; intended to be driven by the auto-deploy wrapper
# (/usr/local/sbin/game-autodeploy) but safe to run by hand as root from a checkout:
#
#     sudo scripts/deploy.sh <old-ref> <new-ref>          # apply what changed between the two
#     scripts/deploy.sh --dry-run <old-ref> <new-ref>     # print the plan only (no root needed)
#
# The working tree must already be at <new-ref> (the wrapper resets it before calling us); the refs
# are used only to compute which paths changed. This script never touches git, never fetches, and
# never restarts a running game instance -- only the interface/controller/meter control plane.
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

dry_run=0
if [[ ${1:-} == "--dry-run" ]]; then
    dry_run=1
    shift
fi

old_ref=${1:-}
new_ref=${2:-}
if [[ -z ${old_ref} || -z ${new_ref} ]]; then
    echo "usage: deploy.sh [--dry-run] <old-ref> <new-ref>" >&2
    exit 2
fi

# Files changed between the two refs (empty if identical). Paths are repo-relative.
mapfile -t changed < <(git -C "${repo_root}" diff --name-only "${old_ref}" "${new_ref}")

# Decide which installers are needed. A single changed file can imply several; we de-duplicate and
# order the run below (controller first, then interface, meter, catalog).
needs_phase6=0    # controller + backup scheduler + crash-limit drop-ins (also reinstalls interface)
needs_phase3=0    # unprivileged interface image + static UI
needs_meter=0     # presence meter / billing / ledger tools + meter unit + exclusion seed
needs_catalog=0   # deployed catalog only (controller/meter read it fresh; no restart)
needs_phase5=0    # tailscale serve publishing / firewall

matches() { local path=$1; shift; local pattern; for pattern in "$@"; do [[ ${path} == ${pattern} ]] && return 0; done; return 1; }

for path in "${changed[@]}"; do
    if matches "${path}" "controller/*" "tools/backup_scheduler.py" \
        "deploy/etc/systemd/system/game-server-interface-backup.service" \
        "deploy/etc/systemd/system/game-server-interface-backup.timer" \
        "deploy/etc/systemd/system/game-server-interface-crash-limits.conf" \
        "deploy/etc/logrotate.d/game-server-interface" \
        "scripts/install-phase6.sh"; then
        needs_phase6=1
    fi
    if matches "${path}" "interface/*" "deploy/opt/game-server-interface/compose.yaml" \
        "deploy/etc/systemd/system/game-server-interface.service" \
        "scripts/install-phase3.sh"; then
        needs_phase3=1
    fi
    if matches "${path}" "tools/presence_meter.py" "tools/billing.py" "tools/ledger_admin.py" \
        "deploy/etc/systemd/system/game-presence-meter.service" \
        "deploy/var/lib/game-server-interface/presence-exclusions.json" \
        "deploy/etc/game-server-interface/billing.yaml" \
        "scripts/install-usage-metering.sh"; then
        needs_meter=1
    fi
    if matches "${path}" "deploy/etc/game-server-interface/catalog.yaml"; then
        needs_catalog=1
    fi
    if matches "${path}" "deploy/etc/systemd/system/game-server-interface-serve.service" \
        "scripts/game-firewall.sh" "scripts/install-phase5.sh"; then
        needs_phase5=1
    fi
done

# phase6 reinstalls the interface itself, so a separate phase3 run would be redundant.
if [[ ${needs_phase6} -eq 1 ]]; then
    needs_phase3=0
fi

plan=()
[[ ${needs_phase6}  -eq 1 ]] && plan+=("controller+interface (install-phase6.sh)")
[[ ${needs_phase3}  -eq 1 ]] && plan+=("interface (install-phase3.sh)")
[[ ${needs_meter}   -eq 1 ]] && plan+=("presence meter (install-usage-metering.sh + restart)")
[[ ${needs_catalog} -eq 1 ]] && plan+=("catalog (validate + install to /etc)")
[[ ${needs_phase5}  -eq 1 ]] && plan+=("tailscale serve (install-phase5.sh)")

if [[ ${#plan[@]} -eq 0 ]]; then
    echo "deploy: ${old_ref:0:12}..${new_ref:0:12} touches nothing that needs installing"
    exit 0
fi

echo "deploy plan (${old_ref:0:12}..${new_ref:0:12}):"
printf '  - %s\n' "${plan[@]}"

if [[ ${dry_run} -eq 1 ]]; then
    exit 0
fi

if [[ ${EUID} -ne 0 ]]; then
    echo "deploy: must run as root to apply (use --dry-run to preview)" >&2
    exit 1
fi

# Apply, in dependency order. Each installer is idempotent and validates the catalog itself.
if [[ ${needs_phase6} -eq 1 ]]; then
    echo "==> install-phase6.sh (controller + interface)"
    bash "${repo_root}/scripts/install-phase6.sh"
elif [[ ${needs_phase3} -eq 1 ]]; then
    echo "==> install-phase3.sh (interface)"
    bash "${repo_root}/scripts/install-phase3.sh"
fi

if [[ ${needs_meter} -eq 1 ]]; then
    echo "==> install-usage-metering.sh (presence meter)"
    bash "${repo_root}/scripts/install-usage-metering.sh"
    systemctl restart game-presence-meter.service
fi

if [[ ${needs_catalog} -eq 1 ]]; then
    echo "==> catalog (validate + install)"
    python3 "${repo_root}/tools/validate_catalog.py" "${repo_root}/deploy/etc/game-server-interface/catalog.yaml"
    install -o root -g root -m 0644 "${repo_root}/deploy/etc/game-server-interface/catalog.yaml" /etc/game-server-interface/catalog.yaml
fi

if [[ ${needs_phase5} -eq 1 ]]; then
    echo "==> install-phase5.sh (tailscale serve)"
    bash "${repo_root}/scripts/install-phase5.sh"
fi

echo "deploy: applied ${#plan[@]} change group(s)"
