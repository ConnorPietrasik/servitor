# Servitor — gates.
#
# Gate vocabulary:
#   check          contract lint (stdlib-fast, no venv needed)
#   test           unit + conformance vs mock router
#   sandbox-smoke  root/laptop-CI runnable: overlays, EROFS, sweep walk,
#                  config/credential/context-file gates, protocol suite
#   vm-smoke       root-only: setuid spawn, chown handoff, cross-uid EPERM,
#                  real find -user sweep — loud-skip + merge-blocking in CI
#   doctor         guest-side certification
#
# Every gate states which half it actually ran; vm-smoke is NEVER silently
# green where root is unavailable.

PY := .venv/bin/python
PY_BOOT := python3

.PHONY: venv check test test-stage2 test-api test-all test-standalone test-guest sandbox-smoke vm-smoke guest sync update doctor deploy

venv:
	$(PY_BOOT) -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e '.[dev]'

## contract lint (env names, no hardcoded address/port/path,
## route/compose parse, one published port, agent-config sanity)
check: venv
	$(PY) test/check_contract.py

## unit + conformance vs mock router (needs pi install into a
## scratch prefix — the runner handles it; version-parameterized per .env)
test: venv
	$(PY) -m pytest test/unit test/conformance -v

## lifecycle (spawn/settle/respawn/idle-kill/crash drills) over the
## JSON-over-stdio app. Separate invocation: the stage2 and api test dirs
## both define a `conftest` (imported by bare name).
test-stage2: venv
	$(PY) -m pytest test/stage2 -v

## HTTP/SSE API + delivery + transcript/fork over a real
## subprocess app (root: real uids/mounts). The backpressure gate test
## is in here (slow: ~110s).
test-api: venv
	$(PY) -m pytest test/api -v

## every pytest gate, one invocation each (check is the lint gate)
test-all: venv
	$(PY) test/check_contract.py
	$(PY) -m pytest test/unit test/conformance -q
	$(PY) -m pytest test/stage2 -q
	$(PY) -m pytest test/api -q
	$(PY) -m pytest test/sandbox_smoke -q
	$(PY) -m pytest test/vm_smoke -q

## isolation, root half: overlay mounts, EROFS, whiteouts,
## upper paths, teardown order, sweep walk, config/credential/context-file
## gates, the whole protocol suite
sandbox-smoke: venv
	$(PY) -m pytest test/sandbox_smoke -v

## isolation, vm half (laptop-root or guest only): setuid
## spawn, chown handoff, cross-uid EPERM, real sweep-by-owner, full release()
vm-smoke: venv
	$(PY) -m pytest test/vm_smoke -v

## guest qcow2 build (host-side, root; unshare -m keeps the
## build's mount churn in a private mount namespace — see build.sh header)
guest:
	unshare -m deploy/guest/build.sh --env $(abspath .env) --deploy-key $(if $(DEPLOY_KEY),$(DEPLOY_KEY),$(HOME)/.srv/deploy_key.pub)

## standalone/loopback mode behind the local reverse proxy (no root)
test-standalone: venv
	$(PY) -m pytest test/standalone -v

## guest test suite (live gates + update drills, root)
test-guest: venv
	$(PY) -m pytest test/guest -v

## host-side one-way sync (config/lib -> guest, key slice, doctor)
sync:
	deploy/sync.sh

## single day-2 update path (float in-major + freshness-floor image
## rebuild + swap + host-side gate + automatic rollback, image included)
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
