"""Fleet admin API (kela deployment/provision), read-only.

    GET /admin/v1/nodes                                   S0.5: find the node
    GET /admin/v1/nodes/{id}                              S4.2: the node's images report
    GET /admin/v1/sites/{id}/release                      S4.1: desired vs running release
    GET /admin/v1/system-configuration/{model}/{role}     S4.3: desired k3s / node-controller
    GET /admin/v1/packages                                S4.11: model digest → version tag
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

import httpx

TIMEOUT_S = 15


class Fleet:
    def __init__(self, url: str, token: str) -> None:
        self.url = (url or "").rstrip("/")
        self.token = token or ""

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.token)

    def get(self, path: str) -> Any:
        r = httpx.get(f"{self.url}{path}", headers={"Authorization": f"Bearer {self.token}"},
                      timeout=TIMEOUT_S)
        r.raise_for_status()
        return r.json()

    def nodes(self) -> list[dict]:
        return self.get("/admin/v1/nodes").get("nodes") or []

    def node(self, node_id: str) -> dict:
        """NodeDetail: includes imagesReport {bundleTag, images[{name, role, image}]}."""
        return self.get(f"/admin/v1/nodes/{quote(node_id, safe='')}")

    def site_release(self, site_id: str) -> dict:
        return self.get(f"/admin/v1/sites/{quote(site_id, safe='')}/release")

    def staged_configuration(self, model: str, role: str, version: Optional[int]) -> Optional[dict]:
        """{version, configuration} a node at (model, role) converges to: the
        pinned `version`, or — a node with no pin follows the latest staged
        version for its (model, role) — the newest one when `version` is None."""
        path = f"/admin/v1/system-configuration/{quote(model, safe='')}/{quote(role, safe='')}"
        confs = self.get(path).get("configurations") or []      # newest first
        if version is not None:
            confs = [c for c in confs if c.get("version") == version]
        return confs[0] if confs else None

    def system_configuration(self, model: str, role: str, version: Optional[int]) -> Optional[dict]:
        entry = self.staged_configuration(model, role, version)
        return entry["configuration"] if entry else None

    def package_tags(self, kind: str, name: str) -> dict[str, str]:
        """{digest: tag} for one package (e.g. kind=model, name=gradz-704-clsw3)."""
        for p in self.get("/admin/v1/packages").get("packages") or []:
            if p.get("kind") == kind and p.get("name") == name:
                return {v["digest"]: v["tag"] for v in p.get("versions") or []
                        if v.get("digest") and v.get("tag")}
        return {}
