#!/usr/bin/env python3
"""Render a per-instance Docker Compose file and systemd unit from the catalog.

This is the out-of-band provisioning renderer. It reuses the controller's
``resolve_slot_from_catalog`` so the generated files carry exactly the catalog-derived
image digest, ports, resource limits, and paths -- no values are invented here. The
renderer performs no privileged action: it only reads the catalog and writes the two
generated files (or prints them). ``scripts/provision-game-instance.sh`` installs them.

Each game image needs a slightly different Compose service (its own volume path, env
names, and container UID), so templates are handled by small per-template adapters. The
``enshrouded`` adapter is implemented; add others as games are deployed.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any

import yaml

class _Quoted(str):
    """A string that is always emitted double-quoted (e.g. compose ``user: "10000:10000"``)."""


yaml.add_representer(_Quoted, lambda dumper, data: dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style='"'), Dumper=yaml.SafeDumper)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = Path("/etc/game-server-interface/catalog.yaml")
# bobiverse's Tailscale IPv4; publishing only here keeps game ports on the tailnet.
DEFAULT_BIND_IP = "100.84.161.38"
INSTANCES_ROOT = "/etc/game-server-interface/instances"


def _load_resolver():
    """Load resolve_slot_from_catalog from the controller module without importing by name."""
    module_path = REPO_ROOT / "controller" / "game_controller.py"
    spec = importlib.util.spec_from_file_location("game_controller", module_path)
    if not spec or not spec.loader:
        raise RuntimeError("cannot load controller module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.resolve_slot_from_catalog, module.ControllerError


def _publish(bind_ip: str, ports: list[dict[str, Any]]) -> list[str]:
    return [f"{bind_ip}:{port['host']}:{port['container']}/{port['protocol']}" for port in ports]


def render_enshrouded(resolved: dict[str, Any], bind_ip: str) -> dict[str, Any]:
    """Build the Compose service for sknnr/enshrouded-dedicated-server.

    The image runs as its built-in non-root UID/GID 10000:10000 and persists only the
    savegame directory, which the host bind mount must own. Game and query ports are
    pinned to the catalog-reserved slot (lower = game PORT, higher = query STEAM_PORT).
    """
    template, instance = resolved["template_id"], resolved["instance_id"]
    instance_dir = f"{INSTANCES_ROOT}/{template}-{instance}"
    data_root = resolved["paths"]["instance_data"]
    limits = resolved["resource_limits"]["compose"]
    udp_ports = sorted(port["host"] for port in resolved["ports"] if port.get("protocol") == "udp")
    if not udp_ports:
        raise ValueError("enshrouded requires at least one reserved UDP port")
    # Current Enshrouded uses one game port (the config's queryPort, set from PORT). The
    # lower reserved port is the connect port players use; Steam's server-browser query is a
    # fixed 27015 that we intentionally do not publish on the tailnet (direct-connect only).
    game_port = udp_ports[0]
    return {
        "name": resolved["paths"]["compose_project"],
        "services": {
            "server": {
                "image": f"{resolved['image']}@{resolved['image_digest']}",
                "container_name": resolved["paths"]["compose_project"],
                "user": _Quoted("10000:10000"),
                "init": True,
                # systemd owns the lifecycle; the container must not resurrect itself.
                "restart": "no",
                "stop_grace_period": "60s",
                "environment": {
                    "PORT": str(game_port),
                    "SERVER_IP": "0.0.0.0",
                },
                # SERVER_NAME, SERVER_SLOTS, and the secret SERVER_PASSWORD live in this
                # root-only file written by the provisioning script -- never in the catalog.
                "env_file": [f"{instance_dir}/{template}.env"],
                "ports": _publish(bind_ip, resolved["ports"]),
                "volumes": [
                    {
                        "type": "bind",
                        "source": f"{data_root}/savegame",
                        "target": "/home/steam/enshrouded/savegame",
                        "read_only": False,
                    }
                ],
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "pids_limit": 512,
                "mem_limit": limits["mem_limit"],
                "cpus": float(limits["cpus"]),
                "networks": ["game"],
            }
        },
        "networks": {"game": {"driver": "bridge"}},
    }


ADAPTERS = {"enshrouded": render_enshrouded}


def render_unit(resolved: dict[str, Any]) -> str:
    template, instance = resolved["template_id"], resolved["instance_id"]
    instance_dir = f"{INSTANCES_ROOT}/{template}-{instance}"
    compose_file = f"{instance_dir}/compose.yaml"
    systemd = resolved["resource_limits"]["systemd"]
    # Allow the full catalog startup budget (SteamCMD downloads the server each start).
    start_timeout = max(int(resolved.get("startup_timeout_seconds", 900)), 300)
    compose = "/usr/bin/docker compose"
    project = f"--project-directory {instance_dir} --file {compose_file}"
    return f"""[Unit]
Description={resolved['display_name']} game server ({template}-{instance})
After=docker.service
Requires=docker.service

[Service]
Type=simple
WorkingDirectory={instance_dir}
ExecStartPre={compose} {project} config -q
ExecStart={compose} {project} up --no-color --remove-orphans
ExecStop={compose} {project} down
Restart=on-failure
RestartSec=10s
TimeoutStartSec={start_timeout}s
TimeoutStopSec=120s
CPUQuota={systemd['CPUQuota']}
MemoryMax={systemd['MemoryMax']}

[Install]
WantedBy=multi-user.target
"""


def render(catalog_path: Path, template: str, instance: str, bind_ip: str) -> dict[str, str]:
    resolve_slot_from_catalog, controller_error = _load_resolver()
    catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict):
        raise ValueError("catalog root must be a mapping")
    try:
        resolved = resolve_slot_from_catalog(catalog, template, instance)
    except controller_error as exc:
        raise ValueError(f"catalog rejected {template}/{instance}: {exc}") from exc
    adapter = ADAPTERS.get(template)
    if adapter is None:
        raise ValueError(f"no compose adapter is implemented for template '{template}'")
    compose = adapter(resolved, bind_ip)
    return {
        "compose.yaml": yaml.safe_dump(compose, sort_keys=False, default_flow_style=False),
        resolved["unit"]: render_unit(resolved),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a per-instance compose file and systemd unit.")
    parser.add_argument("template", help="catalog template id, e.g. enshrouded")
    parser.add_argument("instance", help="allowlisted instance id, e.g. primary")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--bind-ip", default=DEFAULT_BIND_IP, help="host IP to publish game ports on")
    parser.add_argument("--output-dir", type=Path, help="write the rendered files here (default: print to stdout)")
    args = parser.parse_args()

    try:
        files = render(args.catalog, args.template, args.instance, args.bind_ip)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"render failed: {exc}", file=sys.stderr)
        return 1

    if args.output_dir is None:
        for name, content in files.items():
            print(f"----- {name} -----")
            print(content)
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (args.output_dir / name).write_text(content, encoding="utf-8")
        print(f"wrote {args.output_dir / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
