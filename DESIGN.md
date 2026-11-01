# Design note - home-service booking agent

## State model

`Conversation` owns an explicit state machine: `collecting -> offering ->
await_approval -> done`, with bounded booking recovery and cancel/reschedule
flows. It collects `service`, `area` and `date_range`, then asks only for the
first missing fact. Corrections merge into the existing slots. Budget, time
window and earliest-hour constraints filter offers; the agent never falls back
to a slot outside a hard constraint. A correction while approval is pending
voids the hold and proposal. State transitions include a trace reason.

## Approval enforcement

`agent/approval.py` stores an approval record; model output and tool arguments
cannot substitute for it. The user sees the exact action, provider, slot and
price before confirming. The record fingerprints
`action|provider|slot|price`, expires after 15 minutes, and is checked before
booking, cancellation and rescheduling. A changed provider, slot or price
(including a re-quote after an expired hold) invalidates approval and requires
a new confirmation. Evals cover price changes, slot corrections and actions
without a valid approval.

## Idempotency and recovery

The booking idempotency key is deterministic for one intent and includes the
user, action, provider, slot, price and hold id. It is saved in `session.json`
before `book()` so a restart can recover the same attempt; a fresh hold creates
a fresh intent. On timeout, the agent first checks `get_booking_status`, the
source of truth. It reports success only for `booked`, retries only after a
verified `not_found` or `failed`, and stops at two retries. Repeated
confirmations and restarts therefore cannot create duplicate bookings. An
expired hold is renewed, and a changed quote invalidates the old approval.

## Untrusted tool output

Provider reviews are data, never instructions. The scripted model reads
structured provider fields and does not parse review text. Reviews are marked
`[provider review]`, bounded, stripped of control characters and redacted
before display or tracing. Approval checks and idempotency remain outside the
model layer. This proves the current offline workflow resists review injection;
it does not claim real-LLM prompt-injection coverage. Any future LLM adapter
must preserve the same untrusted-text boundary and pass model-level attack
tests.

## Memory

`memory.json` labels each preference `stated` or `inferred`. Stated facts can
be used immediately; inferred facts are not used until the user confirms them.
Users can view provenance, correct a value by stating it again, or forget one
fact or all facts. Forgetting leaves a tombstone, preventing reuse or
re-inference. A direct request such as “same plumber as last time” uses booking
history for that request; it does not resurrect a forgotten preference.

## Bounded execution and traces

Each run is limited to 30 model turns, 40 tool calls, 300 seconds and two book
retries. Limits are enforced in the model/tool wrappers, including mid-turn.
Exhaustion and unexpected errors return a clear message about what did or did
not happen.

Each run writes one JSONL trace with state transitions, model calls, tool names
and arguments, sanitised results, errors and latency. Token and cost fields are
included when available; the scripted model reports zero. Secrets are
redacted. The checked-in timeout-after-success trace and the generated
injection campaigns provide concrete examples.

Timeout recovery excerpt:

```
book(key=...) -> timeout
get_booking_status(key=...) -> booked, booking_id=...
state: booking_in_progress -> done (booking verified)
```

The agent reports success only after the status check confirms the booking.

## Trust boundary and what breaks first at 100x

Local JSON files are trusted operator state; someone who can edit them can
forge an approval. Signing or moving them behind a protected persistence layer
is future work. Three offline injection rounds cover user messages, memory,
workflow state, reviews and malformed tool results; network attacks,
multi-user concurrency and real-LLM prompt attacks remain untested.

At 100x, single-writer JSON persistence will fail first under concurrent
sessions; move it to SQLite or a transactional database. The keyword-based
scripted model will then limit language coverage; replace it behind the
`interpret()` interface while keeping approvals and idempotency in code. Long
human approval delays can also expire holds; expose a countdown and renew holds
while preserving re-approval on any price change.
