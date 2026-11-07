"""Windows QoS control through the built-in NetQos PowerShell cmdlets.

* Policies live in the *ActiveStore* (memory only): they vanish on reboot and are never
  written into the persistent configuration.  Every policy is named SPS_*; restore_all()
  removes exactly those and nothing else.
* Upload shaping : TCP-only throttle policy (Windows QoS can only shape OUTBOUND traffic).
* Game marking   : DSCP 46 (EF) on VALORANT executables.
* Download       : no ingress shaping exists in Windows.  Optional receive-window clamp via
                   `netsh int tcp set global autotuninglevel` (original value saved + restored).
"""
import logging, re, subprocess
from .common import IS_WIN, STATE_FILE, load_json, save_json

log = logging.getLogger("qos")
GAME_EXES = ["VALORANT-Win64-Shipping.exe", "VALORANT.exe"]
NOWIN = 0x08000000 if IS_WIN else 0


def default_runner(cmd, timeout=40):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=NOWIN)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


PS_PRELUDE = (
    "function G($pat,$store='ActiveStore'){ try { @(Get-NetQosPolicy -PolicyStore $store -ErrorAction Stop | "
    "Where-Object { $_.Name -like $pat }) } catch { @() } }; "
)

def ps_cmd(script):
    # no 'Stop' globally: missing policies are normal. Real failures are caught and their message is printed to the log.
    body = "try { " + PS_PRELUDE + script + " } catch { Write-Output ('ERR: ' + $_.Exception.Message); exit 1 }"
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", body]


class Qos:
    def __init__(self, runner=None):
        self.run = runner or default_runner

    def _ps(self, script):
        rc, out = self.run(ps_cmd(script))
        if rc != 0: log.error("PowerShell failed rc=%s: %s", rc, out.strip()[:500] or "(no output)")
        return rc == 0

    def set_upload_limit(self, mbps):
        bps = max(100_000, int(mbps * 1_000_000))
        ok = self._ps(
            f"if ((G 'SPS_Throttle').Count -gt 0) {{ Set-NetQosPolicy -PolicyStore ActiveStore -Name 'SPS_Throttle' "
            f"-ThrottleRateActionBitsPerSecond {bps} -ErrorAction Stop | Out-Null }} "
            f"else {{ New-NetQosPolicy -PolicyStore ActiveStore -Name 'SPS_Throttle' -NetworkProfile All "
            f"-IPProtocolMatchCondition TCP -ThrottleRateActionBitsPerSecond {bps} -ErrorAction Stop | Out-Null }}")
        if ok: log.info("Upload TCP throttle set to %.2f Mbps", bps / 1e6)
        return ok

    def clear_throttle(self):
        return self._ps("foreach($p in (G 'SPS_Throttle')){ Remove-NetQosPolicy -PolicyStore ActiveStore -Name 'SPS_Throttle' "
                        "-Confirm:$false -ErrorAction Stop }")

    def set_game_marking(self, enable):
        if not enable:
            return self._ps("foreach($p in (G 'SPS_Game*')){ Remove-NetQosPolicy -PolicyStore ActiveStore -Name $p.Name "
                            "-Confirm:$false -ErrorAction Stop }")
        parts = []
        for i, exe in enumerate(GAME_EXES):
            n = f"SPS_Game{i}"
            parts.append(f"if ((G '{n}').Count -eq 0) {{ New-NetQosPolicy -PolicyStore ActiveStore -Name '{n}' -NetworkProfile All "
                         f"-AppPathNameMatchCondition '{exe}' -DSCPAction 46 -ErrorAction Stop | Out-Null }}")
        return self._ps("; ".join(parts))

    # ---- optional download-side clamp --------------------------------------
    def _get_autotuning(self):
        rc, out = self.run(["netsh", "int", "tcp", "show", "global"])
        m = re.search(r"Auto-Tuning Level\s*:\s*(\w+)", out or "", re.I)
        return m.group(1).lower() if m else None

    def set_download_clamp(self, enable):
        st = load_json(STATE_FILE, {})
        if enable:
            cur = self._get_autotuning()
            if cur and cur != "restricted" and "orig_autotuning" not in st:
                st["orig_autotuning"] = cur; save_json(STATE_FILE, st)
            rc, _ = self.run(["netsh", "int", "tcp", "set", "global", "autotuninglevel=restricted"])
            return rc == 0
        return self.restore_autotuning()

    def restore_autotuning(self):
        st = load_json(STATE_FILE, {}); orig = st.pop("orig_autotuning", None)
        if not orig: return True
        rc, _ = self.run(["netsh", "int", "tcp", "set", "global", f"autotuninglevel={orig}"])
        if rc == 0: save_json(STATE_FILE, st)
        return rc == 0

    def restore_all(self):
        """Emergency / shutdown path: remove every SPS policy, restore TCP setting."""
        a = self._ps("foreach($st in 'ActiveStore','localhost'){ foreach($p in (G 'SPS_*' $st)){ "
                     "Remove-NetQosPolicy -PolicyStore $st -Name $p.Name -Confirm:$false -ErrorAction SilentlyContinue } }")
        b = self.restore_autotuning()
        log.info("restore_all: policies=%s autotuning=%s", a, b)
        return a and b
