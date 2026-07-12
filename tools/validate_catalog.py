#!/usr/bin/env python3
"""Validate the root-owned game catalog before it is deployed or changed."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
PORT_PROTOCOLS = {"tcp", "udp"}
REQUIRED_PATH_TEMPLATES = {
    "instance_data",
    "backup_root",
    "compose_project",
    "compose_file",
    "systemd_unit",
    "log_file",
    "service_account",
}
REQUIRED_TEMPLATE_FIELDS = {
    "display_name",
    "description",
    "documentation_url",
    "enabled",
    "supported_players",
    "connection",
    "image",
    "startup",
    "resources",
    "dependencies",
    "incompatibilities",
    "instance_policy",
    "deployment",
    "update_policy",
}


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def required(mapping: dict[str, Any], fields: set[str], location: str, errors: list[str]) -> None:
    for field in sorted(fields - mapping.keys()):
        fail(errors, f"{location}: missing required field '{field}'")


def validate_template(template_id: str, template: Any, all_ports: set[tuple[str, int]], errors: list[str]) -> None:
    location = f"templates.{template_id}"
    if not ID_PATTERN.fullmatch(template_id):
        fail(errors, f"{location}: template ID must match {ID_PATTERN.pattern}")
    if not isinstance(template, dict):
        fail(errors, f"{location}: must be a mapping")
        return

    required(template, REQUIRED_TEMPLATE_FIELDS, location, errors)
    image = template.get("image", {})
    if not isinstance(image, dict):
        fail(errors, f"{location}.image: must be a mapping")
    elif not DIGEST_PATTERN.fullmatch(str(image.get("digest", ""))):
        fail(errors, f"{location}.image.digest: must be a sha256 image digest")

    resources = template.get("resources", {})
    if not isinstance(resources, dict):
        fail(errors, f"{location}.resources: must be a mapping")
    else:
        for field in ("cpu_cores", "memory_mib", "disk_gib", "player_capacity"):
            if not isinstance(resources.get(field), (int, float)) or resources[field] <= 0:
                fail(errors, f"{location}.resources.{field}: must be a positive number")

    policy = template.get("instance_policy", {})
    if not isinstance(policy, dict):
        fail(errors, f"{location}.instance_policy: must be a mapping")
        return

    allowed = policy.get("allowed_instance_ids")
    slots = policy.get("slots")
    maximum = policy.get("max_instances")
    if not isinstance(allowed, list) or not allowed or not all(isinstance(item, str) and ID_PATTERN.fullmatch(item) for item in allowed):
        fail(errors, f"{location}.instance_policy.allowed_instance_ids: must be non-empty valid IDs")
        return
    if len(set(allowed)) != len(allowed):
        fail(errors, f"{location}.instance_policy.allowed_instance_ids: contains duplicates")
    if not isinstance(maximum, int) or maximum < 1 or maximum > len(allowed):
        fail(errors, f"{location}.instance_policy.max_instances: must be between 1 and the number of allowed IDs")
    if not isinstance(slots, dict) or set(slots) != set(allowed):
        fail(errors, f"{location}.instance_policy.slots: must define exactly the allowed instance IDs")
        return

    for instance_id, slot in slots.items():
        slot_location = f"{location}.instance_policy.slots.{instance_id}"
        if not isinstance(slot, dict) or not isinstance(slot.get("display_label"), str) or not slot["display_label"].strip():
            fail(errors, f"{slot_location}: requires a non-empty display_label")
            continue
        ports = slot.get("ports")
        if not isinstance(ports, list) or not ports:
            fail(errors, f"{slot_location}.ports: must be a non-empty list")
            continue
        for port in ports:
            if not isinstance(port, dict):
                fail(errors, f"{slot_location}.ports: each entry must be a mapping")
                continue
            protocol, host, container = port.get("protocol"), port.get("host"), port.get("container")
            if protocol not in PORT_PROTOCOLS:
                fail(errors, f"{slot_location}.ports: protocol must be tcp or udp")
            if not isinstance(host, int) or not 1 <= host <= 65535:
                fail(errors, f"{slot_location}.ports: host port must be between 1 and 65535")
                continue
            if not isinstance(container, int) or not 1 <= container <= 65535:
                fail(errors, f"{slot_location}.ports: container port must be between 1 and 65535")
            key = (str(protocol), host)
            if key in all_ports:
                fail(errors, f"{slot_location}.ports: {protocol}/{host} is allocated more than once")
            all_ports.add(key)

    deployment = template.get("deployment", {})
    if not isinstance(deployment, dict):
        fail(errors, f"{location}.deployment: must be a mapping")
    else:
        for field in ("backup_identity_template", "compose_project_template", "systemd_unit_template"):
            value = deployment.get(field)
            if not isinstance(value, str) or "{template}" not in value or "{instance}" not in value:
                fail(errors, f"{location}.deployment.{field}: must include '{{template}}' and '{{instance}}'")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("catalog", type=Path, help="path to catalog.yaml")
    args = parser.parse_args()

    try:
        catalog = yaml.safe_load(args.catalog.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        print(f"Catalog could not be read: {exc}", file=sys.stderr)
        return 2

    errors: list[str] = []
    if not isinstance(catalog, dict):
        print("Catalog root must be a mapping", file=sys.stderr)
        return 1
    if catalog.get("schema_version") != 1:
        fail(errors, "schema_version: must equal 1")
    path_templates = catalog.get("path_templates")
    if not isinstance(path_templates, dict):
        fail(errors, "path_templates: must be a mapping")
    else:
        required(path_templates, REQUIRED_PATH_TEMPLATES, "path_templates", errors)
        for name in REQUIRED_PATH_TEMPLATES & path_templates.keys():
            value = path_templates[name]
            if not isinstance(value, str) or "{template}" not in value or "{instance}" not in value:
                fail(errors, f"path_templates.{name}: must include '{{template}}' and '{{instance}}'")

    templates = catalog.get("templates")
    if not isinstance(templates, dict) or not templates:
        fail(errors, "templates: must be a non-empty mapping")
    else:
        all_ports: set[tuple[str, int]] = set()
        for template_id, template in templates.items():
            validate_template(str(template_id), template, all_ports, errors)

    if errors:
        print("Catalog validation failed:", file=sys.stderr)
        print("\n".join(f"- {error}" for error in errors), file=sys.stderr)
        return 1
    print(f"Catalog validation passed: {args.catalog}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
