# Servitor — HANDOFF (2026-11-16 UTC — Stage 5 COMMITTED, gate stack green on the final config, portcullis install documented)

**Read this first if you are resuming.** Stages 0–5 are committed. The only
remaining work is the **portcullis-server install** — it is fully documented
step-by-step in **`deploy/portcullis/INSTALL.md`** (with the libvirt domain
template at `deploy/guest/servitor-domain.xml`); nothing in the repo blocks it.

**Latest state (end of session, operator to bed):** the model config was
aligned with the operator's proven pi setup — `agent-config/models.json`
(`maxTokens: 32768` → `200000`, dropped `reasoning: false`) and
`agent-config/settings.json` (compaction `keepRecentTokens: 40000` +
`reserveTokens: 40000`; pi defaults are 20000/16384). Verified on a fresh
guest: sync deployed the new content-addressed config
(`0dddfa5-cb8b7800ce868`), the mock capture shows pi now sends
`max_completion_tokens: 200000`, and a live prompt answered over the real
router with a populated thinking field. **The final full guest-suite re-run
on the new config passed** (`/tmp/guest_suite8.log`: 11 passed, 1 expected
skip, GUEST_RC=0), and every host gate was re-confirmed the same night
(make check 31/31, unit 25, api 20, stage2 update-waiting, standalone 1).
Stage 5 is COMMITTED (see §4) and pushed to origin if the laptop had GitHub
access at commit time — verify with `git status` ("ahead of origin" means
push again).

The last four sessions chased the guest suite to green. The failures were
NOT in the app's model path — they were in the deploy pipeline, the test
harness, and the laptop's network/firewall environment. Findings are in
`DECISIONS.md` ("Stage 5 findings" sections, 2026-11-16); the short list is
in §2.

---

## 0. Current state

### Guest image (on disk, patched in place)
- `deploy/guest/images/servitor-pi1.5.2-20261116.qcow2`
  - sha256 `f4959cf926cb815ee778c3321254af41dffab55d125cf0a5284261026ae9ffe8`,
    size `1584857088`; `manifest.json` `active` points at it.
  - Pristine-base semantics: **ships no API keys and no extension** — the
    harness boots a COPY and the first (provisioning) sync injects keys +
    extension + config. App code in the image is updated by sync when the
    repo app sha changes.
  - In-place patches (qemu-nbd, all recorded in the manifest note):
    1. `slot0` (uid/gid 9000, home 0700) — the doctor PROBE slot, outside
       the app pool (slot1–16) so the service's uid-gated stray kills can
       never SIGTERM the doctor's probe pi child.
    2. app tree synced from the repo (D8 flag fixes, doctor fixes).
    3. network gateway `10.42.0.1` (the build.sh derivation bug produced an
       invalid 5-octet gateway; without it the guest boots with no default
       route).
    4. systemd-resolved enabled with a public upstream + stub resolv.conf
       (the image shipped resolved disabled with a stale resolv.conf — no
       public name resolved).
    5. app tree re-synced (doctor keys-absent per-needle grep fix).
  - A clean rebuild via `deploy/guest/build.sh` now carries every fix
    (build.sh was corrected in the same sessions).

### Guest suite (`pytest test/guest`, session-scoped VM, root)
- FINAL run on the aligned model config (`/tmp/guest_suite8.log`):
  **11 passed, 1 skipped, 0 failed** (`GUEST_RC=0`).
- The skip is expected: `test_z_drills.py:157` — "no alternative
  pi-web-access version available" (the extension-swap drill is
  selection-gated; set a different `SERVITOR_PI_WEB_ACCESS_VERSION` to
  exercise it).
- The suite now genuinely validates the live router: `live_prompt` answers
  come from the real router, and `live_search` requires a successful,
  non-empty exa tool_result (a dead network can no longer fake a pass).

### Host gates
- `make check`: **31/31 GREEN** (contract lint).
- `pytest test/unit -q`: 25 passed.
- `pytest test/api -q` (root): 20 passed (`/tmp/api_suite2.log`).
- `pytest test/stage2/test_update_waiting.py` (root): passed.

---

## 1. Host prerequisites (laptop-specific; re-create after a host reimage)

These are NOT in the repo (host firewall + host .env). The guest suite
cannot pass without them:

1. **ufw** (the laptop runs ufw, default `deny (routed)` / `deny` incoming):
   ```
   sudo ufw route allow from 10.42.0.0/24            # guest egress NAT
   sudo ufw allow in from 10.42.0.0/24 to any port 48700:48799 proto tcp
   ```
   - rule 1: the guest's public/egress traffic exits through the host
     FORWARD chain; the nft masquerade (harness-created, idempotent) alone
     is not enough.
   - rule 2: the test mock router (`test/mock_router`) binds the host's
     bridge IP and allocates ports from 48700:48799 so the GUEST can reach
     it (default-deny input would drop ephemeral ports).
   - Verify: `sudo ufw status | grep -E '48700|FWD'`.
2. **`.env`** additions (host checkout, quoted values — sync.sh `source`s
   the whole file; an unquoted multi-word value makes bash execute the
   second token):
   - `SERVITOR_DNS_UPSTREAM="1.1.1.1 9.9.9.9"` (guest's resolved upstream;
     build.sh now REQUIRES it — no-literal gate bans IP constants in
     deploy/).
   - `SERVITOR_DEPLOY_KEY=/home/connor/.srv/deploy_key` must be exported
     when running anything as root (root's `~/.srv` does not exist).
3. **`/tmp` is a 7.8 G tmpfs.** The guest suite's workdir holds a 1.5 G
   root copy + a 16 G state disk (sparse on real disk, DENSE on tmpfs).
   Run the guest/api suites with a disk-backed basetemp:
   ```
   --basetemp=/home/connor/.cache/servitor-pytest/guestfinal
   ```
   A full /tmp also breaks `make check` (ensurepip unpacks in /tmp).
4. **Laptop `/etc/hosts` panel-hosts block** (managed by
   `tools/panel-hosts.sh`): pins `*.pietrasik.top` to the server's LAN IP.
   sync.sh mirrors those pins into the guest's `/etc/hosts` on every deploy
   (hairpin NAT: the public DNS name for the router dies in the host's
   NAT loop; the LAN pin works).

---

## 2. What the final sessions fixed (details: DECISIONS.md)

- **Guest egress (3 stacked bugs):** invalid 5-octet gateway baked into the
  image (no default route) → ufw `deny (routed)` dropped guest egress even
  with the default route → resolved disabled (no public DNS). All fixed in
  build.sh + host ufw + image patch; sync.sh self-heals the gateway on
  older images at deploy time.
- **Hairpin NAT:** sync.sh mirrors the laptop's panel-hosts pins into the
  guest's `/etc/hosts` (managed block, idempotent).
- **Env-file exponential bloat:** sync's env rewriter double-newlined
  preserved lines; blank lines DOUBLED each sync (~28 syncs → 184 MB →
  systemd `Argument list too long` crash loop). Rewriter now normalizes and
  drops blank/comment lines; self-heals bloated files.
- **Rollback nesting:** `cp -a` into an existing `app.previous.app` nested
  the package; update.sh's `mv` then restored `/opt/servitor/app/app/app`
  while the service kept the stale flat tree (rollback "failed" with the
  OLD doctor's str/str crash). sync now refreshes the previous tree
  rm-then-copy (flat) before every push, rm's the package before extracting
  (stale files also broke GUEST_APP_SHA NO-OP stability), update.sh guards
  on the directory, build.sh ships no `app.previous*`.
- **D8 flag deadlock + pre-restart doctor FAIL** (earlier this day): a
  fully-consumed flag now clears the file (it wedged sync's APPLIED gate
  and doctor `update.waiting`); a pre-restart doctor FAIL no longer holds
  the staged restart (provisioning injects keys only at restart).
- **Doctor probe:** dedicated `slot0` outside the app pool + probe lock +
  stale-probe-row cleanup (intermittent code -15 was boot reconciliation's
  uid-gated strays SIGTERM'ing the probe child).
- **Harness:** `service_uptime()` now computes the monotonic DELTA
  (raw `ActiveEnterTimestampMonotonic` is boot-uptime; the waiting drill
  never saw the restart); body-less POSTs send `{}` (FastAPI 422s);
  autouse post-test baseline re-sync converges the shared session VM.
- **sync identity:** config identity computed from the staged artifact
  AFTER the fault hook (the fault drill could never land before).
- **Test hardening:** `live_search` requires a successful non-empty
  tool_result; fault-drill asserts the stdout success marker `"ROLLED BACK"`
  (the `"AUTOMATIC ROLLBACK"` trigger goes to stderr).
- **Mock router:** allocates ports from the ufw-allowed range (guest→host
  mock was dropped by ufw before).

---

## 3. How to run the gates

```bash
cd /home/connor/Desktop/AgentStuff/servitor
make check                                  # stage-0 contract lint (no root)
.venv/bin/python -m pytest test/unit -q     # stage-1 unit (no root)
echo 6969 | sudo -S -p '' .venv/bin/python -m pytest test/api -q \
  --basetemp=/home/connor/.cache/servitor-pytest/apitemp      # stage-3 (root)
echo 6969 | sudo -S -p '' .venv/bin/python -m pytest test/stage2/test_update_waiting.py -q   # (root)
echo 6969 | sudo -S -p '' env SERVITOR_DEPLOY_KEY=/home/connor/.srv/deploy_key \
  .venv/bin/python -m pytest test/guest -q -rs \
  --basetemp=/home/connor/.cache/servitor-pytest/guestfinal   # stage-5 (root, ~12 min)
```
Run the guest suite in the background and poll the log (it boots a VM and
drills include a 150 s model gap): `nohup ... > /tmp/guest_suite.log 2>&1 &`.
Guest journal timestamps are UTC (host shell is PST) when correlating.

---

## 4. Commit (DONE)

Stage 5 is committed: `Stage 5: guest image + standalone deploy + doctor
(gate stack green)` — deploy/ (doctor.sh, sync.sh, update.sh,
standalone-proxy.py, guest/build.sh, guest/finish-build.sh, guest/pins.json,
guest/servitor.service, guest/servitor-domain.xml, portcullis/INSTALL.md),
`app/doctor.py`, `app/update_pending.py`, the app/env/main/lifecycle/
registry/rpc/api changes, `agent-config/` (aligned model config),
`test/guest/`, `test/standalone/`, `test/mock_router/server.py`, Makefile,
`lib/`, `.env.example`, `.gitignore` (image artifacts excluded),
DECISIONS.md, HANDOFF.md. Everything under `deploy/guest/images/` is
gitignored except `manifest.json` (the qcow2 is 1.5 G; reproducible via
build.sh). `.env` is gitignored (keys).

---

## 6. Portcullis-server install (tomorrow)

**The step-by-step is now `deploy/portcullis/INSTALL.md`** — host packages,
`.env` template, image (build on the server via `make guest`, or the ~10-min
fast path of reusing the laptop qcow2 with an nbd patch), state disk
(`mkfs.ext4 -L servitor-state`, loop-free), the libvirt domain
(`deploy/guest/servitor-domain.xml` — the image has no bootloader; libvirt
boots it via `<kernel>/<initrd>` exactly like the harness), first sync =
provisioning, the traefik/panel-do merge, the verify gate list, and a
failure-mode table distilled from the laptop sessions. Short version:

1. Server `.env` per the template in INSTALL.md §3 (edge subnet from
   `docker network inspect edge`, `SERVITOR_DNS_UPSTREAM` QUOTED).
2. `make guest` on the server (bakes THIS server's subnet + deploy key), or
   fast path: copy the laptop image and nbd-patch (INSTALL.md Appendix A).
3. State disk + `virsh define`/`start` from the domain template; watch the
   serial console on first boot.
4. `deploy/sync.sh` provisions everything (keys, extension, config, doctor
   GREEN). The image ships no keys and no extension by design — not a defect.
5. Portcullis side: merge the route (`deploy/portcullis/traefik-dynamic/
servitor.yml`, VM IP + 8787 substituted) and the panel-do actions;
   `make update` there. The compose contract publishes exactly
   `${SERVITOR_PORT}` (8787); both are validated by `make check` (contract
   lint), so the integration is "point the route at the VM's published port".

---

## 5. Known / expected

- **One guest-suite skip by design** (pi-web-access version-swap drill —
  needs an alternative version selectable in `.env`; loud-skip with reason).
- **Image is patched in place** (manifest hash updated). A clean rebuild is
  now equivalent (build.sh carries the fixes); rebuild with
  `deploy/guest/build.sh` if the base drifts (needs `.env` incl.
  `SERVITOR_DNS_UPSTREAM`, network for pacman/npm).
- **Model config = the operator's proven values** (see top): maxTokens
  200000, compaction 40000/40000, no explicit `reasoning` flag. pi still
  clamps output to context room (`contextWindow − input − 15%`), so the
  ceiling is dynamic, not a fixed budget. If the operator's
  `~/.pi/agent/settings.json` changes, mirror the compaction block into
  `agent-config/settings.json` (the TUI/theme/packages keys are
  laptop-local and intentionally NOT copied).
- **Host ufw rules don't survive a host reimage** — re-run §1.1.
- **pam_faillock:** sudo via `echo 6969 | sudo -S -p '' …` locks after a few
  bad attempts — wait ~10 min or `faillock --user connor` (check as root).
- **Pi process liveness:** the pi child shows up as `pi`, not
  `node … pi-coding-agent`; use `pgrep -x pi` / `ps aux | grep -iE 'pi|node'`.
  (`pgrep -x` on names > 15 chars silently matches nothing — use `-f`.)
- **Key material** lives in the repo `.env` (gitignored, 0600):
  `OMNISSIAH_API_KEY`, `EXA_API_KEY`, `SERVITOR_DNS_UPSTREAM`, edge/ops
  `10.42.0.0/24`, `SERVITOR_VM_IP=10.42.0.15`, port 8787, pi 1.5.2,
  pi-web-access 0.47.0, intended model `qwen35` / window 250000.
