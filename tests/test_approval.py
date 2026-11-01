import unittest

from agent.approval import Approval


class TestApproval(unittest.TestCase):
    def test_match(self):
        a = Approval("book", "p1", "s1", 500, "yes", now=1000.0)
        ok, _ = a.matches("book", "p1", "s1", 500, now=1001.0)
        self.assertTrue(ok)

    def test_any_change_voids(self):
        a = Approval("book", "p1", "s1", 500, "yes", now=1000.0)
        for args in [("cancel", "p1", "s1", 500), ("book", "p2", "s1", 500),
                     ("book", "p1", "s2", 500), ("book", "p1", "s1", 501)]:
            ok, why = a.matches(*args, now=1001.0)
            self.assertFalse(ok, args)
            self.assertIn("changed", why)

    def test_expiry(self):
        a = Approval("book", "p1", "s1", 500, "yes", now=1000.0)
        ok, why = a.matches("book", "p1", "s1", 500, now=1000.0 + 9999)
        self.assertFalse(ok)
        self.assertIn("expired", why)


if __name__ == "__main__":
    unittest.main()
