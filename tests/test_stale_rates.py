"""A dash means never measured, not quietly expired.

The ports panel used to lose whole columns of counters and get them
back a minute later. Nothing was wrong with the switch: `current()`
dropped every rate older than three counters intervals, the port fell
out of the answer, and the panel drew "—" — the same "—" it draws for a
column the agent does not implement.

Those are opposite diagnoses. One says "this switch has no such
counter, stop looking"; the other says "nobody has measured this
lately, and here is how lately". The cure is the same one v0.6.4
applied to zero: report the absence as an absence, with its age, and
let whatever is displaying it decide.

Run with:  python -m unittest discover -s tests
"""

import time
import unittest

from moonlan.counters import (
    CounterStore,
    PortRates,
    Sample,
    rate_for_display,
)


def _sample(ts: float, octets: int) -> Sample:
    return Sample(
        ts=ts, in_octets=octets, out_octets=octets,
        in_errors=0, out_errors=0, in_discards=0, out_discards=0,
        hc_in=True, hc_out=True,
    )


class CurrentKeepsEverythingTest(unittest.TestCase):
    """The store reports; it does not decide what is too old."""

    def setUp(self):
        self.store = CounterStore()
        now = time.time()
        # two cycles a minute apart, taken four minutes ago
        self.store.update("10.0.0.10", {1: _sample(now - 300, 0)})
        self.store.update(
            "10.0.0.10", {1: _sample(now - 240, 60 * 1_000_000)}
        )

    def test_a_four_minute_old_rate_is_still_reported(self):
        rates = self.store.current("10.0.0.10")
        self.assertIn(1, rates)
        age = time.time() - rates[1].ts
        self.assertGreater(age, 180)
        self.assertIsNotNone(rates[1].in_mbps)

    def test_the_old_cutoff_still_works_when_asked_for(self):
        """`max_age` is not gone — nothing in the service passes one."""
        self.assertEqual(self.store.current("10.0.0.10", max_age=180), {})
        self.assertIn(1, self.store.current("10.0.0.10", max_age=3600))

    def test_a_port_with_one_sample_has_no_rate_at_all(self):
        """One measurement is not a rate, and must not read as zero."""
        store = CounterStore()
        store.update("10.0.0.21", {5: _sample(time.time(), 0)})
        self.assertEqual(store.current("10.0.0.21"), {})

    def test_a_skipped_cycle_does_not_break_the_baseline(self):
        """The next poll simply measures over a longer interval.

        `self._last[key] = cur` is set before the first-cycle check, so
        a gap costs one data point and nothing else.
        """
        store = CounterStore()
        now = time.time()
        store.update("10.0.0.22", {2: _sample(now - 600, 0)})
        fresh = store.update(
            "10.0.0.22", {2: _sample(now, 600 * 1_000_000 // 8)}
        )
        self.assertIn(2, fresh)
        self.assertAlmostEqual(fresh[2].in_mbps, 1.0, places=3)


class RateForDisplayTest(unittest.TestCase):
    """The three outcomes the panel has to tell apart."""

    def setUp(self):
        self.now = time.time()
        self.rates = {
            1: PortRates(ts=self.now - 240, in_mbps=1.0, out_mbps=2.0,
                         in_errors_per_min=0.0, out_errors_per_min=0.0,
                         in_discards_per_min=0.0, out_discards_per_min=0.0),
            2: PortRates(ts=self.now - 2400, in_mbps=3.0, out_mbps=4.0,
                         in_errors_per_min=0.0, out_errors_per_min=0.0,
                         in_discards_per_min=0.0, out_discards_per_min=0.0),
        }
        self.hide = 30 * 60  # stale_rate_hide_minutes: 30

    def test_four_minutes_old_is_shown_with_its_age(self):
        rate, age = rate_for_display(self.rates, 1, self.now, self.hide)
        self.assertIsNotNone(rate)
        self.assertEqual(rate.in_mbps, 1.0)
        self.assertAlmostEqual(age, 240, delta=2)

    def test_forty_minutes_old_is_withheld_but_dated(self):
        """A half-hour-old rate is a memory. The memory still counts."""
        rate, age = rate_for_display(self.rates, 2, self.now, self.hide)
        self.assertIsNone(rate)
        self.assertAlmostEqual(age, 2400, delta=2)

    def test_never_measured_has_no_value_and_no_age(self):
        """The one case a bare dash is allowed to mean."""
        self.assertEqual(
            rate_for_display(self.rates, 3, self.now, self.hide), (None, None)
        )

    def test_a_cutoff_of_zero_shows_everything(self):
        rate, age = rate_for_display(self.rates, 2, self.now, 0)
        self.assertIsNotNone(rate)
        self.assertAlmostEqual(age, 2400, delta=2)


if __name__ == "__main__":
    unittest.main()
