# Attack campaign guides

These guides explain the purpose and observed result of every adversarial case
in the three `attacks/` campaigns. They complement the raw JSON records and the
campaign methodology notes; they do not replace the executable probes.

- [Round 1: initial probes](attack-round-1.md) — 50 cases across reviews, user
  input, memory, session state and provider results.
- [Round 2: independent follow-up](attack-round-2.md) — 50 cases, including
  new attack phrasings and regression canaries.
- [Round 3: review injections](attack-round-3.md) — 50 independently varied
  review payloads tested against an existing booking and a pending proposal.

A passing result means the case's stated invariant held in the recorded run.
Round 1 case E07 documents the local-session-file trust limitation; it is not a
claim that a filesystem attacker is prevented from forging local state. The
review-injection campaigns run against the deterministic scripted model, which
does not parse review text. They do not measure prompt-injection resistance of
a live LLM.
