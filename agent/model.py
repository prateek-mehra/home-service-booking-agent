"""Pluggable model layer.

The default `ScriptedModel` is a deterministic, offline planner: it parses the
user's message into an intent + slot updates and drafts replies from workflow
state. It never treats tool free text (reviews) as instructions - it only reads
structured fields - so prompt injection in reviews cannot change intent by
construction. An LLM adapter can implement the same `interpret` interface; the
eval suite runs against this scripted model, per the assignment's
"scripted or recorded model" requirement.
"""
import re
import time

SERVICES = {
    "plumber": "plumber", "plumbing": "plumber", "tap": "plumber",
    "electrician": "electrician", "wiring": "electrician",
    "ac": "ac servicing", "ac servicing": "ac servicing", "air conditioner": "ac servicing",
    "cleaner": "cleaning", "cleaning": "cleaning", "deep clean": "cleaning",
    "carpenter": "carpenter", "painter": "painter", "pest": "pest control",
}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _est_tokens(text):
    return max(1, len(text) // 4)


class ModelResult:
    def __init__(self, intent, slots=None, payload=None, raw=""):
        self.intent = intent          # see Conversation._dispatch for intents
        self.slots = slots or {}      # extracted slot updates
        self.payload = payload or {}  # intent-specific data
        self.raw = raw


class ScriptedModel:
    name = "scripted-v1"

    def interpret(self, message, state_summary, tracer=None):
        t0 = time.time()
        result = self._parse(message, state_summary)
        if tracer:
            tracer.model_call(
                model=self.name, latency_ms=round((time.time() - t0) * 1000, 1),
                prompt_tokens=_est_tokens(message) + _est_tokens(state_summary),
                completion_tokens=_est_tokens(result.raw),
                cost_usd=0.0,
                summary=f"intent={result.intent} slots={sorted(result.slots)}",
            )
        return result

    # ---------------- parsing ----------------
    def _parse(self, message, state_summary):
        m = message.strip()
        low = m.lower()

        # memory commands
        if re.search(r"\bwhat do you remember\b|\bshow (me )?your memory\b|\bmy memory\b", low):
            return ModelResult("memory_view", raw=m)
        forget = re.search(r"\bforget\s+(?:my\s+)?([\w\s]+)$", low)
        if forget and "booking" not in low:
            return ModelResult("memory_forget", payload={"key": self._memory_key(forget.group(1))}, raw=m)
        remember = re.search(r"\bremember (?:that )?(?:my\s+)?([\w\s]+?)\s+(?:is|are)\s+(.+)$", low)
        if remember:
            return ModelResult("memory_set",
                               payload={"key": self._memory_key(remember.group(1)),
                                        "value": remember.group(2).strip()}, raw=m)

        # confirmations / rejections
        if re.fullmatch(r"(yes+|yeah|yep|confirm|confirmed|book it|go ahead|ok|okay|sure|do it)[.!]?", low):
            return ModelResult("confirm", raw=m)
        if re.fullmatch(r"(no|nope|cancel that|don't|do not|stop)[.!]?", low):
            return ModelResult("reject", raw=m)
        if re.search(r"\bshow (my )?bookings?\b|\blist (my )?bookings?\b", low):
            return ModelResult("list_bookings", raw=m)

        # cancellations / reschedules of existing bookings
        if re.search(r"\bcancel\b", low):
            svc = self._service(low)
            return ModelResult("cancel_request", payload={"service": svc}, raw=m)
        mv = re.search(r"\b(?:move|reschedule|shift)\b", low)
        if mv:
            slots = self._slots(low)
            svc = self._service(low)
            if svc:
                slots["service"] = svc
            return ModelResult("reschedule_request", slots=slots, raw=m)

        # provider selection
        pick = re.search(r"\b(?:the )?(first|second|third|1st|2nd|3rd|option [123]|[123])\b", low)
        if pick and ";offering;" in f";{state_summary}":
            word = pick.group(1)
            idx = {"first": 0, "1st": 0, "option 1": 0, "1": 0,
                   "second": 1, "2nd": 1, "option 2": 1, "2": 1,
                   "third": 2, "3rd": 2, "option 3": 2, "3": 2}[word]
            return ModelResult("choose_provider", payload={"index": idx}, raw=m)
        name_pick = re.search(r"\b(?:go with|pick|choose|book)\s+([A-Z][\w]+(?:\s+[A-Z][\w]+){0,3})", m)
        if name_pick and ";offering;" in f";{state_summary}":
            return ModelResult("choose_provider", payload={"name": name_pick.group(1)}, raw=m)
        if re.search(r"\bsame (electrician|plumber|technician|provider|guy) as last time\b", low):
            slots = self._slots(low)
            svc = self._service(low)
            if svc:
                slots["service"] = svc
            return ModelResult("use_last_provider", slots=slots, raw=m)

        # default: a request / correction carrying slot info
        slots = self._slots(low)
        svc = self._service(low)
        if svc:
            slots["service"] = svc
        is_correction = bool(re.search(r"\bactually\b|\binstead\b|\bmake it\b|\bchange\b|\brather\b", low))
        if slots:
            return ModelResult("correct" if is_correction else "provide", slots=slots, raw=m)
        return ModelResult("unknown", raw=m)

    def _memory_key(self, phrase):
        p = phrase.strip().lower()
        if "area" in p or "location" in p or "locality" in p:
            return "area"
        if "technician" in p or "provider" in p or "electrician" in p or "plumber" in p:
            return "preferred_technician"
        if "time" in p or "morning" in p or "evening" in p:
            return "earliest_time"
        return p.replace(" ", "_")

    def _service(self, low):
        for kw in sorted(SERVICES, key=len, reverse=True):
            if re.search(rf"\b{re.escape(kw)}\b", low):
                return SERVICES[kw]
        return None

    def _slots(self, low):
        slots = {}
        budget = re.search(r"(?:under|below|max(?:imum)?|within)\s*(?:rs\.?|₹|inr)?\s*([\d,]+)", low)
        if budget:
            slots["budget"] = int(budget.group(1).replace(",", ""))
        area = re.search(r"\b(?:in|near|around)\s+([a-z][a-z\s]+?)(?:\s*,|\s+this\b|\s+next\b|\s+under\b|\s+morning|\s+evening|\s+afternoon|$)", low)
        if area and area.group(1).strip() not in {"my", "the"}:
            slots["area"] = area.group(1).strip().title()
        if "this week" in low:
            slots["date_range"] = "this_week"
        elif "next week" in low:
            slots["date_range"] = "next_week"
        elif "tomorrow" in low:
            slots["date_range"] = "tomorrow"
        elif "today" in low:
            slots["date_range"] = "today"
        else:
            for i, d in enumerate(WEEKDAYS):
                if d in low:
                    slots["date_range"] = d
                    break
        if re.search(r"\bmornings?\s*only\b|\bmorning\b", low):
            slots["time_window"] = "morning"
        elif re.search(r"\bafternoon\b", low):
            slots["time_window"] = "afternoon"
        elif re.search(r"\bevening\b", low):
            slots["time_window"] = "evening"
        nb = re.search(r"\bnot before\s+(\d{1,2})(?:\s*(?:a\.?m\.?|am))?", low)
        if nb:
            slots["earliest_hour"] = int(nb.group(1))
        return slots
