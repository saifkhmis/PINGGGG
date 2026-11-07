"""Adaptive limit selection. Pure logic, no I/O, fully unit-testable.

  * short-lived spikes are ignored (an up-jump needs 3 consistent confirmations),
  * real drops are honoured fast (conservative cap at once, regime switch after 2),
  * the limit is a LOW percentile of a rolling window times a safety factor,
  * increases are rate-limited (+10% per step) with a dead-band,
  * latency-under-load feedback backs the limit off further when queues build.
"""
import math
from collections import deque
from statistics import median


def percentile(vals, p):
    s = sorted(vals)
    if not s: return 0.0
    k = (len(s) - 1) * p / 100.0
    f, c = math.floor(k), math.ceil(k)
    return s[f] + (s[c] - s[f]) * (k - f)


class AdaptiveLimiter:
    def __init__(self, safety=0.92, pct=25, window=12, min_mbps=0.5,
                 up_confirm=3, down_confirm=2, jump_up=1.3, jump_down=0.75,
                 max_up_step=1.15, deadband=0.05,
                 bloat_hi_ms=45.0, bloat_lo_ms=15.0, min_penalty=0.5):
        self.safety, self.pct, self.min_mbps = safety, pct, min_mbps
        self.up_confirm, self.down_confirm = up_confirm, down_confirm
        self.jump_up, self.jump_down = jump_up, jump_down
        self.max_up_step, self.deadband = max_up_step, deadband
        self.bloat_hi, self.bloat_lo, self.min_penalty = bloat_hi_ms, bloat_lo_ms, min_penalty
        self.window = deque(maxlen=window)
        self.pending, self.pending_dir = [], 0
        self.penalty, self.good_streak = 1.0, 0
        self.limit = None

    def add_sample(self, mbps):
        mbps = max(0.0, float(mbps))
        if not self.window:
            self.window.append(mbps); return self._update()
        med = median(self.window)
        d = 1 if mbps > med * self.jump_up else (-1 if mbps < med * self.jump_down else 0)
        if d == 0:
            self.pending, self.pending_dir = [], 0
            self.window.append(mbps)
        else:
            if d != self.pending_dir: self.pending = []
            self.pending_dir = d
            self.pending.append(mbps)
            need = self.up_confirm if d > 0 else self.down_confirm
            if len(self.pending) >= need:
                if max(self.pending) <= 1.4 * max(min(self.pending), 0.01):   # consistent => real change
                    self.window.clear(); self.window.extend(self.pending)
                    self.pending, self.pending_dir = [], 0
                else:
                    self.pending = self.pending[-(need - 1):]                   # noisy: keep waiting
        return self._update()

    def feedback(self, bloat_ms):
        """bloat_ms = current ping - idle baseline, measured while the link is busy."""
        if bloat_ms >= self.bloat_hi:
            self.penalty = max(self.min_penalty, self.penalty * 0.90); self.good_streak = 0
        elif bloat_ms <= self.bloat_lo:
            self.good_streak += 1
            if self.good_streak >= 4:
                self.penalty = min(1.0, self.penalty * 1.05); self.good_streak = 0
        else:
            self.good_streak = 0
        return self._update()

    def _update(self):
        if not self.window: return None
        raw = percentile(self.window, self.pct) * self.safety * self.penalty
        st = getattr(self, "_stable", None)
        if st is None:
            st = raw
        elif raw < st * (1 - self.deadband):
            st = raw                                   # decreases apply at once
        elif raw > st * (1 + self.deadband):
            st = min(raw, st * self.max_up_step)       # increases are rate-limited
        self._stable = max(self.min_mbps, st)
        out = self._stable
        if self.pending_dir < 0 and self.pending:      # possible drop: temporary cap, not remembered
            out = min(out, min(self.pending) * self.safety * self.penalty)
        self.limit = max(self.min_mbps, out)
        return self.limit

    @property
    def volatility(self):
        if len(self.window) < 3: return 1.0
        m = median(self.window)
        return (max(self.window) - min(self.window)) / m if m else 1.0


def stability_score(ping_ms, jitter_ms, loss_pct, baseline_ms):
    """0-100: penalises jitter, loss and latency inflation over the idle baseline."""
    if ping_ms is None: return 0
    infl = max(0.0, ping_ms - (baseline_ms or ping_ms))
    s = 100 - 1.5 * jitter_ms - 12 * loss_pct - 0.4 * infl
    return int(max(0, min(100, s)))
