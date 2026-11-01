# Test guide

The tests check behavior and safety properties in the local implementation.
They run with Python's standard `unittest` runner and use the seeded mock
provider API; they do not call a live model or real service.

Run the full suite from the repository root:

```bash
python3 -m unittest discover -s tests
```

The suite is grouped by the kind of behavior it protects:

| Test file | What it checks |
|---|---|
| `test_approval.py` | An approval matches only the action, provider, slot and price the user confirmed, and expires after its time limit. |
| `test_memory.py` | User-stated preferences persist and can be used; inferred preferences need confirmation; forgotten facts stay blocked; inferred facts cannot replace stated facts. |
| `test_mocks.py` | The mock API is reproducible, returns the required provider details, honors configured prices and failures, reports booking status authoritatively, and models cancel and reschedule failures. |
| `test_regressions.py` | Regression checks for bugs found while building: retrying a verified failure, preserving service and price across restarts, voiding corrected approvals, respecting time windows, avoiding duplicate slots, bounding hold retries, and checking cancel/reschedule outcomes. |
| `test_injection_regressions.py` | Round-one guards for review sanitization, fake confirmations, negated cancel requests, memory abuse, corrupt state files, and malformed provider output. |
| `test_injection_regressions2.py` | Round-two guards for more parser phrasings, memory poisoning, tool-call limits, misleading tool responses, malformed approval records, trace redaction, and display hygiene. |

Tests assert state and side effects as well as replies. For example, they check
that no booking exists without a valid approval, a price change invalidates an
approval, and a timeout does not cause a duplicate. Tests use local isolated
state and do not depend on external services.

The 50-case third review-injection campaign is a separate adversarial runner,
not part of `unittest`. Run it with `python3 -m attacks.round3_campaign`; its
recorded results are in `attacks/round3_final.json`.
