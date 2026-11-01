"""Persistent user memory: stable preferences across sessions.

Every fact is marked `stated` (user said it / answered a direct question) or
`inferred` (agent derived it from behaviour). Inferred facts are never used
without explicit confirmation. Forgotten facts are tombstoned and never reused
or re-inferred.
"""
import json
import os
import time


class MemoryStore:
    def __init__(self, path):
        self.path = path
        self.facts = {}       # key -> {value, source: stated|inferred, confirmed: bool, updated_at}
        self.tombstones = set()
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                self.facts = data.get("facts", {})
                self.tombstones = set(data.get("tombstones", []))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                # corrupt state must not crash startup: quarantine and start clean
                os.replace(path, path + ".corrupt")

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"facts": self.facts, "tombstones": sorted(self.tombstones)}, f, indent=2)
        os.replace(tmp, self.path)

    def set_stated(self, key, value):
        """A fact the user stated directly; usable immediately."""
        self.facts[key] = {"value": value, "source": "stated", "confirmed": True,
                           "updated_at": time.time()}
        self.tombstones.discard(key)
        self._save()

    def _blocked(self, key):
        """A key is blocked if it or its base (before any ':') is tombstoned,
        so 'forget my technician' kills preferred_technician AND every
        preferred_technician:<service> variant forever."""
        return key in self.tombstones or key.split(":")[0] in self.tombstones

    def set_inferred(self, key, value):
        """A behaviour-derived fact. Unconfirmed: must not be used yet. Never
        re-infer a forgotten (tombstoned) fact."""
        if self._blocked(key):
            return False
        existing = self.facts.get(key)
        if existing and existing["source"] == "stated":
            return False
        self.facts[key] = {"value": value, "source": "inferred", "confirmed": False,
                           "updated_at": time.time()}
        self._save()
        return True

    def confirm(self, key):
        if key in self.facts:
            self.facts[key]["confirmed"] = True
            self._save()

    def usable(self, key):
        """A fact the agent may act on: stated, or inferred AND user-confirmed."""
        f = self.facts.get(key)
        if not f or self._blocked(key):
            return None
        if f["source"] == "stated" or f["confirmed"]:
            return f["value"]
        return None

    def pending_confirmation(self, key):
        f = self.facts.get(key)
        if f and f["source"] == "inferred" and not f["confirmed"] and not self._blocked(key):
            return f["value"]
        return None

    def forget_all(self):
        """Forget everything: remove every fact and tombstone every key family,
        so nothing is ever re-inferred."""
        for k in list(self.facts):
            self.tombstones.add(k)
            self.tombstones.add(k.split(":")[0])
        self.facts = {}
        self._save()

    def forget(self, key):
        """Forget a fact forever: remove it and every scoped variant, and block
        future re-use/re-inference of the whole family."""
        self.facts.pop(key, None)
        for k in [k for k in self.facts if k.startswith(key + ":")]:
            self.facts.pop(k, None)
        self.tombstones.add(key)
        self._save()

    def view(self):
        return {k: dict(v) for k, v in self.facts.items() if not self._blocked(k)}
