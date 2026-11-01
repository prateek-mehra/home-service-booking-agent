# High-level design

The conversation state machine owns workflow decisions and enforces approval
before any booking change. The model interprets the user's message; it cannot
call provider tools or grant approval. Tool results, memory, session state and
traces are handled by separate local components.

```mermaid
flowchart TD
    U[User] --> CLI[CLI]
    CLI --> C[Conversation state machine]
    C <--> M[Scripted model<br/>intent and slot extraction]
    C --> F{Required details complete?}
    F -->|No| Q[Ask for next missing detail]
    Q --> U
    F -->|Yes| API[Mock provider API]
    API --> C
    C --> P[Present provider, slot and quoted price]
    P --> A{User explicitly confirms?}
    A -->|No or correction| C
    A -->|Yes| G[Code checks approval record]
    G -->|Book| B[Book, then verify status]
    G -->|Cancel or reschedule| X[Apply action, then verify]
    B --> API
    X --> API
    B --> C
    X --> C
    C --> U
    C --> S[(session.json)]
    C --> MEM[(memory.json)]
    API --> DB[(mock_db.json)]
    C --> T[TraceLogger<br/>one JSONL trace per run]
```

## Responsibilities

- **CLI** accepts a user's message and displays the agent's response.
- **Conversation state machine** collects missing details, filters provider
  offers against constraints, tracks the pending action, and handles booking,
  cancellation and rescheduling.
- **Scripted model** turns text into intent and slots. It has no direct tool
  access; the workflow code controls all side effects.
- **Approval record** binds an explicit confirmation to the exact action,
  provider, slot and price. Any material change requires confirmation again.
- **Mock provider API** implements the fixed local contract and reproducible
  failure cases. It persists its mock booking state in `mock_db.json`.
- **Memory store** persists user-stated and inferred preferences separately.
  Inferred facts are unused until confirmed; forgotten facts are tombstoned.
- **Session store** saves workflow state and the in-flight booking key so a
  restart can check the outcome before attempting recovery.
- **Trace logger** writes one sanitized JSONL trace per run with transitions,
  model calls, tool calls, results and timing.

Provider reviews enter as untrusted data. They do not enter the model's intent
parser or bypass the approval check. The current model is deterministic and
offline; a future model adapter must retain the same code-enforced boundaries.
