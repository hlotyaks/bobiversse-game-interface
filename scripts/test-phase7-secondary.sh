#!/usr/bin/env bash
# Run destructive lifecycle checks against only enshrouded:secondary.
# This script never stops, restarts, or changes enshrouded:primary.
set -Eeuo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
controller_client=/usr/local/libexec/game-server-interface/controller_client.py
controller_user=game-interface-api
unit=game-enshrouded-secondary.service
runtime_dropin=/run/systemd/system/${unit}.d/phase7-failed-start.conf
actor=phase7-secondary-test

if [[ ${EUID} -ne 0 ]]; then
    echo "Run with sudo: sudo ./scripts/test-phase7-secondary.sh" >&2
    exit 1
fi
if ! systemctl is-active --quiet game-enshrouded-primary.service; then
    echo "Refusing test: primary instance is not active." >&2
    exit 1
fi
if ! systemctl is-active --quiet "${unit}"; then
    echo "Refusing test: secondary instance must be active before testing." >&2
    exit 1
fi

controller() {
    runuser -u "${controller_user}" -- "${controller_client}" "$1"
}

cleanup_failure_injection() {
    rm -f "${runtime_dropin}"
    rmdir "$(dirname "${runtime_dropin}")" 2>/dev/null || true
    systemctl daemon-reload
}
trap cleanup_failure_injection EXIT

# 1. Controller start against an active instance must be a no-op.
already_running=$(controller '{"action":"start","template_id":"enshrouded","instance_id":"secondary","actor":"phase7-secondary-test"}')
grep -q '"state": "already-running"' <<<"${already_running}"

# 2. A controller restart must return the secondary to a healthy state.
restart=$(controller '{"action":"restart","template_id":"enshrouded","instance_id":"secondary","actor":"phase7-secondary-test"}')
operation_id=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["operation_id"])' <<<"${restart}")
for _ in {1..75}; do
    operation=$(controller "{\"action\":\"operation_status\",\"operation_id\":\"${operation_id}\",\"actor\":\"${actor}\"}")
    state=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["state"])' <<<"${operation}")
    if [[ ${state} == healthy ]]; then break; fi
    if [[ ${state} == failed ]]; then echo "Secondary restart failed" >&2; exit 1; fi
    sleep 2
done
[[ ${state} == healthy ]]

# 3. Inject a runtime-only failed-start precondition. This targets only the
# secondary unit and lets systemd hit the configured five-start crash limit.
systemctl stop "${unit}"
install -d -m 0755 "$(dirname "${runtime_dropin}")"
printf '[Service]\nExecStartPre=/usr/bin/false\nRestartSec=1s\n' >"${runtime_dropin}"
systemctl daemon-reload
systemctl start "${unit}" || true
for _ in {1..45}; do
    active_state=$(systemctl show "${unit}" --property=ActiveState --value --no-pager)
    restart_count=$(systemctl show "${unit}" --property=NRestarts --value --no-pager)
    if [[ ${active_state} == failed && ${restart_count} -ge 3 ]]; then break; fi
    sleep 2
done
[[ ${active_state} == failed && ${restart_count} -ge 3 ]]
crash_status=$(controller '{"action":"status","template_id":"enshrouded","instance_id":"secondary","actor":"phase7-secondary-test"}')
python3 -c 'import json,sys; assert json.load(sys.stdin)["result"]["crash_loop"] is True' <<<"${crash_status}"

# 4. Remove the runtime failure condition. A daemon reload may let systemd resume
# a pending restart; if it does not, use the controller's manual retry path.
cleanup_failure_injection
for _ in {1..75}; do
    systemctl is-active --quiet "${unit}" && break
    sleep 2
done
if ! systemctl is-active --quiet "${unit}"; then
    retry=$(controller '{"action":"start","template_id":"enshrouded","instance_id":"secondary","actor":"phase7-secondary-test"}')
    retry_id=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["operation_id"])' <<<"${retry}")
    for _ in {1..75}; do
        operation=$(controller "{\"action\":\"operation_status\",\"operation_id\":\"${retry_id}\",\"actor\":\"${actor}\"}")
        state=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["state"])' <<<"${operation}")
        if [[ ${state} == healthy ]]; then break; fi
        if [[ ${state} == failed ]]; then echo "Secondary manual retry failed" >&2; exit 1; fi
        sleep 2
    done
    [[ ${state} == healthy ]]
fi

# 5. Back up and validate the test world. The scheduler handles each provisioned
# instance independently and records only safe status metadata.
systemctl start game-server-interface-backup.service
python3 - <<'PY'
import json
from pathlib import Path
status = json.loads(Path('/var/lib/game-server-interface/backup_status.json').read_text())
secondary = status['instances'].get('enshrouded:secondary', {})
assert secondary.get('verification_passed') is True, secondary
PY

"${repo_root}/scripts/game-firewall.sh" 15640 15641
systemctl is-active --quiet game-enshrouded-primary.service
systemctl is-active --quiet "${unit}"
printf 'PASS: Phase 7 secondary lifecycle, crash-loop, recovery, backup, and port-isolation checks passed.\n'
