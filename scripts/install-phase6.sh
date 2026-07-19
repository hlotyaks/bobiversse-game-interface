#!/usr/bin/env bash
# Install Phase 6 reliability controls. Run with sudo from the repository root.
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
install_root=/usr/local/libexec/game-server-interface

if [[ ${EUID} -ne 0 ]]; then
    echo "Run with sudo: sudo ./scripts/install-phase6.sh" >&2
    exit 1
fi

python3 -m py_compile "${repo_root}/controller/game_controller.py" "${repo_root}/tools/backup_scheduler.py"
python3 "${repo_root}/tools/validate_catalog.py" "${repo_root}/deploy/etc/game-server-interface/catalog.yaml"
install -d -o root -g root -m 0700 /var/lib/game-server-interface
install -o root -g root -m 0755 "${repo_root}/controller/game_controller.py" "${install_root}/game_controller.py"
install -o root -g root -m 0755 "${repo_root}/tools/backup_scheduler.py" "${install_root}/backup_scheduler.py"
install -o root -g root -m 0644 "${repo_root}/deploy/etc/systemd/system/game-server-interface-backup.service" /etc/systemd/system/game-server-interface-backup.service
install -o root -g root -m 0644 "${repo_root}/deploy/etc/systemd/system/game-server-interface-backup.timer" /etc/systemd/system/game-server-interface-backup.timer
install -o root -g root -m 0644 "${repo_root}/deploy/etc/logrotate.d/game-server-interface" /etc/logrotate.d/game-server-interface
for unit_path in /etc/systemd/system/game-*.service; do
    [[ -e ${unit_path} ]] || continue
    unit_name=$(basename "${unit_path}")
    [[ ${unit_name} == game-server-interface-* ]] && continue
    dropin_dir="/etc/systemd/system/${unit_name}.d"
    install -d -o root -g root -m 0755 "${dropin_dir}"
    install -o root -g root -m 0644 "${repo_root}/deploy/etc/systemd/system/game-server-interface-crash-limits.conf" "${dropin_dir}/crash-limits.conf"
done
for unit_name in game-server-interface-controller.service game-server-interface.service game-server-interface-serve.service game-server-interface-backup.service; do
    rm -f "/etc/systemd/system/${unit_name}.d/crash-limits.conf"
    rmdir "/etc/systemd/system/${unit_name}.d" 2>/dev/null || true
done

systemctl daemon-reload
systemctl restart game-server-interface-controller.service
systemctl enable --now game-server-interface-backup.timer
bash "${repo_root}/scripts/install-phase3.sh"
systemctl is-active --quiet game-server-interface-controller.service
systemctl is-enabled --quiet game-server-interface-backup.timer
printf 'Phase 6 reliability controls are installed. Scheduled backups run daily at 02:00 with up to 15 minutes of jitter.\n'
