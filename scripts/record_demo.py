"""Records the demo video: the REAL agent CLI and the REAL trace viewer run in
real ptys; their actual screen output is captured and rendered side by side.

Usage: python3 scripts/record_demo.py OUT.mp4
"""
import fcntl
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import sys
import termios
import time

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "scripts/.demo_data")
FRAMES = "/tmp/demo_frames"
FPS = 3
W, H = 1760, 960
COLS = 2
LEFT_W = 980
FONT = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 16)
FONT_B = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 17)
LINE_H = 21
PAD = 14
HEADER_H = 40

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][AB0]|\r")


def clean(text):
    return ANSI.sub("", text).replace("\x1b", "")


def wrap(line, width_chars):
    if not line:
        return [""]
    out = []
    while len(line) > width_chars:
        out.append(line[:width_chars])
        line = line[width_chars:]
    out.append(line)
    return out


class Pane:
    def __init__(self, argv, width_chars):
        self.master, slave = pty.openpty()
        sz = struct.pack("HHHH", 60, width_chars + 4, 0, 0)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, sz)
        self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                     cwd=ROOT, close_fds=True)
        os.close(slave)
        os.set_blocking(self.master, False)
        self.width_chars = width_chars
        self.lines = []
        self._partial = ""

    def read(self):
        try:
            data = os.read(self.master, 65536)
        except (BlockingIOError, OSError):
            return
        if not data:
            return
        text = clean(data.decode("utf-8", "replace"))
        self._partial += text
        parts = self._partial.split("\n")
        self._partial = parts.pop()  # last fragment stays open
        for raw in parts:
            self.lines.extend(wrap(raw.rstrip(), self.width_chars))
        self.lines = self.lines[-400:]

    def type(self, text, cps=14):
        for ch in text:
            os.write(self.master, ch.encode())
            time.sleep(1.0 / cps + 0.02)
            self.read()
        os.write(self.master, b"\r")

    def visible(self, max_rows):
        current = wrap(self._partial.rstrip(), self.width_chars) if self._partial else []
        return (self.lines + current)[-max_rows:]


def render(chat, trace, path, clock_s):
    img = Image.new("RGB", (W, H), (24, 24, 28))
    d = ImageDraw.Draw(img)
    panels = [("chat - python -m agent --preset timeout-booked", 0, LEFT_W, chat),
              ("live trace - traces/*.jsonl", LEFT_W, W - LEFT_W, trace)]
    for title, x0, pw, lines in panels:
        d.rectangle([x0, 0, x0 + pw, HEADER_H], fill=(40, 44, 52))
        d.text((x0 + PAD, 10), title, font=FONT_B, fill=(180, 200, 255))
        max_rows = (H - HEADER_H - PAD) // LINE_H
        y = HEADER_H + PAD
        for ln in lines[-max_rows:]:
            d.text((x0 + PAD, y), ln, font=FONT, fill=(210, 215, 220))
            y += LINE_H
    d.rectangle([LEFT_W - 1, 0, LEFT_W + 1, H], fill=(70, 74, 82))
    d.text((W - 130, 10), f"{int(clock_s//60):02d}:{int(clock_s%60):02d}",
           font=FONT_B, fill=(150, 150, 160))
    img.save(path)


# dialogue: (at_second, action, payload)
SCRIPT = [
    (6,   "type", "Find a plumber for my kitchen tap in Marathahalli this week, mornings only, under 1500"),
    (30,  "type", "1"),
    (58,  "type", "yes"),
    (92,  "type", "show my bookings"),
    (112, "type", "what do you remember"),
    (132, "type", "cancel my plumber booking"),
    (152, "type", "yes"),
    (176, "type", "show my bookings"),
]
END_S = 205


def main(out_path):
    shutil.rmtree(DATA, ignore_errors=True)
    shutil.rmtree(FRAMES, ignore_errors=True)
    os.makedirs(FRAMES)
    chat = Pane([sys.executable, "-m", "agent", "--preset", "timeout-booked",
                 "--data-dir", DATA], width_chars=56)
    trace = Pane([sys.executable, "scripts/trace_tail.py",
                  os.path.relpath(os.path.join(DATA, "traces"), ROOT)], width_chars=42)
    t0 = time.time()
    frame = 0
    si = 0
    while True:
        now = time.time() - t0
        if now >= END_S:
            break
        chat.read()
        trace.read()
        while si < len(SCRIPT) and now >= SCRIPT[si][0]:
            _, action, payload = SCRIPT[si]
            chat.type(payload)
            si += 1
        if frame % 1 == 0:
            render(chat.visible(100), trace.visible(100),
                   os.path.join(FRAMES, f"{frame:05d}.png"), now)
        frame += 1
        # hold ~1/FPS cadence accounting for work done
        target = t0 + frame / FPS
        delay = target - time.time()
        if delay > 0:
            time.sleep(min(delay, 0.5))
    chat.proc.terminate()
    trace.proc.terminate()
    print(f"frames: {frame}")
    subprocess.run(["ffmpeg", "-y", "-framerate", str(FPS), "-i",
                    os.path.join(FRAMES, "%05d.png"), "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-r", "30", out_path],
                   check=True, capture_output=True)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/downloads/demo.mp4")
