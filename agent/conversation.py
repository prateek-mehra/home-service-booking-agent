"""The booking agent: explicit workflow state machine.

States: collecting -> offering -> await_approval -> done, with cancel and
reschedule sub-flows. All must-have invariants live here in code:

* asks only for missing slots; corrections keep everything else
* book/cancel/reschedule require a matching Approval record (approval.py)
* booking is idempotent; timeout -> get_booking_status before retry/report
* tool free text (reviews) is sanitised data, never instructions
* hard caps on model turns, tool calls and wall-clock; every exit is a message
* session state persists so a restart mid-booking resumes safely
"""
import datetime
import hashlib
import json
import os
import time

from . import config
from .approval import Approval
from .memory import MemoryStore
from .mock_tools import MockProviderAPI, ToolError
from .model import ScriptedModel
from .sanitize import plain
from .trace import TraceLogger

REQUIRED_SLOTS = ["service", "area", "date_range"]
WINDOWS = {"morning": (6, 12), "afternoon": (12, 17), "evening": (17, 21)}


class LimitExceeded(Exception):
    pass


class Conversation:
    def __init__(self, user_id="user", data_dir=".agent_data", seed=42,
                 scenario=None, model=None, clock=time.time, trace_dir=None):
        self.user_id = user_id
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        scenario = dict(scenario or {})
        scenario["user_id"] = user_id
        self.api = MockProviderAPI(seed=seed, scenario=scenario,
                                   db_path=os.path.join(data_dir, "mock_db.json"),
                                   clock=clock)
        self.memory = MemoryStore(os.path.join(data_dir, "memory.json"))
        self.model = model or ScriptedModel()
        self.clock = clock
        self.tracer = TraceLogger(trace_dir or os.path.join(data_dir, "traces"))
        self.session_path = os.path.join(data_dir, "session.json")

        self.state = "collecting"
        self.slots = {}
        self.offers = []
        self.hold = None            # {hold_id, provider_id, slot, quoted_price, expires_at, name}
        self.approval = None        # Approval
        self.pending_action = None  # {"type": "cancel"/"reschedule", ...}
        self.pending_prompt = None  # e.g. "memory_confirm:preferred_technician"
        self.last_booking_key = None
        self.booking_in_progress = None  # idempotency key persisted before booking
        self.aborted = False

        self.model_turns = 0
        self.tool_calls = 0
        self.run_started = clock()
        self._resume_notice = self._load_session()

    # ------------------------------------------------------------------ infra
    def _transition(self, to, reason):
        self.tracer.state_transition(self.state, to, reason)
        self.state = to
        self._save_session()

    def _call_tool(self, name, **args):
        if self.aborted:
            raise LimitExceeded("session already aborted")
        if self.tool_calls >= config.MAX_TOOL_CALLS:
            self._abort("tool-call limit")
            raise LimitExceeded("tool-call limit")
        if self.clock() - self.run_started > config.MAX_WALL_CLOCK_SEC:
            self._abort("wall-clock limit")
            raise LimitExceeded("wall-clock limit")
        self.tool_calls += 1
        t0 = self.clock()
        try:
            result = getattr(self.api, name)(**args)
            self.tracer.tool_call(name, args, result=result,
                                  latency_ms=(self.clock() - t0) * 1000)
            return result
        except ToolError as e:
            self.tracer.tool_call(name, args, error=e.code,
                                  latency_ms=(self.clock() - t0) * 1000)
            raise

    def _check_bounds(self):
        if self.aborted:
            raise LimitExceeded("session aborted")
        if self.model_turns >= config.MAX_MODEL_TURNS:
            self._abort("model-turn limit")
            raise LimitExceeded("model-turn limit")
        if self.clock() - self.run_started > config.MAX_WALL_CLOCK_SEC:
            self._abort("wall-clock limit")
            raise LimitExceeded("wall-clock limit")

    def _abort(self, reason):
        self.aborted = True
        self._transition("aborted", reason)

    # ------------------------------------------------------------- persistence
    def _save_session(self):
        data = {
            "state": self.state, "slots": self.slots, "offers": self.offers,
            "hold": self.hold,
            "approval": self.approval.to_dict() if self.approval else None,
            "pending_action": self.pending_action,
            "pending_prompt": self.pending_prompt,
            "last_booking_key": self.last_booking_key,
            "booking_in_progress": self.booking_in_progress,
        }
        tmp = self.session_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.session_path)

    def _load_session(self):
        """Resume after a restart. If a booking was in flight, consult the
        authoritative status before doing anything - never rebook blindly."""
        if not os.path.exists(self.session_path):
            return None
        with open(self.session_path) as f:
            data = json.load(f)
        self.slots = data.get("slots", {})
        self.offers = data.get("offers", [])
        self.hold = data.get("hold")
        self.approval = Approval.from_dict(data["approval"]) if data.get("approval") else None
        self.pending_action = data.get("pending_action")
        self.pending_prompt = data.get("pending_prompt")
        self.last_booking_key = data.get("last_booking_key")
        self.booking_in_progress = data.get("booking_in_progress")
        self.state = data.get("state", "collecting")
        if self.state == "aborted":
            self.state = "collecting"  # per-run caps reset on restart

        if self.booking_in_progress:
            key = self.booking_in_progress
            status = self._call_tool("get_booking_status", idempotency_key=key)
            if status["status"] == "booked":
                self.last_booking_key = key
                self.booking_in_progress = None
                self._transition("done", "recovered booking after restart")
                return ("I was restarted mid-booking, so I verified before doing anything: "
                        "your booking went through "
                        f"(booking id {status['booking_id']}). No duplicate was made.")
            # not actually booked: hold may be gone; ask to re-confirm
            self.booking_in_progress = None
            self.hold = None
            self._transition("await_approval", "restart with unconfirmed booking attempt")
            return ("I was restarted mid-booking and the booking did NOT complete. "
                    "Your approval is still on record; say 'yes' to retry safely.")
        return "Welcome back - I restored our previous session."

    # ------------------------------------------------------------------- main
    def handle(self, message):
        """One user turn -> agent reply. Every path returns a clear message."""
        try:
            self._check_bounds()
        except LimitExceeded:
            return ("I've hit this session's safety limit (turns, tool calls or time), "
                    "so I'm stopping here rather than guessing. Nothing further was booked. "
                    "Please start a fresh session to continue.")
        self._taken = {(b["provider_id"], b["slot"])
                       for b in self._call_tool("list_bookings", user_id=self.user_id)
                       if b["status"] == "booked"}
        self.model_turns += 1
        self.tracer.user_message(message)
        summary = (f"state={self.state};"
                   f"{'offering;' if self.offers else 'no-offers;'}"
                   f"slots={sorted(self.slots)}")
        result = self.model.interpret(message, summary, tracer=self.tracer)
        try:
            reply = self._dispatch(result, message)
        except LimitExceeded:
            reply = ("I've hit this session's safety limit and must stop. "
                     "No further changes were made. Start a fresh session to continue.")
        except Exception as e:  # every exit ends with a clear message
            self.tracer._emit("error", kind=type(e).__name__, detail=str(e))
            reply = ("Something went wrong on my side while handling that "
                     f"({type(e).__name__}). No booking, cancellation or change was "
                     "made beyond what I already confirmed. Please try again or "
                     "rephrase.")
        self.tracer.agent_message(reply)
        self._save_session()
        return reply

    def _dispatch(self, result, message):
        intent = result.intent
        if intent == "memory_view":
            return self._memory_view()
        if intent == "memory_forget":
            key = result.payload["key"]
            self.memory.forget(key)
            return f"Done - I've forgotten your {key.replace('_', ' ')} and won't use it again."
        if intent == "memory_set":
            key, value = result.payload["key"], result.payload["value"]
            if key == "area":
                value = value.title()
            self.memory.set_stated(key, value)
            return f"Noted, I'll remember that ({result.payload['key'].replace('_', ' ')})."
        if intent == "list_bookings":
            return self._list_bookings()
        if intent == "cancel_request":
            return self._start_cancel(result.payload.get("service"))
        if intent == "reschedule_request":
            return self._start_reschedule(result.slots)
        if intent == "confirm":
            return self._on_confirm(message)
        if intent == "reject":
            return self._on_reject()
        if intent == "choose_provider":
            return self._on_choose(result.payload)
        if intent == "use_last_provider":
            return self._on_use_last_provider(result.slots)
        if intent in ("provide", "correct"):
            return self._on_slots(result.slots, corrected=(intent == "correct"))
        return ("I didn't quite get that. You can describe the service you need, "
                "pick an option number, confirm or change a detail, or ask what I remember.")

    # ---------------------------------------------------------- slot gathering
    def _on_slots(self, new_slots, corrected):
        if not corrected and self.state == "done":
            self._reset_for_new_request(new_slots)
        if self.state == "await_approval" and corrected:
            # a correction voids any pending hold/approval - whether or not the
            # user has already said "yes" to it
            self.approval = None
            self.hold = None
            self.pending_action = None
            self._transition("collecting", "correction voided approval")
            self.slots.update(new_slots)
            return ("Got it - I've updated "
                    f"{', '.join(new_slots)}. Because a detail changed, the earlier "
                    "confirmation no longer applies. " + self._next_step())
        self.slots.update(new_slots)
        if self.state == "done":
            self._transition("collecting", "new request")
        return self._next_step()

    def _prefill_from_memory(self):
        used = []
        for slot, key in (("area", "area"),):
            if slot not in self.slots:
                v = self.memory.usable(key)
                if v:
                    self.slots[slot] = v
                    used.append(f"{slot}={v} (remembered)")
        if "time_window" not in self.slots and "earliest_hour" not in self.slots:
            v = self.memory.usable("earliest_time")
            if v:
                self.slots["time_window"] = v
                used.append(f"time={v} (remembered)")
        return used

    def _next_step(self):
        remembered = self._prefill_from_memory()
        missing = [s for s in REQUIRED_SLOTS if s not in self.slots]
        if missing:
            ask = {"service": "What service do you need (plumber, electrician, AC servicing, cleaning, ...)?",
                   "area": "Which area should I search in?",
                   "date_range": "When should they come - today, tomorrow, a weekday, or this week?"}[missing[0]]
            return ask
        return self._search(remembered)

    # ----------------------------------------------------------------- search
    def _date_range(self):
        today = datetime.date.fromtimestamp(self.clock())
        dr = self.slots["date_range"]
        if dr == "today":
            return today.isoformat(), today.isoformat()
        if dr == "tomorrow":
            d = today + datetime.timedelta(days=1)
            return d.isoformat(), d.isoformat()
        if dr == "this_week":
            return today.isoformat(), (today + datetime.timedelta(days=7)).isoformat()
        if dr == "next_week":
            return (today + datetime.timedelta(days=7)).isoformat(), (today + datetime.timedelta(days=14)).isoformat()
        # weekday name
        names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        target = names.index(dr)
        delta = (target - today.weekday()) % 7
        d = today + datetime.timedelta(days=delta)
        return d.isoformat(), d.isoformat()

    def _search(self, remembered):
        start, end = self._date_range()
        providers = self._call_tool("search_providers", service=self.slots["service"],
                                    area=self.slots["area"], date_range=[start, end])
        budget = self.slots.get("budget")
        if budget:
            providers = [p for p in providers if p["price"] <= budget]
        providers.sort(key=lambda p: (-p["rating"], p["price"]))
        fitting = [p for p in providers if self._best_slot(p)]
        if providers and not fitting:
            return (f"None of the {len(providers)} {self.slots['service']} providers in "
                    f"{self.slots['area']} have a free slot inside your time window. "
                    "Want me to drop the time constraint or pick different dates?")
        providers = fitting
        if not providers:
            return (f"I couldn't find any {self.slots['service']} providers in "
                    f"{self.slots['area']} within your constraints. Want to relax "
                    "the budget or pick different dates?")
        preferred = self.slots.pop("preferred_name", None)
        missed = None
        if preferred:
            match = next((p for p in providers
                          if p["name"].strip().lower() == preferred.strip().lower()), None)
            if match:
                return self._hold_and_propose(match)
            missed = preferred
        prefix = (f"Your preferred {missed} isn't available for this search, "
                  "so here are the alternatives. " if missed else "")
        self.offers = providers[:3]
        self._transition("offering", "search complete")
        lines = [prefix + f"Here are the top options for {self.slots['service']} in {self.slots['area']}:"]
        for i, p in enumerate(self.offers, 1):
            lines.append(f"{i}. {p['name']} - Rs.{p['price']}, rating {p['rating']}, "
                         f"earliest slot {self._best_slot(p) or 'none'}")
        if remembered:
            lines.append("Using " + ", ".join(remembered) + ".")
        lines.append("Which one should I go with? (1, 2 or 3)")
        return "\n".join(lines)

    def _best_slot(self, provider):
        """Best slot honouring the user's hard constraints; None if none fit -
        never silently falls back to a slot outside the stated window, and never
        proposes a slot that is already booked (including this user's own active
        booking, which would surface as a bogus 'taken by someone else')."""
        slots = provider.get("free_slots", [])
        window = self.slots.get("time_window")
        earliest = self.slots.get("earliest_hour")
        taken = getattr(self, "_taken", set())
        for s in sorted(slots):
            if (provider.get("id"), s) in taken:
                continue  # never double-book a slot
            hour = int(s[11:13])
            if window and not (WINDOWS[window][0] <= hour < WINDOWS[window][1]):
                continue
            if earliest and hour < earliest:
                continue
            return s
        return None

    # -------------------------------------------------------------- selection
    def _on_choose(self, payload):
        if self.state != "offering" or not self.offers:
            return "There's no shortlist to pick from yet - tell me what you need first."
        if "index" in payload:
            idx = payload["index"]
            if idx >= len(self.offers):
                return f"Only {len(self.offers)} options - pick 1 to {len(self.offers)}."
            provider = self.offers[idx]
        else:
            name = payload["name"].lower()
            provider = next((p for p in self.offers if name.split()[0] in p["name"].lower()), None)
            if not provider:
                return "That name isn't in the shortlist - pick 1, 2 or 3."
        return self._hold_and_propose(provider)

    def _tech_key(self, service=None):
        svc = service or self.slots.get("service")
        return f"preferred_technician:{svc}" if svc else "preferred_technician"

    def _reset_for_new_request(self, new_slots):
        """A request for a different service starts fresh: only the slots the
        user explicitly stated in THIS message carry over."""
        new_service = new_slots.get("service")
        if new_service and self.slots.get("service") and new_service != self.slots["service"]:
            self.slots = {}

    def _last_booking_of(self, service):
        bookings = [b for b in self._call_tool("list_bookings", user_id=self.user_id)
                    if service and service.split()[0] in (b.get("service") or "")]
        return bookings[-1] if bookings else None

    def _on_use_last_provider(self, slots=None):
        slots = slots or {}
        self._reset_for_new_request(slots)
        self.slots.update(slots)
        service = self.slots.get("service")
        last = self._last_booking_of(service)
        # "same X as last time" grounds the search in that booking's area
        if last and last.get("area") and "area" not in self.slots:
            self.slots["area"] = last["area"]
        key = self._tech_key()
        tech = self.memory.usable(key)
        from_history = False
        if not tech and last and last.get("name") and not self.memory.pending_confirmation(key):
            # no preference stored either way: the user explicitly referenced
            # history, so use that booking's identity directly (not a stored
            # preference - forgetting never blocks an explicit request)
            self.slots["preferred_name"] = last["name"]
            tech = last["name"]
            from_history = True
        if not tech:
            pending = self.memory.pending_confirmation(key)
            if pending:
                # ask once, then RESUME this request with the answer
                if self.state == "done":
                    # this prompt belongs to a NEW request, not the finished one
                    self._transition("collecting", "new request via booking history")
                self.pending_prompt = f"memory_confirm:{key}"
                self._save_session()
                return (f"I've noticed you booked {pending} before, but I won't use that "
                        "without your say-so. Should I treat them as your preferred "
                        f"{self.slots.get('service', 'technician')}? "
                        "Reply yes to confirm or no to ignore.")
            svc = self.slots.get("service")
            return (f"I don't have a previous {svc} on record. " + self._next_step()
                    if svc else
                    "I don't have a previous technician on record. Tell me what you need.")
        match = next((p for p in self.offers
                      if p["name"].strip().lower() == tech.strip().lower()), None)
        if self.state == "offering" and match:
            return self._hold_and_propose(match)
        self.slots["preferred_name"] = tech
        if from_history:
            return (f"Your last {self.slots.get('service', 'booking')} was {tech}"
                    + (f" in {self.slots['area']}" if "area" in self.slots else "")
                    + ". " + self._next_step())
        return f"Got it - I'll look for {tech} (confirmed preferred). " + self._next_step()

    def _hold_and_propose(self, provider):
        details = self._call_tool("get_provider", provider_id=provider["id"])
        slot = self._best_slot(provider)
        if not slot:
            return f"{provider['name']} has no free slots in your window. Pick another?"
        try:
            hold = self._call_tool("hold_slot", provider_id=provider["id"], slot=slot)
        except ToolError as e:
            if e.code == "slot_unavailable":
                return (f"That slot was just taken. {provider['name']} also has "
                        f"{', '.join(sorted(provider['free_slots'])[:3])} - want one of those, "
                        "or a different provider?")
            raise
        self.hold = {"hold_id": hold["hold_id"], "provider_id": provider["id"],
                     "name": provider["name"], "slot": slot,
                     "quoted_price": hold["quoted_price"], "expires_at": hold["expires_at"]}
        self.pending_action = {"type": "book"}
        self.offers = []
        self._transition("await_approval", "hold placed")
        search_price = provider["price"]
        quoted = hold["quoted_price"]
        price_note = (" (note: the provider now quotes "
                      f"Rs.{quoted}, different from the listed Rs.{search_price})"
                      if quoted != search_price else "")
        review_line = plain({"content": details["reviews"][0]})
        return (f"{provider['name']}: {details['jobs_completed']} jobs, "
                f"{details['years_in_business']} yrs in business. {review_line}\n"
                f"I've placed a 10-minute hold for {slot} at Rs.{quoted}{price_note}.\n"
                f"Confirm booking {provider['name']} for {slot} at Rs.{quoted}? (yes/no)")

    # --------------------------------------------------------------- approval
    def _on_confirm(self, message):
        if self.pending_prompt and self.pending_prompt.startswith("memory_confirm:"):
            key = self.pending_prompt.split(":", 1)[1]
            value = self.memory.pending_confirmation(key)
            self.pending_prompt = None
            if value:
                self.memory.confirm(key)
                self.slots["preferred_name"] = value
                if self.state == "done":
                    # the request that raised the question already finished
                    return (f"Great - {value} is now confirmed as your preference. "
                            "Your booking is already complete; I'll use it next time.")
                # resume the request the confirmation interrupted
                return (f"Great - {value} is now confirmed as your preference. "
                        + self._next_step())
            return "Okay, nothing to confirm on that anymore."
        if self.state == "done" and self.last_booking_key:
            status = self._call_tool("get_booking_status",
                                     idempotency_key=self.last_booking_key)
            if status["status"] == "booked":
                current = next((b for b in self._call_tool("list_bookings", user_id=self.user_id)
                                if b["id"] == status["booking_id"]), None)
                if current and current["status"] == "cancelled":
                    return ("That booking was cancelled - there is nothing active to confirm. "
                            "Tell me what you'd like to book and I'll start fresh.")
                return ("That's already booked - saying yes again won't create a duplicate. "
                        f"Booking id {status['booking_id']}.")
            detail = status.get("detail", "it is no longer active")
            return (f"There's nothing to confirm - {detail}. "
                    "Tell me what you'd like to book and I'll start fresh.")
        if self.state != "await_approval" or not self.pending_action:
            return "There's nothing waiting for confirmation right now."

        action = self.pending_action
        if action["type"] == "book":
            if not self.hold:
                # resumed after restart: approval on record, need a fresh hold
                return self._rehold_then_book(message)
            self.approval = Approval("book", self.hold["provider_id"], self.hold["slot"],
                                     self.hold["quoted_price"], message, now=self.clock())
            self._save_session()
            return self._execute_booking()
        if action["type"] == "cancel":
            return self._execute_cancel(message)
        if action["type"] == "reschedule":
            return self._execute_reschedule(message)
        return "Nothing to confirm."

    def _on_reject(self):
        if self.pending_prompt and self.pending_prompt.startswith("memory_confirm:"):
            key = self.pending_prompt.split(":", 1)[1]
            self.pending_prompt = None
            self.memory.forget(key)
            if self.state == "done":
                # the request that raised the question already finished
                return ("No problem - I won't treat them as preferred, and I've dropped "
                        "that note. Your booking is already complete.")
            return ("No problem - I won't treat them as preferred, and I've dropped "
                    "that note. " + self._next_step())
        if self.state == "await_approval":
            self.approval = None
            self.hold = None
            self.pending_action = None
            self._transition("collecting", "user rejected proposal")
            return "Okay, cancelled that. Nothing was booked. What would you like instead?"
        return "Okay. What would you like to do?"

    # ---------------------------------------------------------------- booking
    def _idem_key(self, action, provider_id, slot, price):
        """Deterministic within one booking intent (hold), unique across intents:
        re-asking for the same provider/slot/price after a cancellation is a NEW
        booking, never a replay of the old one."""
        hold_id = self.hold["hold_id"] if self.hold else "nohold"
        return hashlib.sha256(
            f"{self.user_id}|{action}|{provider_id}|{slot}|{price}|{hold_id}".encode()
        ).hexdigest()[:20]

    def _rehold_then_book(self, message):
        a = self.approval
        try:
            hold = self._call_tool("hold_slot", provider_id=a.provider_id, slot=a.slot)
        except ToolError as e:
            if e.code == "slot_unavailable":
                self._transition("collecting", "slot gone after restart")
                return "That slot is no longer available. Let's find another - what changed?"
            raise
        self.hold = {"hold_id": hold["hold_id"], "provider_id": a.provider_id,
                     "name": a.provider_id, "slot": a.slot,
                     "quoted_price": hold["quoted_price"], "expires_at": hold["expires_at"]}
        if hold["quoted_price"] != a.price:
            self.approval = None
            self._transition("await_approval", "price changed on re-hold")
            return (f"The price changed while we were away: Rs.{a.price} -> "
                    f"Rs.{hold['quoted_price']}. Your earlier confirmation is void. "
                    "Confirm the new price? (yes/no)")
        return self._execute_booking()

    def _execute_booking(self):
        a = self.approval
        key = self._idem_key("book", a.provider_id, a.slot, a.price)
        # Never claim success until verified; never duplicate on retry/restart.
        status = self._call_tool("get_booking_status", idempotency_key=key)
        if status["status"] == "booked":
            return self._finish_booking(key, status["booking_id"], recovered=True)

        self.booking_in_progress = key
        self._transition("booking_in_progress", "approval recorded, booking")
        attempts = 0
        reholds = 0
        while True:
            ok, why = a.matches("book", self.hold["provider_id"], self.hold["slot"],
                                self.hold["quoted_price"], now=self.clock())
            if not ok:
                self.booking_in_progress = None
                self._transition("await_approval", f"approval invalid: {why}")
                return (f"I stopped before booking: {why}. "
                        "Please confirm the current provider, slot and price again.")
            try:
                self._call_tool("book", hold_id=self.hold["hold_id"], idempotency_key=key)
            except ToolError as e:
                if e.code == "timeout":
                    st = self._call_tool("get_booking_status", idempotency_key=key)
                    if st["status"] == "booked":
                        return self._finish_booking(key, st["booking_id"], recovered=True)
                    attempts += 1
                    if attempts > config.MAX_BOOK_RETRIES:
                        self.booking_in_progress = None
                        self._transition("await_approval", "book retries exhausted")
                        return ("The provider's system kept timing out and I verified the "
                                "booking did NOT go through. No charge, no duplicate. "
                                "Your hold may have lapsed - say yes to try once more.")
                    continue
                if e.code == "hold_expired":
                    reholds += 1
                    if reholds > config.MAX_BOOK_RETRIES:
                        self.booking_in_progress = None
                        self._transition("await_approval", "re-hold attempts exhausted")
                        return ("The provider's holds kept expiring before the booking "
                                "could complete, so I stopped. Nothing was booked and "
                                "nothing was charged. Say yes to start over with a "
                                "fresh hold.")
                    try:
                        hold = self._call_tool("hold_slot", provider_id=a.provider_id, slot=a.slot)
                    except ToolError as e2:
                        if e2.code == "slot_unavailable":
                            self.booking_in_progress = None
                            self.approval = None
                            self._transition("collecting", "slot lost on re-hold")
                            return ("Your hold expired and the slot was taken meanwhile. "
                                    "Nothing was booked. Want me to search again?")
                        raise
                    self.hold.update({"hold_id": hold["hold_id"],
                                      "quoted_price": hold["quoted_price"],
                                      "expires_at": hold["expires_at"]})
                    if hold["quoted_price"] != a.price:
                        self.booking_in_progress = None
                        self.approval = None
                        self._transition("await_approval", "price changed on re-hold")
                        return (f"Your hold expired; the new quote is Rs.{hold['quoted_price']} "
                                f"(was Rs.{a.price}), so your earlier confirmation is void. "
                                "Confirm the new price? (yes/no)")
                    continue
                if e.code == "slot_taken":
                    self.booking_in_progress = None
                    self.approval = None
                    self.hold = None
                    self._transition("collecting", "slot taken")
                    return ("That slot was just taken by someone else, so nothing was booked. "
                            "Want me to search again for another slot or provider?")
                raise
            # book() returned without error - verify before claiming success
            st = self._call_tool("get_booking_status", idempotency_key=key)
            if st["status"] == "booked":
                return self._finish_booking(key, st["booking_id"])
            attempts += 1
            if attempts > config.MAX_BOOK_RETRIES:
                self.booking_in_progress = None
                self._transition("await_approval", "unverified booking, retries exhausted")
                return ("The booking couldn't be verified after several attempts, so I'm "
                        "not treating it as confirmed. Nothing else was booked. "
                        "Say yes to try again.")
        # unreachable

    def _finish_booking(self, key, booking_id, recovered=False):
        self.booking_in_progress = None
        self.last_booking_key = key
        name = self.hold["name"] if self.hold else self.approval.provider_id
        slot = self.hold["slot"] if self.hold else self.approval.slot
        price = self.hold["quoted_price"] if self.hold else self.approval.price
        self.memory.set_inferred(self._tech_key(self.slots.get("service")), name)
        if "area" in self.slots:
            self.memory.set_inferred("area", self.slots["area"])
        self._transition("done", "booking verified")
        note = " (verified after a timeout - it had actually gone through)" if recovered else ""
        return (f"Booked{note}: {name} on {slot} for Rs.{price}. "
                f"Booking id {booking_id}. Anything else?")

    # ----------------------------------------------------------------- cancel
    def _start_cancel(self, service):
        bookings = [b for b in self._call_tool("list_bookings", user_id=self.user_id)
                    if b["status"] == "booked"]
        if service:
            bookings = [b for b in bookings if service.split()[0] in (b.get("service") or "")
                        or service.split()[0] in b["provider_id"]]
        if not bookings:
            return "You have no active bookings to cancel."
        b = bookings[0]
        self.pending_action = {"type": "cancel", "booking": b}
        self._transition("await_approval", "cancel proposed")
        return (f"You're about to cancel booking {b['id']} with {b['provider_id']} "
                f"on {b['slot']} (Rs.{b['price']}). Confirm cancellation? (yes/no)")

    def _execute_cancel(self, message):
        b = self.pending_action["booking"]
        self.approval = Approval("cancel", b["provider_id"], b["slot"], b["price"],
                                 message, now=self.clock())
        ok, why = self.approval.matches("cancel", b["provider_id"], b["slot"],
                                        b["price"], now=self.clock())
        if not ok:
            self.pending_action = None
            self._transition("collecting", f"cancel approval invalid: {why}")
            return f"I stopped before cancelling: {why}. Please confirm again."
        key = self._idem_key("cancel", b["id"], "", "")
        last_err = None
        for _ in range(3):  # contract: first call 503s, retry succeeds
            try:
                self._call_tool("cancel", booking_id=b["id"], idempotency_key=key)
                self.pending_action = None
                self._transition("done", "cancelled")
                return f"Cancelled booking {b['id']}. Anything else?"
            except ToolError as e:
                if e.code == "transient_503":
                    last_err = e
                    continue
                if e.code == "not_found":
                    self.pending_action = None
                    self._transition("done", "booking already gone")
                    return "That booking no longer exists - nothing to cancel."
                raise
        self._transition("await_approval", "cancel retries exhausted")
        return ("The provider's system is unavailable right now and the cancellation "
                "did not complete. Your booking is still active. Say yes to retry.")

    # -------------------------------------------------------------- reschedule
    def _start_reschedule(self, new_slots):
        bookings = [b for b in self._call_tool("list_bookings", user_id=self.user_id)
                    if b["status"] == "booked"]
        svc = new_slots.get("service")
        if svc:
            bookings = [b for b in bookings if svc.split()[0] in b["provider_id"]
                        or svc.split()[0] in (b.get("service") or "")]
        if not bookings:
            return "I couldn't find an active booking to move."
        b = bookings[0]
        providers = self._call_tool("search_providers",
                                    service=b.get("service") or "plumber",
                                    area=b.get("area") or self.slots.get("area", "local"),
                                    date_range=self._date_range_from(new_slots))
        provider = next((p for p in providers if p["id"] == b["provider_id"]), None)
        if not provider:
            return "I couldn't fetch that provider's calendar - try again in a bit."
        self.slots.update({k: v for k, v in new_slots.items() if k != "service"})
        slot = self._best_slot(provider)
        if not slot:
            return "No free slots in that window. Suggest another day or time?"
        self.pending_action = {"type": "reschedule", "booking": b, "new_slot": slot}
        self._transition("await_approval", "reschedule proposed")
        return (f"Move booking {b['id']} ({b['provider_id']}) from {b['slot']} "
                f"to {slot}? Confirm? (yes/no)")

    def _date_range_from(self, slots):
        missing = object()
        saved = self.slots.get("date_range", missing)
        self.slots["date_range"] = slots.get("date_range", "this_week")
        try:
            return list(self._date_range())
        finally:
            if saved is missing:
                self.slots.pop("date_range", None)
            else:
                self.slots["date_range"] = saved

    def _execute_reschedule(self, message):
        b = self.pending_action["booking"]
        new_slot = self.pending_action["new_slot"]
        self.approval = Approval("reschedule", b["provider_id"], new_slot, b["price"],
                                 message, now=self.clock())
        ok, why = self.approval.matches("reschedule", b["provider_id"], new_slot,
                                        b["price"], now=self.clock())
        if not ok:
            self.pending_action = None
            self._transition("collecting", f"reschedule approval invalid: {why}")
            return f"I stopped before rescheduling: {why}. Please confirm again."
        key = self._idem_key("reschedule", b["id"], new_slot, b["price"])
        try:
            self._call_tool("reschedule", booking_id=b["id"], new_slot=new_slot,
                            idempotency_key=key)
        except ToolError as e:
            if e.code == "slot_conflict":
                self.approval = None
                self.pending_action = None
                self._transition("collecting", "reschedule conflict")
                return (f"That new slot conflicts with another job, so nothing changed - "
                        f"your booking still stands at {b['slot']}. Want a different time?")
            raise
        self.pending_action = None
        self._transition("done", "rescheduled")
        return f"Done - booking {b['id']} moved to {new_slot}. Anything else?"

    # ------------------------------------------------------------------- misc
    def _list_bookings(self):
        bookings = self._call_tool("list_bookings", user_id=self.user_id)
        if not bookings:
            return "You have no bookings yet."
        lines = ["Your bookings:"]
        for b in bookings:
            lines.append(f"- {b['id']}: {b['provider_id']} on {b['slot']} "
                         f"(Rs.{b['price']}) - {b['status']}")
        return "\n".join(lines)

    def _memory_view(self):
        facts = self.memory.view()
        if not facts:
            return "I don't remember anything yet."
        lines = ["Here's what I remember:"]
        for k, f in facts.items():
            tag = "you told me" if f["source"] == "stated" else (
                "my guess - confirmed" if f["confirmed"] else "my guess - NOT confirmed, unused")
            lines.append(f"- {k.replace('_', ' ')}: {f['value']} ({tag})")
        lines.append("Say 'forget <thing>' to make me drop any of these.")
        return "\n".join(lines)
