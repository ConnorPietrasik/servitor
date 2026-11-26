# Servitor — gates.
#
# Gate vocabulary:
#   check          contract lint (fast once the venv exists; needs venv)
#   test           unit + conformance vs mock router (conformance
#                  loud-skips without root)
#   test-stage2    lifecycle drills over the stdio app (root, server)
#   sandbox-smoke  root (server): overlays, EROFS, sweep walk,
#                  config/credential/context-file gates, protocol suite
#   doctor         guest-side certification
#
# The root suites run on the server (sudo make <target>); a gate is
# NEVER silently green where root is unavailable.

PY := .venv/bin/python
PY_BOOT := python3
# venv is file-backed (stamped), not .PHONY: rebuild happens only when the
# interpreter version or the declared deps change. Keeps `make check` fast
# and offline-capable once the venv has been built once (first build needs
# network for pip). The stamp lives inside .venv so wiping the venv forces
# a rebuild.
PYVER := $(shell $(PY_BOOT) -V 2>/dev/null | tr ' ' '_')
VENV_STAMP := .venv/.stamp_$(PYVER)

# The suites parameterize on the .env version selections (env-only, no
# default — a missing selection fails loud inside the suite). Source the
# repo .env the way the deploy scripts do — only strict KEY=value lines
# (cf. deploy/update.sh) — so the targets need no exported env. The temp
# file keeps this POSIX-sh safe (no process substitution).
ENV_LOAD := if [ -f .env ]; then set -a; grep -E '^[A-Za-z_][A-Za-z0-9_]*=' ./.env > .env.sh; . ./.env.sh; rm -f ./.env.sh; set +a; fi

.PHONY: venv check test test-stage2 sandbox-smoke guest sync pull update doctor deploy

$(VENV_STAMP): pyproject.toml
	$(PY_BOOT) -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e '.[dev]'
	@true > $(VENV_STAMP)

venv: $(VENV_STAMP)

## contract lint (env names, no hardcoded address/port/path,
## route parse, agent-config sanity)
check: venv
	$(PY) test/check_contract.py

## unit + conformance vs mock router (needs pi install into a
## scratch prefix — the runner handles it; version-parameterized per .env)
test: venv
	$(ENV_LOAD); $(PY) -m pytest test/unit test/conformance -v

## lifecycle (spawn/settle/respawn/idle-kill/crash drills) over the
## JSON-over-stdio app (root, server).
test-stage2: venv
	$(ENV_LOAD); $(PY) -m pytest test/stage2 -v

## isolation gates as root (server): overlay mounts, EROFS, whiteouts,
## upper paths, teardown order, sweep walk, config/credential/context-file
## gates, the whole protocol suite
sandbox-smoke: venv
	$(ENV_LOAD); $(PY) -m pytest test/sandbox_smoke -v

## guest qcow2 build (host-side, root; unshare -m keeps the
## build's mount churn in a private mount namespace — see build.sh header)
guest:
	unshare -m deploy/guest/build.sh --env $(abspath .env) --deploy-key $(if $(DEPLOY_KEY),$(DEPLOY_KEY),$(HOME)/.srv/deploy_key.pub)

## host-side one-way sync (config/lib -> guest, key slice, doctor)
sync:
	deploy/sync.sh

## host-side pull of the durable bytes — the kept shelf and the library's
## UI saves (guest -> host, one-way, rsync).
## Flags ride a variable: `make pull -- flag` cannot work, because make
## parses everything after `--` as more goals — so the flag is a value,
## not a goal:  make pull ARGS=--dry-run
pull:
	deploy/pull.sh $(ARGS)

## single day-2 update path (float in-major + freshness-floor image
## rebuild + swap + automatic rollback, image included)
update:
	deploy/update.sh

## guest-side certification (via host wrapper)
doctor:
	deploy/doctor.sh

## server deploy: NO SCRIPT BY DESIGN — prints where the one-time
## documented install lives. There is no deploy/deploy.sh on purpose:
## the operator decisions (VM IP, image build, first-boot
## console watch) are load-bearing, and a scripted one-shot would hide them.
deploy:
	@echo "no deploy script on purpose (one-time documented install — the"
	@echo "operator decisions must stay visible). Follow, in order:"
	@echo "  this repo:    deploy/portcullis/INSTALL.md (host work, .env, image,"
	@echo "                state disk, domain, first sync, portcullis merge, verify)"
	@echo "  portcullis:   docs/RUNBOOK.md (Servitor agent VM section)"
