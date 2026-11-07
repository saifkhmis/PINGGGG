import http.client, logging, os, socket, statistics, struct, threading, time, urllib.parse
from collections import deque
import psutil
from .algo import percentile
from .common import IS_WIN

log = logging.getLogger("measure")
UA = "SmartPingStabilizer/1.0"


# ------------------------------------------------------------------ latency
class IcmpProbe:
    """Real ICMP echo through Windows iphlpapi (no subprocess, no window flashing).
    ICMP is deliberately used: our QoS throttle matches TCP only, so ICMP/UDP show what
    a game packet experiences, not the queue of the throttled bulk TCP flows."""
    def __init__(self):
        import ctypes
        self.ct = ctypes
        self.lib = ctypes.WinDLL("iphlpapi.dll")
        self.lib.IcmpCreateFile.restype = ctypes.c_void_p
        self.lib.IcmpSendEcho.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_ushort,
                                          ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32]
        self.h = self.lib.IcmpCreateFile()

    def __call__(self, ip, timeout_ms=1000):
        addr = struct.unpack("<L", socket.inet_aton(ip))[0]
        buf = self.ct.create_string_buffer(256)
        n = self.lib.IcmpSendEcho(self.h, addr, b"SPS-ping-payload!", 17, None, buf, 256, timeout_ms)
        if n == 0: return None
        _, status, rtt = struct.unpack_from("<LLL", buf.raw, 0)
        return float(rtt) if status == 0 else None


def tcp_probe(target, timeout=1.0):          # fallback for non-Windows / tests
    host, port = target if isinstance(target, tuple) else (target, 443)
    t = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout): pass
        return (time.perf_counter() - t) * 1000
    except OSError:
        return None


class PingMonitor(threading.Thread):
    def __init__(self, targets=("1.1.1.1", "8.8.8.8", "9.9.9.9"), interval=1.0, probe=None):
        super().__init__(daemon=True, name="ping")
        self.targets, self.interval = list(targets), interval
        if probe is None:
            try: ic = IcmpProbe(); probe = lambda t: ic(t if isinstance(t, str) else t[0])
            except Exception as e:
                log.warning("ICMP unavailable (%s); using TCP probe", e); probe = tcp_probe
        self.probe = probe
        self.samples = deque(maxlen=600)     # ms or None (lost)
        self.lock = threading.Lock(); self.stop_evt = threading.Event()
        self._idx = 0; self._fails = 0

    def run(self):
        while not self.stop_evt.is_set():
            t0 = time.monotonic()
            try: rtt = self.probe(self.targets[self._idx])
            except Exception: rtt = None
            if rtt is None:
                self._fails += 1
                if self._fails >= 3: self._idx = (self._idx + 1) % len(self.targets); self._fails = 0
            else: self._fails = 0
            with self.lock: self.samples.append(rtt)
            self.stop_evt.wait(max(0.0, self.interval - (time.monotonic() - t0)))

    def stats(self):
        with self.lock: data = list(self.samples)
        ok = [x for x in data if x is not None]
        last60 = data[-60:]
        loss = 100.0 * sum(x is None for x in last60) / len(last60) if last60 else 0.0
        rec = [x for x in data[-30:] if x is not None]
        ping = statistics.median(rec[-5:]) if rec else None
        jitter = statistics.mean(abs(a - b) for a, b in zip(rec, rec[1:])) if len(rec) > 1 else 0.0
        base = percentile(ok, 10) if len(ok) >= 20 else None
        return {"ping": ping, "jitter": jitter, "loss": loss, "baseline": base}

    def stop(self): self.stop_evt.set()


# --------------------------------------------------------------- throughput
class NetCounters:
    """Current rate from OS adapter counters (physical/up adapters only)."""
    BAD = ("loopback", "vethernet", "vmware", "virtualbox", "isatap", "teredo", "bluetooth", "wsl", "docker", "pseudo")
    def __init__(self): self.last = None
    def _totals(self):
        stats = psutil.net_if_stats(); r = s = 0
        for n, c in psutil.net_io_counters(pernic=True).items():
            if n == "lo" or any(b in n.lower() for b in self.BAD) or not stats.get(n, None) or not stats[n].isup: continue
            r += c.bytes_recv; s += c.bytes_sent
        return r, s
    def rates(self):
        now = time.monotonic(); r, s = self._totals(); out = (0.0, 0.0)
        if self.last:
            dt = max(1e-3, now - self.last[0])
            out = (max(0, r - self.last[1]) * 8 / dt / 1e6, max(0, s - self.last[2]) * 8 / dt / 1e6)
        self.last = (now, r, s); return out


class TestError(Exception): pass

def _conn(url, timeout=8):
    u = urllib.parse.urlparse(url)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    return cls(u.hostname, u.port, timeout=timeout), (u.path or "/") + ("?" + u.query if u.query else "")

def _run_streams(worker, n):
    errs = []; ths = [threading.Thread(target=worker, args=(i, errs), daemon=True) for i in range(n)]
    [t.start() for t in ths]; [t.join(30) for t in ths]; return errs

def download_test(base, seconds=5.0, ramp=1.0, streams=3):
    """Mbps over `seconds` using parallel streams; first `ramp` seconds (TCP slow start) excluded."""
    url = base.rstrip("/") + "/__down?bytes=200000000"
    start = time.monotonic(); counted = [0] * streams; allb = [0] * streams; last = [start] * streams
    def w(i, errs):
        try:
            c, path = _conn(url); c.request("GET", path, headers={"User-Agent": UA}); r = c.getresponse()
            if r.status != 200: raise TestError(f"HTTP {r.status}")
            while time.monotonic() < start + seconds:
                b = r.read(65536)
                if not b: break
                now = time.monotonic(); last[i] = now; allb[i] += len(b)
                if now - start >= ramp: counted[i] += len(b)
            c.close()
        except Exception as e: errs.append(e)
    errs = _run_streams(w, streams)
    tot = sum(counted)
    if tot == 0 and sum(allb) > 0:       # very fast link: transfer ended inside the ramp window
        return sum(allb) * 8 / 1e6 / max(0.2, max(last) - start)
    if tot == 0: raise TestError(f"download test failed: {errs[:1]}")
    return tot * 8 / 1e6 / max(0.5, max(last) - start - ramp)

def upload_test(base, seconds=4.0, ramp=1.0, streams=2, chunk=32768):
    url = base.rstrip("/") + "/__up"
    start = time.monotonic(); counted = [0] * streams; allb = [0] * streams; last = [start] * streams; data = os.urandom(chunk)
    def w(i, errs):
        try:
            c, path = _conn(url); c.putrequest("POST", path); c.putheader("User-Agent", UA)
            c.putheader("Content-Type", "application/octet-stream"); c.putheader("Content-Length", "500000000"); c.endheaders()
            while time.monotonic() < start + seconds:
                c.send(data); now = time.monotonic(); last[i] = now; allb[i] += chunk
                if now - start >= ramp: counted[i] += chunk
            c.close()
        except Exception as e: errs.append(e)
    errs = _run_streams(w, streams)
    tot = sum(counted)
    if tot == 0 and sum(allb) > 0:
        return sum(allb) * 8 / 1e6 / max(0.2, max(last) - start)
    if tot == 0: raise TestError(f"upload test failed: {errs[:1]}")
    return tot * 8 / 1e6 / max(0.5, max(last) - start - ramp)
