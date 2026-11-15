// Portcullis panel-do actions for servitor (DECISIONS A6).
//
// Operator-merged source: new entries in portcullis's Go ACTIONS registry,
// per the portcullis RUNBOOK "Add a privileged action". Thin wrappers over
// virsh + the servitor host-side scripts in this repo (the host keeps a
// checkout at SERVISOR_REPO_PATH, set when the actions are added).
//
//   servitor-status    — VM domain state + guest /healthz (via host ssh)
//   servitor-vm-restart— virsh reboot (planned; the app's boot
//                        reconciliation handles the unclean path, PLAN §5)
//   servitor-sync      — deploy/sync.sh (config/lib push, key slice,
//                        update-waiting flag when a run is active, exit 0
//                        always — D8)
//   servitor-doctor    — deploy/doctor.sh -> guest `servitorctl doctor`
package actions

import (
	"bufio"
	"fmt"
	"os"
	"os/exec"
	"strings"
)

// servitor repo checkout on the host (set at integration time).
const servitorRepoPath = "/opt/servitor-repo"

// VM domain name (assigned at provisioning, PLAN §6/A6).
const servitorVM = "servitor"

func runAction(name, args ...string) (string, error) {
	out, err := exec.Command(name, args...).CombinedOutput()
	if err != nil {
		return fmt.Sprintf("%s failed: %v\n%s", name, err, out), err
	}
	return string(out), nil
}

// loadDotEnv reads the host checkout's .env (the same contract file
// deploy/sync.sh consumes). Thin parser: KEY=VALUE lines, '#' comments.
func loadDotEnv(path string) (map[string]string, error) {
	out := map[string]string{}
	f, err := os.Open(path)
	if err != nil {
		return out, err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if i := strings.IndexByte(line, '='); i > 0 {
			out[line[:i]] = strings.TrimSpace(line[i+1:])
		}
	}
	return out, sc.Err()
}

func ServitorStatus() (string, error) {
	state, err := runAction("virsh", "domstate", servitorVM)
	if err != nil {
		return state, err
	}
	// best-effort liveness probe from the host through the VM ingress
	// address — the same env contract the sync pipeline uses; the app
	// authenticates nobody, the probe is edge-gated like everything else.
	env, envErr := loadDotEnv(servitorRepoPath + "/.env")
	health := "(env file missing)"
	if envErr == nil && env["SERVITOR_BIND"] != "" && env["SERVITOR_PORT"] != "" {
		url := fmt.Sprintf("http://%s:%s/healthz", env["SERVITOR_BIND"], env["SERVITOR_PORT"])
		health, _ = runAction("curl", "-s", "-m", "5", url)
	}
	return fmt.Sprintf("vm: %s\nhealthz: %s", state, health), nil
}

func ServitorVMRestart() (string, error) {
	// Planned downtime. The app survives: session files and mounts persist;
	// boot reconciliation kills crash-orphans and marks sessions
	// interrupted; pickup respawns (PLAN §5).
	return runAction("virsh", "reboot", servitorVM)
}

func ServitorSync() (string, error) {
	// Never blocks, never errors while a run is active (D8): stages
	// everything non-disruptive, sets the update-waiting flag, exits 0.
	return runAction("bash", servitorRepoPath+"/deploy/sync.sh")
}

func ServitorDoctor() (string, error) {
	return runAction("bash", servitorRepoPath+"/deploy/doctor.sh")
}
