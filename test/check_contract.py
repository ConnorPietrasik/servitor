#!/usr/bin/env python3
"""Contract lint.

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
3. **Deploy heredoc safety** — an unquoted ``<<WORD`` body is expanded
   by the WRITING shell, so it must carry no command substitution (a
   backtick or an unescaped ``$( ``): a bare interpreter at write time
   inherits the operator's tty and hangs the build behind an
   interactive prompt. Quoted ``<<'WORD'`` bodies are literal.
4. **Contract file parses** — the portcullis
   route yml loads as YAML with the expected shape (one router, the
   servitor hostname, the auth-chain middleware, the servitor-edge
   credential middleware, the SSE flush
   setting, and the absence of the dead readTimeout key).
5. **Agent-config sanity** — models.json: literal baseUrl (``$VAR`` is
   NOT interpolated), ``apiKey: "$OMNISSIAH_API_KEY"`` (env-only key),
   image-capable model declared; no ``auth.json``/``models-store.json``
   (a stale stored credential would silently beat the env var); no
   context-file names; SYSTEM.md carries the config-gate marker.
6. **Global event contract** — the app-wide stream carries exactly
   ``GLOBAL_EVENTS`` (``app/lifecycle.py``), and the UI's ``subscribeGlobal``
   names list (``ui/src/api.ts``) is its other half: every emitted kind is
   subscribed, and the UI subscribes to no kind that is never emitted (the
   transport's ``gap`` resync signal excepted). The halves live in different
   languages — a rename is a silent "the list lags" bug without this.

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
    with a constant name. Non-constant reads are invisible to the scan — the
    only ones are the operator-named credential envs (the CLI's
    ``--api-key-env`` / ``--exa-key-env``) read in ``app/main.py`` and
    ``app/doctor.py``: their names are passed in at invocation, so they are
    deliberately not contracted here.
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
HEREDOC_START_RE = re.compile(r"<<(-?)(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")


def unquoted_heredoc_bodies(path: Path) -> list[list[tuple[int, str]]]:
    """Bodies of UNQUOTED-delimiter heredocs in a shell script, as
    (lineno, line) lists. An unquoted ``<<WORD`` body is expanded by the
    writing shell (parameter + command substitution, as in double quotes);
    a quoted ``<<'WORD'`` body is literal. Comment lines never start a
    heredoc in this repo's style — the scripts keep them clear of ``<<``."""
    bodies: list[list[tuple[int, str]]] = []
    lines = path.read_text().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = HEREDOC_START_RE.search(line)
        if m and not m.group(2) and not line.lstrip().startswith("#"):
            dash, delim = m.group(1), m.group(3)
            i += 1
            body: list[tuple[int, str]] = []
            while i < len(lines):
                candidate = lines[i].lstrip("\t") if dash else lines[i]
                if candidate.strip() == delim:
                    break
                body.append((i + 1, lines[i]))
                i += 1
            bodies.append(body)
        i += 1
    return bodies


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
        "route uses the auth-chain middleware (the user gate)",
    )
    check(
        "servitor-edge@file" in router.get("middlewares", []),
        "route uses the servitor-edge credential middleware "
        "(the app re-verifies X-Servitor-Edge-Key on every route)",
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

    # --- 3. deploy heredoc safety ------------------------------------------
    print("\n[3] deploy heredoc safety")
    # Unquoted <<WORD bodies are expanded by the WRITING shell (that is how
    # ${SERVITOR_PI_VERSION} reaches chroot-setup.sh): a backtick or an
    # unescaped $( ) in the body runs a command at cat-time. A bare
    # interpreter there inherits the operator's tty and hangs the build
    # behind an interactive prompt (2026-11: `python` in a comment). The
    # contract for literal shell text in those bodies is escaping (\$(, \`)
    # — the chroot-setup script's own variables already ride it.
    heredoc_hits: list[str] = []
    for path in sorted((REPO / "deploy").rglob("*.sh")):
        for body in unquoted_heredoc_bodies(path):
            for lineno, line in body:
                for k, ch in enumerate(line):
                    if ch == "`" and (k == 0 or line[k - 1] != "\\"):
                        heredoc_hits.append(
                            f"{path.relative_to(REPO)}:{lineno}: {line.strip()[:80]}"
                        )
                    if line[k : k + 2] == "$(" and (k == 0 or line[k - 1] != "\\"):
                        heredoc_hits.append(
                            f"{path.relative_to(REPO)}:{lineno}: {line.strip()[:80]}"
                        )
    check(
        not heredoc_hits,
        "unquoted heredoc bodies in deploy/ carry no command substitution "
        "(a bare interpreter at write time hangs the build on the operator's tty)",
        "; ".join(sorted(set(heredoc_hits))[:5]),
    )

    # --- 4. contract file + agent-config sanity -----------------------------
    print("\n[4] agent-config sanity")
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
            f"provider {pid}: apiKey is $OMNISSIAH_API_KEY (env-only, never on disk)",
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
    strict_md_path = ac / "STRICT_SYSTEM.md"
    check(
        strict_md_path.is_file(),
        "STRICT_SYSTEM.md present (the strict session's replacement prompt, "
        "synced with the config)",
    )
    if strict_md_path.is_file():
        check(
            "[servitor-config v1]" in strict_md_path.read_text(),
            "STRICT_SYSTEM.md carries the config-gate marker [servitor-config v1]",
        )

    # the strict tool allowlist is a capability boundary: it must stay a
    # non-empty subset of pi's read-only builtins (a bash/edit/write slip-in
    # would silently reopen the surface the mode exists to close). Read as
    # AST like the other cross-module checks — this lint never imports
    # runtime code (a broken app tree must fail as a named check, not an
    # import crash).
    launch_tree = ast.parse((REPO / "app" / "rpc" / "launch.py").read_text())
    strict_tools: set[str] = set()
    for node in ast.walk(launch_tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "STRICT_TOOLS"
        ):
            strict_tools = {
                el.value
                for el in node.value.elts
                if isinstance(el, ast.Constant) and isinstance(el.value, str)
            }
    READ_ONLY_BUILTINS = {"read", "grep", "find", "ls"}
    check(
        bool(strict_tools) and strict_tools <= READ_ONLY_BUILTINS,
        "launch.STRICT_TOOLS is a non-empty subset of the read-only pi builtins",
        str(sorted(strict_tools)),
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

    # --- 5. global event contract (python <-> ui) ---------------------------
    print("\n[5] global event contract")
    # The app-wide stream carries exactly GLOBAL_EVENTS, and the UI's
    # subscribeGlobal list is the other half of the same contract — in a
    # second language, so nothing else would notice a rename: an emitted
    # kind nobody subscribes to is bytes burned on a metered phone, and a
    # subscribed name the backend never emits shows up only as "the list
    # lags". "gap" is the transport's resync signal (the ring evicted past
    # Last-Event-ID), not a feed kind — the one allowed extra.
    lc_tree = ast.parse((REPO / "app" / "lifecycle.py").read_text())
    kinds: set[str] = set()
    for node in ast.walk(lc_tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "GLOBAL_EVENTS" for t in node.targets
        ):
            kinds = {
                el.value
                for el in node.value.elts
                if isinstance(el, ast.Constant) and isinstance(el.value, str)
            }
    check(bool(kinds), "lifecycle.py: GLOBAL_EVENTS is a literal tuple of names")
    ui_src = (REPO / "ui" / "src" / "api.ts").read_text()
    fn = re.search(r"export function subscribeGlobal", ui_src)
    check(bool(fn), "ui/src/api.ts: subscribeGlobal found")
    names_block = (
        re.search(r"const names = \[(.*?)\];", ui_src[fn.start():], re.S) if fn else None
    )
    check(bool(names_block), "ui/src/api.ts: the subscribeGlobal names list")
    ui_names = (
        set(re.findall(r'"([A-Za-z0-9_]+)"', names_block.group(1)))
        if names_block
        else set()
    )
    check(
        kinds <= ui_names,
        "every GLOBAL_EVENTS kind is subscribed by the UI",
        "never received by the list: " + str(sorted(kinds - ui_names)),
    )
    extra = ui_names - kinds
    check(
        extra <= {"gap"},
        "the UI subscribes to no kind the backend never emits (gap excepted)",
        "dead or renamed: " + str(sorted(extra - {"gap"})),
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
