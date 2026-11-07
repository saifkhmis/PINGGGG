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

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")

def _headers(extra=None):
    h = {"User-Agent": BROWSER_UA, "Accept": "*/*", "Origin": "https://speed.cloudflare.com",
         "Referer": "https://speed.cloudflare.com/"}
    if extra: h.update(extra)
    return h

def _conn(url, timeout=10):
    u = urllib.parse.urlparse(url)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    return cls(u.hostname, u.port, timeout=timeout), (u.path or "/") + ("?" + u.query if u.query else "")

def _run_streams(worker, n):
    errs = []; ths = [threading.Thread(target=worker, args=(i, errs), daemon=True) for i in range(n)]
    [t.start() for t in ths]; [t.join(40) for t in ths]; return errs

def _rate_from_events(events, start, ramp, errs, what):
    """events = [(completion_time, bytes)]. Skips slow-start ramp; measures between completions."""
    if not events: raise TestError(f"{what} failed: {errs[:1]}")
    events.sort(); late = [e for e in events if e[0] - start >= ramp]
    if len(late) >= 2:
        return sum(b for _, b in late[1:]) * 8 / 1e6 / max(0.05, late[-1][0] - late[0][0])
    return sum(b for _, b in events) * 8 / 1e6 / max(0.2, events[-1][0] - start)

def download_test(base, seconds=6.0, ramp=1.5, streams=3, req_bytes=10_000_000):
    """Cloudflare-style: many 10 MB requests per stream (big single requests get HTTP 403)."""
    url = base.rstrip("/") + f"/__down?bytes={req_bytes}"
    start = time.monotonic(); events = []; lock = threading.Lock()
    def w(i, errs):
        try:
            c, path = _conn(url)
            while time.monotonic() < start + seconds:
                c.request("GET", path, headers=_headers()); r = c.getresponse()
                if r.status != 200: raise TestError(f"HTTP {r.status}")
                while time.monotonic() < start + seconds:
                    b = r.read(65536)
                    if not b: break
                    with lock: events.append((time.monotonic(), len(b)))
                else: break
            c.close()
        except Exception as e: errs.append(e)
    errs = _run_streams(w, streams)
    return _rate_from_events(events, start, ramp, errs, "download test")

def download_file_test(url, seconds=6.0, ramp=1.5, streams=3):
    """Fallback for plain static files (one long GET per stream)."""
    start = time.monotonic(); events = []; lock = threading.Lock()
    def w(i, errs):
        try:
            c, path = _conn(url); c.request("GET", path, headers=_headers()); r = c.getresponse()
            if r.status != 200: raise TestError(f"HTTP {r.status}")
            while time.monotonic() < start + seconds:
                b = r.read(65536)
                if not b: break
                with lock: events.append((time.monotonic(), len(b)))
            c.close()
        except Exception as e: errs.append(e)
    errs = _run_streams(w, streams)
    return _rate_from_events(events, start, ramp, errs, "download test")

def upload_test(url, seconds=6.0, ramp=1.5, streams=3, body_bytes=512 * 1024):
    """POST many 512 KB bodies; bytes are counted only when the server has acknowledged them."""
    start = time.monotonic(); events = []; lock = threading.Lock(); data = os.urandom(body_bytes)
    def w(i, errs):
        try:
            c, path = _conn(url)
            while time.monotonic() < start + seconds:
                c.request("POST", path, body=data, headers=_headers({"Content-Type": "application/octet-stream"}))
                r = c.getresponse(); r.read()
                if r.status >= 400: raise TestError(f"HTTP {r.status}")
                with lock: events.append((time.monotonic(), body_bytes))
            c.close()
        except Exception as e: errs.append(e)
    errs = _run_streams(w, streams)
    return _rate_from_events(events, start, ramp, errs, "upload test")

# (download, upload) sources tried in order until one works
DOWNLOAD_FALLBACKS = ["https://proof.ovh.net/files/100Mb.dat", "http://speedtest.tele2.net/100MB.zip"]
UPLOAD_FALLBACKS = ["http://speedtest.tele2.net/upload.php"]

def smart_download(base):
    errs = []
    try: return download_test(base), base
    except Exception as e: errs.append(f"{base}: {e}")
    for u in DOWNLOAD_FALLBACKS:
        try: return download_file_test(u), u
        except Exception as e: errs.append(f"{u}: {e}")
    raise TestError("; ".join(errs)[:300])

def smart_upload(base):
    errs = []
    for u in [base.rstrip("/") + "/__up"] + UPLOAD_FALLBACKS:
        try: return upload_test(u), u
        except Exception as e: errs.append(f"{u}: {e}")
    raise TestError("; ".join(errs)[:300])
