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
        previous = path.read_text() if path.exists() else None
        state = json.loads(previous) if previous is not None else {
            "core": "fakecore00000000", "next": 1, "terminals": {}, "screens": {}, "docs": {},
            "calls": [], "received": [], "rules": {}, "polls": {}, "sent": {},
        }
        try:
            yield state
        finally:
            snapshot = json.dumps(state)
            if snapshot != previous:
                staging = path.with_suffix(".tmp")
                staging.write_text(snapshot)
                staging.replace(path)


class RpcFailure(Exception):
    def __init__(self, kind, message=None):
        self.kind, self.message = kind, message


def terminal(state, address):
    record = state["terminals"].get(address)
    if record is None:
        raise RpcFailure("not_found", f"no terminal {address}")
    return record


def set_status(record, word, kind=None, msg=None):
    previous = record["status"]
    status = None if word is None else {"state": word, "app": "fake"}
    if status is not None:
        if kind is not None:
            status["kind"] = kind
        if msg is not None:
            status["msg"] = msg
    if previous == status:
        return
    if previous and previous["state"] == "working" and word != "working":
        record["turn"] += 1
    record["status"] = status
    record["reported"] = record["reported"] or status is not None
    record["status_ms"] = timestamp() if status is not None else None


def timestamp():
    return time.time_ns() // 1_000_000


def observe(state, record):
    rule = state["polls"].get(record["address"])
    if rule and record["state"] == "live":
        rule["remaining"] -= 1
        if rule["remaining"] <= 0:
            set_status(record, rule["status"])
            del state["polls"][record["address"]]
    return record


def finish(record, reason, exit_code=None):
    record.update(state="finished", reason=reason, finished_ms=timestamp(), exit_code=exit_code, status=None)
    return record


def launch(state, params):
    labels = params.get("labels", {})
    rule = state["rules"].get("run", {}).get(labels.get("factory.member"), {})
    if rule.get("error"):
        raise RpcFailure(rule["error"])
    if not params.get("argv"):
        raise RpcFailure("invalid_params", "missing argv")
    address = f"t{state['next']}"
    state["next"] += 1
    record = example("terminal.run")
    record.update(address=address, labels=labels, argv=params["argv"], cwd=params.get("cwd", "/tmp"),
                  parent=params.get("parent"), keep=params.get("keep", False), notify=not params.get("no_notify"),
                  created_ms=timestamp())
    set_status(record, rule.get("status"))
    state["terminals"][address] = record
    state["screens"][address] = rule.get("screen", "")
    if rule.get("then"):
        remaining, word = rule["then"]
        state["polls"][address] = {"remaining": remaining, "status": word}
    return record


def listing(state, params):
    result = example("terminal.list.all")
    labels = params.get("labels", {})
    matched = [t for t in state["terminals"].values()
               if all(t["labels"].get(k) == v for k, v in labels.items())]
    result["terminals"] = [observe(state, t) for t in matched if params.get("all") or t["state"] == "live"]
    result["hidden_finished"] = len(matched) - len(result["terminals"])
    return result


def document(state, method, params):
    current = state["docs"].get(params["name"], example("doc.get.empty"))
    if method == "doc.get":
        return current
    if state["rules"].get("doc_conflicts", 0):
        state["rules"]["doc_conflicts"] -= 1
        raise RpcFailure("conflict")
    if params.get("version") != current["version"]:
        raise RpcFailure("conflict")
    version = current["version"] + 1
    state["docs"][params["name"]] = {"value": params["value"], "version": version}
    return {**example("doc.set"), "version": version}


def terminal_call(state, method, params):
    target = params.get("terminal")
    record = terminal(state, target)
    if method == "terminal.get":
        return observe(state, record)
    if method == "terminal.label":
        labels = record["labels"]
        for key, value in params["labels"].items():
            if value == "":
                labels.pop(key, None)
            else:
                labels[key] = value
        return record
    if method == "terminal.keep":
        record["keep"] = params["keep"]
        return record
    if method == "terminal.kill":
        return finish(record, "killed") if record["state"] == "live" else record
    if method == "terminal.rm":
        if record["state"] == "live":
            raise RpcFailure("live")
        del state["terminals"][target]
        state["screens"].pop(target, None)
        return {**example("terminal.rm"), "removed": target}
    if method == "terminal.read":
        return {**example("terminal.read"), "text": state["screens"].get(target, "")}
    if method == "terminal.keys":
        if record["state"] != "live":
            raise RpcFailure("finished")
        for key in params["keys"]:
            state["screens"][target] = state["screens"].get(target, "") + ("\n" if key == "enter" else key)
        return {**example("terminal.keys"), "turn": record["turn"], "status": (record.get("status") or {}).get("state")}
    raise RpcFailure("invalid_params", f"unknown method {method}")


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
            if method == "terminal.run":
                return launch(state, params)
            if method in ("doc.get", "doc.set"):
                return document(state, method, params)
            target = params.get("terminal")
            if method != "terminal.send":
                if method in ("terminal.get", "terminal.keys", "terminal.read", "terminal.label",
                              "terminal.keep", "terminal.kill", "terminal.rm"):
                    return terminal_call(state, method, params)
                raise RpcFailure("invalid_params", f"unknown method {method}")
            if not isinstance(params.get("text"), str):
                raise RpcFailure("invalid_params", "missing text")
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
                return {**example("terminal.send.agent" if status else "terminal.send.plain"),
                        "started": send_rule.get("started", True) if status else None,
                        "status": word, "turn": target_record["turn"]}
        time.sleep(0.02)


def serve(directory):
    for line in sys.stdin.buffer:
        request = json.loads(line)
        method = request["method"]
        with state_at(directory) as state:
            if state["rules"].get("offline"):
                return
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
        chunks = rule.get("chunks", (5, 12) if rule.get("fragment") else ())
        if chunks:
            offset = 0
            for size in chunks:
                sys.stdout.buffer.write(output[offset:offset + size])
                sys.stdout.flush()
                offset += size
                time.sleep(rule.get("chunk_delay", 0.01))
            output = output[offset:]
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
