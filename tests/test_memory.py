import os
import tempfile
import unittest

from agent.memory import MemoryStore


class TestMemory(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "memory.json")

    def test_stated_usable_immediately_and_persists(self):
        m = MemoryStore(self.path)
        m.set_stated("area", "Whitefield")
        self.assertEqual(MemoryStore(self.path).usable("area"), "Whitefield")

    def test_inferred_not_usable_until_confirmed(self):
        m = MemoryStore(self.path)
        m.set_inferred("preferred_technician", "Ravi")
        self.assertIsNone(m.usable("preferred_technician"))
        self.assertEqual(m.pending_confirmation("preferred_technician"), "Ravi")
        m.confirm("preferred_technician")
        self.assertEqual(m.usable("preferred_technician"), "Ravi")

    def test_forget_tombstones_against_reuse_and_reinference(self):
        m = MemoryStore(self.path)
        m.set_stated("area", "Whitefield")
        m.forget("area")
        self.assertIsNone(m.usable("area"))
        self.assertFalse(m.set_inferred("area", "Whitefield"))  # never re-inferred
        self.assertIsNone(MemoryStore(self.path).usable("area"))

    def test_inferred_never_overwrites_stated(self):
        m = MemoryStore(self.path)
        m.set_stated("area", "Whitefield")
        self.assertFalse(m.set_inferred("area", "Koramangala"))
        self.assertEqual(m.usable("area"), "Whitefield")


if __name__ == "__main__":
    unittest.main()
