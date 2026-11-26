#!/usr/bin/env python3
"""A raw-terminal OSC 7501 agent for isolated hc tests."""

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import tty

SCRIPTS = ("reports", "forgets", "works_forever", "blocks", "crashes", "silent", "dialog")
START, END = b"\x1b[200~", b"\x1b[201~"
CONTROL = re.compile(rb"[\x1b\r\n]")


def report(state, kind=None):
    if state is not None:
        extra = f":kind={kind}" if kind else ""
        os.write(1, f"\x1b]7501;state={state}:app=fake{extra}\x1b\\".encode())


def submitted_lines():
    """Accept bracketed paste with embedded newlines, and Enter outside a paste."""
    pending = bytearray()
    incoming = b""
    paste = False
    while chunk := os.read(0, 4096):
        incoming += chunk
        while incoming:
            if paste:
                end = incoming.find(END)
                if end < 0:
                    break
                pending.extend(incoming[:end])
                incoming = incoming[end + len(END):]
                paste = False
                continue
            control = CONTROL.search(incoming)
            if control is None:
                pending.extend(incoming)
                incoming = b""
                continue
            pending.extend(incoming[:control.start()])
            incoming = incoming[control.start():]
            if incoming.startswith(START):
                incoming = incoming[len(START):]
                paste = True
            elif START.startswith(incoming):
                break
            elif incoming[:1] == b"\x1b":
                pending.append(incoming[0])
                incoming = incoming[1:]
            else:
                yield pending.decode(errors="replace")
                pending.clear()
                incoming = incoming[1:]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("script", nargs="?", choices=SCRIPTS, default="reports")
    script = parser.parse_args().script
    work = float(os.environ.get("FAKE_WORK", "0.05"))
    reply = json.loads(os.environ.get("FAKE_REPLY_CMD", "[]"))
    log = os.environ.get("FAKE_LOG")

    def show_dialog():
        os.write(1, (os.environ.get("FAKE_DIALOG", "Test dialog") + "\r\n").encode())
        state = os.environ.get("FAKE_DIALOG_STATE", "blocked")
        report(state if state != "none" else None, "permission" if state == "blocked" else None)

    tty.setraw(0)
    os.write(1, b"\x1b[?2004hfake agent ready\r\n")
    if script == "dialog":
        show_dialog()
    elif script != "silent":
        report("idle")

    waiting = False
    for text in submitted_lines():
        if log:
            with pathlib.Path(log).open("a") as stream:
                stream.write(json.dumps({"at": time.time(), "text": text}) + "\n")
        if script == "dialog":
            if text == os.environ.get("FAKE_DIALOG_KEYS", ""):
                script = "reports"
                report("idle")
            continue
        if waiting:
            if not text:
                waiting = False
                report("idle")
            continue
        for line in text.splitlines():
            if line.startswith("FAKE: ") and line[6:].strip() in SCRIPTS:
                script = line[6:].strip()
        if script == "dialog":
            show_dialog()
            continue
        if script in ("silent", "works_forever"):
            report("working" if script == "works_forever" else None)
            continue
        report("working")
        if script == "crashes":
            return 1
        if script == "blocks":
            report("blocked", "permission")
            waiting = True
            continue
        time.sleep(work)
        if script == "reports" and reply:
            subprocess.run(reply, input=text.partition("\n")[0] + "\n", text=True, check=True)
        report("idle")
    return 0


if __name__ == "__main__":
    sys.exit(main())
