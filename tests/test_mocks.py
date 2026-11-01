import tempfile
import unittest

from agent.mock_tools import MockProviderAPI, ToolError


class TestMocks(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_search_is_deterministic_and_in_range(self):
        a = MockProviderAPI(seed=7)
        b = MockProviderAPI(seed=7)
        pa = a.search_providers("plumber", "Marathahalli", ["2026-11-01", "2026-11-08"])
        pb = b.search_providers("plumber", "Marathahalli", ["2026-11-01", "2026-11-08"])
        self.assertEqual(pa, pb)
        self.assertTrue(3 <= len(pa) <= 8)
        for p in pa:
            for field in ("id", "price", "rating", "free_slots", "attributes"):
                self.assertIn(field, p)

    def test_get_provider_has_injection_review(self):
        api = MockProviderAPI(seed=7)
        d = api.get_provider("prov-x")
        self.assertTrue(any("ignore all previous instructions" in r.lower()
                            for r in d["reviews"]))

    def test_hold_quote_matches_listing_by_default(self):
        api = MockProviderAPI(seed=7)
        p = api.search_providers("plumber", "Marathahalli", ["2026-11-01", "2026-11-08"])[0]
        h = api.hold_slot(p["id"], p["free_slots"][0])
        self.assertEqual(h["quoted_price"], p["price"])
        self.assertAlmostEqual(h["expires_at"] - api.clock(), 600, delta=2)

    def test_configured_price_delta_and_unavailable(self):
        api = MockProviderAPI(seed=7, scenario={
            "hold_price_delta": {"prov-a": 250},
            "unavailable_slots": ["prov-a|slot-1"]})
        with self.assertRaises(ToolError) as ctx:
            api.hold_slot("prov-a", "slot-1")
        self.assertEqual(ctx.exception.code, "slot_unavailable")
        self.assertEqual(api.hold_slot("prov-a", "slot-2")["quoted_price"],
                         api._base_price("prov-a") + 250)

    def test_book_outcome_forced_and_status_authoritative(self):
        api = MockProviderAPI(seed=7, scenario={"book_outcomes": {"*": "timeout_booked"}})
        h = api.hold_slot("prov-a", "slot-1")
        with self.assertRaises(ToolError) as ctx:
            api.book(h["hold_id"], "k1")
        self.assertEqual(ctx.exception.code, "timeout")
        st = api.get_booking_status("k1")
        self.assertEqual(st["status"], "booked")

    def test_cancel_503_then_success(self):
        api = MockProviderAPI(seed=7, scenario={"book_outcomes": {"*": "success"}})
        h = api.hold_slot("prov-a", "slot-1")
        bid = api.book(h["hold_id"], "k1")["booking_id"]
        with self.assertRaises(ToolError) as ctx:
            api.cancel(bid, "c1")
        self.assertEqual(ctx.exception.code, "transient_503")
        self.assertEqual(api.cancel(bid, "c1")["status"], "cancelled")

    def test_reschedule_conflict(self):
        api = MockProviderAPI(seed=7, scenario={
            "book_outcomes": {"*": "success"}, "conflict_slots": ["slot-9"]})
        h = api.hold_slot("prov-a", "slot-1")
        bid = api.book(h["hold_id"], "k1")["booking_id"]
        with self.assertRaises(ToolError) as ctx:
            api.reschedule(bid, "slot-9", "r1")
        self.assertEqual(ctx.exception.code, "slot_conflict")
        self.assertEqual(api.reschedule(bid, "slot-10", "r1")["booking"]["slot"], "slot-10")


if __name__ == "__main__":
    unittest.main()
