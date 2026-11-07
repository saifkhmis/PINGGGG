import os, random, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sps.algo import AdaptiveLimiter, stability_score

def run(lim, samples):
    out = []
    for s in samples: out.append(lim.add_sample(s))
    return out

class TestAlgo(unittest.TestCase):
    def setUp(self): random.seed(7)

    def test_18_to_20_selects_about_18_and_ignores_spikes(self):
        lim = AdaptiveLimiter(safety=0.92)
        s = [random.uniform(18, 20) for _ in range(10)]
        s[4] = 31.0; s[7] = 29.0          # isolated up-spikes
        out = run(lim, s)
        print("18-20 w/ spikes ->", [round(x, 1) for x in out])
        self.assertTrue(16.0 <= out[-1] <= 18.5, out[-1])
        self.assertLess(max(out), 18.6)   # spikes never raised the limit

    def test_adapts_up_to_28_30_only_after_confirmation(self):
        lim = AdaptiveLimiter()
        run(lim, [random.uniform(18, 20) for _ in range(8)])
        base = lim.limit
        lim.add_sample(29); lim.add_sample(28)          # 2 samples: not yet trusted
        self.assertAlmostEqual(lim.limit, base, delta=0.5)
        out = run(lim, [random.uniform(28, 30) for _ in range(8)])
        print("up-shift ->", [round(x, 1) for x in out])
        self.assertTrue(24.0 <= out[-1] <= 28.0, out[-1])
        for a, b in zip(out, out[1:]): self.assertLessEqual(b, a * 1.151)   # +15%/step max

    def test_adapts_down_to_5_7_fast(self):
        lim = AdaptiveLimiter()
        run(lim, [random.uniform(18, 20) for _ in range(8)])
        l1 = lim.add_sample(6.0)
        self.assertLess(l1, 6.0)          # immediate conservative cap
        out = run(lim, [random.uniform(5, 7) for _ in range(5)])
        print("down-shift ->", round(l1, 1), [round(x, 1) for x in out])
        self.assertTrue(4.0 <= out[-1] <= 6.0, out[-1])

    def test_transient_dip_recovers(self):
        lim = AdaptiveLimiter()
        run(lim, [19] * 8)
        lim.add_sample(5)                 # one-off dip
        out = run(lim, [19, 19, 19, 19])
        self.assertGreater(out[-1], 15.0)

    def test_latency_feedback_backs_off_and_recovers(self):
        lim = AdaptiveLimiter(); run(lim, [19] * 8); base = lim.limit
        for _ in range(3): lim.feedback(80)
        self.assertLess(lim.limit, base * 0.8)
        for _ in range(40): lim.feedback(5)
        self.assertGreater(lim.limit, base * 0.95)

    def test_stability_score(self):
        self.assertGreater(stability_score(50, 2, 0, 48), 90)
        self.assertLess(stability_score(200, 40, 5, 50), 30)

if __name__ == "__main__": unittest.main(verbosity=2)
