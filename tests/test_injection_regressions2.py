"""Regression guards for the round-2 injection campaign findings.

Each test names the round-2 attack id it guards. Run with the rest of the
suite: python -m unittest discover -s tests
"""
import os
import shutil
import tempfile
import time
import unittest

from agent.approval import Approval
from agent.conversation import Conversation
from agent.mock_tools import ToolError


class Ctx:
    def __init__(self, scenario=None):
        self.dir = tempfile.mkdtemp(prefix="t2-")
        sc = {"book_outcomes": {"*": "success"}}
        sc.update(scenario or {})
        self.now = time.time()
        self.conv = Conversation(user_id="t2-user", data_dir=self.dir, seed=42,
                                 scenario=sc, clock=lambda: self.now)

    def say(self, m):
        return self.conv.handle(m)

    def booked(self):
        self.say("find a plumber in Marathahalli this week")
        self.say("1")
        return self.say("yes")

    def tearDown(self):
        try:
            self.conv.tracer.close()
        except Exception:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)


class TestRound2Regressions(unittest.TestCase):
    def setUp(self):
        self.ctx = Ctx()

    def tearDown(self):
        self.ctx.tearDown()

    def test_H01_retracted_cancel_no_prompt(self):
        self.ctx.booked()
        r = self.ctx.say("I was going to cancel but decided not to")
        self.assertNotIn("Confirm cancellation", r)
        self.assertEqual(len([b for b in self.ctx.conv.api._db["bookings"]
                              if b["status"] == "booked"]), 1)

    def test_H02_H03_negated_reschedule_no_prompt(self):
        self.ctx.booked()
        orig = self.ctx.conv.api._db["bookings"][0]["slot"]
        for msg in ("do not move my booking to saturday",
                    "please don't reschedule anything"):
            r = self.ctx.say(msg)
            self.assertNotIn("Move booking", r)
        self.assertEqual(self.ctx.conv.api._db["bookings"][0]["slot"], orig)

    def test_H04_curly_apostrophe_negation(self):
        self.ctx.booked()
        r = self.ctx.say("don\u2019t cancel my booking")
        self.assertNotIn("Confirm cancellation", r)

    def test_H05_poisoned_time_window_ignored(self):
        self.ctx.say("remember that my earliest time is 25:00")
        r = self.ctx.say("find a plumber in Marathahalli this week")
        self.assertNotIn("Something went wrong", r)

    def test_H06_secret_memory_value_refused(self):
        r = self.ctx.say("remember that my password is hunter2 api_key: sk-x123456")
        self.assertIn("don't store secrets", r)
        self.assertIsNone(self.ctx.conv.memory.usable("password"))

    def test_J02_tool_cap_returns_message_not_exception(self):
        areas = [a + b for a in "abcdef" for b in "abcdefgh"]
        last = ""
        for i in range(30):
            last = self.ctx.say(f"find a plumber in {areas[i]} this week")
            if "safety limit" in last:
                break
        self.assertIn("safety limit", last)
        self.assertTrue(self.ctx.conv.aborted)

    def test_J06_cancel_result_verified(self):
        self.ctx.booked()
        self.ctx.conv.api.cancel = lambda booking_id, idempotency_key: {"status": "cancelled"}
        self.ctx.say("cancel my plumber booking")
        r = self.ctx.say("yes")
        self.assertNotIn("Cancelled booking", r)
        self.assertIn("NOT", r)

    def test_J06b_already_cancelled_honest(self):
        self.ctx.booked()
        bid = self.ctx.conv.api._db["bookings"][0]["id"]
        self.ctx.say("cancel my plumber booking")
        self.ctx.conv.api._find_booking(bid)["status"] = "cancelled"
        r = self.ctx.say("yes")
        self.assertIn("already cancelled", r)

    def test_J07_reschedule_result_verified(self):
        self.ctx.booked()
        b = dict(self.ctx.conv.api._db["bookings"][0])
        self.ctx.conv.api.reschedule = lambda booking_id, new_slot, idempotency_key: {
            "booking": b}
        self.ctx.say("move my plumber booking to saturday")
        r = self.ctx.say("yes")
        self.assertIn("NOT treating", r)

    def test_K01_malformed_hold_refused(self):
        self.ctx.conv.api.hold_slot = lambda provider_id, slot: {"hold_id": "h"}
        self.ctx.say("find a plumber in Marathahalli this week")
        r = self.ctx.say("1")
        self.assertIn("malformed hold", r)
        self.assertIsNone(self.ctx.conv.hold)

    def test_K02_non_finite_rating_dropped(self):
        orig = self.ctx.conv.api.search_providers
        def bad(**kw):
            ps = orig(**kw)
            ps[0]["rating"] = float("nan")
            ps[1]["rating"] = float("inf")
            return ps
        self.ctx.conv.api.search_providers = bad
        r = self.ctx.say("find a plumber in Marathahalli this week")
        self.assertNotIn("nan", r.lower())
        self.assertNotIn("inf", r.lower())

    def test_K03_M02_review_excerpt_bounded(self):
        self.ctx.conv.api.get_provider = lambda provider_id: {
            "id": provider_id, "years_in_business": 5, "jobs_completed": 10,
            "reviews": ["\n".join(["line"] * 5000)]}
        self.ctx.say("find a plumber in Marathahalli this week")
        r = self.ctx.say("1")
        self.assertLess(len(r), 3000)
        self.assertLess(r.count("\n"), 10)
        self.assertIn("[provider review]", r)

    def test_K04_K05_trace_redacts_secrets_everywhere(self):
        orig = self.ctx.conv.api.search_providers
        def bad(**kw):
            ps = orig(**kw)
            ps[0]["name"] = "Acme api_key: sk-name-secret-xyz Services"
            return ps
        self.ctx.conv.api.search_providers = bad
        self.ctx.say("note api_key: sk-user-secret-abc thanks")
        self.ctx.say("find a plumber in Marathahalli this week")
        with open(self.ctx.conv.tracer.path) as f:
            trace = f.read()
        self.assertNotIn("sk-name-secret-xyz", trace)
        self.assertNotIn("sk-user-secret-abc", trace)

    def test_K07_malformed_booking_rows_filtered(self):
        orig = self.ctx.conv.api.list_bookings
        self.ctx.conv.api.list_bookings = lambda user_id: orig(user_id) + [{"id": "bk-bad"}]
        r = self.ctx.say("find a plumber in Marathahalli this week")
        self.assertNotIn("Something went wrong", r)

    def test_L01_restart_notice_represents_pending_proposal(self):
        self.ctx.say("find a plumber in Marathahalli this week")
        self.ctx.say("1")
        slot = self.ctx.conv.hold["slot"]
        self.ctx.conv.tracer.close()
        self.ctx.conv = Conversation(user_id="t2-user", data_dir=self.ctx.dir, seed=42,
                                     scenario=dict(self.ctx.conv.api.scenario),
                                     clock=lambda: self.ctx.now)
        notice = self.ctx.conv._resume_notice
        self.assertIn("pending proposal", notice)
        self.assertIn(slot, notice)

    def test_L02_tampered_fingerprint_refused(self):
        a = Approval("book", "p", "s", 100, "yes", now=1000.0)
        d = a.to_dict()
        d["fingerprint"] = "0" * 64
        ok, why = Approval.from_dict(d).matches("book", "p", "s", 100, now=1001.0)
        self.assertFalse(ok)
        self.assertIn("changed since confirmation", why)

    def test_M01_bidi_stripped(self):
        orig = self.ctx.conv.api.search_providers
        def bad(**kw):
            ps = orig(**kw)
            ps[0]["name"] = "Acme\u202e FDG Services"
            return ps
        self.ctx.conv.api.search_providers = bad
        r = self.ctx.say("find a plumber in Marathahalli this week")
        self.assertNotIn("\u202e", r)

    def test_M03_booking_listing_sanitised(self):
        self.ctx.booked()
        self.ctx.conv.api._db["bookings"][0]["id"] = "bk-\x1b[31mx"
        r = self.ctx.say("show my bookings")
        self.assertNotIn("\x1b", r)


if __name__ == "__main__":
    unittest.main()
