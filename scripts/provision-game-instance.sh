#!/usr/bin/env bash
# Provision a catalog-defined game instance: service account, data dirs, secret env,
# per-instance compose file, and systemd unit. Idempotent and root-only. It does NOT
# start the service -- start it through the controller after any world data is in place.
#
# Usage: sudo ./scripts/provision-game-instance.sh <template> <instance>
#   e.g. sudo ./scripts/provision-game-instance.sh enshrouded primary
set -Eeuo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
catalog=/etc/game-server-interface/catalog.yaml
instances_root=/etc/game-server-interface/instances
container_uid_gid=10000:10000   # sknnr/enshrouded-dedicated-server runs as this fixed UID.

if [[ ${EUID} -ne 0 ]]; then
    echo "Run with sudo: sudo ./scripts/provision-game-instance.sh <template> <instance>" >&2
    exit 1
fi
if [[ $# -ne 2 ]]; then
    echo "Usage: sudo ./scripts/provision-game-instance.sh <template> <instance>" >&2
    exit 2
fi

template=$1
instance=$2
id_pattern='^[a-z][a-z0-9-]{0,62}$'
if ! [[ $template =~ $id_pattern && $instance =~ $id_pattern ]]; then
    echo "Error: template and instance must match ${id_pattern}." >&2
    exit 2
fi

account="${template}-${instance}"
data_dir="/srv/games/${account}"
savegame_dir="${data_dir}/savegame"
instance_dir="${instances_root}/${account}"
secret_env="${instance_dir}/${template}.env"
unit="game-${account}.service"

# 1. The catalog is the source of truth; refuse to proceed if it is invalid.
python3 "${repo_root}/tools/validate_catalog.py" "${catalog}"

# 2. Dedicated, unprivileged service account + isolated data tree (Phase 1 helper).
if getent passwd "${account}" >/dev/null; then
    echo "Service account '${account}' already exists; leaving it in place."
else
    /usr/local/sbin/create-game-account "${account}"
fi

# 3. Savegame bind-mount directory, owned by the container's fixed UID/GID.
install -d -o root -g root -m 0755 "${data_dir}"
install -d -m 0770 "${savegame_dir}"
chown "${container_uid_gid}" "${savegame_dir}"

# 4. Root-only secret/config env. Never written to the catalog or logs; kept if present.
install -d -o root -g root -m 0755 "${instances_root}" "${instance_dir}"
if [[ -f "${secret_env}" ]]; then
    echo "Secret env ${secret_env} already exists; keeping it. Delete it to re-enter values."
else
    default_slots=$(python3 - "$catalog" "$template" <<'PY'
import sys, yaml
catalog, template = sys.argv[1], sys.argv[2]
data = yaml.safe_load(open(catalog, encoding="utf-8"))
print(min(int(data["templates"][template].get("supported_players", 16)), 16))
PY
)
    printf 'Configuring %s. Values are stored root-only at %s\n' "${account}" "${secret_env}"
    read -r -p "Server name [${account}]: " server_name </dev/tty || true
    server_name=${server_name:-${account}}
    read -r -p "Player slots (max 16) [${default_slots}]: " server_slots </dev/tty || true
    server_slots=${server_slots:-${default_slots}}
    while :; do
        read -r -s -p "Server password (min 5 chars): " server_password </dev/tty; echo
        [[ ${#server_password} -ge 5 ]] && break
        echo "Password must be at least 5 characters."
    done
    umask 077
    cat >"${secret_env}" <<EOF
SERVER_NAME=${server_name}
SERVER_SLOTS=${server_slots}
SERVER_PASSWORD=${server_password}
EOF
    chown root:root "${secret_env}"
    chmod 0600 "${secret_env}"
fi

# 5. Render the compose file + systemd unit from the catalog, then install them.
bind_ip=$(tailscale ip -4 2>/dev/null | head -1 || true)
render_args=("${template}" "${instance}" --catalog "${catalog}")
[[ -n ${bind_ip} ]] && render_args+=(--bind-ip "${bind_ip}")

staging=$(mktemp -d)
trap 'rm -rf "${staging}"' EXIT
python3 "${repo_root}/tools/render_instance.py" "${render_args[@]}" --output-dir "${staging}"

install -o root -g root -m 0644 "${staging}/compose.yaml" "${instance_dir}/compose.yaml"
install -o root -g root -m 0644 "${staging}/${unit}" "/etc/systemd/system/${unit}"

# 6. Validate the rendered compose before enabling anything.
/usr/bin/docker compose --project-directory "${instance_dir}" --file "${instance_dir}/compose.yaml" config -q

systemctl daemon-reload
systemctl enable "${unit}" >/dev/null

cat <<EOF

Provisioned ${account}:
  Service account : ${account} ($(id -u "${account}"):$(id -g "${account}"))
  Data directory  : ${data_dir}
  Savegame mount  : ${savegame_dir} (owned ${container_uid_gid})
  Secret env      : ${secret_env} (root-only)
  Compose file    : ${instance_dir}/compose.yaml
  Systemd unit    : ${unit} (enabled, not started)
  Game UDP ports  : bound to ${bind_ip:-<tailnet IP>}

Next steps:
  1. Register the slot with the controller (as game-interface-api).
  2. Place any migrated world save into ${savegame_dir}/ and chown -R ${container_uid_gid}.
  3. Open the tailnet-scoped firewall: sudo ./scripts/game-firewall.sh <udp-ports>
  4. Start via the controller, or: sudo systemctl start ${unit}
EOF
