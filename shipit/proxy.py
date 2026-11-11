"""Zero-downtime cutover through Caddy's admin API.

Apps that set `domain` run as two slots (blue/green) on 127.0.0.1:<host_port> and
127.0.0.1:<host_port+1>. Caddy routes the domain to the active slot; a release
starts the idle slot, health-checks it, flips the Caddy route, then removes the old slot.

shipit owns the Caddy config: every sync POSTs a full config to /load.
"""
from __future__ import annotations

import json
import urllib.request

SLOTS = ("blue", "green")


def slot_port(host_port: int, slot: str) -> int:
    return host_port + SLOTS.index(slot)


def build_config(entries: list[tuple[str, int]]) -> dict:
    """entries: [(domain, upstream_port)] -> Caddy JSON config."""
    routes = [
        {
            "match": [{"host": [domain]}],
            "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": f"127.0.0.1:{port}"}]}],
            "terminal": True,
        }
        for domain, port in sorted(entries)
    ]
    return {"apps": {"http": {"servers": {"shipit": {"listen": [":443"], "routes": routes}}}}}


def apply(admin_url: str, config: dict) -> None:
    req = urllib.request.Request(
        admin_url.rstrip("/") + "/load",
        data=json.dumps(config).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def sync(cfg, states: dict[str, dict]) -> None:
    """Point Caddy at every proxied app's active slot."""
    entries = []
    for name, app in cfg.apps.items():
        port = states.get(name, {}).get("port")
        if app.domain and port:
            entries.append((app.domain, port))
    apply(cfg.caddy_admin, build_config(entries))
