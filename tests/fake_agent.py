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

SCRIPTS = ("reports", "forgets", "works_forever", "blocks", "blocks_once", "crashes", "quits", "silent", "dialog")
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


def act(text):
    """Test-only action protocol (phase_8.md §2): a line "ACT append <path> <text>" appends the text, and
    "ACT run <path> <shell command>" runs the command, as this agent's own tool would, and appends its exit."""
    for line in text.splitlines():
        parts = line.split(" ", 3)
        if len(parts) < 4 or parts[0] != "ACT":
            continue
        verb, path, rest = parts[1], pathlib.Path(parts[2]), parts[3]
        if verb == "append":
            with path.open("a") as stream:
                stream.write(rest + "\n")
        elif verb == "run":
            done = subprocess.run(["sh", "-c", rest], stdin=subprocess.DEVNULL, capture_output=True, text=True)
            with path.open("a") as stream:
                stream.write(f"exit {done.returncode}\n")
    for line in text.splitlines():   # a fake conversation: memory kept per thread, in FAKE_MEMORY_DIR
        parts = line.split(" ", 2)
        if parts[0] != "ACT" or len(parts) < 2 or parts[1] not in ("remember", "recall", "switch"):
            continue
        thread = os.environ.get("CODEX_THREAD_ID", "none")
        memory = pathlib.Path(os.environ.get("FAKE_MEMORY_DIR", ".")) / f"{thread}.mem"
        if parts[1] == "remember":
            memory.write_text(parts[2])
        elif parts[1] == "recall":
            word = memory.read_text().strip() if memory.exists() else "NONE"
            with pathlib.Path(parts[2]).open("a") as stream:
                stream.write((word[::-1] if word != "NONE" else word) + "\n")
        else:                          # a new conversation in the same terminal: later commands see its ID
            os.environ["CODEX_THREAD_ID"] = f"thread-{os.urandom(4).hex()}"


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
                if script == "blocks_once":   # answered: back to ordinary turns
                    script = "reports"
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
        if script == "quits":       # a harness quitting to its shell: hc clears the status, the pane stays
            report("clear")
            return 0
        report("working")
        if script == "crashes":
            return 1
        if script in ("blocks", "blocks_once"):
            report("blocked", "permission")
            waiting = True
            continue
        time.sleep(work)
        act(text)
        if script == "reports" and reply:
            subprocess.run(reply, input=text.partition("\n")[0] + "\n", text=True, check=True)
        report("idle")
    return 0


if __name__ == "__main__":
    sys.exit(main())
