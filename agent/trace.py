"""One JSONL trace per run: state transitions, model calls, tool calls with
sanitised results, latency, tokens and cost where available. No secrets."""
import json
import os
import time
import uuid

from .sanitize import quote_untrusted

# fields whose values are untrusted free text
_UNTRUSTED_FIELDS = {"reviews", "review", "notes", "free_text"}


def _sanitise(value):
    if isinstance(value, dict):
        return {k: ([quote_untrusted(v) for v in val] if k in _UNTRUSTED_FIELDS and isinstance(val, list)
                    else quote_untrusted(val) if k in _UNTRUSTED_FIELDS
                    else _sanitise(val))
                for k, val in value.items()}
    if isinstance(value, list):
        return [_sanitise(v) for v in value]
    return value


class TraceLogger:
    def __init__(self, trace_dir, run_id=None):
        self.run_id = run_id or f"run-{uuid.uuid4().hex[:10]}"
        os.makedirs(trace_dir, exist_ok=True)
        self.path = os.path.join(trace_dir, f"{self.run_id}.jsonl")
        self._fh = open(self.path, "a", buffering=1)

    def _emit(self, event_type, **data):
        self._fh.write(json.dumps({
            "ts": round(time.time(), 3), "run_id": self.run_id,
            "event": event_type, **_sanitise(data),
        }) + "\n")

    def state_transition(self, from_state, to_state, reason):
        self._emit("state_transition", from_state=from_state, to_state=to_state, reason=reason)

    def model_call(self, model, latency_ms, prompt_tokens, completion_tokens, cost_usd, summary):
        self._emit("model_call", model=model, latency_ms=latency_ms,
                   prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                   cost_usd=cost_usd, summary=summary)

    def tool_call(self, tool, args, result=None, error=None, latency_ms=0):
        self._emit("tool_call", tool=tool, args=args, result=result,
                   error=error, latency_ms=round(latency_ms, 1))

    def user_message(self, text):
        self._emit("user_message", text=text)

    def agent_message(self, text):
        self._emit("agent_message", text=text)

    def close(self):
        self._fh.close()
