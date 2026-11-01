# Design note - home-service booking agent

## State model

An explicit state machine, not a prompt: `collecting -> offering ->
await_approval -> done`, plus cancel/reschedule sub-flows and an `aborted`
terminal state. Required slots are `service`, `area`, `date_range`; optional
ones are `budget`, `time_window`, `earliest_hour`, preferences. Hard constraints (time window, earliest hour, budget) are enforced, not
fallen back from: providers without a fitting slot are excluded from the
shortlist, and if none fit the agent says so instead of quietly booking a slot
outside the window. Every turn
updates slots and asks for exactly the first missing one - known facts
(including confirmed memory) are never re-asked. A correction merges into the
slot map, so every other detail survives; in `await_approval` a correction
additionally voids the pending hold and approval. Every transition is traced
with a reason.

## Approval enforcement

Approval is a record the code checks (`agent/approval.py`), never something
inferred from model output or tool args. Before a hold is placed the agent
quotes the exact action, provider, slot and price; the user's "yes" creates an
`Approval` whose fingerprint is `sha256(action|provider|slot|price)`. The
booking path re-verifies the fingerprint before **every** attempt. Any change
to provider, slot or price - including a provider re-quoting on a re-hold -
mismatches the fingerprint, voids the approval, and forces re-confirmation.
Approvals also expire after 15 minutes. Scenario: `price change after approval
voids it` proves nothing is booked when the quote moves.

## Idempotency and recovery

The booking key is deterministic per booking intent:
`sha256(user|action|provider|slot|price|hold_id)` - stable across retries and
restarts of one intent, but unique across intents, so re-asking for the same
provider/slot/price after a cancellation is a NEW booking, never a replay of
the dead one. It is persisted to `session.json` **before** the first `book()`
call, so a crash or restart never loses it. `book()` replays safely: same key returns the
same booking; a verified `failed` key is retryable (this was real bug #1).
On `timeout` the agent never retries or reports blindly: it calls
`get_booking_status(key)` - the authoritative source - and only then finishes
(recovered success), retries (verified `not_found`/`failed`, bounded by
`MAX_BOOK_RETRIES=2`), or reports an honest failure. Success is never claimed
without a `booked` status. Repeated "yes" hits the same status check, so it
can never duplicate. On restart, `Conversation.__init__` finds a persisted
in-flight key, checks status first, and either completes (no duplicate) or
asks to re-confirm. `hold_expired` triggers a re-hold; a changed re-quote
voids the approval.

## Untrusted tool output

Reviews are data. The model layer only reads structured fields (price, rating,
slots); free text is wrapped by `sanitize.quote_untrusted` before it can reach
a prompt, a trace, or the user (rendered `[provider review] ...`), and is
never parsed for instructions. The injection scenario proves the surrounding
workflow cannot be moved by review text - no intent change, no permission, no
approval, no side effects. Precision note: against the scripted model this is
partly by construction (it never parses free text at all); the load-bearing
defenses for any future LLM adapter are the ones outside the model - approval
fingerprint checks, idempotency keys and sanitisation - and the adapter
contract requires the same quoting discipline before any free text enters a
prompt.

## Memory

`memory.json` persists across sessions. Every fact is `stated` (usable
immediately) or `inferred` (unusable until explicitly confirmed; the agent
asks, e.g. "I've noticed you booked X before..."). `forget` removes the fact
and leaves a tombstone so it is never reused or re-inferred. Users can view
("what do you remember", with provenance labels), correct (re-state) and
forget any fact.

Scope of a tombstone: `forget` is a block on remembered preferences, not a
blocklist on reality. An explicit "same plumber as last time" consults the
provider's own booking records (`list_bookings`) and uses that booking's
identity directly - it never reads or resurrects the tombstoned preference,
and a fresh request that happens to match a forgotten fact is a new search,
not remembered memory. So forgetting "I prefer Suresh" stops proactive use of
that preference; it does not stop the user from explicitly asking for Suresh
again.

## Trust boundary and untested surfaces

The agent trusts its own local files (`session.json`, `memory.json`, mock DB):
a local attacker who can write them can forge session state (campaign case
E07) - they are operator-owned state, not a defense boundary, and hardening
them (signing, permissions) is future work. Two injection-campaign rounds
(attacks/, 50 cases each, all passing; round 2 = 46 new attacks + 4 round-1
regression canaries) cover user-message, memory, state and hostile-tool-output
surfaces. Honestly out of scope: review-text immunity is partly by construction
(the scripted parser never reads free text); real-LLM prompt-level attacks,
network attacks (there is no network) and multi-user concurrency are untested.

## Bounded execution

Hard caps per run: 30 model turns, 40 tool calls, 300s wall-clock, 2 book
retries. Every exit - limits, exhausted retries, unknown errors - ends in a
clear user message stating what did and did not happen. Tool calls go through
one wrapper that enforces the cap and traces latency.

## Traces / observability

One JSONL trace per run (`traces/<run_id>.jsonl`): state transitions with
reasons, model calls with latency + token/cost fields (0 for the scripted
model), tool calls with args, sanitised results and errors. No secrets; review
text is tagged untrusted. Checked-in example:
`traces/timeout-after-success.jsonl`.

### Annotated excerpt - timeout-after-success

```
book(hold_id=hold-9912..., idempotency_key=0e10...)   -> error: "timeout"
# The agent does NOT retry or report yet - the outcome is unknown.
get_booking_status(idempotency_key=0e10...)            -> booked, booking_id=bk-6b9f...
# The authoritative source says it actually booked. The agent reports
# success from THIS, not from the timed-out call, and never rebooks.
state: booking_in_progress -> done (reason: "booking verified")
```

## What breaks first at 100x

1. **File-based persistence** (`session.json`, `memory.json`, mock DB):
   single-writer JSON files corrupt under concurrent sessions. Move to SQLite
   (one `UPDATE` per transition) - the persistence layer is already isolated.
2. **The scripted model** is the real ceiling: keyword parsing won't scale to
   messy phrasing. The `interpret()` interface is the swap point for an LLM;
   the approval/idempotency invariants live outside it deliberately, so a
   smarter model cannot weaken them. That's also why evals assert invariants,
   not wording.
3. **Hold TTL vs. human think-time**: users who take >10 min to confirm will
   hit re-holds (and possible re-quotes) often; I'd surface a countdown and
   auto-refresh holds while the user is still deciding.
