import http.server, os, socket, sys, threading, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["LOCALAPPDATA"] = "/tmp/sps_test"
from sps import measure, qos as qosmod, engine as eng

class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_GET(self):
        n = int(self.path.split("bytes=")[1]) if "bytes=" in self.path else 100_000_000
        if n > 50_000_000: self.send_response(403); self.send_header("Content-Length", "0"); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Length", str(n)); self.end_headers()
        try: self.wfile.write(b"x" * n)
        except Exception: pass
    def do_POST(self):
        n = int(self.headers["Content-Length"]); self.rfile.read(n)
        self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()

class TestMeasure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
    @classmethod
    def tearDownClass(cls): cls.srv.shutdown()

    def test_download_upload(self):
        d = measure.download_test(self.base, seconds=2.5, ramp=0.5); u = measure.upload_test(self.base + "/__up", seconds=2.5, ramp=0.5)
        print(f"loopback dl={d:.0f} Mbps ul={u:.0f} Mbps"); self.assertGreater(d, 50); self.assertGreater(u, 50)

    def test_test_error(self):
        with self.assertRaises(measure.TestError): measure.download_test("http://127.0.0.1:1", seconds=1)

    def test_403_big_request_then_fallback(self):   # reproduces the real-world failure: server refuses huge requests
        with self.assertRaises(measure.TestError): measure.download_test(self.base, req_bytes=200_000_000, seconds=1)
        self.assertGreater(measure.download_test(self.base, seconds=1.5, ramp=0.3), 10)
        old = measure.DOWNLOAD_FALLBACKS; measure.DOWNLOAD_FALLBACKS = [self.base + "/file?bytes=5000000"]
        try:
            mbps, src = measure.smart_download("http://127.0.0.1:1"); self.assertIn("/file", src); self.assertGreater(mbps, 10)
        finally: measure.DOWNLOAD_FALLBACKS = old

    def test_ping_monitor_stats(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(50); port = s.getsockname()[1]
        pm = measure.PingMonitor(targets=[("127.0.0.1", port)], interval=0.05, probe=measure.tcp_probe); pm.start(); time.sleep(2.2)
        st = pm.stats(); pm.stop(); self.assertIsNotNone(st["ping"]); self.assertEqual(st["loss"], 0); self.assertLess(st["jitter"], 5)
        s.close()
        pm2 = measure.PingMonitor(targets=[("127.0.0.1", port)], interval=0.05, probe=lambda t: measure.tcp_probe(t, 0.2)); pm2.start(); time.sleep(1.2)
        self.assertGreater(pm2.stats()["loss"], 90); pm2.stop()

    def test_counters(self):
        c = measure.NetCounters(); c.rates(); time.sleep(0.3); d, u = c.rates(); self.assertGreaterEqual(d, 0)

class FakeRunner:
    def __init__(self): self.calls = []
    def __call__(self, cmd, timeout=40):
        self.calls.append(cmd)
        if cmd[0] == "netsh" and "show" in cmd: return 0, "Receive Window Auto-Tuning Level : normal"
        return 0, ""

class TestQosEngine(unittest.TestCase):
    def test_qos_commands(self):
        r = FakeRunner(); q = qosmod.Qos(r)
        self.assertTrue(q.set_upload_limit(17.2)); s = r.calls[-1][-1]
        self.assertIn("17200000", s); self.assertIn("ActiveStore", s); self.assertIn("-IPProtocolMatchCondition TCP", s)
        q.set_game_marking(True); s = r.calls[-1][-1]; self.assertIn("VALORANT-Win64-Shipping.exe", s); self.assertIn("-DSCPAction 46", s)
        q.set_download_clamp(True); self.assertIn(["netsh", "int", "tcp", "set", "global", "autotuninglevel=restricted"], r.calls)
        q.restore_all(); self.assertIn(["netsh", "int", "tcp", "set", "global", "autotuninglevel=normal"], r.calls)
        self.assertIn("SPS_*", r.calls[-2][-1] if "SPS_" in r.calls[-2][-1] else r.calls[-3][-1])

    def test_engine_end_to_end_with_simulated_link(self):
        import tempfile
        r = FakeRunner(); q = qosmod.Qos(r)
        caps = iter([(19.0, 7.0), (18.5, 6.8), (30.0, 7.1), (19.2, 6.9), (19.5, 7.0)])   # spike in the middle
        cur = {}
        def dl(): cur["v"] = next(caps); return cur["v"][0]
        def ul(): return cur["v"][1]
        class P:
            def start(s): pass
            def stop(s): pass
            def stats(s): return dict(ping=55.0, jitter=3.0, loss=0.0, baseline=50.0)
        class C:
            def rates(s): return (1.0, 1.0)
        e = eng.Engine(qos=q, ping=P(), counters=C(), dl_test=dl, ul_test=ul, admin=True)
        e.cfg.update(running=True, auto_mode=True, valorant_mode=True, dl_clamp=False, test_interval_s=0)
        for i in range(5): e._run_test(time.monotonic()); e._next_test = 0
        lims = [round(e.dl.limit, 1), round(e.ul.limit, 1)]; print("engine limits after spike trace:", lims)
        self.assertTrue(16 <= e.dl.limit <= 18.5); self.assertTrue(5.5 <= e.ul.limit <= 6.5)
        cleared = [c for c in r.calls if c[0].startswith("powershell") and "Remove-NetQosPolicy" in c[-1] and "'SPS_Throttle'" in c[-1]]
        sets = [c for c in r.calls if c[0].startswith("powershell") and "ThrottleRateActionBitsPerSecond" in c[-1]]
        self.assertEqual(len(cleared), 5)   # throttle removed before every one of the 5 tests (else we'd measure our own limit)
        self.assertEqual(len(sets), 5)      # and re-applied after every test
        e.emergency_stop(); self.assertFalse(e.cfg["running"]); self.assertIsNone(e.applied_ul)

    def test_non_admin_never_touches_qos(self):
        r = FakeRunner(); q = qosmod.Qos(r)
        class P:
            def start(s): pass
            def stop(s): pass
            def stats(s): return dict(ping=50.0, jitter=1.0, loss=0.0, baseline=48.0)
        class C:
            def rates(s): return (0.0, 0.0)
        e = eng.Engine(qos=q, ping=P(), counters=C(), admin=False); e.cfg["running"] = True
        e._tick(0, time.monotonic()); self.assertEqual(r.calls, []); self.assertIn("Administrator", e.snapshot()["status"])

if __name__ == "__main__": unittest.main(verbosity=2)
