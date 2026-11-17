"""Guest VM harness — boots the built image under QEMU on the
laptop bridge ``servitor-edge`` (10.42.0.0/24), waits for ssh + healthz,
and exposes the gate actions: doctor, sync, update drills, prompt.

Requires root. The VM gets:
  - root disk: the built image qcow2 (manifest "active")
  - state disk: a fresh 16G ext4 qcow2 labeled ``servitor-state``
  - the static guest IP 10.42.0.15 (configured in the image)
  - kernel+initramfs extracted from the image (build.sh ships them
    alongside the qcow2) — the VM has no bootloader on purpose: the
    harness owns the boot line, exactly like production QEMU would.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
IMAGES = REPO / "deploy" / "guest" / "images"
# the key lives in the human's home; tests run as root (sudo make
# test-guest), where Path.home() would point at /root — override with
# SERVITOR_DEPLOY_KEY when running under root.
DEPLOY_KEY = Path(
    os.environ.get("SERVITOR_DEPLOY_KEY")
    or (Path.home() / ".srv" / "deploy_key")
)

GUEST_IP = "10.42.0.15"
HOST_BRIDGE_IP = "10.42.0.1"
BRIDGE = "servitor-edge"
PORT = 8787
STATE_SIZE = "16G"
# the per-VM tap, attached to BRIDGE. We create + attach it ourselves (root)
# and hand it to qemu via `-netdev tap` — this bypasses the setuid
# qemu-bridge-helper, whose ACL rejects our bridge name ("access denied by
# acl file").
TAP = "servitor-tap"


class GuestError(RuntimeError):
    pass


def _ip_cmd(*args: str) -> str:
    return subprocess.run(["ip", *args], capture_output=True, text=True).stdout


def ensure_bridge() -> bool:
    """Create the edge bridge if missing (+ idempotent guest egress NAT).
    Returns True if we created the bridge.

    The NAT (forward + masquerade of the edge subnet through the host) is
    REQUIRED for the live-router gates: the doctor's model assert and the
    live prompt need the guest to reach the real router; without it the
    guest has no internet egress (the bridge is L2 only)."""
    created = BRIDGE not in _ip_cmd("link", "show")
    if created:
        subprocess.run(["ip", "link", "add", BRIDGE, "type", "bridge"], check=True)
    subprocess.run(["ip", "link", "set", BRIDGE, "up"], check=True)
    if HOST_BRIDGE_IP not in _ip_cmd("addr", "show", BRIDGE):
        subprocess.run(["ip", "addr", "add", f"{HOST_BRIDGE_IP}/24", "dev", BRIDGE], check=True)
    try:
        subprocess.run("sysctl -w net.ipv4.ip_forward=1 >/dev/null", shell=True, check=True)
        nft = subprocess.run(["nft", "list", "ruleset"], capture_output=True, text=True)
        if "servitor-edge-nat" not in nft.stdout:
            subprocess.run(["nft", "add", "table", "ip", "servitor_nat"], check=True)
            subprocess.run(["nft", "add", "chain", "ip", "servitor_nat", "postrouting",
                            "{ type nat hook postrouting priority 100; policy accept; }"],
                           check=True)
            subprocess.run(["nft", "add", "rule", "ip", "servitor_nat", "postrouting",
                            f"ip saddr {HOST_BRIDGE_IP}/24 masquerade comment \"servitor-edge-nat\""],
                           check=True)
    except Exception as e:  # NAT is best-effort here; gates fail loudly without it
        print(f"warn: edge NAT setup failed ({e}); live-router gates need guest egress")
    return created


def remove_bridge() -> None:
    subprocess.run(["ip", "addr", "del", f"{HOST_BRIDGE_IP}/24", "dev", BRIDGE],
                   capture_output=True)
    subprocess.run(["ip", "link", "del", BRIDGE], capture_output=True)


def active_image() -> tuple[str, dict]:
    manifest = json.loads((IMAGES / "manifest.json").read_text())
    name = manifest["active"]
    return name, manifest["images"][name]


class GuestVM:
    def __init__(self, root_disk: str | Path, workdir: Path,
                 serial_name: str = "serial.log"):
        self.root_disk = Path(root_disk)
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        # boot a DISPOSABLE COPY of the base image: the provisioning sync
        # writes keys + extension to the root disk, so booting the base
        # directly would pollute the pristine image (and break the next run).
        self._root_copy = self.workdir / "root.qcow2"
        self.state_disk = self.workdir / "state.img"
        self.serial = self.workdir / serial_name
        self.monitor = self.workdir / "monitor.sock"
        self.proc: subprocess.Popen | None = None
        self._qemu: list[str] = []

    # ---- disk + boot -------------------------------------------------
    def make_state_disk(self) -> None:
        # a 16G sparse raw file, mkfs'd DIRECTLY (mkfs.ext4 formats a
        # regular file — no loop device). Keeping this path loop-free avoids
        # the host loop churn that correlates with the intermittent kernel
        # exec-path flakiness (2026-11-16). qemu takes the raw drive directly.
        if not self.state_disk.exists():
            subprocess.run(["qemu-img", "create", "-f", "raw", str(self.state_disk),
                            STATE_SIZE], check=True, capture_output=True)
            subprocess.run(["mkfs.ext4", "-q", "-L", "servitor-state",
                            str(self.state_disk)], check=True, capture_output=True)

    def make_tap(self) -> None:
        # create + attach the tap directly (we are root). Bypasses the setuid
        # qemu-bridge-helper (its ACL denies BRIDGE). Idempotent.
        subprocess.run(["ip", "link", "del", TAP], capture_output=True)
        subprocess.run(["ip", "tuntap", "add", "dev", TAP, "mode", "tap"],
                       check=True, capture_output=True)
        subprocess.run(["ip", "link", "set", TAP, "master", BRIDGE],
                       check=True, capture_output=True)
        subprocess.run(["ip", "link", "set", TAP, "up"], check=True,
                       capture_output=True)

    def remove_tap(self) -> None:
        subprocess.run(["ip", "link", "set", TAP, "down"], capture_output=True)
        subprocess.run(["ip", "link", "del", TAP], capture_output=True)

    def _serial_tail(self, n: int = 2000) -> str:
        # the serial log may not exist if qemu died before opening it; fall
        # back to qemu.stderr so the real boot error is surfaced (not a
        # masking FileNotFoundError)
        try:
            return self.serial.read_text(errors="replace")[-n:]
        except FileNotFoundError:
            qe = self.workdir / "qemu.stderr"
            extra = ("\n--- qemu.stderr ---\n" + qe.read_text(errors="replace")[-n:]) \
                if qe.exists() else ""
            return "(serial log not created)" + extra

    def _kernel_files(self) -> tuple[Path, Path]:
        stem = self.root_disk.name.split(".qcow2")[0]
        k = IMAGES / f"{stem}.vmlinuz"
        i = IMAGES / f"{stem}.initramfs.img"
        if not k.exists() or not i.exists():
            raise GuestError(f"kernel/initramfs not extracted for {self.root_disk.name} "
                             f"(build.sh ships them alongside the image)")
        return k, i

    def start(self) -> None:
        self.make_state_disk()
        self.make_tap()
        # copy the pristine base (qcow2 is ~1.5G; a plain copy is fast and
        # keeps the base unmodified by the provisioning sync)
        if not self._root_copy.exists():
            shutil.copy2(self.root_disk, self._root_copy)
        k, i = self._kernel_files()
        cmd = [
            "qemu-system-x86_64",
            "-machine", "q35,accel=kvm", "-cpu", "host",
            "-smp", "4", "-m", "8G",
            "-drive", f"file={self._root_copy},if=virtio,format=qcow2",
            "-drive", f"file={self.state_disk},if=virtio,format=raw",
            # manual tap (see make_tap): -netdev bridge needs the setuid
            # helper, whose ACL rejects BRIDGE
            "-netdev", f"tap,id=n0,ifname={TAP},script=no,downscript=no",
            "-device", "virtio-net-pci,netdev=n0",
            "-kernel", str(k), "-initrd", str(i),
            "-append", f"root=LABEL=servitor-root console=ttyS0,115200 quiet",
            "-serial", f"file:{self.serial}",
            "-display", "none",
            "-monitor", f"unix:{self.monitor},server,nowait",
        ]
        self._qemu = cmd
        self._qemu_err = open(self.workdir / "qemu.stderr", "w")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                     stderr=self._qemu_err)

    def _mon(self, *cmd: str) -> str:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(str(self.monitor))
        s.settimeout(5)
        s.sendall((" ".join(cmd) + "\n").encode())
        out = b""
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                out += chunk
                if b"OK" in out or b"QEMU" in out:
                    break
        except socket.timeout:
            pass
        s.close()
        return out.decode(errors="replace")

    def stop(self, timeout: int = 60) -> None:
        if self.proc is None or self.proc.poll() is not None:
            self.remove_tap()
            return
        try:
            self._mon("system_powerdown")
            self.proc.wait(timeout=timeout)
        except Exception:
            self.proc.kill()
            try:
                self.proc.wait(timeout=10)
            except Exception:
                pass
        self.remove_tap()

    # ---- guest access --------------------------------------------------
    def wait_ssh(self, timeout: int = 180) -> None:
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                raise GuestError(f"VM died during boot (rc={self.proc.returncode}); "
                                 f"serial tail: {self._serial_tail()}")
            try:
                with socket.create_connection((GUEST_IP, 22), timeout=2):
                    return
            except OSError as e:
                last = str(e)
            time.sleep(2)
        raise GuestError(f"ssh not reachable on {GUEST_IP} within {timeout}s ({last}); "
                         f"serial tail: {self._serial_tail()}")

    def run(self, cmd: str, timeout: int = 120, check: bool = True) -> str:
        ssh = ["ssh", "-i", str(DEPLOY_KEY), "-o", "BatchMode=yes",
               "-o", "StrictHostKeyChecking=accept-new",
               "-o", "ConnectTimeout=10", f"root@{GUEST_IP}", cmd]
        p = subprocess.run(ssh, capture_output=True, text=True, timeout=timeout)
        if check and p.returncode != 0:
            raise GuestError(f"guest cmd failed (rc={p.returncode}): {cmd}\n"
                             f"stdout: {p.stdout[-2000:]}\nstderr: {p.stderr[-2000:]}")
        return p.stdout

    def wait_healthz(self, timeout: int = 120) -> None:
        deadline = time.time() + timeout
        url = f"http://{GUEST_IP}:{PORT}/healthz"
        last = ""
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=3) as r:
                    if r.status == 200:
                        return
            except Exception as e:
                last = str(e)
            time.sleep(2)
        raise GuestError(f"healthz not up within {timeout}s ({last}); guest journal: "
                         + self.run("journalctl -u servitor -n 30 --no-pager", check=False))

    def service_uptime(self) -> float:
        # ActiveEnterTimestampMonotonic is the CLOCK_MONOTONIC value AT the
        # moment the unit entered active state — i.e. VM-seconds-since-boot
        # of the last activation, NOT the time the service has been up.
        # Subtract it from the current monotonic time (/proc/uptime).
        out = self.run("systemctl show servitor -p ActiveEnterTimestampMonotonic")
        entered = int(out.split("=")[1]) / 1e6
        now = float(self.run("cat /proc/uptime").split()[0])
        return max(0.0, now - entered)

    # ---- app API (host side, over the edge) -----------------------------
    def api(self, method: str, path: str, body=None, timeout: int = 60):
        url = f"http://{GUEST_IP}:{PORT}{path}"
        # body-less POSTs send {} (not an absent body): the routes declare a
        # required Pydantic body model (fields may all be optional), and an
        # empty body with Content-Type: application/json is a 422
        if body is None and method in ("POST", "PUT", "PATCH"):
            body = {}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})

    def create_session(self):
        _, s = self.api("POST", "/api/v1/sessions")
        return s

    def prompt(self, sid: str, text: str):
        return self.api("POST", f"/api/v1/sessions/{sid}/prompt", {"text": text})

    def settle(self, sid: str, timeout: int = 300) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, p = self.api("GET", f"/api/v1/sessions/{sid}/probe")
            if not p.get("busy"):
                return p
            time.sleep(2)
        raise GuestError(f"session {sid} did not settle within {timeout}s")

    def tail_transcript(self, sid: str) -> list[dict]:
        _, m = self.api("GET", f"/api/v1/sessions/{sid}/messages")
        return m["messages"]

    def release(self, sid: str):
        self.api("POST", f"/api/v1/sessions/{sid}/release")

    # ---- deploy pipeline (host side) ------------------------------------
    def doctor(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(REPO / "deploy" / "doctor.sh"),
             "--env", str(REPO / ".env"), "--ssh", f"root@{GUEST_IP}"],
            capture_output=True, text=True, timeout=900)

    def sync(self, env_file: str | Path = REPO / ".env", **extra_env: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        for k, v in extra_env.items():
            env[k] = v
        return subprocess.run(
            ["bash", str(REPO / "deploy" / "sync.sh"),
             "--env", str(env_file), "--ssh", f"root@{GUEST_IP}"],
            capture_output=True, text=True, timeout=1800, env=env)

    def update(self, env_file: str | Path = REPO / ".env", **extra_env: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        for k, v in extra_env.items():
            env[k] = v
        return subprocess.run(
            ["bash", str(REPO / "deploy" / "update.sh"),
             "--env", str(env_file), "--ssh", f"root@{GUEST_IP}"],
            capture_output=True, text=True, timeout=3600, env=env)
