"""Approval records, enforced in code.

An approval binds the exact action, provider, slot and price the user confirmed.
Any change to provider, slot or price voids it. The booking path refuses to run
without a matching, unexpired approval record - model output or tool args can
never substitute for one.
"""
import hashlib
import time

APPROVAL_TTL_SEC = 900  # approvals go stale after 15 minutes


def _fingerprint(action, provider_id, slot, price):
    raw = f"{action}|{provider_id}|{slot}|{price}"
    return hashlib.sha256(raw.encode()).hexdigest()


class Approval:
    def __init__(self, action, provider_id, slot, price, confirmed_text, now=None):
        self.action = action
        self.provider_id = provider_id
        self.slot = slot
        self.price = price
        self.confirmed_text = confirmed_text
        self.created_at = time.time() if now is None else now
        self.fingerprint = _fingerprint(action, provider_id, slot, price)

    def matches(self, action, provider_id, slot, price, now=None):
        now = time.time() if now is None else now
        if now - self.created_at > APPROVAL_TTL_SEC:
            return False, "approval expired"
        expected = _fingerprint(action, provider_id, slot, price)
        if expected != self.fingerprint:
            return False, "action, provider, slot or price changed since confirmation"
        return True, "ok"

    def to_dict(self):
        return dict(action=self.action, provider_id=self.provider_id, slot=self.slot,
                    price=self.price, confirmed_text=self.confirmed_text,
                    created_at=self.created_at, fingerprint=self.fingerprint)

    @classmethod
    def from_dict(cls, d):
        a = cls(d["action"], d["provider_id"], d["slot"], d["price"],
                d["confirmed_text"], now=d["created_at"])
        a.fingerprint = d["fingerprint"]
        return a
