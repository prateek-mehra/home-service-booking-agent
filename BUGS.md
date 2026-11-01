# Three real bugs hit while building

Each was caught by the eval suite failing, fixed, and now guarded by a
regression test in `tests/test_regressions.py`.

## Bug 1 - a verified-failed booking attempt could never be retried

**What broke:** the timeout-then-retry eval failed with `slot_taken` on the
retry even though nothing was booked. The mock's idempotent-replay check
treated *any* prior record for an idempotency key that wasn't `booked` as
`slot_taken` - including `failed` (a timeout where nothing booked). The
agent's correct recovery path (status says not_booked -> retry) was therefore
impossible: every retry raised `slot_taken`.
**Why:** the replay branch conflated "this key resolved as a conflict" with
"this key resolved as a failure". Failures are retryable; conflicts aren't.
**Fix:** only `booked` short-circuits to a replay, only `slot_taken` replays
as a conflict; `failed` falls through to a fresh attempt.
**Guard:** `TestBug1FailedAttemptRetryable`.

## Bug 2 - bookings lost their service, breaking cancel/reschedule

**What broke:** the cancel eval got "You have no active bookings to cancel"
right after a successful booking. `cancel my plumber booking` filters bookings
by service, but `hold_slot` never captured the service, so every booking
record had `service: ""` and the filter matched nothing.
**Why:** the service lived only in the search results; the hold record - the
thing a booking is built from - was never given it.
**Fix:** `hold_slot` copies the service from the provider catalog into the
hold, and `_record_booking` persists it on the booking.
**Guard:** `TestBug2BookingKeepsService`.

## Bug 3 - a restart fabricated a price change and voided a valid approval

**What broke:** the restart-mid-booking eval recovered with "the price changed
while we were away" and zero bookings. The hold's `quoted_price` was based on
the in-memory search catalog; after a process restart the catalog was empty
and a hash-based fallback produced a *different* price for the same provider.
The approval check (correctly) refused to book - an approval voided by phantom
data. Same class of issue made list price and quote disagree within a single
session.
**Why:** two sources of truth for a provider's price: the per-search catalog
and a hash fallback.
**Fix:** one deterministic `_base_price(provider_id)` used by both
`search_providers` and `hold_slot`; quotes are stable across searches and
restarts, and only a configured `hold_price_delta` moves them.
**Guard:** `TestBug3PriceStableAcrossRestart`.

### Bonus bug - corrections before "yes" didn't void the pending hold

The slot-change eval showed a correction between the hold proposal and the
user's "yes" silently kept the hold live, because the void path was gated on
an existing `Approval` record, which is only created *at* "yes". Fixed by
keying the void on the workflow state (`await_approval`), not the record's
existence. Guard: `TestBug4CorrectionBeforeYesVoidsHold`.

---

# Appendix: independent adversarial review (checkpoint 1) - findings and fixes

After the suite went green, an independent reviewer probed beyond the supplied
tests. Fourteen findings came back; all user-visible ones are fixed and guarded
by the regression classes `TestReviewFinding*` in `tests/test_regressions.py`.
Kept here as the honest bug history.

1. **HIGH - hard time-window constraint silently violated.** `_best_slot`
   fell back to the first slot of ANY time when none matched "mornings only";
   the agent then booked it as if it fit. Fixed: no fallback, offers are
   filtered to providers with a fitting slot, and if none fit the agent says
   so and offers to relax the constraint.
2. **Restarted recovery re-hold lost `service`** (Bug 2 recurring across
   restarts: catalog was memory-only). Fixed: the catalog persists in the mock
   DB; re-holds after restart keep service and area.
3. **Status truth after cancel.** (a) `get_booking_status` claimed "booked"
   for a since-cancelled booking; (b) the agent could then report a cancelled
   booking as freshly booked. Fixed in the mock (cancelled no longer reads as
   booked), in the agent (repeated-yes cross-checks `list_bookings`), and in
   the idempotency scheme: the key now binds the hold id, so re-asking after a
   cancellation is a genuinely new booking, never a replay.
4. **Spec example 3 broken end-to-end** ("book the same electrician as last
   time, but not before 10 a.m."): slots were dropped on that intent,
   preferences weren't service-scoped (a plumber was offered for an
   electrician request), the say-so prompt hijacked and discarded the request,
   and a confirmed preference never influenced the shortlist. Fixed: the
   intent carries its slots, preferences are keyed per service, the request
   resumes after the confirmation answer, and a confirmed preferred technician
   is auto-selected from the search results.
5. **hold_expired retry storm** bypassed the book-retry bound (one "yes" could
   burn all 40 tool calls). Fixed: re-holds count against a dedicated cap with
   a clear stop message.
6. **Same-slot double booking after a re-quote** (key included price; mock had
   no slot uniqueness). Fixed: the mock rejects booking a slot with an active
   booking (slot_taken); with the hold-bound key, a re-quote is also a new
   approval anyway.
7. **Wall-clock bound only checked between turns.** Fixed: also enforced in
   the tool-call wrapper, mid-turn.
8. **"offering" token always present** in the model's state summary, so bare
   "1" parsed as a provider pick in any state. Fixed: exact-token match.
9. **Persisted "aborted" state was dead weight** across restarts. Fixed:
   resets to collecting on load (per-run caps restart with the process).
10. **Unhandled-exception path could leave the user with no message**
    (`_date_range_from` restored a missing slot as None; no catch-all).
    Fixed: sentinel restore + a catch-all in `handle()` that always returns a
    clear "nothing was booked/changed" message and traces the error.
11. **Cancel/reschedule approvals were decorative** (created, never checked).
    Fixed: both paths run `Approval.matches(...)` before touching the tool.
12. **Injection eval precision.** True and now stated plainly in DESIGN.md:
    against the scripted model, injection immunity is by construction (it
    never parses free text); the eval proves the surrounding workflow can't be
    moved by review text, and the structural defenses (approval, idempotency,
    sanitisation) sit outside any model. The LLM adapter contract requires the
    same quoting discipline.
13. **Eval report reproducibility:** volatile ids in "actual" cells are now
    normalised (`bk-<id>`), so the generated report is stable across runs.
14. **Cancel idempotency nits:** `cancel()` now honours its idempotency key
    and persists attempt counts across restarts.
