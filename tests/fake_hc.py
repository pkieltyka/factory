#!/usr/bin/env python3
"""Small hc connect stand-in with state shared by the short-lived RPC clients."""

import contextlib
import copy
import fcntl
import json
import os
import pathlib
import sys
import time

FIXTURES = json.loads(pathlib.Path(__file__).with_name("hc_fixtures.json").read_text())


def example(name):
    return copy.deepcopy(FIXTURES["replies"][name]["result"])


@contextlib.contextmanager
def state_at(directory):
    directory = pathlib.Path(directory)
    with (directory / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / "state.json"
        state = json.loads(path.read_text()) if path.exists() else {
            "core": "fakecore00000000", "next": 1, "terminals": {}, "docs": {},
            "calls": [], "received": [], "rules": {}, "polls": {}, "sent": {},
        }
        try:
            yield state
        finally:
            path.write_text(json.dumps(state))


class RpcFailure(Exception):
    def __init__(self, kind, message=None):
        self.kind, self.message = kind, message


def terminal(state, address):
    record = state["terminals"].get(address)
    if record is None:
        raise RpcFailure("not_found", f"no terminal {address}")
    return record


def listing(state, params):
    result = example("terminal.list.all")
    labels = params.get("labels", {})
    matched = [t for t in state["terminals"].values()
               if all(t["labels"].get(k) == v for k, v in labels.items())]
    result["terminals"] = [t for t in matched if params.get("all") or t["state"] == "live"]
    result["hidden_finished"] = len(matched) - len(result["terminals"])
    return result


def dispatch(directory, request):
    method, params = request["method"], request.get("params", {})
    while True:
        with state_at(directory) as state:
            if method == "hello":
                result = example("hello")
                result["core"]["id"] = state["core"]
                result["version"] = os.environ.get("FAKE_HC_VERSION", FIXTURES["hc_version"])
                return result
            if method == "terminal.list":
                return listing(state, params)
            target = params.get("terminal")
            if method == "terminal.get":
                return terminal(state, target)
            if method != "terminal.send":
                raise RpcFailure("invalid_params", f"unknown method {method}")
            target_record = terminal(state, target)
            send_rule = state["rules"].get("send", {}).get(target, {})
            if send_rule.get("die"):
                raise EOFError
            if target_record["state"] != "live":
                raise RpcFailure("finished")
            status = target_record.get("status")
            word = (status or {}).get("state")
            if word == "blocked" and not params.get("force"):
                raise RpcFailure("blocked")
            if params.get("if") and params["if"] != word:
                raise RpcFailure("status_mismatch")
            if send_rule.get("error"):
                raise RpcFailure(send_rule["error"])
            if not send_rule.get("hold"):
                state["sent"].setdefault(target, []).append(params["text"])
                return {"started": send_rule.get("started", True) if status else None,
                        "status": word, "turn": target_record["turn"]}
        time.sleep(0.02)


def serve(directory):
    for line in sys.stdin.buffer:
        request = json.loads(line)
        method = request["method"]
        with state_at(directory) as state:
            state["received"].append(request)
            if method != "hello":
                state["calls"].append(request)
            rule = state["rules"].get("hello" if method == "hello" else "reply", {})
        if rule.get("stall"):
            time.sleep(3600)
            return
        if rule.get("eof"):
            return
        time.sleep(rule.get("delay", 0))
        if rule.get("stderr"):
            sys.stderr.buffer.write(b"x" * rule["stderr"] + b" stderr-tail\n")
            sys.stderr.flush()
        reply = {"jsonrpc": "2.0", "id": request["id"]}
        try:
            reply["result"] = dispatch(directory, request)
        except RpcFailure as problem:
            error = copy.deepcopy(FIXTURES["errors"][problem.kind]["error"])
            if problem.message is not None:
                error["message"] = problem.message
            reply["error"] = error
        except EOFError:
            return
        wire = rule.get("wire")
        if wire is None:
            wire = json.dumps(rule.get("reply", reply)) + "\n"
        output = (wire + rule.get("after", "")).encode()
        if rule.get("fragment"):
            for chunk in (output[:5], output[5:17], output[17:]):
                sys.stdout.buffer.write(chunk)
                sys.stdout.flush()
                time.sleep(0.01)
        else:
            sys.stdout.buffer.write(output)
            sys.stdout.flush()
        if rule.get("stop_reading"):
            time.sleep(3600)
            return


def main():
    if sys.argv[1:] == ["--version"]:
        print("hc " + os.environ.get("FAKE_HC_VERSION", FIXTURES["hc_version"]))
        return 0
    if sys.argv[1:] == ["connect"]:
        serve(os.environ["FAKE_HC_DIR"])
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
