"""API contract gate (versioned in-repo; drift = gate red).

The live app's OpenAPI route set must equal test/.scratch/api-contract.json
exactly (method + path). Nothing extra is published; nothing contract
listed is missing.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((REPO / "test" / ".scratch" / "api-contract.json").read_text())


def test_route_set_matches_contract(api):
    r = api.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    live = set()
    for path, ops in spec["paths"].items():
        for method in ops:
            if method in ("get", "post", "put", "delete", "patch"):
                live.add((method.upper(), path))
    contract = {(rt["method"], rt["path"]) for rt in CONTRACT["routes"]}
    missing = contract - live
    extra = live - contract
    assert not missing, f"contract routes missing from the app: {sorted(missing)}"
    assert not extra, f"app publishes routes not in the contract: {sorted(extra)}"


def test_contract_api_version(api):
    r = api.get("/healthz")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["api"] == CONTRACT["api"]
