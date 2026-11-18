// CANONICAL: portcullis panel-do/main.go — the "servitor (agent VM)"
// block in the ACTIONS section plus the four registry entries at the end of
// its `registry` map.
//
// This file is the servitor repo's record of the merged integration.
// It is NOT built here — if it and portcullis ever
// disagree, PORTCULLIS WINS: it is what builds, installs, and runs
// (`make panel-do` there). It exists so this repo documents the exact
// integration surface without depending on the other repo, and so the
// merge into a fresh portcullis clone is a mechanical diff.
//
//   servitor-status      virsh domstate + best-effort guest /healthz probe
//   servitor-vm-restart  virsh reboot (planned; the app's boot
//                        reconciliation handles the unclean path)
//   servitor-sync        /opt/servitor-repo/deploy/sync.sh — never
//                        blocks a run (active run -> update-waiting flag,
//                        exit 0)
//   servitor-doctor      /opt/servitor-repo/deploy/doctor.sh — guest
//                        certification + firewall probes + live prompt
//                        + live search (one live run that searches the web)
//
// Contract points the merged code enforces (violations would be portcullis
// bugs, not servitor's):
//   - exec ONLY via runCmd(ctx, "/abs/path", ...) — no shell, no relative
//     paths, scrubbed env; the env has no HOME, so the deploy key is passed
//     explicitly as --deploy-key /root/.srv/deploy_key (keep the key there);
//   - both deploy scripts pass checkManagedScript before EVERY exec — the
//     same anti-swap standard as portcullis's auto-update clone scripts
//     (script + dir chain to /, not group/other-writable, not panel-owned,
//     exec bit, bash shebang);
//   - the healthz probe target (SERVITOR_BIND/SERVITOR_PORT from this
//     repo's .env) is validated as plain IPv4 + port before use: a bounded
//     one-shot to a fixed, non-argument target — a documented exception to
//     panel-do's "no network" rule, degrading to a string on any failure;
//   - no action takes arguments; sync/doctor budgets (600s/600s) fit
//     panel-api's 660s HTTP cap (timeout_sec + 10, clamped at 660).

package main

// The functions below (and the registry entries at the end) drop into
// portcullis's panel-do/main.go ACTIONS section; the helpers they call —
// runCmd, checkManagedScript, logf — live in that file too.

// ── servitor (agent VM) ──────────────────────────────────────────────────────
//
// The servitor VM is a separate project (its own repo, checked out on the
// host at /opt/servitor-repo) that portcullis hosts on the edge segment:
// a libvirt domain on the edge bridge, reached by a plain file-provider
// route (traefik/dynamic/servitor.yml — invariant 2 holds: the route is a
// file, no docker.sock). The app authenticates nobody; the edge auth chain
// is its only gate, and the VM's own nftables admits its port from the edge
// subnet only.
//
// The four actions below are thin wrappers over virsh + the servitor repo's
// host-side deploy scripts. The scripts exec as ROOT out of an admin-editable
// checkout, so they pass the SAME anti-swap standard as the auto-update
// clone scripts (checkManagedScript: file + directory chain to /, owner,
// shebang) — a writable checkout is a swapped root script, nothing less.
// RUNBOOK → "Servitor (agent VM)".

const (
	servitorRepoPath  = "/opt/servitor-repo"
	servitorVMName    = "servitor"
	servitorDeployKey = "/root/.srv/deploy_key"
)

// servitorIPRe / servitorPortRe — the healthz probe target must be a plain
// IPv4 + port. The values come from the servitor checkout's .env (below),
// not from any argument, but they are still validated before use: a
// malformed value must degrade the probe, not become a URL.
var servitorIPRe = regexp.MustCompile(`^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$`)
var servitorPortRe = regexp.MustCompile(`^\d{1,5}$`)

// servitorEnvKV reads ONE key from the servitor checkout's .env (the same
// contract file deploy/sync.sh consumes). It never sources the file and
// returns only the one value: the rest of it holds secrets (the API keys)
// this action must not touch, and quoted multi-word values (SERVITOR_DNS_
// UPSTREAM) must not be misparsed.
func servitorEnvKV(key string) (string, error) {
	data, err := os.ReadFile(servitorRepoPath + "/.env")
	if err != nil {
		return "", err
	}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, key+"=") {
			continue
		}
		v := strings.TrimSpace(strings.TrimPrefix(line, key+"="))
		return strings.Trim(v, `"'`), nil
	}
	return "", fmt.Errorf("%s not set in %s/.env", key, servitorRepoPath)
}

// probeServitorHealth — best-effort liveness probe from the host at the VM
// ingress address: the target is the ONE pair of values the servitor
// checkout's .env declares (SERVITOR_BIND/SERVITOR_PORT — the same contract
// the sync pipeline and the doctor's open-probe use). A deliberate exception
// to the "no network" rule, bounded on both ends: a 5 s one-shot curl to a
// fixed, fully-validated target that no caller input can influence. The
// vm-up-but-app-down state is the one the dashboard otherwise cannot see.
// Every probe failure degrades to a string, never an error: status is a
// viewer, not a gate.
func probeServitorHealth(ctx context.Context) string {
	bind, err1 := servitorEnvKV("SERVITOR_BIND")
	port, err2 := servitorEnvKV("SERVITOR_PORT")
	if err1 != nil || err2 != nil {
		return "(no probe: .env incomplete)"
	}
	if m := servitorIPRe.FindStringSubmatch(bind); m == nil {
		return "(no probe: SERVITOR_BIND is not a plain IPv4)"
	} else {
		for _, oct := range m[1:5] {
			if n, _ := strconv.Atoi(oct); n > 255 {
				return "(no probe: SERVITOR_BIND is not a plain IPv4)"
			}
		}
	}
	if !servitorPortRe.MatchString(port) {
		return "(no probe: SERVITOR_PORT is not a number)"
	}
	if n, _ := strconv.Atoi(port); n < 1 || n > 65535 {
		return "(no probe: SERVITOR_PORT out of range)"
	}
	out, err := runCmd(ctx, "/usr/bin/curl", "-s", "-m", "5", "http://"+bind+":"+port+"/healthz")
	if err != nil {
		if t := strings.TrimSpace(out); t != "" {
			return "(unreachable: " + t + ")"
		}
		return "(unreachable)"
	}
	return strings.TrimSpace(out)
}

// servitorStatus — VM domain state + the best-effort /healthz probe. No
// args; read-only. Prints a short human-readable summary (the panel's
// Actions view renders the action's stdout).
func servitorStatus(ctx context.Context, args []string, logf func(string, ...any)) error {
	state, err := runCmd(ctx, "/usr/bin/virsh", "domstate", servitorVMName)
	if err != nil {
		return fmt.Errorf("virsh domstate %s: %v: %s", servitorVMName, err, strings.TrimSpace(state))
	}
	state = strings.TrimSpace(state)
	health := probeServitorHealth(ctx)
	logf("servitor status: vm=%s healthz=%s", state, health)
	fmt.Printf("vm: %s\nhealthz: %s\n", state, health)
	return nil
}

// servitorVMRestart — planned reboot. The app survives: session files and
// mounts persist on the state disk; boot reconciliation kills crash-orphans
// and marks interrupted sessions; pickup respawns (the servitor repo's
// design, not portcullis's). virsh reboot (ACPI) is used, never destroy.
func servitorVMRestart(ctx context.Context, args []string, logf func(string, ...any)) error {
	out, err := runCmd(ctx, "/usr/bin/virsh", "reboot", servitorVMName)
	if err != nil {
		return fmt.Errorf("virsh reboot %s: %v: %s", servitorVMName, err, strings.TrimSpace(out))
	}
	logf("servitor VM reboot requested")
	return nil
}

// servitorScriptTail — the shared exec tail for sync/doctor: on success the
// script's stdout IS the result (sync's success marker, the doctor's GREEN
// line) — print its last 4 KB to stdout (the panel surfaces the action's
// stdout) and log the full output to journald; on failure the same output
// rides the error (panel-api surfaces it in the response).
func servitorScriptTail(what, script, out string, err error, logf func(string, ...any)) error {
	out = strings.TrimSpace(out)
	if len(out) > 4096 {
		out = "…" + out[len(out)-4096:]
	}
	if err != nil {
		logf("servitor-%s failed (%v):\n%s", what, err, out)
		return fmt.Errorf("servitor-%s: %v\n%s", what, err, out)
	}
	logf("servitor-%s ok:\n%s", what, out)
	fmt.Println(out)
	return nil
}

// servitorSync — push the host checkout into the VM (config/lib/app + the
// key slice). The script NEVER blocks a run — with a run active
// it stages everything, sets the update-waiting flag, and exits 0; the
// app's tick applies restart+doctor at the first idle moment. It therefore
// can run for a long while (staging wait ≤150 s + service wait + the
// post-deploy doctor's live run — one prompt that searches the web, a
// real model call): 600 s keeps it inside the panel-api's 660 s HTTP
// cap with headroom. On an action timeout the ctx deadline KILLS the
// script at the deadline (runCmd: direct child dies, in-flight scp/ssh
// grandchildren may linger) — safe because the script is idempotent and
// resumable: re-run it (RUNBOOK → Servitor, troubleshooting).
//
// The deploy key is passed explicitly: runCmd's scrubbed env has no HOME,
// so the script's ${HOME:-/root} default is only a backstop — /root/.srv
// is the canonical root-side home (RUNBOOK → Servitor).
func servitorSync(ctx context.Context, args []string, logf func(string, ...any)) error {
	script, err := checkManagedScript(servitorRepoPath, filepath.Join("deploy", "sync.sh"))
	if err != nil {
		return err
	}
	out, err := runCmd(ctx, script, "--deploy-key", servitorDeployKey)
	return servitorScriptTail("sync", script, out, err, logf)
}

// servitorDoctor — the servitor deploy gate (guest certification + the
// host-side firewall probes + one live run that searches the web, which
// certifies the live prompt and the live search in a single model call).
// Read-only with respect to the VM (it never changes state), so no
// confirm — but it spends a live model run (300 s budget), hence its
// own 600 s action budget.
func servitorDoctor(ctx context.Context, args []string, logf func(string, ...any)) error {
	script, err := checkManagedScript(servitorRepoPath, filepath.Join("deploy", "doctor.sh"))
	if err != nil {
		return err
	}
	out, err := runCmd(ctx, script, "--deploy-key", servitorDeployKey)
	return servitorScriptTail("doctor", script, out, err, logf)
}

// registry entries (as they sit at the end of the `registry` map in
// portcullis main.go — wrapped so this file parses as Go; it is never built):
var _ = map[string]Action{
	"servitor-status": {
		Summary: "Servitor VM state + guest /healthz probe (read-only)",
		Args:    nil,
		Timeout: 30 * time.Second,
		Confirm: false,
		Run:     servitorStatus,
	},
	"servitor-vm-restart": {
		Summary: "Reboot the servitor VM (planned; boot reconciliation handles the unclean path)",
		Args:    nil,
		Timeout: 60 * time.Second,
		Confirm: true,
		Run:     servitorVMRestart,
	},
	"servitor-sync": {
		Summary: "Push the host checkout (config/lib/app + key slice) into the servitor VM; never blocks a run",
		Args:    nil,
		// 10 min: the post-deploy doctor now ends with one live run that
		// makes the agent search the web (a real model call; ~1–5 min,
		// 300 s settle cap), so the doctor itself runs at most ~6 min
		// (static checks + the cap) — on top of the npm stages +
		// artifact pushes + the tick wait. Keep this at or below 650 s:
		// panel-api clamps the action's subprocess to timeout_sec + 10,
		// capped at 660 s, so a bigger budget here would be dead.
		Timeout: 600 * time.Second,
		Confirm: true,
		Run:     servitorSync,
	},
	"servitor-doctor": {
		Summary: "Run the servitor deploy doctor (guest certification + firewall probes + live prompt + live search)",
		Args:    nil,
		// 10 min: the live run (one prompt that searches the web; 300 s
		// budget) is a real model call; the static checks are seconds.
		// Same panel-api clamp as servitor-sync (timeout_sec + 10, cap 660 s).
		Timeout: 600 * time.Second,
		Confirm: false,
		Run:     servitorDoctor,
	},
}
