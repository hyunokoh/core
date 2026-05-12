"""Service registry for chaos engineering.

Maps demo-service ports (5500-5640) to their:
  - human name
  - safety classification  (CRITICAL / NORMAL / EXPERIMENTAL)
  - health-check URL  (via the proxy on 5500, or direct)
  - restart command   (relative to the project root)

CRITICAL services are never killed by kill_loop.py.

This is the single source of truth for the chaos toolkit -- if a new demo
service comes online, add it here, NOT to individual chaos scripts.

WARNING: the zkCEX docker compose stack (8094, 8091, ...) is OFF-LIMITS.
Anything outside the 5500-5640 range is rejected even if the user passes it
explicitly via --kill.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # .../zkCEX/core
TOOLS_DIR = PROJECT_ROOT / "tools"

# Allowed port band -- anything outside is rejected hard.
SAFE_PORT_MIN = 5500
SAFE_PORT_MAX = 5640


class ServiceSpec:
    __slots__ = ("port", "name", "tier", "health_path", "restart_cmd")

    def __init__(
        self, port: int, name: str, tier: str, health_path: str, restart_cmd: list[str]
    ) -> None:
        self.port = port
        self.name = name
        self.tier = tier  # CRITICAL | NORMAL | EXPERIMENTAL
        self.health_path = health_path  # path on http://127.0.0.1:5500/...
        self.restart_cmd = restart_cmd

    def __repr__(self) -> str:
        return f"<{self.name}:{self.port} tier={self.tier}>"


def _py(script: str, *args: str) -> list[str]:
    return ["python3", str(TOOLS_DIR / script), *args]


def _py_mod(module: str, *args: str) -> list[str]:
    return ["python3", "-m", module, *args]


REGISTRY: dict[int, ServiceSpec] = {
    5500: ServiceSpec(5500, "proxy", "CRITICAL", "/v3/ping", _py("serve_homepage.py", "5500")),
    5501: ServiceSpec(5501, "auth", "CRITICAL", "/auth/health", _py("auth_server.py", "5501")),
    5502: ServiceSpec(5502, "chain", "CRITICAL", "/chain/info", _py("chain_server.py", "5502")),
    5503: ServiceSpec(5503, "pol", "NORMAL", "/pol/health", _py("pol_server.py", "5503")),
    5504: ServiceSpec(
        5504, "zkpol-bridge", "NORMAL", "/bridge/health", _py("zkpol_bridge.py", "5504")
    ),
    5505: ServiceSpec(
        5505, "pol-feed", "NORMAL", "/pol-feed/health", _py("pol_snapshot_feed.py", "5505")
    ),
    5510: ServiceSpec(5510, "ws-feed", "NORMAL", "/ws/health", _py("ws_feed.py", "5510")),
    5520: ServiceSpec(
        5520,
        "custody-signer-0",
        "EXPERIMENTAL",
        "/health",
        _py_mod("tools.custody.signer_node", "--port", "5520", "--node-id", "n0"),
    ),
    5521: ServiceSpec(
        5521,
        "custody-signer-1",
        "EXPERIMENTAL",
        "/health",
        _py_mod("tools.custody.signer_node", "--port", "5521", "--node-id", "n1"),
    ),
    5522: ServiceSpec(
        5522,
        "custody-signer-2",
        "EXPERIMENTAL",
        "/health",
        _py_mod("tools.custody.signer_node", "--port", "5522", "--node-id", "n2"),
    ),
    5523: ServiceSpec(
        5523,
        "custody-signer-3",
        "EXPERIMENTAL",
        "/health",
        _py_mod("tools.custody.signer_node", "--port", "5523", "--node-id", "n3"),
    ),
    5524: ServiceSpec(
        5524,
        "custody-signer-4",
        "EXPERIMENTAL",
        "/health",
        _py_mod("tools.custody.signer_node", "--port", "5524", "--node-id", "n4"),
    ),
    5530: ServiceSpec(
        5530,
        "custody-coord",
        "EXPERIMENTAL",
        "/health",
        _py_mod("tools.custody.coordinator", "--port", "5530"),
    ),
    5540: ServiceSpec(
        5540, "export", "NORMAL", "/export/index.json", _py("export_server.py", "5540")
    ),
    5550: ServiceSpec(
        5550, "api-keys", "NORMAL", "/api-keys/health", _py("api_key_server.py", "5550")
    ),
    5560: ServiceSpec(
        5560,
        "mcp",
        "NORMAL",
        "/mcp/health",
        _py("mcp_server.py", "--transport", "http", "--port", "5560"),
    ),
    5570: ServiceSpec(
        5570, "order-engine", "NORMAL", "/orders/health", _py("order_engine.py", "5570")
    ),
    5580: ServiceSpec(5580, "push", "NORMAL", "/push/health", _py("push_server.py", "5580")),
    5590: ServiceSpec(
        5590, "perp-engine", "NORMAL", "/fapi/v1/ping", _py("perp_engine.py", "5590")
    ),
    5600: ServiceSpec(5600, "mm-bot", "NORMAL", "/mm/health", _py("mm_bot.py", "5600")),
    5601: ServiceSpec(5601, "safu", "NORMAL", "/safu/health", _py("safu_server.py", "5601")),
}


def is_safe_port(port: int) -> bool:
    return SAFE_PORT_MIN <= port <= SAFE_PORT_MAX


def get(port: int) -> ServiceSpec | None:
    return REGISTRY.get(port)


def killable() -> list[ServiceSpec]:
    """All services that are NOT critical."""
    return [s for s in REGISTRY.values() if s.tier != "CRITICAL"]


def by_tier(tier: str) -> list[ServiceSpec]:
    return [s for s in REGISTRY.values() if s.tier == tier]
