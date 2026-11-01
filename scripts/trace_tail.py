"""Live trace viewer: polls a traces dir, pretty-prints new events as they land."""
import glob
import json
import os
import sys
import time


def fmt(e):
    ev = e.get("event")
    if ev == "user_message":
        return f"USER   > {e['text']}"
    if ev == "agent_message":
        return f"AGENT  > {e['text'][:110]}"
    if ev == "state_transition":
        return f"STATE    {e['from_state']} -> {e['to_state']}  ({e['reason']})"
    if ev == "model_call":
        return (f"MODEL    {e['summary']}  [{e['latency_ms']}ms, "
                f"{e['prompt_tokens']}+{e['completion_tokens']} tok, ${e['cost_usd']}]")
    if ev == "tool_call":
        args = json.dumps(e.get("args", {}))
        if e.get("error"):
            return f"TOOL     {e['tool']}({args}) -> ERROR {e['error']}  [{e['latency_ms']}ms]"
        res = json.dumps(e.get("result"))[:100]
        return f"TOOL     {e['tool']}({args}) -> {res}  [{e['latency_ms']}ms]"
    return ev


def main(trace_dir):
    print(f"watching {trace_dir} ...", flush=True)
    path, pos = None, 0
    while True:
        files = sorted(glob.glob(os.path.join(trace_dir, "*.jsonl")),
                       key=os.path.getmtime)
        if files and files[-1] != path:
            path, pos = files[-1], 0
            print(f"--- tracing {os.path.basename(path)} ---", flush=True)
        if path and os.path.exists(path):
            with open(path) as f:
                f.seek(pos)
                for line in f:
                    line = line.strip()
                    if line:
                        print(fmt(json.loads(line)), flush=True)
                pos = f.tell()
        time.sleep(0.3)


if __name__ == "__main__":
    main(sys.argv[1])
