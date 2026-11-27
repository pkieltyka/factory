#!/usr/bin/env python3
"""A raw-terminal OSC 7501 agent for isolated hc tests."""

import argparse
import json
import os
import pathlib
import re
import select
import subprocess
import sys
import time
import tty

SCRIPTS = ("reports", "forgets", "works_forever", "blocks", "blocks_once", "crashes", "quits", "silent", "dialog")
START, END = b"\x1b[200~", b"\x1b[201~"
CONTROL = re.compile(rb"[\x1b\r\n]")
PUSHBACK = bytearray()   # input that arrived while a tool ran, read before the terminal again


def report(state, kind=None):
    if state is not None:
        extra = f":kind={kind}" if kind else ""
        os.write(1, f"\x1b]7501;state={state}:app=fake{extra}\x1b\\".encode())


def read_input():
    if PUSHBACK:
        data = bytes(PUSHBACK)
        PUSHBACK.clear()
        return data
    return os.read(0, 4096)


def run_tool(command):
    """Run a shell command as this agent's tool: input arriving meanwhile waits for the next turn, except a lone
    Esc, which interrupts the turn (the command is killed). Returns the exit code, or None when interrupted."""
    proc = subprocess.Popen(["sh", "-c", command], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    while proc.poll() is None:
        if select.select([0], [], [], 0.2)[0]:
            chunk = os.read(0, 4096)
            if chunk == b"\x1b":
                proc.kill()
                proc.wait()
                return None
            PUSHBACK.extend(chunk)
    return proc.returncode


def add_typed(pending, data):
    """Typed input into the composer: a backspace (DEL or BS) removes the character before it."""
    for byte in data:
        if byte in (0x7f, 0x08):
            if pending:
                pending.pop()
        else:
            pending.append(byte)


def submitted_lines(interruptible=False):
    """Accept bracketed paste with embedded newlines, and Enter outside a paste."""
    pending = bytearray()
    incoming = b""
    paste = False
    echo = bool(os.environ.get("FAKE_ECHO"))
    while chunk := read_input():
        if chunk == b"\x04":                    # Ctrl+D: handled at once, like a harness's quit key
            yield "\x04"
            continue
        incoming += chunk
        if echo:   # show the unsubmitted input, as a harness's composer would
            os.write(1, b"\r\x1b[K> " + bytes(pending) + CONTROL.sub(b" ", incoming))
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
                add_typed(pending, incoming)
                incoming = b""
                continue
            add_typed(pending, incoming[:control.start()])
            incoming = incoming[control.start():]
            if incoming.startswith(START):
                incoming = incoming[len(START):]
                paste = True
            elif START.startswith(incoming):
                if interruptible and incoming == b"\x1b" and not select.select([0], [], [], 0.02)[0]:
                    pending.clear()
                    incoming = b""
                    yield None
                    continue
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
    "ACT run <path> <shell command>" runs the command, as this agent's own tool would, and appends its exit.
    An Esc during a run interrupts the turn: its later lines are not acted on."""
    for line in text.splitlines():
        parts = line.split(" ", 3)
        if len(parts) < 4 or parts[0] != "ACT":
            continue
        verb, path, rest = parts[1], pathlib.Path(parts[2]), parts[3]
        if verb == "append":
            with path.open("a") as stream:
                stream.write(rest + "\n")
        elif verb == "run":
            code = run_tool(rest)
            if code is None:
                return
            with path.open("a") as stream:
                stream.write(f"exit {code}\n")
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
    for text in submitted_lines(interruptible=os.environ.get("FAKE_INTERRUPT") == "1"):
        if text == "\x04":                      # Ctrl+D on its own line: quit to the shell, as "quits" does
            report("clear")
            return 0
        if text is None:
            report("idle")
            continue
        if log:
            with pathlib.Path(log).open("a") as stream:
                stream.write(json.dumps({"at": time.time(), "text": text}) + "\n")
        if script == "dialog":
            if text == os.environ.get("FAKE_DIALOG_KEYS", ""):
                script = "reports"
                os.write(1, b"\x1b[2J\x1b[Hfake agent ready\r\n")   # the dialog is gone, as a harness redraws
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
