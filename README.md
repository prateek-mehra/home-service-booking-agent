# Home-Service Booking Agent (Orange Invest Founding AI Engineer assignment)

A chat agent that books, reschedules and cancels home-service errands against a
mocked provider API. It never double-books, never acts without approval, and
never gets talked into anything by a provider review.

Pure Python 3.10+, standard library only. Everything runs locally and offline.
(The agent, mocks, tests and evals are stdlib-only. Only the optional demo
recording helper `scripts/record_demo.py` needs Pillow to render frames; it is
not part of the agent or required to run anything above.)

## Run it (3 commands)

```bash
python -m unittest discover -s tests   # 1. 65 unit + regression tests
python -m evals.run                    # 2. 23 eval scenarios -> eval_report.md
python -m attacks.campaign            # 3. 50-attack injection campaign (round 1)
python -m agent                        # 4. chat with the agent
```

Try in the chat:

- `Find a plumber for my kitchen tap in Marathahalli this week, mornings only, under 1500`
- `1` then `yes`
- `show my bookings` / `cancel my plumber booking` / `move my plumber booking to saturday`
- `what do you remember` / `forget my area`

Useful flags: `python -m agent --data-dir .agent_data --seed 42 --scenario '{"book_outcomes": {"*": "timeout_booked"}}'`

Presets (`--preset`) name canned scenarios used in the demo video:
`timeout-booked` (book() times out but actually booked - watch the agent
verify via get_booking_status instead of retrying), `timeout-not-booked`,
`slot-taken`. Example: `python -m agent --preset timeout-booked --show-trace`.

## Layout

- `agent/mock_tools.py` - the fixed tool contract, seeded and scenario-configurable
- `agent/conversation.py` - the workflow state machine and all enforced invariants
- `agent/approval.py` - approval records (bound to action + provider + slot + price)
- `agent/memory.py` - persistent preferences, stated vs inferred, forget with tombstones
- `agent/model.py` - pluggable model layer; default is a deterministic scripted
  model (the evals run against it, per the offline-reproducibility requirement);
  an LLM adapter can implement the same `interpret()` interface
- `agent/trace.py` - one JSONL trace per run
- `evals/` - scripted-user scenarios, generated `eval_report.md`
- `tests/` - unit + regression tests (`BUGS.md` describes the regressions)
- `attacks/` - round-1 injection campaign: 50 attacks, full metadata, before/after results and the fixes they drove
- `traces/timeout-after-success.jsonl` - checked-in end-to-end trace
- `DESIGN.md` - design note; `BUGS.md` - three real bugs hit while building
