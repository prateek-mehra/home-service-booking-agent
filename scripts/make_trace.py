"""Generates the checked-in end-to-end trace for a timeout-after-success run."""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.conversation import Conversation

DATA = "scripts/.trace_data"
shutil.rmtree(DATA, ignore_errors=True)
conv = Conversation(user_id="demo", data_dir=DATA,
                    scenario={"book_outcomes": {"*": "timeout_booked"}},
                    trace_dir="traces")
for msg in ["Find a plumber for my kitchen tap in Marathahalli this week, under 1500",
            "1", "yes"]:
    print(f"User: {msg}")
    print(f"Agent: {conv.handle(msg)}\n")
conv.tracer.close()
src = conv.tracer.path
dst = "traces/timeout-after-success.jsonl"
shutil.copy(src, dst)
shutil.rmtree(DATA, ignore_errors=True)
print(f"Trace written to {dst}")
