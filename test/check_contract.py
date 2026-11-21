#!/usr/bin/env python3
"""Contract lint: `make check`.

Asserts the interface contract before any runtime code exists:

1. **Env contract** — every env var the app reads is named in
   `.env.example`; the file is the complete record of the host `.env`,
   so its name set equals ``app.env.ENV_NAMES`` plus
   ``HOST_DEPLOY_ENV_NAMES`` (host deploy scripts + image build —
   never the app; asserted disjoint). App code reads env only through
   ``app/env.py`` (AST scan). Test code reads only contracted names
   plus ``app.env.TEST_SCOPED_ENV_NAMES`` (suite-internal state; asserted
   disjoint from the contracted set) — AST scan, so an off-contract
   read in ``test/`` (e.g. a renamed ghost name) goes RED, not silent.
2. **Nothing hardcodes an address/port/path** — no IPv4 literals
   anywhere in ``app/`` or ``deploy/`` code; no loopback/any-address
   literal in ``app/`` (the bind comes from env even for loopback dev);
   the portcullis route file references the VM via placeholders, no
   literal port.
3. **Contract file parses** — the portcullis
   route yml loads as YAML with the expected shape (one router, the
   servitor hostname, the auth-chain middleware, the SSE flush
   setting, and the absence of the dead readTimeout key).
4. **Agent-config sanity** — models.json: literal baseUrl (``$VAR`` is
   NOT interpolated), ``apiKey: "$OMNISSIAH_API_KEY"`` (env-only key),
   image-capable model declared; no ``auth.json``/``models-store.json``
   (a stale stored credential would silently beat the env var); no
   context-file names; SYSTEM.md carries the config-gate marker.

Exit 0 = green. Run: `make check` (stdlib + PyYAML from the venv).
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

FAILURES: list[str] = []
CHECKS = 0


def check(ok: bool, label: str, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {label}"
    if detail and not ok:
        line += f" — {detail}"
    print(line)
    if not ok:
        FAILURES.append(label + (f" — {detail}" if detail else ""))


def fail(label: str, detail: str = "") -> None:
    check(False, label, detail)


def env_example_names() -> list[str]:
    names = []
    for line in (REPO / ".env.example").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if m:
            names.append(m.group(1))
    return names


def env_read_sites(paths: list[Path]) -> tuple[set[str], dict[str, list[str]]]:
    """AST-scan ``paths`` for env var reads; return (names, name -> locations).

    Catches ``os.environ["X"]``, ``os.getenv("X")``, ``os.environ.get("X")``
    with a constant name. (Non-constant reads are invisible — none exist.)
    """
    names: set[str] = set()
    sites: dict[str, list[str]] = {}

    def add(name: str, path: Path, lineno: int) -> None:
        names.add(name)
        sites.setdefault(name, []).append(f"{path.relative_to(REPO)}:{lineno}")

    for path in paths:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript):
                # os.environ["X"]
                v = node.value
                if (
                    isinstance(v, ast.Attribute)
                    and v.attr == "environ"
                    and isinstance(v.value, ast.Name)
                    and v.value.id == "os"
                    and isinstance(node.slice, ast.Constant)
                ):
                    add(str(node.slice.value), path, node.lineno)
            elif isinstance(node, ast.Call):
                func = node.func
                # os.getenv("X") / os.environ.get("X")
                target = None
                if isinstance(func, ast.Attribute) and func.attr == "getenv":
                    target = (
                        isinstance(func.value, ast.Name) and func.value.id == "os"
                    )
                elif (
                    isinstance(func, ast.Attribute)
                    and func.attr == "get"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "environ"
                    and isinstance(func.value.value, ast.Name)
                    and func.value.value.id == "os"
                ):
                    target = True
                if target and node.args and isinstance(node.args[0], ast.Constant):
                    add(str(node.args[0].value), path, node.lineno)
    return names, sites


def env_reads_in_app() -> set[str]:
    """AST-scan app/ for env var reads; return the set of names read."""
    names, sites = env_read_sites(sorted((REPO / "app").rglob("*.py")))
    # app/env.py is the one sanctioned reader; anywhere else is a violation
    direct_env_files = sorted(
        site
        for site_list in sites.values()
        for site in site_list
        if not site.startswith("app/env.py:")
    )
    if direct_env_files:
        fail(
            "no env reads outside app/env.py",
            "found in " + ", ".join(direct_env_files),
        )
    return names


def code_files() -> list[Path]:
    out = []
    for d in ("app", "deploy"):
        base = REPO / d
        for pattern in ("*.py", "*.sh", "*.yml", "*.yaml", "*.go"):
            out.extend(base.rglob(pattern))
    return sorted(out)


IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def main() -> int:
    import yaml  # PyYAML — in the venv (`make venv`)

    print("servitor contract check")

    # --- 1. env contract -------------------------------------------------
    print("\n[1] env contract")
    example_names = env_example_names()
    dupes = {n for n in example_names if example_names.count(n) > 1}
    check(not dupes, "no duplicate names in .env.example", str(sorted(dupes)))

    from app.env import (  # noqa: E402
        ENV_NAMES,
        HOST_DEPLOY_ENV_NAMES,
        TEST_SCOPED_ENV_NAMES,
    )

    deploy_only = set(HOST_DEPLOY_ENV_NAMES)
    check(
        set(ENV_NAMES) & deploy_only == set(),
        "app.env.HOST_DEPLOY_ENV_NAMES is disjoint from ENV_NAMES",
        str(sorted(set(ENV_NAMES) & deploy_only)),
    )
    contracted = set(ENV_NAMES) | deploy_only
    check(
        contracted == set(example_names),
        "app.env.ENV_NAMES + HOST_DEPLOY_ENV_NAMES == .env.example names",
        f"only-in-code={sorted(contracted - set(example_names))} "
        f"only-in-example={sorted(set(example_names) - contracted)}",
    )

    read_names = env_reads_in_app()
    check(
        read_names <= set(ENV_NAMES),
        "every env var read in app/ is in the contract",
        str(sorted(read_names - set(ENV_NAMES))),
    )

    # test/ may read contracted names plus the suite-scoped set — anything
    # else is an off-contract (ghost) name: a renamed .env line would be
    # silently ignored by the suites, exactly like a renamed app read would
    test_scoped = set(TEST_SCOPED_ENV_NAMES)
    check(
        test_scoped & (set(ENV_NAMES) | deploy_only) == set(),
        "app.env.TEST_SCOPED_ENV_NAMES is disjoint from the contracted set",
        str(sorted(test_scoped & (set(ENV_NAMES) | deploy_only))),
    )
    test_read_names, test_read_sites = env_read_sites(
        sorted((REPO / "test").rglob("*.py"))
    )
    ghost = test_read_names - contracted - test_scoped
    check(
        not ghost,
        "every env var read in test/ is contracted or test-scoped",
        "; ".join(
            f"{n} ({', '.join(test_read_sites[n][:3])})" for n in sorted(ghost)
        ),
    )

    # --- 2. no hardcoded address/port/path --------------------------------
    print("\n[2] no hardcoded address/port/path")
    ip_hits = []
    for path in code_files():
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if IPV4_RE.search(line):
                ip_hits.append(f"{path.relative_to(REPO)}:{i}: {line.strip()[:80]}")
    check(not ip_hits, "no IPv4 literals in app/ or deploy/", "; ".join(ip_hits[:5]))

    app_blob = "\n".join(
        p.read_text() for p in sorted((REPO / "app").rglob("*.py"))
    )
    for bad in ("127.0.0.1", "0.0.0.0", "localhost", "/var/lib/servitor"):
        check(
            bad not in app_blob,
            f"app/ contains no {bad!r} literal (env-only)",
        )

    # route file: placeholders only, no literal port
    route_raw = (REPO / "deploy" / "portcullis" / "traefik-dynamic" / "servitor.yml").read_text()
    route = yaml.safe_load(route_raw)
    routers = route["http"]["routers"]
    check(len(routers) == 1, "route file: exactly one router")
    router = next(iter(routers.values()))
    check(
        "Host(`servitor.pietrasik.top`)" in router.get("rule", ""),
        "route rule is the servitor hostname",
    )
    check(
        "auth-chain@file" in router.get("middlewares", []),
        "route uses the auth-chain middleware (the only auth gate)",
    )
    lb = route["http"]["services"]["servitor"]["loadBalancer"]
    urls = [s["url"] for s in lb.get("servers", [])]
    check(
        len(urls) == 1 and "VM_INGRESS_IP" in urls[0],
        "route targets the VM via placeholder (substituted at merge)",
        str(urls),
    )
    check(
        not re.search(r":\d+", urls[0] if urls else ""),
        "route URL has no literal port",
        urls[0] if urls else "",
    )
    check(
        lb.get("responseForwarding", {}).get("flushInterval") in ("100ms", 100),
        "route sets responseForwarding.flushInterval (SSE, no buffering)",
    )
    check(
        "readTimeout" not in lb,
        "route has no readTimeout key (not a Traefik v3 loadBalancer field — "
        "silently ignored; the no-cut contract rides the v3 defaults)",
    )

    # --- 3/4. agent-config sanity ------------------------------------------
    print("\n[3] agent-config sanity")
    ac = REPO / "agent-config"
    models = json.loads((ac / "models.json").read_text())
    providers = models.get("providers", {})
    check(len(providers) >= 1, "models.json declares at least one provider")
    for pid, prov in providers.items():
        base = prov.get("baseUrl", "")
        check(
            "$" not in base,
            f"provider {pid}: baseUrl is literal ($VAR is NOT interpolated)",
            base,
        )
        check(
            prov.get("api") == "openai-completions",
            f"provider {pid}: api is openai-completions",
        )
        check(
            prov.get("apiKey") == "$OMNISSIAH_API_KEY",
            f"provider {pid}: apiKey is $OMNISSIAH_API_KEY (env-only, invariant 5)",
            str(prov.get("apiKey")),
        )
        model_ids = [m["id"] for m in prov.get("models", [])]
        image_models = [
            m["id"] for m in prov.get("models", []) if "image" in m.get("input", [])
        ]
        check(bool(model_ids), f"provider {pid}: declares models", str(model_ids))
        check(
            bool(image_models),
            f"provider {pid}: an image-capable model is declared (binder workflow)",
            str(image_models),
        )

    bad_files = [
        p for p in ac.rglob("*") if p.name in ("auth.json", "models-store.json")
    ]
    check(
        not bad_files,
        "no auth.json / models-store.json in agent-config (stored creds would "
        "silently beat the env var)",
        str(bad_files),
    )
    ctx_files = [
        p
        for p in list(ac.rglob("*")) + list((REPO / "app").rglob("*"))
        if p.name in ("AGENTS.md", "CLAUDE.md")
    ]
    check(
        not ctx_files,
        "no AGENTS.md / CLAUDE.md in agent-config/ or app/ (context-file names)",
        str(ctx_files),
    )
    system_md = (ac / "SYSTEM.md").read_text()
    check(
        "[servitor-config v1]" in system_md,
        "SYSTEM.md carries the config-gate marker [servitor-config v1]",
    )
    check(
        "outbox/" in system_md and "inbox/" in system_md and "lib/" in system_md,
        "SYSTEM.md documents the inbox/outbox/lib layout",
    )

    settings = json.loads((ac / "settings.json").read_text())
    check(
        settings.get("defaultTools") == ["read", "bash", "edit", "write"],
        "settings.json: default tools only (codemode off)",
        str(settings.get("defaultTools")),
    )
    check(
        settings.get("compaction", {}).get("enabled") is True,
        "settings.json: compaction on",
    )

    # --- summary ------------------------------------------------------------
    print(f"\n{CHECKS} checks, {len(FAILURES)} failures")
    if FAILURES:
        print("RED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("make check: GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
