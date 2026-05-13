from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
VALUES_PATH = ROOT / "tools" / "deploy" / "k8s" / "charts" / "zkcex" / "values.yaml"
DOCKERFILES_DIR = ROOT / "deploy" / "images"


def _parse_service_contracts() -> dict[str, dict[str, str]]:
    contracts: dict[str, dict[str, str]] = {}
    current: str | None = None
    in_image = False
    in_services = False

    for raw_line in VALUES_PATH.read_text().splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line:
            continue
        if line == "services:":
            in_services = True
            continue
        if not in_services:
            continue
        if not raw_line.startswith("  "):
            break
        if raw_line.startswith("  ") and not raw_line.startswith("    "):
            current = line.strip().rstrip(":")
            contracts[current] = {}
            in_image = False
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped == "image:":
            in_image = True
            continue
        if raw_line.startswith("    ") and not raw_line.startswith("      "):
            in_image = False
            if stripped.startswith("port:"):
                contracts[current]["port"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("healthPath:"):
                contracts[current]["healthPath"] = stripped.split(":", 1)[1].strip()
            continue
        if in_image and raw_line.startswith("      ") and stripped.startswith("name:"):
            contracts[current]["imageName"] = stripped.split(":", 1)[1].strip()

    return contracts


def _parse_docker_contract(image_name: str) -> dict[str, str]:
    dockerfile = DOCKERFILES_DIR / f"Dockerfile.{image_name}"
    text = dockerfile.read_text()
    exposed = re.search(r"^EXPOSE\s+(\d+)", text, re.MULTILINE)
    health = re.search(
        r'healthcheck\.py",\s*"(?P<port>\d+)",\s*"(?P<path>[^"]+)"',
        text,
    )
    assert exposed is not None, f"missing EXPOSE in {dockerfile.name}"
    assert health is not None, f"missing healthcheck in {dockerfile.name}"
    return {
        "port": exposed.group(1),
        "healthPath": health.group("path"),
        "healthPort": health.group("port"),
    }


def test_chart_service_contracts_match_dockerfiles() -> None:
    contracts = _parse_service_contracts()
    image_name_overrides = {
        "custody-coordinator": "custody-coord",
        "pol": "pol-py",
    }

    for service_name, expected in contracts.items():
        image_name = (
            expected.get("imageName") or image_name_overrides.get(service_name) or service_name
        )
        docker = _parse_docker_contract(image_name)
        assert expected.get("port") == docker["port"], service_name
        assert expected.get("port") == docker["healthPort"], service_name
        assert expected.get("healthPath") == docker["healthPath"], service_name
