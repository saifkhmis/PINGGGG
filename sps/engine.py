import logging, threading, time
import psutil
from .algo import AdaptiveLimiter, stability_score
from .common import CFG_FILE, DEFAULTS, IS_WIN, is_admin, load_json, save_json
from . import measure
from .qos import GAME_EXES, Qos

log = logging.getLogger("engine")


def game_running():
    names = {n.lower() for n in GAME_EXES}
    try:
        return any((p.info["name"] or "").lower() in names for p in psutil.process_iter(["name"]))
    except Exception:
        return False


class Engine:
    def __init__(self, qos=None, ping=None, counters=None, dl_test=None, ul_test=None, admin=None):
        self.cfg = load_json(CFG_FILE, DEFAULTS)
        self.qos = qos or Qos()
        self.ping = ping or measure.PingMonitor()
        self.counters = counters or measure.NetCounters()
        self.dl_test = dl_test or (lambda: self._src("dl", *measure.smart_download(self.cfg["speedtest_base"])))
        self.ul_test = ul_test or (lambda: self._src("ul", *measure.smart_upload(self.cfg["speedtest_base"])))
        self._err = None
        self.admin = is_admin() if admin is None else admin
        self.dl = AdaptiveLimiter(safety=0.92)
        self.ul = AdaptiveLimiter(safety=0.90)
        self.lock = threading.Lock()
        self.snap = dict(status="Starting", cur_dl=0.0, cur_ul=0.0, dl_limit=None, ul_limit=None,
                         ping=None, jitter=0.0, loss=0.0, stability=0, game=False, last_test=None, admin=self.admin)
        self.applied_ul = None; self._next_test = 0.0; self._game_prev = False
        self._quit = threading.Event(); self._thread = None; self._clamp_applied = False; self._mark_applied = False

    def _src(self, kind, mbps, url):
        log.info("%s test used %s -> %.2f Mbps", kind, url, mbps); return mbps

    # ------------------------------------------------------------ lifecycle
    def start(self):
        log.info("Engine start (admin=%s)", self.admin)
        if self.admin: self.qos.restore_all()          # clear leftovers from a crash
        self.ping.start()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="engine"); self._thread.start()

    def shutdown(self):
        self._quit.set()
        if self._thread: self._thread.join(5)
        self.ping.stop()
        if self.admin: self.qos.restore_all()
        log.info("Engine shutdown, networking restored")

    def set_option(self, key, value):
        self.cfg[key] = value; save_json(CFG_FILE, self.cfg)
        if key in ("running", "auto_mode", "valorant_mode", "dl_clamp"): self._next_test = min(self._next_test, time.monotonic())

    def emergency_stop(self):
        log.warning("EMERGENCY STOP requested")
        self.cfg["running"] = False; save_json(CFG_FILE, self.cfg)
        ok = self.qos.restore_all() if self.admin else False
        self.applied_ul = None; self._clamp_applied = False; self._mark_applied = False
        self._set(status="Emergency stop - networking restored" if ok else "Emergency stop - run as Administrator to restore")
        return ok

    def snapshot(self):
        with self.lock: return dict(self.snap)

    def _set(self, **kw):
        with self.lock: self.snap.update(kw)

    # ----------------------------------------------------------------- loop
    @property
    def managed(self): return bool(self.cfg["running"] and self.cfg["auto_mode"] and self.admin)

    def _loop(self):
        tick = 0
        while not self._quit.is_set():
            t0 = time.monotonic()
            try: self._tick(tick, t0)
            except Exception: log.exception("tick failed")
            tick += 1; self._quit.wait(max(0.0, 1.0 - (time.monotonic() - t0)))

    def _tick(self, tick, now):
        dl, ul = self.counters.rates()
        p = self.ping.stats()
        if tick % 5 == 0:
            g = game_running()
            if self._game_prev and not g: self._next_test = now + 30   # game just closed: re-measure soon
            self._game_prev = g
        game = self._game_prev
        stab = stability_score(p["ping"], p["jitter"], p["loss"], p["baseline"])
        self._set(cur_dl=dl, cur_ul=ul, ping=p["ping"], jitter=p["jitter"], loss=p["loss"], stability=stab, game=game)

        if not self.cfg["running"]: self._release("Stopped"); return
        if not self.admin: self._set(status="Monitoring only - restart as Administrator"); return
        if not self.cfg["auto_mode"]: self._release("Automatic mode OFF (monitoring)"); return

        self._ensure_extras(game)
        skip_test = game and self.cfg["valorant_mode"]
        if now >= self._next_test and not skip_test:
            self._run_test(now)
        else:
            self._set(status="Gaming - limits held, no speed tests" if skip_test else
                      (f"Speed test failed ({self._err}) - retrying" if self._err and self.ul.limit is None else "Active - stabilizing"))
            if tick % 5 == 0 and p["baseline"] and p["ping"] and self.ul.limit:
                util = ul / max(self.ul.limit, 0.1)
                bloat = p["ping"] - p["baseline"]
                if p["loss"] > 3: bloat = max(bloat, 60)
                if util > 0.5 or bloat > self.ul.bloat_hi:       # only trust it when we are loaded / clearly bad
                    old = self.ul.penalty; self.ul.feedback(bloat)
                    if self.ul.penalty != old: log.info("latency feedback: bloat=%.0fms penalty %.2f->%.2f", bloat, old, self.ul.penalty)
        self._apply()

    def _release(self, status):
        if self.applied_ul is not None or self._clamp_applied or self._mark_applied:
            if self.admin: self.qos.restore_all()
            self.applied_ul = None; self._clamp_applied = False; self._mark_applied = False
        self._set(status=status)

    def _ensure_extras(self, game):
        want_mark = bool(self.cfg["valorant_mode"])
        if want_mark != self._mark_applied:
            if self.qos.set_game_marking(want_mark): self._mark_applied = want_mark
        want_clamp = bool(self.cfg["dl_clamp"])
        if want_clamp != self._clamp_applied:
            if self.qos.set_download_clamp(want_clamp): self._clamp_applied = want_clamp

    def _run_test(self, now):
        self._set(status="Measuring bandwidth...")
        self.qos.clear_throttle()                      # otherwise we would measure our own limit
        self.applied_ul = None
        try:
            time.sleep(0.5)
            d = self.dl_test(); u = self.ul_test()
            self.dl.add_sample(d); self.ul.add_sample(u)
            log.info("measured dl=%.2f ul=%.2f -> limits dl=%.2f ul=%.2f", d, u, self.dl.limit, self.ul.limit)
            vol = max(self.dl.volatility, self.ul.volatility)
            self._next_test = time.monotonic() + (self.cfg["test_interval_s"] if vol < 0.25 else self.cfg["test_interval_s"] / 2.5)
            self._err = None
            self._set(last_test=time.strftime("%H:%M:%S"), dl_limit=self.dl.limit, ul_limit=self.ul.limit)
        except Exception as e:
            log.warning("speed test failed: %s", e)
            self._next_test = time.monotonic() + 60
            self._err = str(e)[:90]
            self._set(status=f"Speed test failed ({self._err}) - retrying")
        finally:
            self._apply(force=True)                    # always put the throttle back

    def _apply(self, force=False):
        lim = self.ul.limit
        self._set(dl_limit=self.dl.limit, ul_limit=lim)
        if lim is None: return
        if force or self.applied_ul is None or abs(lim - self.applied_ul) / self.applied_ul > 0.05:
            if self.qos.set_upload_limit(lim): self.applied_ul = lim
