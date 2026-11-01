"""Interactive chat CLI. Everything local and offline; a fresh trace per session.

--show-trace prints each trace event beside the chat as it happens - built for
the demo video's "chat and trace side by side" requirement.
"""
import argparse
import json
import os
import sys

from .conversation import Conversation

SCENARIO_PRESETS = {
    "timeout-booked": {"book_outcomes": {"*": "timeout_booked"}},
    "timeout-not-booked": {"book_outcomes": {"*": "timeout_not_booked"}},
    "slot-taken": {"book_outcomes": {"*": "slot_taken"}},
}


def _drain_trace(path, pos):
    """Read new trace lines since pos; returns (rendered_lines, new_pos)."""
    if not os.path.exists(path):
        return [], pos
    with open(path) as f:
        f.seek(pos)
        lines = [json.loads(l) for l in f if l.strip()]
        pos = f.tell()
    out = []
    for e in lines:
        ev = e["event"]
        if ev == "state_transition":
            out.append(f"  state: {e['from_state']} -> {e['to_state']} ({e['reason']})")
        elif ev == "model_call":
            out.append(f"  model: {e['summary']} [{e['latency_ms']}ms, "
                       f"{e['prompt_tokens']}+{e['completion_tokens']} tok, ${e['cost_usd']}]")
        elif ev == "tool_call":
            res = f"error={e['error']}" if e.get("error") else "ok"
            out.append(f"  tool: {e['tool']}({json.dumps(e['args'])}) -> {res} [{e['latency_ms']}ms]")
    return out, pos


def main(argv=None):
    p = argparse.ArgumentParser(description="Home-service booking agent (local, offline)")
    p.add_argument("--user-id", default="user")
    p.add_argument("--data-dir", default=".agent_data")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--preset", choices=sorted(SCENARIO_PRESETS),
                   help="named failure scenario for the mock provider API")
    p.add_argument("--scenario", default="{}",
                   help="JSON scenario overrides for the mock provider API")
    p.add_argument("--show-trace", action="store_true",
                   help="print trace events beside the chat")
    args = p.parse_args(argv)

    scenario = SCENARIO_PRESETS.get(args.preset, {})
    scenario.update(json.loads(args.scenario))
    conv = Conversation(user_id=args.user_id, data_dir=args.data_dir,
                        seed=args.seed, scenario=scenario)
    trace_pos_holder = [0]

    def reply(text):
        print(f"Agent: {text}")
        if args.show_trace:
            lines, pos = _drain_trace(conv.tracer.path, trace_pos_holder[0])
            trace_pos_holder[0] = pos
            for l in lines:
                print(f"  [trace]{l}")

    if conv._resume_notice:
        reply(conv._resume_notice)
    print("Agent: Hi! I can book, move or cancel home-service visits.")
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            continue
        if line.strip().lower() in {"quit", "exit"}:
            print("Agent: Bye!")
            break
        reply(conv.handle(line))
    conv.tracer.close()


if __name__ == "__main__":
    main()
