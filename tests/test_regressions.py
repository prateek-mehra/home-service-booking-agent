"""Regression tests for the four real bugs documented in BUGS.md."""
import tempfile
import unittest

from agent.conversation import Conversation
from agent.mock_tools import MockProviderAPI, ToolError


class TestBug1FailedAttemptRetryable(unittest.TestCase):
    """book() replay logic treated a verified-failed idempotency key as
    slot_taken, so a legitimate retry after timeout-without-booking always
    failed. Fixed: only 'booked' short-circuits; 'failed' retries."""

    def test_failed_key_retries_successfully(self):
        api = MockProviderAPI(seed=7)
        h = api.hold_slot("prov-a", "slot-1")
        api._db["idempotency"]["k1"] = "failed"  # verified failed earlier attempt
        api._book_outcome = lambda hold: "success"
        out = api.book(h["hold_id"], "k1")
        self.assertIn("booking_id", out)
        self.assertEqual(api.get_booking_status("k1")["status"], "booked")


class TestBug2BookingKeepsService(unittest.TestCase):
    """holds never captured the service, so bookings had service='' and
    service-scoped cancel/reschedule could not find them."""

    def test_booking_records_service(self):
        api = MockProviderAPI(seed=7, scenario={"book_outcomes": {"*": "success"}})
        p = api.search_providers("plumber", "Marathahalli", ["2026-11-01", "2026-11-08"])[0]
        h = api.hold_slot(p["id"], p["free_slots"][0])
        api.book(h["hold_id"], "k1")
        self.assertEqual(api._db["bookings"][0]["service"], "plumber")


class TestBug3PriceStableAcrossRestart(unittest.TestCase):
    """quoted_price came from the in-memory search catalog; after a restart the
    catalog was empty and a hash fallback produced a different price, voiding a
    still-valid approval with a spurious 'price changed'."""

    def test_quote_matches_listing_after_restart(self):
        import os
        d = tempfile.mkdtemp()
        db = os.path.join(d, "mock_db.json")
        api1 = MockProviderAPI(seed=7, db_path=db)
        p = api1.search_providers("plumber", "Marathahalli", ["2026-11-01", "2026-11-08"])[0]
        h1 = api1.hold_slot(p["id"], p["free_slots"][0])
        api2 = MockProviderAPI(seed=7, db_path=db)  # 'restarted': catalog empty
        h2 = api2.hold_slot(p["id"], p["free_slots"][0])
        self.assertEqual(h1["quoted_price"], h2["quoted_price"])


class TestBug4CorrectionBeforeYesVoidsHold(unittest.TestCase):
    """A correction in await_approval only voided the approval if the user had
    already confirmed; a correction between hold and 'yes' left the hold live
    and never told the user their confirmation no longer applied."""

    def test_correction_before_confirm_voids_hold(self):
        c = Conversation(user_id="u", data_dir=tempfile.mkdtemp(),
                         scenario={"book_outcomes": {"*": "success"}})
        c.handle("Find a plumber in Marathahalli this week")
        c.handle("1")                      # hold placed, no approval yet
        self.assertEqual(c.state, "await_approval")
        r = c.handle("actually, make it evening")
        self.assertIn("no longer applies", r.lower())
        self.assertIsNone(c.hold)
        self.assertIsNone(c.approval)


class TestReviewFinding1WindowNeverViolated(unittest.TestCase):
    """Adversarial review HIGH-1: _best_slot fell back to ANY slot when none
    matched the stated window; a 'mornings only' request booked a 13:00 slot."""

    def test_no_fallback_outside_window(self):
        c = Conversation(user_id="u", data_dir=tempfile.mkdtemp(), seed=39,
                         scenario={"book_outcomes": {"*": "success"}})
        c.slots["time_window"] = "morning"
        self.assertIsNone(c._best_slot({"free_slots": ["2026-11-02T18:00:00"]}))

    def test_offered_providers_all_fit_window(self):
        c = Conversation(user_id="u", data_dir=tempfile.mkdtemp(), seed=39,
                         scenario={"book_outcomes": {"*": "success"}})
        r = c.handle("Find a cleaning in Whitefield this week, mornings only")
        self.assertTrue("None of the" in r or "Which one" in r)
        for p in c.offers:
            self.assertIsNotNone(c._best_slot(p))


class TestReviewFinding2RestartKeepsService(unittest.TestCase):
    """Recovered (re-held) bookings after a restart kept service='' because the
    catalog was in-memory only."""

    def test_rehold_after_restart_keeps_service(self):
        import os
        d = tempfile.mkdtemp()
        db = os.path.join(d, "mock_db.json")
        api1 = MockProviderAPI(seed=7, db_path=db)
        p = api1.search_providers("plumber", "Marathahalli", ["2026-11-01", "2026-11-08"])[0]
        api2 = MockProviderAPI(seed=7, db_path=db)  # restarted process
        h = api2.hold_slot(p["id"], p["free_slots"][0])
        self.assertEqual(h["quoted_price"], p["price"])
        api2._book_outcome = lambda hold: "success"
        api2.book(h["hold_id"], "k1")
        self.assertEqual(api2._db["bookings"][0]["service"], "plumber")


class TestReviewFinding3CancelThenRebook(unittest.TestCase):
    """After a cancel, booking the same provider/slot/price again is a NEW
    booking - never 'verified after a timeout' on the dead one."""

    def test_rebook_after_cancel_is_fresh(self):
        c = Conversation(user_id="u", data_dir=tempfile.mkdtemp(),
                         scenario={"book_outcomes": {"*": "success"}})
        c.handle("Find a plumber in Marathahalli this week")
        c.handle("1")
        c.handle("yes")
        bid1 = c.api._db["bookings"][0]["id"]
        c.handle("cancel my plumber booking")
        c.handle("yes")
        self.assertEqual(c.api._db["bookings"][0]["status"], "cancelled")
        r = c.handle("yes")  # must not claim the cancelled booking is active
        self.assertIn("cancelled", r.lower())
        # same request again -> fresh hold -> fresh key -> second booking
        c.handle("Find a plumber in Marathahalli this week")
        c.handle("1")
        c.handle("yes")
        booked = [b for b in c.api._db["bookings"] if b["status"] == "booked"]
        self.assertEqual(len(booked), 1)
        self.assertNotEqual(booked[0]["id"], bid1)


class TestReviewFinding5HoldStormBounded(unittest.TestCase):
    """hold_expired re-holds bypassed MAX_BOOK_RETRIES; one 'yes' could burn
    the whole 40-call tool budget."""

    def test_hold_storm_stops_with_clear_message(self):
        c = Conversation(user_id="u", data_dir=tempfile.mkdtemp(),
                         scenario={"book_outcomes": {"*": "success"},
                                   "expire_holds": True})
        c.handle("Find a plumber in Marathahalli this week")
        c.handle("1")
        # fresh holds expire instantly; booking can never complete
        r = c.handle("yes")
        self.assertIn("kept expiring", r)
        self.assertFalse(c.api._db["bookings"])
        self.assertLess(c.tool_calls, 15)


class TestReviewFinding6SlotUniqueness(unittest.TestCase):
    """Two active bookings for the same provider+slot were possible after a
    price re-quote (key includes price)."""

    def test_second_booking_same_slot_rejected(self):
        api = MockProviderAPI(seed=7, scenario={"book_outcomes": {"*": "success"}})
        h1 = api.hold_slot("prov-a", "slot-1")
        api.book(h1["hold_id"], "k1")
        h2 = api.hold_slot("prov-a", "slot-1")
        with self.assertRaises(ToolError) as ctx:
            api.book(h2["hold_id"], "k2")
        self.assertEqual(ctx.exception.code, "slot_taken")
        self.assertEqual(len(api._db["bookings"]), 1)


class TestReviewFinding10CrashSafety(unittest.TestCase):
    """Unhandled exception path left the user with no message."""

    def test_handle_never_raises(self):
        c = Conversation(user_id="u", data_dir=tempfile.mkdtemp(),
                         scenario={"book_outcomes": {"*": "success"}})
        c.handle("Find a plumber in Marathahalli this week")
        c.handle("1")
        c.handle("yes")
        c.slots["date_range"] = None  # corrupted state (e.g. restore bug)
        r = c.handle("actually, make it evening")  # correction keeps the bad slot
        self.assertIn("Something went wrong", r)


class TestReviewFinding11CancelApprovalChecked(unittest.TestCase):
    """Cancel/reschedule approvals were created but never code-checked."""

    def test_cancel_checks_approval_record(self):
        c = Conversation(user_id="u", data_dir=tempfile.mkdtemp(),
                         scenario={"book_outcomes": {"*": "success"}})
        c.handle("Find a plumber in Marathahalli this week")
        c.handle("1")
        c.handle("yes")
        c.handle("cancel my plumber booking")
        b = dict(c.pending_action["booking"])
        orig = c.api.cancel
        def spy(**kw):
            # the approval must exist and match before the tool is ever called
            self.assertIsNotNone(c.approval)
            ok, _ = c.approval.matches("cancel", b["provider_id"], b["slot"],
                                       b["price"], now=c.clock())
            self.assertTrue(ok)
            return orig(**kw)
        c.api.cancel = spy
        c.handle("yes")
        self.assertEqual(c.api._db["bookings"][0]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()

class TestRetestScopedForget(unittest.TestCase):
    """Retest HIGH: 'forget my technician' must kill service-scoped variants."""

    def test_forget_base_covers_scoped(self):
        import tempfile, os
        from agent.memory import MemoryStore
        m = MemoryStore(os.path.join(tempfile.mkdtemp(), "memory.json"))
        m.set_inferred("preferred_technician:plumber", "Ravi Services")
        m.confirm("preferred_technician:plumber")
        m.forget("preferred_technician")
        self.assertIsNone(m.usable("preferred_technician:plumber"))
        self.assertFalse(m.set_inferred("preferred_technician:plumber", "Ravi Services"))
        self.assertEqual(m.view(), {})


class TestRetestOwnSlotNeverReproposed(unittest.TestCase):
    """Checkpoint-3 retest: _best_slot's no-constraint early return bypassed the
    _taken filter. After booking a plumber with no time window and restarting,
    the same request re-proposed the user's OWN active slot; book()'s clash
    check then rejected it as 'taken by someone else'. Fixed: taken filtering
    applies on every branch."""

    def test_own_active_slot_not_reproposed_after_restart(self):
        d = tempfile.mkdtemp()
        c1 = Conversation(user_id="u", data_dir=d, seed=7,
                          scenario={"book_outcomes": {"*": "success"}})
        c1.handle("Find a plumber in Marathahalli this week")
        c1.handle("1")
        c1.handle("yes")
        self.assertEqual(c1.state, "done")
        booked = [b for b in c1._call_tool("list_bookings", user_id="u")
                  if b["status"] == "booked"]
        self.assertEqual(len(booked), 1)
        own_pid, own_slot = booked[0]["provider_id"], booked[0]["slot"]

        # restart over the same data, then repeat the same request
        c2 = Conversation(user_id="u", data_dir=d, seed=7,
                          scenario={"book_outcomes": {"*": "success"}})
        c2.handle("Find a plumber in Marathahalli this week")
        own_offer = next((p for p in c2.offers if p["id"] == own_pid), None)
        self.assertIsNotNone(own_offer, "repro needs the same provider offered")
        self.assertNotEqual(c2._best_slot(own_offer), own_slot)

        # full path: choosing that provider books a DIFFERENT slot cleanly
        idx = c2.offers.index(own_offer) + 1
        r = c2.handle(str(idx))
        self.assertNotIn(own_slot, r)
        r = c2.handle("yes")
        self.assertNotIn("taken", r.lower())
        bookings = [b for b in c2._call_tool("list_bookings", user_id="u")
                    if b["status"] == "booked"]
        self.assertEqual(len(bookings), 2)
        new_slots = {b["slot"] for b in bookings} - {own_slot}
        self.assertEqual(len(new_slots), 1)  # second booking landed elsewhere
