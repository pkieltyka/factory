"""Factory tests with fake harnesses and a fake or private real hc core."""
import argparse
import importlib.util
import concurrent.futures
import contextlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import datetime
import errno
import fcntl
import hashlib
import inspect
import io
import json
import os
import pathlib
import random
import re
import shlex
import shutil
import stat
import subprocess
import signal
import sys
import tempfile
import textwrap
import tomllib
import threading
import time
import types
import unittest
import uuid
from urllib.parse import unquote, urlsplit
from unittest.mock import call, patch

if __package__:
    from . import e2e, fake_hc, live
else:
    import e2e
    import fake_hc
    import live

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "skill/scripts/factory.py"
spec = importlib.util.spec_from_file_location("factory", SCRIPT)
factory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(factory)
os.environ["LOGNAME"] = "ada"  # whose team the tests run as: the first part of worktree branches
# Fixtures are written as `+++ settings +++ prose`; Env.write_brief puts them on disk as factory.toml and FACTORY.md.
TEMPLATE = ("+++\n" + (factory.SKILL / "templates/factory.toml").read_text().strip() + "\n+++\n"
            + (factory.SKILL / "templates/FACTORY.md").read_text())
GIT = shutil.which("git")
HC_FIXTURES = json.loads(pathlib.Path(__file__).with_name("hc_fixtures.json").read_text())
REAL_HC = os.environ.get("FACTORY_TEST_HC") == "real"
fake_only = unittest.skipIf(REAL_HC, "scripts the fake hc")
real_only = unittest.skipUnless(REAL_HC, "requires a real hc PTY")
REAL_CORE = None
REAL_CASES = set()


def setUpModule():
    global REAL_CORE
    if REAL_HC:
        REAL_CORE = live.Core("unit", extra_env={"HC_SETTLE_MS": "0"})


def tearDownModule():
    if REAL_CORE is not None:
        REAL_CORE.stop()
        print(f"Ran {len(REAL_CASES)} hc tests against the real core", file=sys.stderr)


def conforms(value, spec, named_types, path="$"):
    kind = spec.get("type")
    if kind == "any" or (value is None and spec.get("nullable")):
        return []
    if "ref" in spec:
        if not isinstance(value, dict):
            return [f"{path}: expected {spec['ref']} object"]
        fields = named_types[spec["ref"]]
        missing = [f"{path}.{key}: required" for key in fields if fields[key].get("required") and key not in value]
        unknown = [f"{path}.{key}: unknown" for key in value if key not in fields]
        return missing + unknown + [problem for key in value.keys() & fields.keys()
                                    for problem in conforms(value[key], fields[key], named_types, f"{path}.{key}")]
    valid_types = {"string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,),
                   "array": (list,), "object": (dict,)}
    if type(value) not in valid_types[kind]:
        return [f"{path}: expected {kind}"]
    if kind == "array":
        return [problem for i, item in enumerate(value)
                for problem in conforms(item, spec["items"], named_types, f"{path}[{i}]")]
    if "values" in spec:
        return [problem for key, item in value.items()
                for problem in conforms(item, spec["values"], named_types, f"{path}.{key}")]
    return []


RPC_ERROR_TYPES = {
    "Error": {"code": {"type": "integer", "required": True}, "message": {"type": "string", "required": True},
              "data": {"ref": "ErrorData", "required": True}},
    "ErrorData": {"kind": {"type": "string", "required": True}, "hint": {"type": "string"}},
}


class HcAssertions:
    @staticmethod
    def check_result(method, value):
        schema = HC_FIXTURES["schema"]
        return conforms(value, {"ref": schema["results"][method]}, schema["types"])

    def checked_call(self, hc, method, params):
        result = hc.call(method, params)
        self.assertEqual(self.check_result(method, result), [])
        return result

    def assert_hc_error(self, kind, fn, *args):
        with self.assertRaises(factory.HcError) as raised:
            fn(*args)
        self.assertEqual(raised.exception.kind, kind)

    def assert_error_payload(self, kind, error):
        self.assertEqual(conforms(error, {"ref": "Error"}, RPC_ERROR_TYPES), [])
        self.assertEqual(error["data"]["kind"], kind)


class HcContract(HcAssertions, unittest.TestCase):
    def test_recorded_replies_conform(self):
        for name, example in HC_FIXTURES["replies"].items():
            with self.subTest(name=name):
                self.assertEqual(self.check_result(example["method"], example["result"]), [])
        for kind, example in HC_FIXTURES["errors"].items():
            with self.subTest(kind=kind):
                self.assert_error_payload(kind, example["error"])

    def test_validator_rejects(self):
        hello = HC_FIXTURES["replies"]["hello"]["result"]
        terminal = HC_FIXTURES["replies"]["terminal.get.live"]["result"]
        for method, value in (
            ("hello", {k: v for k, v in hello.items() if k != "version"}),
            ("hello", {**hello, "extra": True}),
            ("hello", {**hello, "version": None}),
            ("hello", {**hello, "generation": True}),
            ("terminal.get", {**terminal, "labels": {"team": 42}}),
            ("terminal.get", {**terminal, "records": {"agent": {"state": 42}}}),
        ):
            with self.subTest(method=method, value=value):
                self.assertTrue(self.check_result(method, value))

    def test_any_accepts_null(self):
        self.assertEqual(self.check_result("doc.get", {"value": None, "version": 0}), [])

    def test_wire_exceptions_are_applied(self):
        for rule in HC_FIXTURES["wire_exceptions"]:
            with self.subTest(type=rule["type"], field=rule["field"]):
                self.assertEqual(rule["version"], HC_FIXTURES["hc_version"])
                field = HC_FIXTURES["schema"]["types"][rule["type"]][rule["field"]]
                self.assertEqual(field["required"], rule["applied"]["required"])
        result = HC_FIXTURES["replies"]["terminal.list.all"]["result"]
        self.assertNotIn("machines", result)
        self.assertEqual(self.check_result("terminal.list", result), [])
        self.assertEqual(self.check_result("terminal.list", {**result, "machines": []}), [])
        self.assertTrue(self.check_result("terminal.list", {**result, "machines": [False]}))

    @fake_only
    def test_fake_replies_conform(self):
        with FakeHc() as fake:
            hc = factory.Hc()
            fake.on_run("worker", "idle")
            params = {"argv": ["probe"], "labels": {"factory.member": "worker"}}
            term = self.checked_call(hc, "terminal.run", params)["address"]
            target = {"terminal": term}
            self.checked_call(hc, "terminal.list", {"all": True})
            self.checked_call(hc, "terminal.get", target)
            self.checked_call(hc, "terminal.send", {**target, "text": "hello"})
            self.checked_call(hc, "terminal.keys", {**target, "keys": ["enter"]})
            self.checked_call(hc, "terminal.read", {**target, "lines": 0})
            self.checked_call(hc, "terminal.label", {**target, "labels": {"extra": "one"}})
            self.checked_call(hc, "terminal.keep", {**target, "keep": False})
            missing = {"terminal": "t999"}
            self.assert_hc_error("not_found", hc.get, "t999")
            self.assert_hc_error("invalid_params", hc.call, "terminal.send", target)
            self.assert_hc_error("live", hc.rm, term)
            self.assert_hc_error("status_mismatch", hc.call, "terminal.send",
                                 {**target, "text": "x", "if": "working"})
            fake.set_status(term, "blocked", kind="permission")
            self.assert_hc_error("blocked", hc.send, term, "x")
            self.checked_call(hc, "terminal.kill", target)
            self.assert_hc_error("finished", hc.send, term, "x")
            self.checked_call(hc, "terminal.rm", target)
            self.assert_hc_error("not_found", hc.call, "terminal.get", missing)
            fake.on_run("worker", error="spawn_failed")
            self.assert_hc_error("spawn_failed", hc.call, "terminal.run", params)
            self.checked_call(hc, "doc.get", {"name": "herdr-app"})
            layout = {"name": "herdr-app", "value": {}, "version": 0}
            self.checked_call(hc, "doc.set", layout)
            self.assert_hc_error("conflict", hc.call, "doc.set", layout)

class FakeHc:
    def __enter__(self):
        self.stack = contextlib.ExitStack()
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="fake-hc-"))
        self.stack.enter_context(patch.dict(os.environ, {"FAKE_HC_DIR": self.directory}, clear=False))
        self.stack.enter_context(patch.object(factory, "HC", str(pathlib.Path(fake_hc.__file__))))
        return self

    def __exit__(self, *error):
        self.stack.close()

    def state(self):
        return fake_hc.state_at(self.directory)

    def received(self):
        with self.state() as state:
            return state["received"]

    def calls(self, method):
        with self.state() as state:
            return [request for request in state["calls"] if request["method"] == method]

    def rule(self, phase, **options):
        with self.state() as state:
            state["rules"][phase] = options

    def add(self, labels, status="idle", **fields):
        with self.state() as state:
            address = f"t{state['next']}"
            state["next"] += 1
            record = fake_hc.example("terminal.get.live")
            record.update(address=address, labels=labels, turn=0, created_ms=fake_hc.timestamp(),
                          status_ms=fake_hc.timestamp() if status else None,
                          status={"state": status, "app": "fake"} if status else None, reported=status is not None)
            record.update(fields)
            state["terminals"][address] = record
            return address

    def on_send(self, term, **options):
        with self.state() as state:
            state["rules"].setdefault("send", {})[term] = options

    def release(self, term):
        with self.state() as state:
            state["rules"]["send"][term]["hold"] = False

    def on_run(self, member, status=None, screen="", then=None, *, error=None):
        with self.state() as state:
            state["rules"].setdefault("run", {})[member] = {
                "status": status, "screen": screen, "then": then, "error": error,
            }

    def set_status(self, term, state, kind=None, msg=None):
        with self.state() as data:
            fake_hc.set_status(data["terminals"][term], state, kind, msg)

    def finish(self, term, reason="exited", exit_code=0):
        with self.state() as state:
            fake_hc.finish(state["terminals"][term], reason, exit_code)

    def remove(self, term):
        with self.state() as state:
            del state["terminals"][term]
            state["screens"].pop(term, None)

    def screen(self, term, text):
        with self.state() as state:
            state["screens"][term] = text

    def reply_in_chunks(self, sizes, delay):
        self.rule("reply", chunks=sizes, chunk_delay=delay)

    def hello(self, version="0.1.20", core=None):
        envelope = fake_hc.example("hello")
        with self.state() as state:
            envelope["core"]["id"] = state["core"] if core is None else core
        envelope["version"] = version
        self.rule("hello", reply={"jsonrpc": "2.0", "id": 0, "result": envelope})

    def doc_conflicts(self, count):
        with self.state() as state:
            state["rules"]["doc_conflicts"] = count

    def stop(self):
        with self.state() as state:
            state["rules"]["offline"] = True

    def sent(self, term):
        with self.state() as state:
            return state["sent"].get(term, [])


@fake_only
class FakeHcTests(unittest.TestCase):
    def setUp(self):
        self.fake = self.enterContext(FakeHc())
        self.hc = factory.Hc(timeout=2)

    def test_hold_and_release(self):
        term = self.fake.add({})
        self.fake.on_send(term, hold=True)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.hc.send, term, "report")
            try:
                deadline = time.monotonic() + 3
                while not self.fake.calls("terminal.send"):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                self.assertFalse(pending.done())
                self.assertEqual(self.fake.sent(term), [])
            finally:
                self.fake.release(term)
            self.assertTrue(pending.result(timeout=3)["started"])
        self.assertEqual(self.fake.sent(term), ["report"])

    def test_die_gives_no_reply(self):
        term = self.fake.add({})
        self.fake.on_send(term, die=True)
        with self.assertRaises(factory.HcDown) as raised:
            self.hc.send(term, "report")
        self.assertTrue(raised.exception.sent)
        self.assertEqual(self.fake.sent(term), [])

    def test_on_run_status_after_polls(self):
        self.fake.on_run("worker", screen="starting", then=(2, "idle"))
        record = self.hc.run(["probe"], "/tmp", {}, {"factory.member": "worker"})
        self.assertIsNone(record["status"])
        self.assertIsNone(self.hc.get(record["address"])["status"])
        self.assertEqual(self.hc.get(record["address"])["status"]["state"], "idle")
        self.assertEqual(self.hc.read(record["address"]), "starting")

    def test_display_text_is_only_in_read(self):
        self.fake.on_run("worker", screen="Trust this folder?")
        labels = {"factory.team": "test-team", "factory.member": "worker"}
        record = self.hc.run(["probe"], "/tmp", {}, labels)
        target = record["address"]
        self.assertEqual(self.hc.read(target), "Trust this folder?")
        for reply in (record, self.hc.get(target), self.hc.list("test-team")[0]):
            self.assertIn(reply["screen"], ("exact", "inexact"))
            self.assertNotIn("Trust this folder?", json.dumps(reply))

    def test_interrupted_write_preserves_snapshot(self):
        term = self.fake.add({})
        self.hc.send(term, "already typed")
        with patch.object(fake_hc.os, "replace", side_effect=InterruptedError("before commit")):
            with self.assertRaises(InterruptedError):
                with self.fake.state() as state:
                    state["sent"][term].append("interrupted")
        self.assertEqual(self.fake.sent(term), ["already typed"])
        self.hc.send(term, "later")
        self.assertEqual(self.fake.sent(term), ["already typed", "later"])

    def test_doc_conflicts_rule(self):
        self.fake.doc_conflicts(1)
        with self.assertRaises(factory.HcError) as raised:
            self.hc.doc_set({}, 0)
        self.assertEqual(raised.exception.kind, "conflict")
        self.assertEqual(self.hc.doc_set({}, 0), 1)
        self.assertEqual(self.hc.doc_get(), ({}, 1))

    def test_calls_sent_and_received(self):
        term = self.fake.add({})
        self.hc.send(term, "hello")
        self.assertEqual([req["method"] for req in self.fake.received()], ["hello", "terminal.send"])
        self.assertEqual(len(self.fake.calls("terminal.send")), 1)
        self.assertEqual(self.fake.sent(term), ["hello"])

    def test_unknown_method_is_invalid_params(self):
        with self.assertRaises(factory.HcError) as raised:
            self.hc.call("unknown")
        self.assertEqual(raised.exception.kind, "invalid_params")

    def test_stopped_core_refuses_connect(self):
        self.fake.stop()
        with self.assertRaises(factory.HcDown) as raised:
            self.hc.list("team")
        self.assertFalse(raised.exception.sent)


@fake_only
class HcClientTests(unittest.TestCase):
    def setUp(self):
        self.fake = self.enterContext(FakeHc())
        self.hc = factory.Hc(timeout=2)

    def check_failure(self, *, sent, timeout=False, method="terminal.list", params=None, client=None):
        client = client or self.hc
        with self.assertRaises(factory.HcDown) as raised:
            client.call(method, params)
        self.assertEqual((raised.exception.sent, raised.exception.timeout), (sent, timeout))
        self.assertFalse(client.children)
        return raised.exception

    def test_call_returns_result(self):
        self.assertEqual(self.hc.list("team"), [])
        self.assertEqual(self.hc.version, (0, 1, 20))
        self.assertEqual(self.hc.core, "fakecore00000000")
        self.assertFalse(self.hc.children)

    def test_call_maps_error_kind(self):
        with self.assertRaises(factory.HcError) as raised:
            self.hc.get("t999")
        error = raised.exception
        self.assertEqual(error.kind, "not_found")
        self.assertIn("t999", error.message)
        self.assertTrue(error.hint)
        self.assertFalse(self.hc.children)

    def test_old_hc_refused_before_request(self):
        with patch.dict(os.environ, {"FAKE_HC_VERSION": "0.1.19"}):
            self.check_failure(sent=False)
        self.assertEqual([r["method"] for r in self.fake.received()], ["hello"])

    def test_version_suffixes_use_numeric_floor(self):
        for version, expected in (("0.2.0-rc.1", (0, 2, 0)), ("0.1.21+nix", (0, 1, 21))):
            with self.subTest(version=version):
                self.fake.hello(version)
                self.assertEqual(self.hc.list("team"), [])
                self.assertEqual(self.hc.version, expected)
        self.fake.hello("0.1.19-dev")
        self.check_failure(sent=False)

    def test_version_requires_numeric_prefix(self):
        self.fake.hello("dev-0.1.20")
        self.assertIn("unrecognised hc version", self.check_failure(sent=False).detail)
        self.assertEqual([r["method"] for r in self.fake.received()], ["hello"])

    def test_wrong_core_refused_before_request(self):
        self.check_failure(sent=False, client=factory.Hc(expect_core="wrong-core"))
        self.assertEqual([r["method"] for r in self.fake.received()], ["hello"])

    def test_missing_binary(self):
        error = self.check_failure(sent=False, client=factory.Hc(bin=str(pathlib.Path(self.fake.directory) / "missing")))
        self.assertEqual(error.detail, "hc not found")

    def test_hello_timeout_is_unsent(self):
        self.fake.rule("hello", stall=True)
        self.check_failure(sent=False, timeout=True, client=factory.Hc(timeout=0.3))

    def test_send_handshake_is_bounded(self):
        self.fake.rule("hello", stall=True)
        client = factory.Hc(timeout=0.3)
        with self.assertRaises(factory.HcDown) as raised:
            client.send("t1", "hello")
        self.assertEqual((raised.exception.sent, raised.exception.timeout), (False, True))
        self.assertFalse(client.children)

    def test_send_reply_outlives_setup_timeout(self):
        self.fake.rule("reply", delay=0.6)
        term = self.fake.add({})
        self.assertTrue(factory.Hc(timeout=0.3).send(term, "hello")["started"])

    def test_nullable_error_hint_is_optional(self):
        self.fake.rule("reply", reply={"jsonrpc": "2.0", "id": 1, "error": {
            "code": -32000, "message": "oops", "data": {"kind": "blocked", "hint": None},
        }})
        with self.assertRaises(factory.HcError) as raised:
            self.hc.get("t1")
        self.assertEqual((raised.exception.kind, raised.exception.hint), ("blocked", ""))

    def test_hello_eof_is_unsent(self):
        self.fake.rule("hello", eof=True)
        self.check_failure(sent=False)

    def test_malformed_hello_is_unsent(self):
        info = HC_FIXTURES["replies"]["hello"]["result"]
        hello = {"jsonrpc": "2.0", "id": 0, "result": info}
        for rule in (
            {"wire": "not JSON\n"}, {"reply": []}, {"reply": {**hello, "id": 1}},
            {"reply": {**hello, "error": {}}}, {"reply": {**hello, "result": {**info, "framed": True}}},
            {"reply": {**hello, "result": {**info, "generation": 2}}},
            {"reply": {**hello, "result": {k: v for k, v in info.items() if k not in ("generation", "version")}}},
            {"reply": {**hello, "result": {**info, "version": 20}}},
            {"reply": {**hello, "result": {**info, "core": {}}}},
        ):
            with self.subTest(rule=rule):
                prior = len(self.fake.received())
                self.fake.rule("hello", **rule)
                self.check_failure(sent=False)
                self.assertEqual([r["method"] for r in self.fake.received()[prior:]], ["hello"])

    def test_request_write_failures_use_actual_byte_count(self):
        raw_write = os.write
        for partial in (False, True):
            with self.subTest(partial=partial):
                writes = 0

                def fail_request(fd, data):
                    nonlocal writes
                    if b'"method": "hello"' in data.tobytes():
                        return raw_write(fd, data)
                    writes += 1
                    if partial and writes == 1:
                        return raw_write(fd, data[:5])
                    raise BrokenPipeError("injected")

                with patch.object(factory.os, "write", side_effect=fail_request):
                    self.check_failure(sent=partial)
                self.assertEqual(writes, 2 if partial else 1)

    def test_blocked_write_hits_the_deadline(self):
        self.fake.rule("hello", stop_reading=True)
        self.check_failure(sent=True, timeout=True, params={"large": "x" * (200 * 1024)},
                           client=factory.Hc(timeout=0.6))

    def test_request_deadline_before_any_byte_is_unsent_timeout(self):
        select = factory.selectors.PollSelector.select
        writes = 0

        def wait(poller, timeout):
            nonlocal writes
            if any(key.events == factory.selectors.EVENT_WRITE for key in poller.get_map().values()):
                writes += 1
                if writes == 2:
                    return []
            return select(poller, timeout)

        with patch.object(factory.selectors.PollSelector, "select", wait):
            self.check_failure(sent=False, timeout=True)
        self.assertEqual([r["method"] for r in self.fake.received()], ["hello"])

    def test_stderr_flood_does_not_block(self):
        term = self.fake.add({}, "idle")
        self.fake.rule("reply", stderr=1024 * 1024)
        self.assertTrue(self.hc.send(term, "hello")["started"])
        self.fake.rule("reply", stderr=1024 * 1024, wire="wrong JSON\n")
        detail = self.check_failure(sent=True).detail
        self.assertTrue(detail.endswith("stderr-tail"))
        self.assertLess(len(detail), 2200)

    def test_reply_timeout_is_sent_and_kills_connect(self):
        self.fake.rule("reply", stall=True)
        process = subprocess.Popen
        children = []

        def start(*args, **kwargs):
            child = process(*args, **kwargs)
            children.append(child)
            return child

        with patch.object(factory.subprocess, "Popen", side_effect=start):
            self.check_failure(sent=True, timeout=True, client=factory.Hc(timeout=0.3))
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)

    def test_reply_eof_is_sent(self):
        self.fake.rule("reply", eof=True)
        self.check_failure(sent=True)

    def test_malformed_reply_is_sent(self):
        envelope = {"jsonrpc": "2.0", "id": 1}
        for rule in (
            {"wire": "not JSON\n"}, {"reply": {**envelope, "id": 0, "result": {}}},
            {"reply": envelope}, {"reply": {**envelope, "error": []}},
            {"reply": {**envelope, "error": {"data": "wrong", "message": "oops"}}},
        ):
            with self.subTest(rule=rule):
                self.fake.rule("reply", **rule)
                self.check_failure(sent=True)

    def test_malformed_error_code_is_not_authoritative(self):
        error = {"message": "oops", "data": {"kind": "not_found"}}
        for fields in ({}, {"code": "invalid"}, {"code": True}, {"code": 1.5}):
            with self.subTest(fields=fields):
                self.fake.rule("reply", reply={"jsonrpc": "2.0", "id": 1, "error": {**error, **fields}})
                self.check_failure(sent=True, method="terminal.get", params={"terminal": "t999"})

    def test_fragmented_and_coalesced_replies(self):
        self.fake.rule("hello", fragment=True)
        self.fake.rule("reply", after='{"unused":true}\n')
        self.assertEqual(self.hc.list("team"), [])

    def test_one_deadline_covers_hello_and_reply(self):
        self.fake.rule("hello", delay=0.35)
        self.fake.rule("reply", delay=0.45)
        self.check_failure(sent=True, timeout=True, client=factory.Hc(timeout=0.68))

    def test_kill_children_ends_held_send(self):
        term = self.fake.add({})
        self.fake.on_send(term, hold=True)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.hc.send, term, "held")
            try:
                deadline = time.monotonic() + 3
                while not self.fake.calls("terminal.send"):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
            finally:
                self.hc.kill_children()
            with self.assertRaises(factory.HcDown) as raised:
                future.result(timeout=3)
        self.assertTrue(raised.exception.sent)
        self.assertFalse(raised.exception.timeout)
        self.assertFalse(self.fake.sent(term))
        self.assertFalse(self.hc.children)

    def test_no_call_after_kill_children(self):
        self.hc.kill_children()
        self.check_failure(sent=False)
        self.assertEqual(self.fake.received(), [])

    def test_kill_during_hello_is_unsent(self):
        self.fake.rule("hello", stall=True)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.hc.list, "team")
            try:
                deadline = time.monotonic() + 2
                while not self.fake.received():
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
            finally:
                self.hc.kill_children()
            with self.assertRaises(factory.HcDown) as raised:
                pending.result(timeout=3)
        self.assertFalse(raised.exception.sent)
        self.assertFalse(raised.exception.timeout)
        self.assertFalse(self.hc.children)

    def test_kill_children_sees_a_child_being_started(self):
        starting, release = threading.Event(), threading.Event()
        popen = subprocess.Popen

        def start(*args, **kwargs):
            child = popen(*args, **kwargs)
            starting.set()
            if not release.wait(3):
                child.kill()
                child.wait()
                raise RuntimeError("test did not release child")
            return child

        self.fake.rule("hello", stall=True)
        with patch.object(factory.subprocess, "Popen", side_effect=start), ThreadPoolExecutor(2) as pool:
            call_future = pool.submit(self.hc.list, "team")
            self.assertTrue(starting.wait(3))
            kill_future = pool.submit(self.hc.kill_children)
            release.set()
            kill_future.result(timeout=3)
            with self.assertRaises(factory.HcDown) as raised:
                call_future.result(timeout=3)
        self.assertFalse(raised.exception.sent)
        self.assertFalse(raised.exception.timeout)
        self.assertFalse(self.hc.children)

    def test_wrapper_requests(self):
        with patch.object(self.hc, "call", return_value={"terminals": [], "text": "", "value": None, "version": 0}) as rpc:
            self.hc.list("team")
            self.hc.get("t1")
            self.hc.run(["python"], "/tmp", {"X": "Y"}, {"team": "one"})
            self.hc.send("t1", "hello")
            self.hc.keys("t1", ["enter"])
            self.hc.read("t1")
            self.hc.label("t1", {"x": "y"})
            self.hc.keep_off("t1")
            self.hc.kill("t1")
            self.hc.rm("t1")
            self.hc.doc_get()
            self.hc.doc_set({}, 7)
        self.assertEqual(rpc.call_args_list, [
            call("terminal.list", {"all": True, "labels": {"factory.team": "team"}}),
            call("terminal.get", {"terminal": "t1"}),
            call("terminal.run", {"argv": ["python"], "cwd": "/tmp", "env": {"X": "Y"},
                                  "labels": {"team": "one"}, "no_parent": True, "no_notify": True}),
            call("terminal.send", {"terminal": "t1", "text": "hello"}, timeout=None),
            call("terminal.keys", {"terminal": "t1", "keys": ["enter"]}),
            call("terminal.read", {"terminal": "t1", "lines": 0}),
            call("terminal.label", {"terminal": "t1", "labels": {"x": "y"}}),
            call("terminal.keep", {"terminal": "t1", "keep": False}),
            call("terminal.kill", {"terminal": "t1"}),
            call("terminal.rm", {"terminal": "t1"}),
            call("doc.get", {"name": "herdr-app"}),
            call("doc.set", {"name": "herdr-app", "value": {}, "version": 7}),
        ])


@fake_only
class ViewTests(unittest.TestCase):
    def setUp(self):
        self.fake = self.enterContext(FakeHc())
        self.hc = factory.Hc()

    def add(self, name, status="idle", team="team", kind="member", **record):
        labels = {"factory.team": team, "factory.kind": kind}
        if name is not None:
            labels["factory.member"] = name
        return self.fake.add(labels, status, **record)

    def view(self):
        return factory.team_view(self.hc, "team")

    def test_view_states(self):
        self.add("starting", None)
        term = self.add("no-reported-field", None)
        with self.fake.state() as state:
            del state["terminals"][term]["reported"]
        self.add("shell", None, reported=True)
        self.fake.finish(self.add("exited"))
        words = ("idle", "working", "done", "blocked", "error")
        for word in words:
            self.add(word, word)
        view = self.view()
        expected = {"missing": "not started", "starting": "starting", "no-reported-field": "starting",
                    "shell": "gone", "exited": "gone",
                    **{word: word for word in words}}
        self.assertEqual({name: view.state(name) for name in expected}, expected)

    def test_view_ignores_other_team(self):
        own = self.add("worker")
        self.add("worker", "working", team="other", created_ms=30)
        self.add("other", team="other")
        view = self.view()
        self.assertEqual(view.terminal("worker")["address"], own)
        self.assertIsNone(view.terminal("other"))
        self.assertEqual(len(self.fake.calls("terminal.list")), 1)

    def test_view_newest_live_wins(self):
        old = self.add("worker", created_ms=1)
        fresh = self.add("worker", "working", created_ms=2)
        view = self.view()
        self.assertEqual(view.terminal("worker")["address"], fresh)
        self.assertEqual(view.state("worker"), "working")
        self.assertEqual([record["address"] for record in view.strays], [old])

    def test_view_down_record_is_not_gone(self):
        term = self.add("worker")
        self.hc.label(term, {"factory.down": "1"})
        self.fake.finish(term)
        self.assertEqual(self.view().state("worker"), "not started")

    def test_view_operator_keeper_and_lost(self):
        previous = self.add("boss", kind="operator-lost")
        active = self.add("boss", kind="operator")
        keeper = self.add(None, None, kind="keeper")
        view = self.view()
        self.assertEqual(view.operator["address"], active)
        self.assertEqual(view.operator_lost["address"], previous)
        self.assertEqual(view.keeper["address"], keeper)
        self.assertEqual(view.terminal("boss")["address"], active)
        self.assertFalse(view.strays)


class HcBackend:
    def setUp(self):
        self.team = "t-" + uuid.uuid4().hex[:8]
        if REAL_HC:
            self.use_real_core(REAL_CORE)
        else:
            self.fake = self.enterContext(FakeHc())
            self.hc = factory.Hc()

    def use_real_core(self, core):
        self.enterContext(patch.dict(os.environ, core.env, clear=True))
        self.hc = factory.Hc(bin=live.HC, expect_core=core.id)
        self.directory = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory(dir=core.dir)))
        REAL_CASES.add(self.id())

    def launch(self, script="reports", *, name="worker", **env):
        labels = {"factory.team": self.team, "factory.kind": "member", "factory.member": name}
        if not REAL_HC:
            return self.fake.add(labels, None if script == "silent" else env.get("FAKE_DIALOG_STATE", "idle")), None
        log = self.directory / f"{name}.jsonl"
        agent = pathlib.Path(__file__).resolve().with_name("fake_agent.py")
        term = self.hc.run([sys.executable, str(agent), script], str(self.directory),
                           {"FAKE_LOG": str(log), **env}, labels)["address"]
        self.addCleanup(self.remove, term)
        self.until(lambda: "fake agent ready" in self.hc.read(term), "agent startup")
        if script not in ("silent", "dialog"):
            self.state(term, "idle")
        return term, log

    def remove(self, term):
        try:
            if self.hc.get(term)["state"] == "live":
                self.hc.kill(term)
            self.hc.rm(term)
        except factory.HcError as error:
            if error.kind != "not_found":
                raise

    def until(self, predicate, detail):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.025)
        self.fail(f"timed out waiting for {detail}")

    def state(self, term, expected):
        def record_matching():
            record = self.hc.get(term)
            return record if (record["status"] or {}).get("state") == expected else None

        return self.until(record_matching, f"{term} to report {expected}")

    def inputs(self, term, log):
        if not REAL_HC:
            return self.fake.sent(term)
        return [json.loads(line)["text"] for line in log.read_text().splitlines()] if log.exists() else []


class HcBackendTests(HcBackend, HcAssertions, unittest.TestCase):
    def test_call_returns_result(self):
        self.assertEqual(self.hc.list(self.team), [])
        self.assertGreaterEqual(self.hc.version, factory.HC_MIN)
        self.assertEqual(self.hc.core, REAL_CORE.id if REAL_HC else "fakecore00000000")
        self.assertFalse(self.hc.children)

    def test_call_maps_error_kind(self):
        with self.assertRaises(factory.HcError) as raised:
            self.hc.get("t999")
        self.assertEqual(raised.exception.kind, "not_found")
        self.assertIn("t999", raised.exception.message)
        self.assertTrue(raised.exception.hint)

    def test_send_types_into_statusless_terminal(self):
        term, log = self.launch("silent")
        self.assertIsNone(self.hc.send(term, "hello")["started"])
        self.until(lambda: self.inputs(term, log) == ["hello"], "statusless input")

    def test_send_to_blocked_is_refused(self):
        term, log = self.launch("dialog", FAKE_DIALOG_STATE="blocked")
        self.state(term, "blocked")
        self.assert_hc_error("blocked", self.hc.send, term, "refused")
        self.assertEqual(self.inputs(term, log), [])

    def test_turn_goes_up_on_leaving_working(self):
        term, _ = self.launch("forgets", FAKE_WORK="0.8")
        initial = self.state(term, "idle")["turn"]
        if REAL_HC:
            self.hc.send(term, "work")
        else:
            self.fake.set_status(term, "working")
        self.assertEqual(self.state(term, "working")["turn"], initial)
        if not REAL_HC:
            self.fake.set_status(term, "idle")
        self.assertEqual(self.state(term, "idle")["turn"], initial + 1)

    def test_kill_then_rm(self):
        term, _ = self.launch()
        killed = self.hc.kill(term)
        self.assertEqual((killed["state"], killed["reason"], killed["status"]), ("finished", "killed", None))
        self.hc.rm(term)
        self.assert_hc_error("not_found", self.hc.get, term)

    def test_rm_live_is_refused(self):
        term, _ = self.launch()
        self.assert_hc_error("live", self.hc.rm, term)

    def test_label_on_finished_record_and_empty_removes(self):
        term, _ = self.launch()
        self.hc.kill(term)
        self.assertEqual(self.hc.label(term, {"test": "1"})["labels"]["test"], "1")
        self.assertNotIn("test", self.hc.label(term, {"test": ""})["labels"])

    def test_doc_set_stale_version_conflicts(self):
        previous, version = self.hc.doc_get()
        written = self.hc.doc_set({"spaces": []}, version)
        self.addCleanup(self.hc.doc_set, previous, written)
        self.assert_hc_error("conflict", self.hc.doc_set, {"spaces": []}, version)
        self.assertEqual(self.hc.doc_get(), ({"spaces": []}, written))

    def test_error_envelopes_conform(self):
        active, _ = self.launch()
        blocked, _ = self.launch("dialog", name="blocked", FAKE_DIALOG_STATE="blocked")
        self.state(blocked, "blocked")
        finished, _ = self.launch(name="finished")
        self.hc.kill(finished)
        if not REAL_HC:
            self.fake.on_run("missing", error="spawn_failed")
        previous, version = self.hc.doc_get()
        written = self.hc.doc_set({"spaces": []}, version)
        self.addCleanup(self.hc.doc_set, previous, written)
        cases = (
            ("not_found", "terminal.send", {"terminal": "t999", "text": "x"}),
            ("finished", "terminal.send", {"terminal": finished, "text": "x"}),
            ("blocked", "terminal.send", {"terminal": blocked, "text": "x"}),
            ("invalid_params", "terminal.send", {"terminal": active}),
            ("status_mismatch", "terminal.send", {"terminal": active, "text": "x", "if": "working"}),
            ("spawn_failed", "terminal.run", {"argv": ["/nonexistent/factory-agent"],
                                             "labels": {"factory.member": "missing"}}),
            ("live", "terminal.rm", {"terminal": active}),
            ("conflict", "doc.set", {"name": "herdr-app", "value": {}, "version": version}),
        )
        self.assertEqual({kind for kind, _, _ in cases}, set(HC_FIXTURES["errors"]))
        with patch.object(live, "HC", self.hc.bin or factory.HC):
            for kind, method, params in cases:
                with self.subTest(kind=kind), contextlib.closing(live.Conn(os.environ, self.hc.core)) as conn:
                    conn.request(method, params)
                    reply = conn.message()
                    self.assertEqual(set(reply), {"id", "jsonrpc", "error"})
                    self.assertIs(type(reply["id"]), int)
                    self.assertEqual((reply["id"], reply["jsonrpc"]), (1, "2.0"))
                    self.assert_error_payload(kind, reply["error"])

    @real_only
    def test_version_and_core_refusals_do_not_dispatch(self):
        args = ([sys.executable, "-c", "print('must not start')"], str(self.directory), {}, {"factory.team": self.team})
        wrong_core = factory.Hc(bin=live.HC, expect_core="wrong-core")
        with self.assertRaises(factory.HcDown) as raised:
            wrong_core.run(*args)
        self.assertFalse(raised.exception.sent)
        with patch.object(factory, "HC_MIN", (99,)):
            with self.assertRaises(factory.HcDown) as raised:
                self.hc.run(*args)
        self.assertFalse(raised.exception.sent)
        self.assertEqual(self.hc.list(self.team), [])

    @real_only
    def test_wire_exceptions_still_hold(self):
        result = self.checked_call(self.hc, "terminal.list", {"all": True})
        self.assertNotIn("machines", result, "hc now includes machines: recheck the wire exception")

    @real_only
    def test_real_replies_conform(self):
        hc = self.hc
        self.assertEqual(self.check_result("hello", REAL_CORE.hello()), [])
        self.checked_call(hc, "server.status", {})
        plain = self.checked_call(hc, "terminal.run", {
            "argv": [sys.executable, "-c", "import time; time.sleep(60)"],
            "cwd": str(self.directory), "labels": {"factory.team": self.team}, "no_parent": True, "no_notify": True,
        })["address"]
        self.addCleanup(self.remove, plain)
        self.checked_call(hc, "terminal.send", {"terminal": plain, "text": "hello"})
        term, _ = self.launch()
        target = {"terminal": term}
        self.checked_call(hc, "terminal.list", {"all": True, "labels": {"factory.team": self.team}})
        self.checked_call(hc, "terminal.get", target)
        self.checked_call(hc, "terminal.send", {**target, "text": "hello"})
        self.checked_call(hc, "terminal.keys", {**target, "keys": ["enter"]})
        self.checked_call(hc, "terminal.read", {**target, "lines": 0})
        self.checked_call(hc, "terminal.label", {**target, "labels": {"extra": "one"}})
        self.checked_call(hc, "terminal.keep", {**target, "keep": False})
        self.assert_hc_error("invalid_params", hc.call, "terminal.send", target)
        self.state(term, "idle")
        self.assert_hc_error("status_mismatch", hc.call, "terminal.send", {**target, "text": "x", "if": "working"})
        self.checked_call(hc, "terminal.kill", target)
        self.assert_hc_error("finished", hc.send, term, "x")
        self.checked_call(hc, "terminal.rm", target)
        self.assert_hc_error("spawn_failed", hc.run, ["/nonexistent/factory-agent"], str(self.directory), {}, {})
        doc = self.checked_call(hc, "doc.get", {"name": "herdr-app"})
        result = self.checked_call(hc, "doc.set", {"name": "herdr-app", **doc})
        self.assertEqual(hc.doc_get()[0], doc["value"])
        self.assertGreater(result["version"], doc["version"])

    @real_only
    def test_view_states_with_fake_agents(self):
        self.launch("silent", name="silent")
        self.launch(name="idle")
        blocked, _ = self.launch("dialog", name="blocked")
        self.state(blocked, "blocked")
        finished, _ = self.launch(name="exited")
        self.hc.kill(finished)
        view = factory.team_view(self.hc, self.team)
        self.assertEqual([view.state(name) for name in ("silent", "idle", "blocked", "exited")],
                         ["starting", "idle", "blocked", "gone"])

    @real_only
    def test_view_gone_after_core_restart(self):
        core = self.enterContext(live.Core("restart"))
        self.use_real_core(core)
        term, _ = self.launch()
        reply = core.server_stop()
        if reply is not None:
            self.assertEqual(self.check_result("server.stop", reply["result"]), [])
        self.until(lambda: core.listening() is False, "private core to stop")
        core.start()
        view = factory.team_view(self.hc, self.team)
        self.assertEqual(view.state("worker"), "gone")
        record = view.finished("worker")
        self.assertEqual((record["address"], record["reason"], record["labels"]["factory.team"]),
                         (term, "server_stop", self.team))


@real_only
class FakeAgentTests(HcBackend, unittest.TestCase):
    def launch_with_reply(self, script):
        reply = self.directory / "reply"
        command = [sys.executable, "-c", "from pathlib import Path; import sys; "
                   "Path(sys.argv[1]).write_text(sys.stdin.read())", str(reply)]
        term, log = self.launch(script, FAKE_REPLY_CMD=json.dumps(command))
        return term, log, reply

    def test_reports(self):
        term, _, reply = self.launch_with_reply("reports")
        self.hc.send(term, "first\nsecond")
        self.until(reply.exists, "reply command")
        self.state(term, "idle")
        self.assertEqual(reply.read_text(), "first\n")

    def test_forgets(self):
        term, _, reply = self.launch_with_reply("forgets")
        initial = self.hc.get(term)["turn"]
        self.hc.send(term, "forgettable")
        self.until(lambda: self.hc.get(term)["turn"] > initial, "completed turn")
        self.assertFalse(reply.exists())

    def test_works_forever(self):
        term, _ = self.launch("works_forever")
        self.hc.send(term, "stay busy")
        self.state(term, "working")
        self.assertEqual(self.hc.get(term)["status"]["state"], "working")

    def test_blocks(self):
        term, _ = self.launch("blocks")
        self.hc.send(term, "ask")
        self.assertEqual(self.state(term, "blocked")["status"]["kind"], "permission")
        self.hc.keys(term, ["enter"])
        self.state(term, "idle")

    def test_crashes(self):
        term, _ = self.launch("crashes")
        try:
            self.hc.send(term, "crash")
        except factory.HcError as error:
            self.assertEqual(error.kind, "finished")
        self.until(lambda: self.hc.get(term)["state"] == "finished", "process exit")
        self.assertEqual(self.hc.get(term)["exit_code"], 1)

    def test_silent(self):
        term, log = self.launch("silent")
        self.hc.send(term, "silent input")
        self.until(lambda: self.inputs(term, log) == ["silent input"], "silent input")
        self.assertFalse(self.hc.get(term)["reported"])

    def test_dialog(self):
        term, _ = self.launch("dialog", FAKE_DIALOG="Trust this folder?", FAKE_DIALOG_KEYS="y")
        self.state(term, "blocked")
        self.assertIn("Trust this folder?", self.hc.read(term))
        self.hc.keys(term, ["y", "enter"])
        self.state(term, "idle")

    def test_fake_line_switches_script(self):
        term, _ = self.launch()
        self.hc.send(term, "first line\nFAKE: blocks")
        self.state(term, "blocked")

    def test_act_lines_append_and_run(self):
        receipt = self.directory / "receipt.txt"
        term, _ = self.launch("reports")
        self.hc.send(term, f"[from @x]\nACT append {receipt} tok-1\nACT run {receipt} exit 3")
        self.until(lambda: receipt.exists() and receipt.read_text() == "tok-1\nexit 3\n", "both actions")

    def test_log_has_every_input(self):
        term, log = self.launch("silent")
        self.hc.send(term, "first\nsecond")
        self.hc.keys(term, ["enter"])
        self.hc.send(term, "third")
        self.until(lambda: self.inputs(term, log) == ["first\nsecond", "", "third"], "all input")
        self.assertTrue(all(entry["at"] > 0 for entry in map(json.loads, log.read_text().splitlines())))


# pi prints a table of configured models, filtered by the query like pi does.
FAKE_PI = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import os, sys
    rows = [l.split() for l in os.environ.get("FAKE_MODELS", "").splitlines() if l.strip()]
    q = sys.argv[-1].lower()
    hits = [r for r in rows if q in r[1].lower()]
    if not hits:
        print(f'No models matching "{q}"'); sys.exit(0)
    print("provider    model    context  max-out  thinking  images")
    for p, m in hits:
        print(f"{p}  {m}  1M  64K  yes  no")
''').lstrip()

# omp prints one box table per provider.
FAKE_OMP = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import collections, os, sys
    rows = [l.split() for l in os.environ.get("FAKE_MODELS", "").splitlines() if l.strip()]
    q = sys.argv[-1].lower()
    by = collections.defaultdict(list)
    for p, m in rows:
        if q in m.lower():
            by[p].append(m)
    if not by:
        print(f'No models matching "{q}"'); sys.exit(0)
    for p, ms in by.items():
        print(f"{p} ({len(ms)})\n┌───┬───┐\n│ model │ context │\n├───┼───┤")
        for m in ms:
            print(f"│ {m} │ 1M │")
        print("└───┴───┘")
''').lstrip()

FAKE_OPENCODE = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import os, sys
    if sys.argv[1:] == ["models", "--standalone"]:
        for l in os.environ.get("FAKE_MODELS", "").splitlines():
            if l.strip():
                p, m = l.split(); print(f"{p}/{m}")
''').lstrip()

FAKE_AGY = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import sys
    if sys.argv[1:] != ["models"]:
        sys.exit(2)
    print("Fetching available models...")
    print("gemini-4-neon-high\tGemini 4 Neon (High)")
    print("gemini-4-neon-low\tGemini 4 Neon (Low)")
    print("gemini-pro-orchestrator-neon\tOrchestrator (Neon)")
''').lstrip()

MODELS = """\
openrouter z-ai/glm-5.3
openrouter z-ai/glm-5.3-flash
openrouter deepseek/deepseek-v4.1-flash-1017
openrouter openai/gpt-6-astra
pax-vllm nvidia/Qwen3.8-Flash-Next-NVFP4
"""

LEVELS = ["low", "medium", "high", "xhigh", "max"]
READY = "Send @operator one line saying `ready` with the say command in your standing instructions, then wait for your task."
CODEX_CACHE = {"models": [
    {"slug": "gpt-6.2-astra", "visibility": "list", "supported_reasoning_levels": [{"effort": e} for e in LEVELS + ["ultra"]]},
    {"slug": "gpt-6.2-sol", "visibility": "list", "supported_reasoning_levels": [{"effort": e} for e in LEVELS + ["ultra"]]},
    {"slug": "gpt-6-luna", "visibility": "list", "supported_reasoning_levels": [{"effort": e} for e in LEVELS]},
    {"slug": "gpt-6-astra", "visibility": "list", "supported_reasoning_levels": [{"effort": e} for e in LEVELS + ["ultra"]]},
    {"slug": "gpt-6.1-sol", "visibility": "list", "supported_reasoning_levels": [{"effort": e} for e in LEVELS + ["ultra"]]},
    {"slug": "gpt-6-sol", "visibility": "list", "supported_reasoning_levels": [{"effort": e} for e in LEVELS]},
    {"slug": "gpt-reserve", "visibility": "hide", "supported_reasoning_levels": [{"effort": e} for e in LEVELS]},
]}


def team_brief(settings, body="## Workflow\n\n1. @operator hands out work.\n"):
    return "+++\n" + textwrap.dedent(settings).strip() + "\n+++\n" + body


def formation(members, **tables):
    """Ordinary fixture tables; syntax/error tests use literal TOML instead."""
    sections = {**tables, **{f"members.{name}": fields for name, fields in members.items()}}
    return team_brief("\n".join(f"[{name}]\n" + "\n".join(f"{k} = {json.dumps(v)}" for k, v in fields.items())
                                for name, fields in sections.items()))


def assert_todo_items(case, text):
    """Check ticket numbers, item spacing, and indentation of details and nested lists."""
    lines = text.splitlines()
    checkbox = ("- [ ]", "- [x]")
    numbers = set()
    in_item = False
    for i, line in enumerate(lines):
        if line.startswith("## "):
            in_item = False
        elif line.startswith(checkbox):
            match = re.match(r"^- \[[ x]\] \(([1-9][0-9]*)\) \S", line)
            case.assertIsNotNone(match, f"expected a numbered TODO item in {line!r}")
            number = match[1]
            case.assertNotIn(number, numbers, f"duplicate TODO number ({number})")
            numbers.add(number)
            if in_item:
                case.assertEqual(lines[i - 1], "", f"missing blank line before {line}")
                case.assertTrue(lines[i - 2].strip(), f"extra blank line before {line}")
            in_item = True
        elif in_item and line.strip():
            case.assertTrue(lines[i - 1].strip(), f"blank line inside item before {line}")
            if lines[i - 1].startswith(checkbox):
                case.assertRegex(line, r"^    \* \S")
            elif re.match(r"\s*(?:[-+*]|\d+[.)])(?:\s|$)", line):
                case.assertRegex(line, r"^(?:    \*|        (?:[-+*]|\d+[.)])) \S")
    case.assertTrue(numbers, "expected a TODO checkbox item")


CATALOG_MEMBERS = {
    "architect": {"harness": "claude", "model": "claude-fable-5-2", "thinking": "high"},
    "product": {"harness": "claude", "model": "claude-opus-5-5", "thinking": "medium"},
    "lead-dev": {"harness": "codex", "model": "gpt-6-astra", "thinking": "high"},
    "backend-dev": {"harness": "pi", "model": "z-ai/glm-5.3", "thinking": "medium"},
    "frontend-dev": {"harness": "claude", "model": "claude-opus-5-5", "thinking": "medium"},
    "designer": {"harness": "claude", "model": "claude-sonnet-5-5", "thinking": "high"},
    "reviewer": {"harness": "codex", "model": "gpt-6-luna", "thinking": "high"},
    "pm": {"harness": "pi", "model": "deepseek/deepseek-v4.1-flash-1017", "thinking": "low"},
    "qa-frontend": {"harness": "claude", "model": "claude-sonnet-5-5", "thinking": "medium"},
    "qa-backend": {"harness": "pi", "model": "z-ai/glm-5.3-flash", "thinking": "medium"},
}
CATALOG = formation(CATALOG_MEMBERS)
UI = formation({n: CATALOG_MEMBERS[n] for n in ("lead-dev", "frontend-dev", "reviewer", "pm")})


class Env:
    """A temporary repo with a fixture brief, fake hc and harness binaries, and a Codex model cache."""

    def __init__(self, brief=CATALOG, harnesses=factory.HARNESSES, models=MODELS, git=False):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".codex/models_cache.json").write_text(json.dumps(CODEX_CACHE))
        self.repo = self.root / "repo"
        (self.repo / "sub").mkdir(parents=True)
        if brief is not None:
            self.write_brief(brief)
        bin_ = self.root / "bin"
        bin_.mkdir()
        (bin_ / "python3").symlink_to(sys.executable)
        scripts = {"pi": FAKE_PI, "omp": FAKE_OMP, "opencode": FAKE_OPENCODE, "agy": FAKE_AGY,
                   "claude": "#!/bin/sh\n", "codex": "#!/bin/sh\n"}
        for name, body in scripts.items():
            if name in harnesses:
                (bin_ / name).write_text(body)
                (bin_ / name).chmod(0o755)
        hc_dir = self.root / "hc"
        hc_dir.mkdir()
        self.env = {**{key: value for key, value in os.environ.items() if not key.startswith("HC_")},
                    "HC_BIN": str(pathlib.Path(fake_hc.__file__).resolve()), "FAKE_HC_DIR": str(hc_dir),
                    "PATH": str(bin_), "HOME": str(self.home), "XDG_CACHE_HOME": str(self.root / "cache"),
                    "FAKE_MODELS": models}
        self.env.pop("CODEX_HOME", None)
        if git:  # a real repo with one commit, for worktrees
            (bin_ / "git").symlink_to(GIT)
            for cmd in (["init", "-q", "-b", "main"], ["add", "-A"],
                        ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"]):
                subprocess.run([GIT, "-C", str(self.repo), *cmd], check=True, capture_output=True,
                               env={**self.env, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})

    def write_brief(self, text):
        """A fixture's settings become factory.toml and its prose FACTORY.md."""
        head, _, body = text.removeprefix("+++\n").partition("\n+++\n")
        (self.repo / "factory.toml").write_text(head + "\n")
        (self.repo / "FACTORY.md").write_text(body)

    def run(self, *args, check=True, cwd=None, **env):
        command = list(args)
        command[1:1] = ["--cwd", str(cwd or self.repo)]
        p = subprocess.run([sys.executable, str(SCRIPT), *command],
                           capture_output=True, text=True, env={**self.env, **env})
        if check and p.returncode:
            raise AssertionError(p.stderr + p.stdout)
        return p

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.tmp.cleanup()


def flag(args, name):
    return args[args.index(name) + 1]


class UpTests(unittest.TestCase):
    def launches(self, e, *names):
        """{name: (argv, env)}: the checked plans' native launch arguments, as up passes them."""
        with patch.dict(os.environ, e.env, clear=True):
            cfg = factory.Config(e.repo)
            plans = factory.checked(cfg, [factory.plan(cfg, n) for n in names or cfg.members])
            return {m["name"]: factory.launch_argv(m, pathlib.Path("/p.md")) for m in plans}


    def test_failed_check_launches_nothing_and_explains_the_fix(self):
        with Env(team_brief('''
            [members.backend-dev]
            harness = "pi"
            model = "z-ai/glm-9"
            [members.lead-dev]
            harness = "codex"
            model = "gpt-6-luna"
        ''')) as e:
            p = e.run("check", check=False)
            self.assertEqual(p.returncode, 2)
            self.assertIn("Cannot launch: 1 member(s)", p.stdout)
            self.assertIn("pi has no model z-ai/glm-9 configured", p.stdout)
            self.assertIn("OpenRouter key", p.stdout)


    def test_thinking_levels_are_guarded(self):
        cases = [
            ("claude", "claude-opus-5-5", "max", "not allowed"),
            ("codex", "gpt-6-astra", "ultra", "not allowed"),
            ("codex", "gpt-6.2-astra", "xhigh", None),
            ("codex", "gpt-6.2-sol", "xhigh", None),
            ("codex", "gpt-6-luna", "xhigh", None),
            ("pi", "z-ai/glm-5.3", "max", None),
            ("pi", "z-ai/glm-5.3", "turbo", "has no thinking level"),
        ]
        for harness, model, thinking, error in cases:
            with self.subTest(harness=harness, thinking=thinking), Env(formation({
                "lead-dev": {"harness": harness, "model": model, "thinking": thinking},
            })) as e:
                result = e.run("check", "lead-dev", check=False)
                self.assertEqual(result.returncode, 2 if error else 0)
                if error:
                    self.assertIn(error, result.stdout)

    def test_model_availability_and_missing_model(self):
        with Env(formation({
            'lead-dev': {'harness': 'codex', 'model': 'gpt-7-nova'},
            'frontend-dev': {'harness': 'claude', 'model': 'z-ai/glm-5.3'},
            'designer': {'harness': 'claude', 'model': 'claude-opus-9'},
            'pm': {'harness': 'claude'},
        })) as e:
            result = e.run("check", "lead-dev", "frontend-dev", "designer", "pm", check=False)
            out = result.stdout
            self.assertIn("Codex does not offer gpt-7-nova", out)
            self.assertNotIn("gpt-reserve", out)  # hidden Codex models are not offered
            self.assertIn("z-ai/glm-5.3 is not a Claude model", out)
            self.assertIn("claude-opus-9 is not among", result.stderr)
            self.assertIn("no model set", out)

    def test_unknown_claude_model_warns_without_blocking(self):
        with Env(formation({"reviewer": {"harness": "claude", "model": "claude-future-9"}})) as e:
            result = e.run("check", "reviewer")
            self.assertIn("claude-future-9 is not among", result.stderr)
            with contextlib.redirect_stderr(io.StringIO()):
                argv = self.launches(e, "reviewer")["reviewer"][0]
            self.assertEqual(flag(argv, "--model"), "claude-future-9")

    def test_harness_not_installed(self):
        with Env(harnesses=("claude", "pi")) as e:
            out = e.run("check", "lead-dev", check=False).stdout
            self.assertIn("codex is not installed", out)
            self.assertIn("npm i -g @openai/codex", out)

    def test_local_model_and_ambiguous_providers(self):
        with Env(formation({
            'pm': {'harness': 'omp', 'model': 'nvidia/Qwen3.8-Flash-Next-NVFP4'},
            'backend-dev': {'harness': 'pi', 'model': 'z-ai/glm-5.3'},
            'qa-backend': {'harness': 'omp', 'model': 'z-ai/glm-5.3'},
        }), models=MODELS + "ollama z-ai/glm-5.3\n") as e:
            out = e.run("check", "pm", "backend-dev", "qa-backend", check=False).stdout
            self.assertIn("pax-vllm/nvidia/Qwen3.8-Flash-Next-NVFP4", out)  # omp's box table keeps the provider
            self.assertIn("pi serves z-ai/glm-5.3 from several providers: ollama/z-ai/glm-5.3, openrouter/z-ai/glm-5.3", out)
            self.assertIn("omp serves z-ai/glm-5.3 from several providers", out)

    def test_opencode_member_uses_its_config_and_standalone_server(self):
        with Env(formation({
            'reviewer': {'harness': 'opencode', 'model': 'z-ai/glm-5.3', 'thinking': 'high'},
        })) as e:
            argv, env = self.launches(e, "reviewer")["reviewer"]
            self.assertEqual(argv, ["--standalone", "--auto"])
            self.assertEqual(json.loads(env["OPENCODE_CONFIG_CONTENT"]), {"model": "openrouter/z-ai/glm-5.3#high"})
            cfg = factory.Config(e.repo)
            m = factory.plan(cfg, "reviewer")
            text = factory.message_for(cfg, m, [m], "operator", "ready")
            self.assertIn("You are @reviewer", text)  # OpenCode gets its instructions as a message
            self.assertIn("# Team brief", text)


    def test_agy_trusted_merges_into_the_settings_it_already_passes(self):
        self.assertEqual(factory.agy_trusted(["--mode", "x", "--settings", '{"a":1}'], "/w"),
                         ["--mode", "x", "--settings", '{"a":1,"trustedWorkspaces":["/w"]}'])
        self.assertEqual(factory.agy_trusted(["--mode", "x"], "/w"),
                         ["--mode", "x", "--settings", '{"trustedWorkspaces":["/w"]}'])

    def test_agy_model_variants_and_guarded_permissions(self):
        cases = {"pm": ("gemini-4-neon", "low", "gemini-4-neon-low"),
                 "reviewer": ("gemini-4-neon-low", None, "gemini-4-neon-low"),
                 "designer": ("gemini-pro-orchestrator-neon", None, "gemini-pro-orchestrator-neon")}
        members = {name: {"harness": "agy", "model": model, "permissions": "guarded",
                          **({"thinking": level} if level else {})} for name, (model, level, _) in cases.items()}
        with Env(formation(members)) as e:
            launches = self.launches(e)
            with patch.dict(os.environ, e.env, clear=True):
                cfg = factory.Config(e.repo)
                dirs = {m["name"]: m["dir"] for m in factory.checked(cfg, [factory.plan(cfg, n) for n in cfg.members])}
            for name, (_, _, wanted) in cases.items():
                argv = launches[name][0]
                self.assertEqual(argv[:5], ["--model", wanted, "--mode", "accept-edits", "--settings"])
                # LC-7: the member's own folder is trusted for this launch only, in the session-only settings
                self.assertEqual(json.loads(argv[5]), {"toolPermission": "always-proceed",
                                                       "trustedWorkspaces": [str(dirs[name])]})
                self.assertEqual(argv.count("--settings"), 1)
            brief = e.run("brief").stdout
            self.assertIn("gemini-4-neon-low, low", brief)
            self.assertIn("gemini-pro-orchestrator-neon, default", brief)
            self.assertIn("agy offers: gemini-4-neon-high, gemini-4-neon-low, gemini-pro-orchestrator-neon",
                          e.run("harnesses").stdout)

    def test_agy_preflight_rejects_missing_models_and_conflicting_levels(self):
        with Env(formation({
            "pm": {"harness": "agy", "model": "gemini-4-neon-low", "thinking": "high"},
            "reviewer": {"harness": "agy", "model": "gemini-4-neon", "thinking": "max"},
            "designer": {"harness": "agy", "model": "Gemini 4 Neon (High)"},
        })) as e:
            result = e.run("check", check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("gemini-4-neon-low has no thinking level 'high'", result.stdout)
            self.assertIn("Use one of: low.", result.stdout)
            self.assertIn("gemini-4-neon has no thinking level 'max'", result.stdout)
            self.assertIn("agy does not offer Gemini 4 Neon (High)", result.stdout)
        with Env(formation({"pm": {"harness": "agy"}}), harnesses=("claude",)) as e:
            self.assertIn("agy is not installed", e.run("check", check=False).stdout)

    def test_permissions_and_extra_args_come_from_the_brief(self):
        with Env(team_brief('''
            permissions = "guarded"
            [members.lead-dev]
            harness = "codex"
            model = "gpt-6-luna"
            args = ["--search"]
            [members.frontend-dev]
            harness = "claude"
            model = "claude-opus-5-5"
            [members.reviewer]
            harness = "codex"
            model = "gpt-6-astra"
            permissions = "full"
            [members.pm]
            harness = "omp"
            model = "z-ai/glm-5.3"
        ''')) as e:
            launches = self.launches(e, "lead-dev", "frontend-dev", "reviewer", "pm")
            starts = {n: argv for n, (argv, _) in launches.items()}
            self.assertEqual(starts["lead-dev"][-4:], ["--approve-for-me", "-c", "sandbox_workspace_write.network_access=true", "--search"])
            self.assertNotIn("-s", starts["lead-dev"])  # --approve-for-me cannot be combined with -s
            self.assertEqual(flag(starts["frontend-dev"], "--permission-mode"), "auto")
            self.assertNotIn("network", json.loads(factory.claude_settings("guarded", e.root / "m"))["sandbox"])
            self.assertEqual(starts["reviewer"][-1], "--dangerously-bypass-approvals-and-sandbox")
            self.assertEqual(starts["pm"][-2:], ["--approval-mode", "yolo"])  # omp has no guard that lets messages through
            e.write_brief(team_brief('''
            permissions = "yolo"
            [members.pm]
            harness = "claude"
        '''))
            self.assertIn("must be one of full, guarded", e.run("check", "pm", check=False).stderr)


    def test_full_claude_allows_unix_sockets_without_a_specific_socket(self):
        with patch.dict(os.environ, {"HC_TERMINAL_SOCKET": ""}):
            full = json.loads(factory.claude_settings("full", "/s/messages/x"))["sandbox"]
            self.assertEqual(full["network"], {"allowAllUnixSockets": True})
            guarded = json.loads(factory.claude_settings("guarded", "/s/messages/x"))["sandbox"]
            self.assertNotIn("network", guarded)


    def test_existing_member_settings_only_come_from_the_brief(self):
        with Env(git=True) as e:
            original = (e.repo / "factory.toml").read_bytes()
            for args in (("--role", "reviewer"), ("--model", "gpt-6-luna"), ("--harness", "claude"),
                         ("--thinking", "low"), ("--worktree",), ("--team", "ui")):
                p = e.run("up", "lead-dev", *args, check=False)
                self.assertEqual(p.returncode, 2)
                self.assertIn("unrecognized arguments", p.stderr)
            self.assertEqual((e.repo / "factory.toml").read_bytes(), original)
            with fake_hc.state_at(e.env["FAKE_HC_DIR"]) as state:
                self.assertEqual(state["received"], [])


@unittest.skipUnless(GIT, "git is not installed")
class WorktreeTests(unittest.TestCase):
    BRIEF = '''
        [members.backend-dev]
        harness = "codex"
        role = "backend-dev"
        model = "gpt-6-luna"
        worktree = true
        [members.backend-dev-2]
        harness = "pi"
        role = "backend-dev"
        model = "z-ai/glm-5.3"
        worktree = "feature/login"
        [members.frontend-dev]
        harness = "claude"
        model = "claude-opus-5-5"
        worktree = "feature/login"
        [members.reviewer]
        harness = "codex"
        model = "gpt-6-astra"
    '''

    def test_worktree_members_get_their_own_checkout_and_commit_there(self):
        with Env(team_brief(self.BRIEF), git=True) as e:
            cfg = factory.Config(e.repo)
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                for name in ("backend-dev", "backend-dev-2", "frontend-dev", "reviewer"):
                    m = factory.plan(cfg, name)
                    if m["branch"]:
                        self.assertIsNone(factory.ensure_worktree(cfg, m))
            out = printed.getvalue()
            trees = e.root / "repo.worktrees"
            self.assertEqual(sorted(p.name for p in trees.iterdir()), ["ada-backend-dev", "feature-login"])  # one is shared
            self.assertEqual(out.count("created worktree"), 2)
            branch = lambda d: subprocess.run([GIT, "-C", str(d), "rev-parse", "--abbrev-ref", "HEAD"],
                                              capture_output=True, text=True).stdout.strip()
            self.assertEqual((branch(trees / "ada-backend-dev"), branch(trees / "feature-login")),
                             ("ada/backend-dev", "feature/login"))
            names = ("backend-dev", "backend-dev-2", "frontend-dev", "reviewer")
            where = {n: factory.plan(cfg, n)["dir"] for n in names}
            self.assertEqual(where, {"backend-dev": str(trees / "ada-backend-dev"), "backend-dev-2": str(trees / "feature-login"),
                                     "frontend-dev": str(trees / "feature-login"), "reviewer": str(e.repo)})
            prompt = e.run("brief", "backend-dev").stdout
            self.assertIn("Commit your work on `ada/backend-dev`", prompt)
            self.assertIn("`git merge main`", prompt)
            self.assertIn("agree who commits it for you", prompt)
            self.assertNotIn("Never run `git commit`", prompt)
            staging = "Stage only task files, never `git add -A` or `git add .`"
            self.assertIn(staging, prompt)
            self.assertNotIn("Do not stage files", prompt)
            shared = e.run("brief", "reviewer").stdout
            self.assertIn("Do not stage files. Never run `git commit`, `git merge` or `git push`. "
                          "@operator stages only task files and commits.", shared)
            self.assertNotIn(staging, shared)
            brief = e.run("brief").stdout
            self.assertIn("| branch `feature/login` in `" + str(trees / "feature-login") + "` |", brief)
            self.assertIn("`git diff main...<branch>`", brief)
            self.assertIn("| the main checkout |", brief)
            with contextlib.redirect_stdout(io.StringIO()) as again:
                self.assertIsNone(factory.ensure_worktree(cfg, factory.plan(cfg, "backend-dev")))
            self.assertEqual(again.getvalue(), "")                          # the checkout is kept

    def test_new_worktree_branch_passes_no_track(self):
        env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "branch.autoSetupMerge", "GIT_CONFIG_VALUE_0": "always"}
        with Env(team_brief(self.BRIEF), git=True) as e, patch.dict(os.environ, {**e.env, **env}):
            cfg = factory.Config(e.repo)
            member = factory.plan(cfg, "backend-dev")
            config = e.repo / ".git/config"
            before = config.read_bytes()
            with patch.object(factory.subprocess, "run", wraps=subprocess.run) as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(factory.ensure_worktree(cfg, member))
                self.assertEqual(run.call_args.args[0], [
                    "git", "-C", str(e.repo), "worktree", "add", "-b", "ada/backend-dev", "--no-track", member["dir"],
                ])
            self.assertEqual(config.read_bytes(), before)

    def test_numbered_worktree_and_no_repository(self):
        with Env(formation({**CATALOG_MEMBERS, "reviewer-2": {
            "role": "reviewer", "harness": "codex", "model": "gpt-6-luna", "worktree": True,
        }}), git=True) as e:
            cfg = factory.Config(e.repo)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(factory.ensure_worktree(cfg, factory.plan(cfg, "reviewer-2")))
            self.assertTrue((e.root / "repo.worktrees/ada-reviewer-2").is_dir())
            self.assertIs(factory.Config(e.repo).members["reviewer-2"]["worktree"], True)
        with Env(team_brief(self.BRIEF)) as e:  # not a git repository
            p = e.run("check", "backend-dev", check=False)
            self.assertEqual(p.returncode, 2)
            self.assertIn("a worktree needs a git repository with a commit", p.stdout)

    def test_distinct_branches_cannot_share_a_worktree_path(self):
        brief = formation({
            'backend-dev': {'harness': 'codex', 'model': 'gpt-6-luna', 'worktree': 'feature/a'},
            'backend-dev-2': {'role': 'backend-dev', 'harness': 'codex', 'model': 'gpt-6-luna', 'worktree': 'feature-a'},
        })
        with Env(brief, git=True) as e:
            result = e.run("check", "backend-dev", "backend-dev-2", check=False)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            for needle in ("worktree", "feature/a", "feature-a"):
                self.assertIn(needle, result.stdout)
            self.assertFalse((e.root / "repo.worktrees").exists())

    def test_check_rejects_unregistered_or_wrong_branch_directory(self):
        for existing in ("directory", "wrong-branch"):
            with self.subTest(existing=existing), Env(git=True) as e:
                e.write_brief(CATALOG.replace('[members.lead-dev]', '[members.lead-dev]\nworktree = true'))
                target = e.root / "repo.worktrees/ada-lead-dev"
                if existing == "directory":
                    target.mkdir(parents=True)
                else:
                    subprocess.run([GIT, "-C", str(e.repo), "worktree", "add", "-b", "unrelated", str(target)],
                                   check=True, capture_output=True, env=e.env)
                result = e.run("check", "lead-dev", check=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("worktree", result.stdout)

    def test_branch_names_git_rejects_fail_the_check(self):
        team = ('[members.reviewer]\nharness = "codex"\nmodel = "gpt-6-luna"\n'
                '[members.lead-dev]\nharness = "codex"\nmodel = "gpt-6-luna"\nworktree = {}\n')
        cases = [(f'user = "{u}"\n', "true") for u in (".ada", "ada..x", "ada.lock", "-ada")] + [("", '"feature//x"'), ("", '"x.lock"')]
        for settings, worktree in cases:
            with self.subTest(settings=settings, worktree=worktree), Env(team_brief(settings + team.format(worktree)), git=True) as e:
                p = e.run("check", check=False)
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertIn("is not a branch name git accepts", p.stdout)
                self.assertIn("Change `user`, or this member's `worktree`", p.stdout)


class TeamStateTests(unittest.TestCase):
    def setUp(self):
        self.base = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.base / "repo"
        self.core = "0011223344556677"
        self.enterContext(patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.base / "cache")}))
        self.state = factory.state_dir(self.root, self.core)
        self.metadata = {"root": str(self.root), "team": factory.team_key(self.root), "core": self.core, "up": True}

    def test_state_dir_uses_the_checkout_and_core(self):
        self.assertEqual(self.state, self.base / "cache" / "factory-skill" /
                         factory.team_key(self.root) / "00112233")
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.base / "another-cache")}):
            self.assertEqual(factory.open_log(self.state).state, self.state)
        for invalid in ("relative", str(self.state.parent), str(self.state) + "/../00112233"):
            with self.subTest(invalid=invalid), self.assertRaises(factory.Problem):
                factory.open_log(invalid)
        self.assertFalse(self.state.exists())

    def test_read_team_missing_is_empty_and_unreadable_is_a_problem(self):
        self.assertEqual(factory.read_team(self.state), {})
        self.assertFalse(self.state.exists())
        self.state.mkdir(parents=True)
        self.assertEqual(factory.read_team(self.state), {})
        path = self.state / "team.json"
        for value in (b"torn", b'{"v":1}', json.dumps({"v": True, **self.metadata}).encode()):
            with self.subTest(value=value), self.assertRaisesRegex(factory.Problem, "team.json cannot be read"):
                path.write_bytes(value)
                factory.read_team(self.state)
        path.unlink()
        outside = self.base / "outside.json"
        outside.write_text(json.dumps({"v": 1, **self.metadata}))
        path.symlink_to(outside)
        with self.assertRaisesRegex(factory.Problem, "team.json cannot be read"):
            factory.read_team(self.state)
        self.assertTrue(path.is_symlink())

    def test_update_team_keeps_other_keys_and_is_atomic(self):
        expected = {"v": 1, **self.metadata, "layout": {"tab": "one"}}
        self.assertEqual(factory.update_team(self.state, **self.metadata, layout={"tab": "one"}), expected)
        self.assertEqual(factory.read_team(self.state), expected)
        path = self.state / "team.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with patch.object(factory.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                factory.update_team(self.state, up=False)
        self.assertEqual(list(self.state.iterdir()), [path])
        self.assertEqual(factory.read_team(self.state), expected)
        self.assertEqual(factory.update_team(self.state, up=False), {**expected, "up": False})

    def test_update_team_reset_replaces_unreadable(self):
        self.state.mkdir(parents=True)
        path = self.state / "team.json"
        path.write_text("torn")
        with self.assertRaises(factory.Problem):
            factory.update_team(self.state, **self.metadata)
        self.assertEqual(path.read_text(), "torn")
        factory.update_team(self.state, reset=True, **self.metadata)
        self.assertEqual(factory.read_team(self.state), {"v": 1, **self.metadata})

    def test_log_refresh_rebuilds_an_edited_prefix(self):
        log = factory.open_log(self.state)
        log.add("pm", ["operator"], "one")
        box = factory.Box(log.path)
        log.refresh(box)
        self.assertEqual(box.msgs[1]["text"], "one")
        before = log.path.stat()
        log.path.write_bytes(log.path.read_bytes().replace(b'"one"', b'"two"'))
        os.utime(log.path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
        log.add("pm", ["operator"], "another message")
        log.refresh(box)
        self.assertEqual([msg["text"] for msg in box.msgs.values()], ["two", "another message"])
        absent = factory.open_log(self.state.with_name("absent"))
        partial = factory.Box.for_message(absent.path, 1, ["operator"], 10)
        with self.assertRaisesRegex(ValueError, "partial box"):
            absent.refresh(partial)
        self.assertEqual(list(partial.msgs), [1])


class DeliveryDecisionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = types.SimpleNamespace(operator={"name": "operator"})
        self.box = factory.Box("log.jsonl")

    @staticmethod
    def terminal(name, status, *, kind="member", reported=None, state="live"):
        return {"address": f"t-{name}", "created_ms": 1, "state": state,
                "reported": status is not None if reported is None else reported,
                "status": {"state": status} if status is not None else None,
                "labels": {"factory.kind": kind, "factory.member": name}}

    def message(self, ident, kind="say", **extra):
        msg = {"t": "msg", "id": ident, "from": "operator", "to": ["lead-dev"], "text": str(ident),
               "kind": kind, "at": "2026-11-26T17:00:00Z", **extra}
        self.box.apply([msg, {"t": "ok", "id": ident}])
        return msg

    def briefed(self):
        self.message(1, "brief")
        receipt = {"t": "out", "id": 1, "to": "lead-dev", "term": "t-lead-dev", "at": "2026-11-26T17:00:00Z"}
        self.box.apply([{**receipt, "o": "sending"}, {**receipt, "o": "delivered", "started": True, "turn": 1}])

    def test_operator_rows(self):
        for row, state, since, expected in (
            ("O1", "absent", None, ("queue", "gone")),
            ("O2", None, 6, ("hold",)),
            ("O2 first observation", None, None, ("hold",)),
            ("O3", None, 5, ("latch",)),
            ("O4", "blocked", None, ("queue", "blocked")),
            ("O5", "working", None, ("send", "t-operator")),
        ):
            with self.subTest(row=row):
                records = [] if state == "absent" else [self.terminal("operator", state, kind="operator")]
                decision = factory.decide("operator", {"kind": "say"}, factory.View(records),
                                          self.box, self.cfg, 10, since)
                self.assertEqual(decision, expected)

    def test_stale_note_takes_precedence_over_an_absent_operator(self):
        note = {"kind": "note", "meta": {"what": "quiet"}}
        self.assertEqual(factory.decide("operator", note, factory.View([]), self.box, self.cfg, 10, None,
                                        still_true=False), ("drop", "no longer true"))

    def test_member_rows(self):
        for row, terminal, kind, expected in (
            ("M1", None, "say", ("drop", "stopped by down")),
            ("M2", self.terminal("lead-dev", None, reported=True), "say", ("queue", "gone")),
            ("M2 finished", self.terminal("lead-dev", None, state="finished"), "say", ("queue", "gone")),
            ("M3", self.terminal("lead-dev", None), "brief", ("hold",)),
            ("M4", self.terminal("lead-dev", "blocked"), "say", ("queue", "not ready")),
            ("M5", self.terminal("lead-dev", "blocked"), "brief", ("queue", "blocked")),
            ("M6", self.terminal("lead-dev", "working"), "brief", ("send", "t-lead-dev")),
        ):
            with self.subTest(row=row):
                view = factory.View([] if terminal is None else [terminal])
                self.assertEqual(factory.decide("lead-dev", {"kind": kind}, view, self.box, self.cfg, 0, None),
                                 expected)
        self.briefed()
        view = factory.View([self.terminal("lead-dev", "working")])
        self.assertEqual(factory.decide("lead-dev", {"kind": "say"}, view, self.box, self.cfg, 0, None),
                         ("send", "t-lead-dev"))

    def test_head_prefers_newest_startup_brief_then_preserves_order(self):
        first = self.message(1)
        self.assertEqual(factory.head_of("lead-dev", self.box, False), (first, []))
        older = self.message(2, "brief")
        latest = self.message(3, "brief")
        self.message(4)
        self.assertEqual(factory.head_of("lead-dev", self.box, True), (first, []))
        self.assertEqual(factory.head_of("lead-dev", self.box, False), (latest, [older]))
        self.assertEqual(factory.head_of("another", self.box, False), (None, []))


class ManualClock:
    def __init__(self):
        self.now = 0
        self.start = time.time()

    def monotonic(self):
        return self.now

    def time(self):
        return self.start + self.now

    def sleep(self, seconds):
        self.now += seconds


class TeamCase(HcBackend, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.env = self.enterContext(Env(UI))
        self.enterContext(patch.dict(os.environ, {"XDG_CACHE_HOME": self.env.env["XDG_CACHE_HOME"]}))
        self.root = self.env.repo
        self.cfg = factory.Config(self.root)
        self.team = factory.team_key(self.root)
        self.hc.list(self.team)  # learn the core ID before computing the state path
        self.state_dir = factory.state_dir(self.root, self.hc.core)
        factory.update_team(self.state_dir, root=str(self.root), team=self.team, core=self.hc.core, up=True)
        self.log = factory.open_log(self.state_dir)
        self.clock = ManualClock()
        self.jobs, self.pane = [], []
        self.keeper = factory.Keeper(self.state_dir, self.hc, self.clock, self.pane.append, self.jobs.append)
        self.keeper.title_out = self.title = io.StringIO()

    def terminal(self, name, *, kind="member", status="idle"):
        labels = {"factory.team": self.team, "factory.kind": kind,
                  "factory.member": name, "factory.dir": str(self.state_dir)}
        if REAL_HC:
            terminal, agent_log = self.launch("silent" if status is None else "reports", name=name)
            self.hc.label(terminal, labels)
            return terminal, agent_log
        return self.fake.add(labels, status), None

    def briefed(self, name, terminal):
        ident = self.log.add("operator", [name], "brief completed", kind="brief")[0]
        _, box = self.log.read_box()
        self.log.out(box, ident, name, "sending", term=terminal)
        self.log.out(box, ident, name, "delivered", term=terminal, started=True, turn=0)

    def add(self, name, body="first", **extra):
        return self.log.add("pm", [name], body, **extra)[0]

    def outcome(self, ident, name="lead-dev"):
        return self.log.read_box()[1].outcome(ident, name)

    def run_jobs(self, *, reverse=False):
        jobs, self.jobs[:] = list(self.jobs), []
        for job in reversed(jobs) if reverse else jobs:
            job()

    def say(self, terminal, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        hc_bin = self.hc.bin or factory.HC
        with (patch.dict(os.environ, {"HC_TERMINAL_ID": terminal, "HC_BIN": hc_bin}),
              patch.object(factory, "HC", hc_bin),
              contextlib.redirect_stdout(out), contextlib.redirect_stderr(err)):
            try:
                factory.main(["say", *argv])
            except SystemExit as error:
                code = error.code
        return code, out.getvalue(), err.getvalue()


@fake_only
class TeamCommandTests(TeamCase):
    """v2 up, down, status and brief (launch_layout.md §§2, 6, 8; cli_skill.md §2) on the fake hc, with the keeper
    driven in-process."""

    def setUp(self):
        super().setUp()
        bin_ = self.env.root / "bin"
        self.enterContext(patch.dict(os.environ, {"PATH": f"{bin_}:{os.environ.get('PATH', '')}",
                                                  "HOME": self.env.env["HOME"],
                                                  "FAKE_MODELS": self.env.env["FAKE_MODELS"]}))
        self.enterContext(patch.object(factory, "ensure_keeper", lambda *args, **kwargs: "keeper"))
        real = factory.wait_outcomes

        def wait(log, ident, recipients, offset, timeout=factory.SAY_WAIT):
            self.deliver()
            return real(log, ident, recipients, offset, 0)
        self.enterContext(patch.object(factory, "wait_outcomes", wait))
        self.operator = self.fake.add({}, "working")
        for name in self.cfg.members:
            self.fake.on_run(name, status="idle")

    def deliver(self):
        for _ in range(2):
            self.keeper.tick()
            self.run_jobs()
        self.keeper.tick()

    def cmd(self, terminal, *argv):
        out, err, code = io.StringIO(), io.StringIO(), 0
        hc_bin = self.hc.bin or factory.HC
        env = {"HC_BIN": hc_bin, **({"HC_TERMINAL_ID": terminal} if terminal else {})}
        with (patch.dict(os.environ, env), patch.object(factory, "HC", hc_bin),
              contextlib.redirect_stdout(out), contextlib.redirect_stderr(err)):
            if not terminal:
                os.environ.pop("HC_TERMINAL_ID", None)
            try:
                factory.main([*argv, "--cwd", str(self.root)] if argv[0] != "say" else list(argv))
            except SystemExit as error:
                code = error.code or 0
        return code, out.getvalue(), err.getvalue()

    def member_terms(self):
        return {r["labels"]["factory.member"]: r["address"] for r in self.hc.list(self.team)
                if r["labels"].get("factory.kind") == "member" and r["state"] == "live"}

    def mutations(self):
        changes = ("terminal.run", "terminal.label", "terminal.kill", "terminal.rm", "doc.set")
        return [c for c in self.fake.received() if c.get("method") in changes]

    def test_up_starts_briefs_and_saves_each_launch(self):
        code, out, err = self.cmd(self.operator, "up", "lead-dev", "frontend-dev")
        self.assertEqual(code, 0, out + err)
        terms = self.member_terms()
        self.assertEqual(set(terms), {"lead-dev", "frontend-dev"})
        labels = self.hc.get(self.operator)["labels"]
        self.assertEqual((labels["factory.kind"], labels["factory.member"], labels["name"]),
                         ("operator", "operator", "@operator"))
        _, box = self.log.read_box()
        briefs = [m for m in box.msgs.values() if m["kind"] == "brief"]
        self.assertEqual(sorted(m["to"][0] for m in briefs), ["frontend-dev", "lead-dev"])
        self.assertTrue(all(box.outcome(m["id"], m["to"][0])["o"] == "delivered" for m in briefs))
        for name, term in terms.items():
            self.assertIn(f"started @{name}", out)
            sent = self.fake.sent(term)
            self.assertEqual(len(sent), 1)
            self.assertIn("one line saying `ready`", sent[0])
        newest = factory.saved_sessions(self.state_dir)[0]
        self.assertEqual({n: (e["term"], e["how"]) for n, e in newest.items()},
                         {n: (t, "new") for n, t in terms.items()})
        for name, term in terms.items():                               # the launch's model, for say's sessions
            self.assertEqual(self.hc.get(term)["labels"].get("factory.model"), newest[name]["model"])
        self.assertTrue(factory.read_team(self.state_dir)["up"])
        runs = self.fake.calls("terminal.run")
        self.assertTrue(all(not any(k.startswith("herdr.") for k in r["params"]["labels"]) for r in runs))
        self.assertTrue(self.fake.calls("doc.set"))
        before = len(self.mutations())
        code, out, _ = self.cmd(self.operator, "up")                   # running members are left alone
        self.assertEqual(len(self.member_terms()), 4)
        self.assertIn("already running: @lead-dev, @frontend-dev", out)
        self.assertEqual(len([m for m in self.log.read_box()[1].msgs.values() if m["kind"] == "brief"]), 4)
        self.assertEqual(sum(1 for c in self.mutations()[before:] if c["method"] == "terminal.run"), 2)

    def test_up_labels_are_written_after_doc_set(self):
        term = self.terminal("lead-dev")[0]
        self.hc.label(term, {"herdr.seen": "9"})
        code, out, err = self.cmd(self.operator, "up", "lead-dev")
        self.assertEqual(code, 0, out + err)
        doc, version = self.hc.doc_get()
        space, tab = factory.where(doc, term)
        calls = self.mutations()
        write = next(i for i, req in enumerate(calls) if req["method"] == "doc.set")
        label = next(i for i, req in enumerate(calls) if req["method"] == "terminal.label"
                     and "herdr.space" in req["params"]["labels"])
        self.assertGreater(label, write)
        self.assertEqual(calls[label]["params"]["labels"], {"herdr.space": space, "herdr.tab": tab})
        self.assertEqual(self.hc.get(term)["labels"]["herdr.seen"], "9")
        self.assertIsNone(factory.where(doc, self.operator))
        self.assertEqual(factory.read_team(self.state_dir)["layout"],
                         {"space": space, "tab": tab, "keeper": "keeper", "made_space": True})
        self.assertEqual(self.cmd(self.operator, "up", "lead-dev")[0], 0)
        self.assertEqual(self.hc.doc_get(), (doc, version))

    def test_up_recovers_existing_ids_without_a_write_and_keeps_newly_proposed_ids_out(self):
        self.assertEqual(self.cmd(self.operator, "up", "lead-dev")[0], 0)
        layout = factory.read_team(self.state_dir)["layout"]
        _, version = self.hc.doc_get()
        factory.update_team(self.state_dir, layout={})
        self.assertEqual(self.cmd(self.operator, "up", "lead-dev")[0], 0)
        self.assertEqual(self.hc.doc_get()[1], version)
        self.assertEqual(factory.read_team(self.state_dir)["layout"]["tab"], layout["tab"])
        doc, version = self.hc.doc_get()
        tab = doc["spaces"][0]["tabs"][0]
        tab["id"] = "owner-tab"
        tab["root"]["children"].append(LayoutTests.pane(self.operator))
        self.hc.doc_set(doc, version)
        factory.update_team(self.state_dir, layout={})
        before = self.hc.doc_get()
        self.assertEqual(self.cmd(self.operator, "up", "lead-dev")[0], 0)
        self.assertEqual(factory.read_team(self.state_dir)["layout"], {})
        self.assertEqual(self.hc.doc_get(), before)

    def test_up_swaps_a_restarted_member_in_its_moved_pane_without_creating_a_space(self):
        self.assertEqual(self.cmd(self.operator, "up", "lead-dev")[0], 0)
        old = self.member_terms()["lead-dev"]
        doc, version = self.hc.doc_get()
        moved, keeper = factory.blocks(doc)[old], factory.blocks(doc)["keeper"]
        moved["size"] = {"weight": 2}
        owner_tab = LayoutTests.tab("owner-tab", moved, keeper, LayoutTests.pane(self.operator))
        doc = {"spaces": [LayoutTests.space("owner-space", owner_tab)]}
        self.hc.doc_set(doc, version)
        factory.update_team(self.state_dir, layout={})
        self.fake.finish(old)
        code, out, err = self.cmd(self.operator, "up", "lead-dev")
        self.assertEqual(code, 0, out + err)
        new = self.member_terms()["lead-dev"]
        self.assertNotEqual(new, old)
        doc, _ = self.hc.doc_get()
        self.assertEqual([space["id"] for space in doc["spaces"]], ["owner-space"])
        self.assertEqual(factory.blocks(doc)[new], {**moved, "content": {"terminal": new}})
        self.assertEqual(factory.where(doc, new), ("owner-space", "owner-tab"))
        labels = self.hc.get(new)["labels"]
        self.assertEqual((labels["herdr.space"], labels["herdr.tab"]), ("owner-space", "owner-tab"))
        self.assertNotIn("herdr.seen", labels)
        self.assertEqual(factory.read_team(self.state_dir)["layout"], {})

    def test_replaced_keeper_gets_its_old_pane(self):
        self.assertEqual(self.cmd(self.operator, "up", "lead-dev")[0], 0)
        doc, _ = self.hc.doc_get()
        old = factory.blocks(doc)["keeper"]
        replacement = self.terminal("keeper", kind="keeper")[0]
        with patch.object(factory, "ensure_keeper", return_value=replacement):
            self.assertEqual(self.cmd(self.operator, "up", "lead-dev")[0], 0)
        doc, _ = self.hc.doc_get()
        self.assertEqual(factory.blocks(doc)[replacement], {**old, "content": {"terminal": replacement}})
        self.assertEqual(factory.read_team(self.state_dir)["layout"]["keeper"], replacement)

    def test_layout_failure_does_not_change_up_exit_code_or_write_placement_labels(self):
        self.fake.doc_conflicts(factory.CAS_TRIES)
        code, out, err = self.cmd(self.operator, "up", "lead-dev")
        self.assertEqual(code, 0, out + err)
        self.assertIn("layout not updated (another client kept changing it)", err)
        self.assertFalse(any("herdr.space" in r["params"]["labels"] for r in self.fake.calls("terminal.label")))
        self.assertNotIn("layout", factory.read_team(self.state_dir))

    def test_layout_metadata_write_failure_is_only_a_warning(self):
        real = factory.update_team

        def update(state, **changes):
            if "layout" in changes:
                raise OSError("test metadata failure")
            return real(state, **changes)

        with patch.object(factory, "update_team", update):
            code, out, err = self.cmd(self.operator, "up", "lead-dev")
        self.assertEqual(code, 0, out + err)
        self.assertIn("layout placed, but team.json was not updated: test metadata failure", err)
        term = self.member_terms()["lead-dev"]
        self.assertIn(term, factory.blocks(self.hc.doc_get()[0]))
        self.assertNotIn("layout", factory.read_team(self.state_dir))
        self.assertEqual(self.cmd(self.operator, "up", "lead-dev")[0], 0)
        old = factory.read_team(self.state_dir)["layout"]
        with patch.object(factory, "update_team", update):
            code, out, err = self.cmd(self.operator, "down")
        self.assertEqual(code, 0, out + err)
        self.assertIn("layout updated, but team.json was not updated: test metadata failure", err)
        self.assertNotIn(term, factory.blocks(self.hc.doc_get()[0]))
        self.assertEqual(factory.read_team(self.state_dir)["layout"], old)

    def test_conflict_recomputes_the_winning_tab_and_keeps_an_owner_moved_pane(self):
        lead = self.terminal("lead-dev")[0]
        space = LayoutTests.space("a", LayoutTests.tab("first", LayoutTests.pane(lead)))
        self.hc.doc_set({"spaces": [space]}, 0)
        real_set = factory.Hc.doc_set
        changed = False

        def conflict(hc, doc, version):
            nonlocal changed
            if not changed:
                changed = True
                owner = LayoutTests.tab("moved-to", LayoutTests.pane(lead))
                real_set(hc, {"spaces": [LayoutTests.space("b", owner)]}, version)
            return real_set(hc, doc, version)

        with patch.object(factory.Hc, "doc_set", conflict):
            code, out, err = self.cmd(self.operator, "up", "reviewer")
        self.assertEqual(code, 0, out + err)
        doc, _ = self.hc.doc_get()
        reviewer = self.member_terms()["reviewer"]
        self.assertEqual(factory.where(doc, lead), ("b", "moved-to"))
        self.assertEqual(factory.where(doc, reviewer), ("b", "moved-to"))
        labels = self.hc.get(reviewer)["labels"]
        self.assertEqual((labels["herdr.space"], labels["herdr.tab"]), ("b", "moved-to"))
        self.assertEqual(factory.read_team(self.state_dir)["layout"]["space"], "b")
        self.assertNotIn("herdr.tab", self.hc.get(lead)["labels"])  # an existing pane was left alone

    def test_up_refuses_before_changing_anything(self):
        member = self.terminal("lead-dev")[0]
        quiet = self.fake.add({}, None)
        other = self.fake.add({"factory.team": "elsewhere", "factory.kind": "member", "factory.member": "x"}, "idle")
        for caller, reason in ((None, "run the operator inside an hc terminal"),
                               (member, "run up from the operator's pane, not a member's"),
                               (quiet, "reports no agent status to hc"),
                               (other, "of team elsewhere")):
            with self.subTest(reason=reason):
                before = len(self.mutations())
                code, _, err = self.cmd(caller, "up")
                self.assertEqual(code, 2)
                self.assertIn(reason, err)
                self.assertEqual(self.mutations()[before:], [])
        reachable = self.terminal("operator", kind="operator", status="working")[0]
        code, _, err = self.cmd(self.operator, "up")
        self.assertEqual(code, 2)
        self.assertIn(f"@operator is running in {reachable}; use that pane", err)
        self.assertEqual(self.hc.get(reachable)["labels"]["factory.kind"], "operator")

    def test_up_is_one_at_a_time(self):
        lock = factory.lifecycle_lock(self.state_dir)
        self.addCleanup(os.close, lock)
        code, _, err = self.cmd(self.operator, "up")
        self.assertEqual(code, 2)
        self.assertIn("another up, down or brief --send is running for this team", err)
        self.assertFalse(self.fake.calls("terminal.run"))

    def test_a_failed_preflight_changes_nothing(self):
        self.env.write_brief(formation({**{n: CATALOG_MEMBERS[n] for n in ("lead-dev", "reviewer")},
                                        "pm": {"harness": "pi", "model": "z-ai/glm-9"}}))
        before = len(self.mutations())
        code, out, _ = self.cmd(self.operator, "up")
        self.assertEqual(code, 2)
        self.assertIn("pi has no model z-ai/glm-9 configured", out)
        self.assertEqual(self.mutations()[before:], [])

    def test_a_gone_member_gets_a_new_terminal_before_its_old_record_goes(self):
        old = self.terminal("lead-dev")[0]
        self.briefed("lead-dev", old)
        self.fake.finish(old, exit_code=1)
        code, out, err = self.cmd(self.operator, "up", "lead-dev")
        self.assertEqual(code, 0, out + err)
        self.assertIn("@lead-dev interrupted: exited (code 1) (state unknown). Last message to it: #1", out)
        calls = [(c["method"], c["params"].get("terminal")) for c in self.mutations()]
        run, rm = calls.index(("terminal.run", None)), calls.index(("terminal.rm", old))
        self.assertLess(run, rm)
        self.assertIn("starts in a new conversation: none was recorded for it", out)

    def test_a_brief_still_in_flight_points_to_status_not_a_second_send(self):
        with patch.object(factory, "wait_outcomes",
                          lambda log, ident, recipients, offset, timeout=0: {recipients[0]: {"o": "queued",
                                                                                           "why": "waiting"}}):
            code, out, err = self.cmd(self.operator, "up", "lead-dev")
        self.assertEqual(code, 3)
        self.assertIn("is still queued (waiting); the keeper delivers it when it can. Run factory.py status.", out)
        self.assertIn("Then run: factory.py status", err)
        self.assertNotIn("brief --send", err)

    def test_up_with_everyone_running_still_starts_the_keeper(self):
        self.cmd(self.operator, "up", "lead-dev")
        calls = []
        with patch.object(factory, "ensure_keeper", lambda *args, **kwargs: calls.append(kwargs) or "keeper"):
            code, out, _ = self.cmd(self.operator, "up", "lead-dev")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [{"force": True}])

    def test_up_never_asks_for_a_second_brief_when_storage_or_outcome_is_unknown(self):
        with patch.object(factory.Log, "add", side_effect=OSError("disk gone")):
            code, _, err = self.cmd(self.operator, "up", "lead-dev")
        self.assertEqual(code, 3)
        self.assertIn("its brief may be stored: disk gone", err)
        self.assertNotIn("brief --send", err)
        with patch.object(factory, "wait_outcomes", side_effect=OSError("cannot read")):
            code, _, err = self.cmd(self.operator, "up", "reviewer")
        self.assertEqual(code, 3)
        self.assertIn("is stored; its outcome could not be read (cannot read). Then run: factory.py status", err)

    def test_an_unrecorded_stop_is_finished_by_the_next_down_and_blocks_up(self):
        self.cmd(self.operator, "up", "lead-dev")
        term = self.member_terms()["lead-dev"]
        with patch.object(factory, "mark_stopped", side_effect=OSError("no space")):
            code, out, _ = self.cmd(self.operator, "down", "lead-dev")
        self.assertEqual(code, 1)
        self.assertIn(f"its record {term} is kept. Run factory.py down lead-dev again.", out)
        code, _, err = self.cmd(self.operator, "up", "lead-dev")
        self.assertEqual(code, 2)
        self.assertIn("the last stop of @lead-dev was not recorded; run factory.py down lead-dev first", err)
        code, out, err = self.cmd(self.operator, "down", "lead-dev")
        self.assertEqual(code, 0, out + err)
        self.assertIn("stopped @lead-dev (was stopped)", out)
        self.assertEqual(factory.read_team(self.state_dir)["stopped"], [term])
        self.assertNotIn(term, [r["address"] for r in self.hc.list(self.team)])
        self.assertEqual(self.cmd(self.operator, "up", "lead-dev")[0], 0)

    def test_a_record_that_cannot_be_removed_still_sweeps_and_exits_1(self):
        self.cmd(self.operator, "up", "lead-dev")
        self.log.add("pm", ["lead-dev"], "for later")

        def refuse(hc, term):
            raise factory.HcError("live", "still there")
        with patch.object(factory.Hc, "rm", refuse):
            code, out, _ = self.cmd(self.operator, "down", "lead-dev")
        self.assertEqual(code, 1)
        self.assertIn("could not be removed (", out)
        self.assertIn("stopped @lead-dev (was idle); 1 queued messages dropped", out)

    def test_a_failed_sweep_keeps_the_records_so_up_waits(self):
        self.cmd(self.operator, "up", "lead-dev")
        term = self.member_terms()["lead-dev"]
        ident = self.add("lead-dev", "for later")
        with patch.object(factory.Log, "drop_queued", side_effect=OSError("disk full")):
            code, out, _ = self.cmd(self.operator, "down", "lead-dev")
        self.assertEqual(code, 1)
        self.assertIn("their records are kept. Run factory.py down lead-dev again.", out)
        self.assertIn(term, [r["address"] for r in self.hc.list(self.team)])
        self.assertEqual(self.cmd(self.operator, "up", "lead-dev")[0], 2)          # no replacement yet
        code, out, err = self.cmd(self.operator, "down", "lead-dev")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.outcome(ident)["o"], "dropped")
        self.assertNotIn(term, [r["address"] for r in self.hc.list(self.team)])
        self.assertEqual(self.cmd(self.operator, "up", "lead-dev")[0], 0)
        self.assertFalse(any("for later" in text for text in self.fake.sent(self.member_terms()["lead-dev"])))

    def test_a_fresh_launch_never_falls_back_to_the_old_conversation(self):
        old = self.terminal("lead-dev")[0]
        factory.record_session(self.cfg, self.state_dir, "lead-dev", old, {"CODEX_THREAD_ID": "old-conversation"})
        self.fake.finish(old)
        def in_flight(log, ident, recipients, offset, timeout=0):
            return {recipients[0]: {"o": "queued", "why": "waiting"}}
        with patch.object(factory, "wait_outcomes", in_flight):
            code, _, _ = self.cmd(self.operator, "up", "lead-dev", "--fresh")
        self.assertEqual(code, 3)                                      # its brief is still in flight
        new = self.member_terms()["lead-dev"]
        self.deliver()                                                 # delivered after up gave up waiting
        self.fake.finish(new)                                          # and it ends before any say
        entry = factory.saved_sessions(self.state_dir)[0]["lead-dev"]
        self.assertEqual((entry["term"], entry["how"], entry["value"]), (new, "new", None))
        m = factory.plan(self.cfg, "lead-dev", self.state_dir)
        self.assertFalse(factory.session_plan(m, self.state_dir, entry, fresh=False)["resume"])

    def test_a_fresh_launch_that_cannot_be_recorded_is_stopped(self):
        old = self.terminal("lead-dev")[0]
        factory.record_session(self.cfg, self.state_dir, "lead-dev", old, {"CODEX_THREAD_ID": "old-conversation"})
        self.fake.finish(old)
        with patch.object(factory, "launch_entry", side_effect=OSError("disk full")):
            code, _, err = self.cmd(self.operator, "up", "lead-dev", "--fresh")
        self.assertEqual(code, 3)
        self.assertIn("its fresh launch could not be recorded (disk full), so up stopped it", err)
        self.assertIn("then factory.py up lead-dev --fresh", err)                # the retry stays fresh
        self.assertEqual(self.member_terms(), {})                               # nothing runs unrecorded
        self.assertIn(old, [r["address"] for r in self.hc.list(self.team)])     # the old record stays
        self.assertFalse([m for m in self.log.read_box()[1].msgs.values() if m["kind"] == "brief"])

    def test_the_launch_entry_gives_way_to_say_in_either_order(self):
        m = {**factory.plan(self.cfg, "frontend-dev", self.state_dir), "model": "claude-opus-5-5"}
        for order in ("say first", "say after"):
            with self.subTest(order=order):
                term = f"t-{order[4:9]}"
                factory.launch_entry(self.state_dir, m, term, "new", None)            # at its start
                if order == "say first":
                    factory.record_session(self.cfg, self.state_dir, "frontend-dev", term,
                                           {"CLAUDE_CODE_SESSION_ID": "from-say"})
                factory.launch_entry(self.state_dir, m, term, "id", "assigned-1")       # at its delivery
                if order == "say after":
                    factory.record_session(self.cfg, self.state_dir, "frontend-dev", term,
                                           {"CLAUDE_CODE_SESSION_ID": "from-say"})
                entry = factory.saved_sessions(self.state_dir)[0]["frontend-dev"]
                self.assertEqual((entry["term"], entry["how"], entry["value"]), (term, "env", "from-say"))
        factory.launch_entry(self.state_dir, m, "t-plain", "new", None)
        factory.launch_entry(self.state_dir, m, "t-plain", "id", "assigned-2")          # no say: the ID wins
        factory.launch_entry(self.state_dir, m, "t-plain", "id", "assigned-2")          # and is not repeated
        factory.launch_entry(self.state_dir, m, "t-plain", "new", None)                 # nor undone by a late new
        lines = (self.state_dir / factory.SESSIONS).read_text().splitlines()
        self.assertEqual(sum('"assigned-2"' in line for line in lines), 1)
        self.assertEqual(factory.saved_sessions(self.state_dir)[0]["frontend-dev"]["value"], "assigned-2")

    def test_brief_send_does_not_wait_for_a_missing_keeper(self):
        self.cmd(self.operator, "up", "lead-dev")
        with patch.object(factory, "ensure_keeper", side_effect=factory.Problem("no keeper", "fix")), \
                patch.object(factory, "wait_outcomes", wraps=lambda log, i, r, o, timeout: {r[0]: {"o": "queued",
                                                                                                  "why": "waiting"}}
                             ) as waited:
            code, _, err = self.cmd(self.operator, "brief", "--send", "lead-dev")
        self.assertEqual(code, 1)
        self.assertIn("the keeper is not running (no keeper)", err)
        self.assertEqual(waited.call_args.args[4], 0.0)

    def test_a_trust_dialog_without_keys_is_left_to_the_owner(self):
        self.assertEqual(factory.TABLE["agy"]["dialogs"], [["Do you trust the contents of this project?", []]])
        self.env.write_brief(formation({"pm": {"harness": "agy"}}))
        self.fake.on_run("pm", status="idle", screen="Do you trust the contents of this project?")
        code, out, err = self.cmd(self.operator, "up")
        self.assertEqual(code, 3)
        self.assertIn("waits on its trust dialog: accept it in its pane, then run: factory.py brief --send pm", err)
        self.assertFalse(self.fake.calls("terminal.keys"))
        self.assertFalse([m for m in self.log.read_box()[1].msgs.values() if m["kind"] == "brief"])

    def test_a_resume_that_exits_is_retried_fresh_once(self):
        old = self.terminal("lead-dev")[0]
        factory.record_session(self.cfg, self.state_dir, "lead-dev", old, {"CODEX_THREAD_ID": "thread-a"})
        self.fake.finish(old)
        results = iter([{"lead-dev": "exited (code 1): no such session"}, {"lead-dev": None}])
        with patch.object(factory, "await_ready", lambda *args, **kwargs: next(results)):
            code, out, err = self.cmd(self.operator, "up", "lead-dev")
        self.assertEqual(code, 0, out + err)
        self.assertIn("its saved conversation could not be resumed (exited (code 1): no such session); starting fresh",
                      out)
        first, second = [c["params"]["argv"] for c in self.fake.calls("terminal.run")]
        self.assertEqual(first[1:3], ["resume", "thread-a"])
        self.assertNotIn("resume", second)
        self.assertEqual(len(self.member_terms()), 1)
        edits = [c["params"]["value"] for c in self.fake.calls("doc.set")]
        original, fresh = (next(p for p in factory.blocks(doc).values() if p["title"] == "@lead-dev")
                           for doc in edits)
        self.assertEqual(fresh["id"], original["id"])
        self.assertNotEqual(fresh["content"]["terminal"], original["content"]["terminal"])

    def test_members_already_running_hear_who_joined(self):
        lead = self.terminal("lead-dev")[0]
        self.briefed("lead-dev", lead)
        code, out, err = self.cmd(self.operator, "up", "reviewer")
        self.assertEqual(code, 0, out + err)
        news = [m for m in self.log.read_box()[1].msgs.values() if m.get("text", "").startswith("Joined the team:")]
        self.assertEqual([m["to"] for m in news], [["lead-dev"]])
        self.assertIn("@reviewer (reviewer, Codex gpt-6-luna)", news[0]["text"])

    def launch_fact(self, terminal, how, value, *, fresh=True, harness="claude", model=None):
        raw = json.dumps({"how": how, "value": value, "fresh": fresh, "harness": harness, "model": model})
        self.hc.label(terminal, {"factory.launch": raw})

    def test_late_briefed_member_that_exited_keeps_its_conversation(self):
        folder = factory.session_folder(self.state_dir, {"name": "pm", "harness": "pi"})
        with patch.dict(factory.TABLE["pi"], session="dir"):
            with patch.object(factory, "wait_outcomes", return_value={"pm": {"o": "queued", "why": "waiting"}}):
                self.assertEqual(self.cmd(self.operator, "up", "pm")[0], 3)
            old = self.member_terms()["pm"]
            self.assertEqual(factory.saved_sessions(self.state_dir)[0]["pm"]["how"], "new")
            self.deliver()
            self.fake.finish(old)
            code, out, err = self.cmd(self.operator, "up", "pm")
        self.assertEqual(code, 0, out + err)
        self.assertIn("resumed @pm", out)
        self.assertIn("--continue", self.fake.calls("terminal.run")[-1]["params"]["argv"])
        new = self.member_terms()["pm"]
        entry = factory.saved_sessions(self.state_dir)[0]["pm"]
        self.assertEqual((entry["term"], entry["how"], entry["value"]), (new, "dir", str(folder)))
        self.assertFalse(self.hc.get(new)["labels"].get("factory.launch"))

    def test_a_fresh_launch_without_a_journal_entry_blocks_an_older_resume(self):
        lead = self.terminal("lead-dev")[0]
        factory.record_session(self.cfg, self.state_dir, "lead-dev", "previous", {"CODEX_THREAD_ID": "old-id"})
        self.launch_fact(lead, "new", None, harness="codex")
        self.fake.finish(lead)
        code, out, err = self.cmd(self.operator, "up", "lead-dev")
        self.assertEqual(code, 0, out + err)
        self.assertIn("none was recorded for it", out)
        self.assertNotIn("resume", self.fake.calls("terminal.run")[-1]["params"]["argv"])

    def test_up_does_not_restart_a_member_whose_launch_cannot_be_saved(self):
        term = self.terminal("lead-dev")[0]
        self.launch_fact(term, "id", "old-id", harness="codex")
        self.briefed("lead-dev", term)
        self.fake.finish(term)
        with patch.object(factory, "launch_entry", side_effect=OSError("no space")):
            code, out, err = self.cmd(self.operator, "up", "lead-dev")
        self.assertEqual(code, 3, out + err)
        self.assertIn("conversation could not be saved (no space); not restarted", err)
        self.assertFalse(self.fake.calls("terminal.run"))
        self.assertTrue(self.hc.get(term)["labels"]["factory.launch"])
        code, out, err = self.cmd(self.operator, "up", "lead-dev")
        self.assertEqual(code, 0, out + err)
        self.assertIn("resumed @lead-dev", out)
        self.assertEqual(self.fake.calls("terminal.run")[-1]["params"]["argv"][1:3], ["resume", "old-id"])

    def test_rebrief_saves_the_original_model_and_preserves_a_ready_line_session(self):
        self.assertEqual(self.cmd(self.operator, "up", "lead-dev")[0], 0)
        term = self.terminal("frontend-dev")[0]
        for from_say in (False, True):
            with self.subTest(from_say=from_say):
                self.launch_fact(term, "id", "from-launch", harness="claude", model=None)
                if from_say:
                    factory.record_session(self.cfg, self.state_dir, "frontend-dev", term,
                                           {"CLAUDE_CODE_SESSION_ID": "from-say"})
                code, out, err = self.cmd(self.operator, "brief", "--send", "frontend-dev")
                self.assertEqual(code, 0, out + err)
                entry = factory.saved_sessions(self.state_dir)[0]["frontend-dev"]
                self.assertEqual(entry["value"], "from-say" if from_say else "from-launch")
                if not from_say:
                    self.assertIsNone(entry["model"])
                    self.assertEqual(entry["harness"], "claude")
                self.assertFalse(self.hc.get(term)["labels"].get("factory.launch"))

    def test_launch_fact_does_not_follow_a_formation_change(self):
        lead = self.terminal("lead-dev")[0]
        self.launch_fact(lead, "id", "claude-id", model="claude-opus-5-5")
        self.briefed("lead-dev", lead)
        code, out, err = self.cmd(self.operator, "up", "lead-dev")  # no launches needed
        self.assertEqual(code, 0, out + err)
        entry = factory.saved_sessions(self.state_dir)[0]["lead-dev"]
        m = factory.plan(self.cfg, "lead-dev", self.state_dir)
        self.assertIn("saved conversation is for claude claude-opus-5-5",
                      factory.session_plan(m, self.state_dir, entry, False)["notice"])
        self.launch_fact(lead, "dir", str(self.root), harness="pi", model="openai/gpt-6-astra")
        self.assertEqual(self.cmd(self.operator, "up", "lead-dev")[0], 0)
        entry = factory.saved_sessions(self.state_dir)[0]["lead-dev"]
        with patch.dict(factory.TABLE["pi"], session="dir", resume_model="provider"):
            m = {**m, "harness": "pi", "model": "deepseek/some-model"}
            self.assertIn("saved conversation is for pi openai/gpt-6-astra",
                          factory.session_plan(m, self.state_dir, entry, False)["notice"])

    def test_down_preserves_live_or_finished_members_whose_launch_cannot_be_saved(self):
        for finished in (False, True):
            with self.subTest(finished=finished):
                self.assertEqual(self.cmd(self.operator, "up", "lead-dev", "reviewer")[0], 0)
                term = self.member_terms()["lead-dev"]
                self.launch_fact(term, "id", "assigned", harness="codex")
                if finished:
                    self.fake.finish(term)
                with patch.object(factory, "launch_entry", side_effect=OSError("no space")):
                    code, out, err = self.cmd(self.operator, "down")
                self.assertEqual(code, 1, out + err)
                self.assertIn("@lead-dev's record kept" if finished else "@lead-dev left running", out)
                self.assertIn("stopped @reviewer", out)
                self.assertTrue(self.hc.get(term)["labels"]["factory.launch"])
                self.assertEqual(self.cmd(self.operator, "down")[0], 0)
                self.assertEqual(factory.saved_sessions(self.state_dir)[0]["lead-dev"]["value"], "assigned")

    def test_malformed_or_unclearable_launch_label_is_only_a_warning(self):
        lead = self.terminal("lead-dev")[0]
        self.hc.label(lead, {"factory.launch": '{"how":"new","fresh":true,"harness":"codex","value":null}'})
        code, out, err = self.cmd(self.operator, "up", "lead-dev")
        self.assertEqual(code, 0, out + err)
        self.assertIn("malformed factory.launch label", err)
        self.launch_fact(lead, "new", None, harness="codex")
        label = factory.Hc.label

        def refuse_clear(hc, address, labels):
            if labels == {"factory.launch": ""}:
                raise factory.HcDown("terminal.label", "test")
            return label(hc, address, labels)

        with patch.object(factory.Hc, "label", refuse_clear):
            code, out, err = self.cmd(self.operator, "up", "lead-dev")
        self.assertEqual(code, 0, out + err)
        self.assertIn("launch label could not be cleared", err)
        data = (self.state_dir / factory.SESSIONS).read_bytes()
        self.assertEqual(self.cmd(self.operator, "up", "lead-dev")[0], 0)
        self.assertEqual((self.state_dir / factory.SESSIONS).read_bytes(), data)
        self.assertFalse(self.hc.get(lead)["labels"].get("factory.launch"))

    def test_cold_up_lists_undelivered_files_and_warm_up_leaves_them_alone(self):
        folder = self.state_dir / "messages" / "pm"
        folder.mkdir(parents=True)
        delivered, pending = folder / "lead-dev-done-1.md", folder / "lead-dev-later-2.md"
        delivered.write_text("done")
        pending.write_text("later")
        ident = self.add("lead-dev", "done", file=str(delivered))
        box = self.log.read_box()[1]
        self.log.out(box, ident, "lead-dev", "sending", term="old")
        self.log.out(box, ident, "lead-dev", "delivered", term="old", started=True, turn=1)
        self.add("lead-dev", "later", file=str(pending))
        code, out, err = self.cmd(self.operator, "up", "reviewer")
        self.assertEqual(code, 0, out + err)
        self.assertIn(str(pending), out)
        self.assertNotIn(str(delivered), out)
        self.assertTrue(pending.exists())
        self.assertFalse(delivered.exists())
        code, out, err = self.cmd(self.operator, "up", "reviewer")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("leftover message file", out)

    def test_down_records_the_stop_then_removes_and_drops(self):
        self.cmd(self.operator, "up", "lead-dev")
        term = self.member_terms()["lead-dev"]
        self.log.add("pm", ["lead-dev"], "for later")
        before = len(self.mutations())
        code, out, err = self.cmd(self.operator, "down")
        self.assertEqual(code, 0, out + err)
        self.assertIn("stopped @lead-dev (was idle); 1 queued messages dropped", out)
        self.assertIn("@reviewer was not running", out)
        self.assertIn("run factory.py up to bring members back", out)
        calls = [(c["method"], c["params"].get("labels")) for c in self.mutations()[before:]
                 if c["params"].get("terminal") == term]
        self.assertEqual(calls, [("terminal.label", {"factory.down": "1"}), ("terminal.kill", None),
                                 ("terminal.rm", None)])
        team = factory.read_team(self.state_dir)
        self.assertEqual((team["stopped"], team["up"]), ([term], False))
        _, box = self.log.read_box()
        late = max(box.msgs)
        self.assertEqual(box.outcome(late, "lead-dev"), {**box.outcome(late, "lead-dev"), "o": "dropped",
                                                          "why": "stopped by down"})

    def test_down_refuses_or_stops_nobody(self):
        self.cmd(self.operator, "up", "lead-dev")
        member = self.member_terms()["lead-dev"]
        code, _, err = self.cmd(member, "down")
        self.assertEqual(code, 2)
        self.assertIn("run down from the operator's pane", err)
        with patch.object(factory, "sessions_writable", return_value="No space left on device"):
            code, out, _ = self.cmd(self.operator, "down")
        self.assertEqual(code, 1)
        self.assertIn("sessions cannot be saved (No space left on device): members left running", out)
        self.assertEqual(self.member_terms(), {"lead-dev": member})

    def test_down_keeps_the_keeper_pane_until_the_last_member(self):
        self.assertEqual(self.cmd(self.operator, "up", "lead-dev", "reviewer")[0], 0)
        terms = self.member_terms()
        self.assertEqual(self.cmd(self.operator, "down", "lead-dev")[0], 0)
        doc, _ = self.hc.doc_get()
        self.assertEqual(set(factory.blocks(doc)), {"keeper", terms["reviewer"]})
        self.assertTrue(factory.read_team(self.state_dir)["up"])
        self.assertEqual(self.cmd(self.operator, "down", "reviewer")[0], 0)
        self.assertEqual(self.hc.doc_get()[0], {"spaces": []})
        self.assertEqual(factory.read_team(self.state_dir)["layout"], {"made_space": False})

    def test_last_down_removes_the_saved_keeper_pane_after_a_headless_replacement(self):
        owner = LayoutTests.space("owner", LayoutTests.tab("home", LayoutTests.pane(self.operator)))
        self.hc.doc_set({"spaces": [owner]}, 0)
        self.hc.label(self.operator, {"herdr.space": "owner", "herdr.tab": "home", "herdr.seen": "7"})
        self.assertEqual(self.cmd(self.operator, "up", "lead-dev")[0], 0)
        replacement = self.terminal("keeper", kind="keeper")[0]
        self.assertNotIn(replacement, factory.blocks(self.hc.doc_get()[0]))
        code, out, err = self.cmd(self.operator, "down")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.hc.doc_get()[0], {"spaces": [owner]})
        self.assertEqual(factory.read_team(self.state_dir)["layout"], {"space": "owner", "made_space": False})
        self.assertEqual(self.hc.get(self.operator)["labels"]["herdr.seen"], "7")

    def test_layout_failure_does_not_change_down_exit_code(self):
        self.assertEqual(self.cmd(self.operator, "up", "lead-dev")[0], 0)
        original = self.hc.doc_get()
        self.fake.doc_conflicts(factory.CAS_TRIES)
        code, out, err = self.cmd(self.operator, "down")
        self.assertEqual(code, 0, out + err)
        self.assertIn("close the panes of @lead-dev in your client", err)
        self.assertEqual(self.hc.doc_get()[0], original[0])
        self.assertFalse(factory.read_team(self.state_dir)["up"])

    def test_status_shows_the_team(self):
        self.cmd(self.operator, "up", "lead-dev", "reviewer")
        reviewer = self.member_terms()["reviewer"]
        self.fake.finish(reviewer, exit_code=1)
        self.log.add("lead-dev", ["operator"], "a report")
        code, out, err = self.cmd(self.operator, "status")
        self.assertEqual(code, 0, out + err)
        lines = out.splitlines()
        self.assertEqual(lines[0], f"factory {factory.VERSION} ({factory.SCRIPT}), hc 0.1.20, team {self.team}")
        self.assertRegex(out, r"@lead-dev +lead-dev +codex +gpt-6-astra +high +idle +0 +t\d+")
        self.assertRegex(out, rf"@reviewer +reviewer +codex +gpt-6-luna +high +gone +{reviewer}")
        self.assertIn("@reviewer interrupted: exited (code 1)", out)
        self.assertIn("pending: @operator 1", out)
        self.assertIn("read them with: factory.py inbox --for operator", out)
        self.assertIn("keeper: starting (keeper)\n", out)                  # placed, no heartbeat yet
        self.assertIn("operator: reachable", out)
        self.assertIn("@reviewer gone: run `factory.py up reviewer` to bring them back.", out)

    def test_brief_previews_offline_with_placeholders(self):
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.env.root / "elsewhere")}):
            code, out, err = self.cmd(None, "brief", "reviewer")
        self.assertEqual(code, 0, err)
        self.assertIn("paths are final after factory.py up", err)
        self.assertIn("<state>/messages/reviewer/FILENAME", out)
        self.assertIn("<state>/reviewer.md", out)
        code, out, err = self.cmd(None, "brief", "reviewer")             # the team's state exists: real paths
        self.assertNotIn("paths are final", err)
        self.assertIn(str(self.state_dir / "messages" / "reviewer"), out)

    def test_status_reports_unplaced_terminals_and_an_unconfirmed_brief(self):
        self.assertEqual(self.cmd(self.operator, "up", "lead-dev")[0], 0)
        reviewer = self.terminal("reviewer")[0]
        starting = self.terminal("frontend-dev", status=None)[0]
        code, out, err = self.cmd(self.operator, "status")
        self.assertEqual(code, 0, err)
        self.assertIn("no pane: @frontend-dev, @reviewer (run factory.py up)", out)
        self.assertIn("@reviewer has no confirmed brief: run factory.py brief --send reviewer", out)
        self.assertNotIn("@frontend-dev has no confirmed brief", out)
        self.assertNotIn("@lead-dev has no confirmed brief", out)
        self.assertIsNone(factory.where(self.hc.doc_get()[0], reviewer))
        self.assertIsNone(factory.where(self.hc.doc_get()[0], starting))
        with patch.object(factory, "ensure_keeper", return_value="replacement"):
            code, out, err = self.cmd(self.operator, "status")
        self.assertEqual(code, 0, err)
        self.assertIn("keeper: starting (replacement), no pane (run factory.py up)", out)

    def test_status_survives_a_malformed_doc_and_a_damaged_log(self):
        self.assertEqual(self.cmd(self.operator, "up", "lead-dev")[0], 0)
        self.terminal("reviewer")
        self.hc.doc_set({"unexpected": []}, self.hc.doc_get()[1])
        with self.log.path.open("ab") as f:
            f.write(b"invalid\n")
        code, out, err = self.cmd(self.operator, "status")
        self.assertEqual(code, 0, out + err)
        self.assertIn("message log needs repair:", out)
        self.assertNotIn("no pane", out)
        self.assertNotIn("has no confirmed brief", out)

    def test_gone_member_gets_the_up_hint_instead_of_a_brief_hint(self):
        self.assertEqual(self.cmd(self.operator, "up", "lead-dev")[0], 0)
        term = self.terminal("reviewer")[0]
        self.fake.set_status(term, None)                      # the terminal survives, but its harness is gone
        code, out, err = self.cmd(self.operator, "status")
        self.assertEqual(code, 0, out + err)
        self.assertIn("@reviewer gone: run `factory.py up reviewer`", out)
        self.assertNotIn("factory.py brief --send reviewer", out)

    def test_brief_send_closes_by_whether_the_terminal_was_briefed(self):
        self.cmd(self.operator, "up", "lead-dev")
        unbriefed = self.terminal("reviewer")[0]
        code, out, err = self.cmd(self.operator, "brief", "--send", "lead-dev", "reviewer")
        self.assertEqual(code, 0, out + err)
        lead = self.member_terms()["lead-dev"]
        self.assertTrue(self.fake.sent(lead)[-1].rstrip().endswith("No reply needed."))
        self.assertIn("mention any work you have already picked up", self.fake.sent(unbriefed)[-1])
        code, _, err = self.cmd(self.operator, "brief", "--send", "pm")
        self.assertEqual(code, 2)
        self.assertIn("not running: @pm", err)

    def test_status_and_brief_send_keep_a_pending_first_brief(self):
        self.assertEqual(self.cmd(self.operator, "up", "lead-dev")[0], 0)
        term = self.terminal("reviewer")[0]
        first, _, _ = self.log.add("operator", ["reviewer"], "original startup brief", kind="brief")
        for pending in ("queued (waiting)", "sending"):
            with self.subTest(pending=pending):
                if pending == "sending":
                    self.keeper.tick()                         # the original worker has not typed anything yet
                    self.assertEqual(len(self.jobs), 1)
                before = self.log.path.read_bytes()
                code, out, err = self.cmd(self.operator, "status")
                self.assertEqual(code, 0, out + err)
                self.assertIn(f"@reviewer: first brief {first} is still {pending}", out)
                self.assertNotIn("run factory.py brief --send reviewer", out)
                code, out, err = self.cmd(self.operator, "brief", "--send", "reviewer")
                self.assertEqual(code, 1, out + err)
                self.assertIn(f"@reviewer: first brief {first} is still {pending}", out)
                self.assertEqual(self.log.path.read_bytes(), before)
                self.assertFalse(self.fake.sent(term))
        self.deliver()
        self.assertEqual(self.outcome(first, "reviewer")["o"], "delivered")
        self.assertEqual(len(self.fake.sent(term)), 1)
        self.assertIn("original startup brief", self.fake.sent(term)[0])
        code, out, err = self.cmd(self.operator, "brief", "--send", "reviewer")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.fake.sent(term)), 2)
        self.assertTrue(self.fake.sent(term)[1].rstrip().endswith("No reply needed."))

    def test_brief_settlement_cannot_overlap_a_fresh_replacement(self):
        self.assertEqual(self.cmd(self.operator, "up", "lead-dev")[0], 0)
        old = self.terminal("frontend-dev")[0]
        self.launch_fact(old, "id", "old-session", model="claude-opus-5-5")
        real = factory.settle_launches
        fresh = ("up", "frontend-dev", "--fresh")

        def interleave(cfg, state, hc, view, box):
            self.assertEqual(view.terminal("frontend-dev")["address"], old)
            self.assertIn(old, box.briefed())
            self.fake.finish(old)
            code, out, err = self.cmd(self.operator, *fresh)
            self.assertEqual(code, 2, out + err)
            self.assertIn("another up, down or brief --send is running for this team", err)
            self.assertFalse(self.fake.calls("terminal.rm"))
            return real(cfg, state, hc, view, box)

        with patch.dict(factory.TABLE["claude"], session="id"):
            with patch.object(factory, "settle_launches", interleave):
                code, out, err = self.cmd(self.operator, "brief", "--send", "frontend-dev")
            self.assertEqual(code, 0, out + err)
            self.assertEqual(factory.saved_sessions(self.state_dir)[0]["frontend-dev"]["value"], "old-session")
            code, out, err = self.cmd(self.operator, *fresh)
        self.assertEqual(code, 0, out + err)
        entry = factory.saved_sessions(self.state_dir)[0]["frontend-dev"]
        self.assertEqual(entry["term"], self.member_terms()["frontend-dev"])
        self.assertNotEqual(entry["term"], old)
        self.assertEqual(entry["how"], "id")
        self.assertNotEqual(entry["value"], "old-session")


@real_only
class IntegratedTeamTests(TeamCase):
    """phase_5a.md §5: check, up, say, status, down and up again from the real command entry points, on the private
    real core, with a real keeper and fake OSC agents standing in for Codex."""

    def setUp(self):
        super().setUp()
        bin_ = self.directory / "bin"
        bin_.mkdir()
        agent = pathlib.Path(__file__).resolve().with_name("fake_agent.py")
        codex = bin_ / "codex"                       # any arguments: the fake agent takes none of Codex's
        codex.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(agent))} reports\n")
        codex.chmod(0o755)
        self.enterContext(patch.dict(os.environ, {"PATH": f"{bin_}:{os.environ.get('PATH', '')}",
                                                  "HOME": self.env.env["HOME"]}))
        self.operator = self.hc.run([sys.executable, str(agent), "reports"], str(self.directory), {}, {})["address"]
        self.addCleanup(self.remove, self.operator)
        self.state(self.operator, "idle")
        self.addCleanup(self.stop_everything)
        previous, _ = self.hc.doc_get()
        self.addCleanup(lambda: self.hc.doc_set(previous, self.hc.doc_get()[1]))

    def stop_everything(self):
        for record in self.hc.list(self.team):
            self.remove(record["address"])

    def cmd(self, *argv):
        out, err, code = io.StringIO(), io.StringIO(), 0
        with (patch.dict(os.environ, {"HC_TERMINAL_ID": self.operator, "HC_BIN": self.hc.bin}),
              patch.object(factory, "HC", self.hc.bin),
              contextlib.redirect_stdout(out), contextlib.redirect_stderr(err)):
            try:
                factory.main([*argv, "--cwd", str(self.root)] if argv[0] != "say" else list(argv))
            except SystemExit as error:
                code = error.code or 0
        return code, out.getvalue(), err.getvalue()

    def members(self):
        return {r["labels"]["factory.member"]: r["address"] for r in self.hc.list(self.team)
                if r["labels"].get("factory.kind") == "member" and r["state"] == "live"}

    def new_operator(self, name):
        term, log = self.launch("reports", name=name)
        self.hc.label(term, {"factory.team": "", "factory.kind": "", "factory.member": ""})
        return term, log

    def test_new_operator_adopts_team_tab_and_queue(self):
        code, out, err = self.cmd("up", "lead-dev", "reviewer")
        self.assertEqual(code, 0, out + err)
        members = self.members()
        saved = factory.read_team(self.state_dir)["layout"]
        doc, version = self.hc.doc_get()
        term, log = self.new_operator("replacement-operator")
        op_space = LayoutTests.space("another-space", LayoutTests.tab("operator-home", LayoutTests.pane(term)))
        doc["spaces"].append(op_space)
        self.hc.doc_set(doc, version)
        self.hc.label(term, {"herdr.space": "another-space", "herdr.tab": "operator-home"})
        self.hc.kill(self.operator)
        self.until(lambda: self.hc.get(self.operator)["state"] == "finished", "old operator to finish")
        first = self.add("operator", "first queued report")
        second = self.add("operator", "second queued report")
        self.until(lambda: self.outcome(first, "operator").get("why") == "gone", "old operator's queued message")
        self.assertEqual([m["id"] for m, _ in self.log.read_box()[1].pending("operator")], [first, second])
        before = self.hc.doc_get()
        self.operator = term
        code, out, err = self.cmd("up", "lead-dev", "reviewer")
        self.assertEqual(code, 0, out + err)
        self.assertIn("already running: @lead-dev, @reviewer", out)
        self.assertEqual(self.members(), members)
        self.assertEqual(self.hc.doc_get(), before)
        self.assertEqual(factory.read_team(self.state_dir)["layout"], saved)
        self.until(lambda: self.outcome(second, "operator")["o"] == "delivered", "second report to reach new operator")
        self.assertEqual(self.outcome(second, "operator")["term"], term)
        self.assertEqual(self.outcome(first, "operator")["term"], term)
        self.assertEqual([text.splitlines()[-1] for text in self.inputs(term, log)],
                         ["first queued report", "second queued report"])

    def test_two_teams_have_separate_tabs_and_stops(self):
        code, out, err = self.cmd("up", "lead-dev")
        self.assertEqual(code, 0, out + err)
        home = self.members()["lead-dev"]
        first_doc, _ = self.hc.doc_get()
        other_root = self.directory / "another-checkout"
        other_root.mkdir()
        for name in ("factory.toml", "FACTORY.md"):
            (other_root / name).write_bytes((self.root / name).read_bytes())
        term, _ = self.new_operator("other-team-operator")
        with patch.object(self, "root", other_root), patch.object(self, "operator", term):
            other_team = factory.team_key(other_root)
            self.addCleanup(lambda: [self.remove(r["address"]) for r in self.hc.list(other_team)])
            code, out, err = self.cmd("up", "lead-dev")
            self.assertEqual(code, 0, out + err)
            other_members = factory.team_view(self.hc, other_team)
            other = other_members.terminal("lead-dev")["address"]
            both, _ = self.hc.doc_get()
            self.assertEqual(factory.where(both, home), factory.where(first_doc, home))
            self.assertNotEqual(factory.where(both, home), factory.where(both, other))
            code, out, err = self.cmd("down", "lead-dev")
            self.assertEqual(code, 0, out + err)
            self.assertEqual(self.hc.doc_get()[0], first_doc)
        self.assertEqual(self.hc.get(home)["state"], "live")

    def test_down_can_stop_the_keeper_after_a_member_is_removed_from_formation(self):
        code, out, err = self.cmd("up", "lead-dev", "reviewer")
        self.assertEqual(code, 0, out + err)
        members = self.members()
        keeper = factory.team_view(self.hc, self.team).keeper["address"]
        self.env.write_brief(formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]}))
        code, out, err = self.cmd("down")
        self.assertEqual(code, 1, out + err)
        self.assertIn("run factory.py up to bring members back", out)
        old = members["reviewer"]
        self.assertIn(f"@reviewer ({old}) is still running outside factory.toml.", err)
        self.assertIn(f"Run hc kill {old} to stop it.", err)
        self.assertEqual(self.members(), {"reviewer": old})
        self.assertFalse(factory.read_team(self.state_dir)["up"])
        self.until(lambda: self.hc.get(keeper)["state"] == "finished", "the keeper to exit")
        self.hc.kill(old)
        self.until(lambda: self.hc.get(old)["state"] == "finished", "the removed member to exit")
        code, out, err = self.cmd("down")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("still running", err)

    def test_a_team_runs_end_to_end(self):
        code, out, err = self.cmd("check", "lead-dev", "reviewer")
        self.assertEqual(code, 0, out + err)
        code, out, err = self.cmd("up", "lead-dev", "reviewer")
        self.assertEqual(code, 0, out + err)
        for name in ("lead-dev", "reviewer"):
            self.assertRegex(out, rf"@{name}: brief \d+ delivered")
        first = self.members()
        _, box = self.log.read_box()
        self.assertEqual(box.briefed(), set(first.values()))
        code, out, err = self.cmd("say", "lead-dev", "a task")
        self.assertEqual(code, 0, out + err)
        self.assertRegex(out, r"message \d+\n@lead-dev: delivered\n")
        task = int(out.split()[1])
        code, out, _ = self.cmd("up")                              # a second up leaves live members alone
        self.assertIn("already running: @lead-dev, @reviewer", out)
        self.assertEqual(self.members(), first)
        self.state(first["lead-dev"], "idle")                         # the fake agent's turn for the task ends
        code, out, err = self.cmd("status")
        self.assertEqual(code, 0, err)
        self.assertRegex(out, r"@lead-dev +lead-dev +codex +gpt-6-astra +high +idle")
        self.assertIn("keeper: running", out)
        self.assertIn("operator: reachable", out)
        doc, _ = self.hc.doc_get()
        for terminal in first.values():
            record = self.hc.get(terminal)
            self.assertEqual(factory.where(doc, terminal), (record["labels"]["herdr.space"], record["labels"]["herdr.tab"]))
            self.assertNotIn("herdr.seen", record["labels"])
        code, out, err = self.cmd("down", "lead-dev")
        self.assertEqual(code, 0, out + err)
        self.assertIn("stopped @lead-dev (was idle)", out)
        self.assertEqual(set(self.members()), {"reviewer"})
        keeper = factory.team_view(self.hc, self.team).keeper["address"]
        self.assertEqual(set(factory.blocks(self.hc.doc_get()[0])), {first["reviewer"], keeper})
        code, out, err = self.cmd("down")
        self.assertEqual(code, 0, out + err)
        self.assertIn("run factory.py up to bring members back", out)
        self.assertFalse(factory.blocks(self.hc.doc_get()[0]))
        self.until(lambda: self.hc.get(keeper)["state"] == "finished", "the keeper's exit after the last down")
        code, out, err = self.cmd("up", "lead-dev")
        self.assertEqual(code, 0, out + err)
        self.assertIn("@lead-dev starts in a new conversation: none was recorded for it", out)
        _, box = self.log.read_box()
        self.assertEqual(sum(1 for m in box.msgs.values() if m["kind"] == "brief" and m["to"] == ["lead-dev"]), 2)
        self.assertEqual(box.outcome(task, "lead-dev")["o"], "delivered")      # the team's history persists


@fake_only
class MemberNoteTests(TeamCase):
    """keeper.md §5 and phase_6.md §2: one note per observed episode, housekeeping on briefed members."""

    def setUp(self):
        super().setUp()
        self.operator = self.terminal("operator", kind="operator")[0]
        self.lead = self.terminal("lead-dev")[0]
        self.briefed("lead-dev", self.lead)

    def notes(self):
        _, box = self.log.read_box()
        return [m for m in box.msgs.values() if m["kind"] == "note"]

    def status(self, term, state, kind=None, msg=None):
        time.sleep(0.003)                    # a new status_ms for a re-entered state
        self.fake.set_status(term, state, kind, msg)
        self.keeper.tick()

    def test_each_blocked_episode_is_noted_once(self):
        self.status(self.lead, "blocked", "permission", "run tests?")
        self.keeper.tick()
        self.status(self.lead, "blocked", "question")
        self.status(self.lead, "blocked", "permission")
        self.status(self.lead, "idle")
        self.status(self.lead, "blocked", "permission")
        notes = self.notes()
        self.assertEqual([n["meta"]["what"] for n in notes],
                         ["blocked:permission", "blocked:question", "blocked:permission", "blocked:permission"])
        self.assertEqual(notes[0]["text"], "@lead-dev is blocked (permission): run tests?. It needs an answer in"
                                           " its pane.")
        self.assertEqual((notes[0]["from"], notes[0]["to"], notes[0]["meta"]["about"], notes[0]["meta"]["term"]),
                         ("factory", ["operator"], "lead-dev", self.lead))
        self.assertNotEqual(notes[2]["meta"]["since"], notes[3]["meta"]["since"])

    def test_a_restarted_keeper_repeats_nothing(self):
        self.status(self.lead, "blocked", "permission")
        again = factory.Keeper(self.state_dir, self.hc, self.clock, self.pane.append, self.jobs.append)
        again.tick()
        again.tick()
        self.assertEqual(len(self.notes()), 1)

    def test_errors_and_exits_are_noted_with_their_facts(self):
        self.status(self.lead, "working")
        task = self.log.add("operator", ["lead-dev"], "do it")[0]
        _, box = self.log.read_box()
        self.log.out(box, task, "lead-dev", "sending", term=self.lead)
        self.log.out(box, task, "lead-dev", "delivered", term=self.lead, started=True, turn=1)
        self.status(self.lead, "error", msg="login expired")
        self.fake.finish(self.lead, exit_code=1)
        self.keeper.tick()
        self.keeper.tick()
        error, exit_note = self.notes()
        self.assertEqual(error["text"], "@lead-dev hit an error: login expired.")
        self.assertEqual(exit_note["meta"], {"what": "exit", "about": "lead-dev", "term": self.lead})
        self.assertEqual(exit_note["text"], f"@lead-dev exited (code 1) while error. Its last message from you was"
                                            f" #{task}. Run: factory.py up lead-dev")

    def test_nothing_for_unbriefed_stopping_or_unwatched_members(self):
        reviewer = self.terminal("reviewer")[0]                          # never briefed
        self.status(reviewer, "blocked", "permission")
        self.hc.label(self.lead, {"factory.down": "1"})
        self.status(self.lead, "blocked", "permission")
        self.assertEqual(self.notes(), [])
        self.hc.label(self.lead, {"factory.down": ""})
        self.fake.set_status(self.operator, None)                         # no reachable operator
        self.keeper.tick()
        self.assertEqual(self.notes(), [])
        self.fake.set_status(self.operator, "idle")                       # back: the current episode
        self.keeper.tick()
        self.assertEqual([n["meta"]["what"] for n in self.notes()], ["blocked:permission"])

    def test_housekeeping_changes_only_what_changed(self):
        self.hc.call("terminal.keep", {"terminal": self.lead, "keep": True})
        self.status(self.lead, "working")
        self.keeper.tick()
        labels = [c["params"] for c in self.fake.calls("terminal.label") if c["params"]["terminal"] == self.lead]
        keeps = [c["params"] for c in self.fake.calls("terminal.keep") if c["params"]["terminal"] == self.lead]
        self.assertEqual(labels, [{"terminal": self.lead, "labels": {"factory.last": "working"}}])
        self.assertEqual(keeps[-1], {"terminal": self.lead, "keep": False})
        self.assertFalse(self.hc.get(self.lead)["keep"])

    def test_the_operator_cannot_be_named_factory(self):
        self.env.write_brief(formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]}, operator={"name": "factory"}))
        with self.assertRaisesRegex(SystemExit, "'factory' is reserved for Factory's own notes"):
            factory.Config(self.root)

    def test_the_note_reaches_the_operator_from_factory(self):
        self.status(self.lead, "blocked", "permission")
        self.keeper.tick()
        self.run_jobs()
        self.keeper.tick()
        self.assertTrue(self.fake.sent(self.operator)[0].startswith("[from factory]\n@lead-dev is blocked"))

    def test_the_delivery_count_replays_the_same(self):
        ident = self.log.add("pm", ["lead-dev", "reviewer"], "two recipients")[0]
        note = self.log.add("factory", ["operator"], "a note", kind="note", meta={"what": "quiet"})[0]
        _, box = self.log.read_box()
        base = box.deliveries
        for to, term in (("lead-dev", self.lead), ("reviewer", "t-r")):
            self.log.out(box, ident, to, "sending", term=term)
            self.log.out(box, ident, to, "delivered", term=term, started=True, turn=0)
        self.log.out(box, note, "operator", "sending", term=self.operator)
        self.log.out(box, note, "operator", "delivered", term=self.operator, started=True, turn=0)
        self.assertEqual(box.deliveries, base + 2)                       # notes never count
        self.assertEqual(self.log.read_box()[1].deliveries, base + 2)     # a full replay counts the same
        partial = factory.Box.for_message(self.log.path, ident, ["lead-dev"], 0)
        self.assertEqual(partial.deliveries, 0)


@fake_only
class QuietTests(TeamCase):
    """keeper.md §6 and phase_6.md §3: the team-quiet note, its re-arming and its freshness at hand-off."""

    def setUp(self):
        super().setUp()
        self.operator = self.terminal("operator", kind="operator")[0]
        self.lead = self.terminal("lead-dev")[0]
        self.reviewer = self.terminal("reviewer")[0]
        for name, term in (("lead-dev", self.lead), ("reviewer", self.reviewer)):
            self.briefed(name, term)

    def quiet_notes(self):
        """The quiet notes as written, text included (a box drops the text once every recipient is final)."""
        records = [json.loads(line) for line in self.log.path.read_bytes().splitlines()]
        return [r for r in records if r["t"] == "msg" and r["kind"] == "note" and r["meta"].get("what") == "quiet"]

    def run_for(self, seconds, step=10):
        for _ in range(int(seconds / step)):
            self.clock.sleep(step)
            self.keeper.tick()
            self.run_jobs()
        self.keeper.tick()

    def deliver(self, sender, to, text, term):
        ident = self.log.add(sender, [to], text)[0]
        _, box = self.log.read_box()
        self.log.out(box, ident, to, "sending", term=term)
        self.log.out(box, ident, to, "delivered", term=term, started=True, turn=0)
        return ident

    def test_stall_member_without_report(self):
        self.keeper.tick()
        self.run_for(110)
        self.assertEqual(self.quiet_notes(), [])                         # inside the grace
        self.run_for(20)
        notes = self.quiet_notes()
        self.assertEqual(len(notes), 1)
        text = notes[0]["text"]
        self.assertTrue(text.startswith("team quiet for 2m: nobody is working and nothing is waiting"))
        self.assertIn("@lead-dev idle", text)
        self.assertIn("Last received #1 from @operator.", text)
        self.assertTrue(text.endswith("may owe a reply."))
        self.assertEqual(self.outcome(notes[0]["id"], "operator")["o"], "delivered")
        self.run_for(15 * 60, step=30)                                    # no loop while nothing happens
        self.assertEqual(len(self.quiet_notes()), 1)

    def test_stall_operator_ended_early_or_mutual_wait(self):
        self.run_for(130)
        self.assertEqual(len(self.quiet_notes()), 1)
        self.fake.set_status(self.operator, "working")                    # the operator's own turn
        self.run_for(30)
        self.fake.set_status(self.operator, "idle")
        self.run_for(300)
        self.assertEqual(len(self.quiet_notes()), 1)                      # told once
        self.deliver("lead-dev", "reviewer", "over to you", self.reviewer)   # real activity
        self.run_for(130)
        self.assertEqual(len(self.quiet_notes()), 2)

    def test_working_or_young_members_keep_the_team_busy(self):
        self.fake.set_status(self.lead, "working")
        self.run_for(300)
        self.assertEqual(self.quiet_notes(), [])
        self.fake.set_status(self.lead, "idle")
        self.fake.add({"factory.team": self.team, "factory.kind": "member", "factory.member": "pm",
                       "factory.dir": str(self.state_dir)}, None)          # starting, never reported
        self.run_for(30)
        self.assertEqual(self.quiet_notes(), [])                          # younger than LAUNCH_TIMEOUT
        self.run_for(160)
        self.assertEqual(len(self.quiet_notes()), 1)

    def test_a_member_turn_new_terminal_or_receipt_rearms(self):
        self.run_for(130)
        for change in ("turn", "terminal", "receipt"):
            with self.subTest(change=change):
                before = len(self.quiet_notes())
                if change == "turn":
                    self.fake.set_status(self.lead, "working")
                    self.keeper.tick()
                    self.fake.set_status(self.lead, "idle")
                elif change == "terminal":
                    self.fake.finish(self.reviewer)
                    self.reviewer = self.terminal("reviewer")[0]
                    self.briefed("reviewer", self.reviewer)
                else:
                    self.deliver("operator", "lead-dev", "next", self.lead)
                self.run_for(130)
                self.assertEqual(len(self.quiet_notes()), before + 1)

    def test_a_held_older_message_delivered_after_the_note_rearms(self):
        self.fake.set_status(self.reviewer, "blocked", "permission")
        held = self.log.add("lead-dev", ["reviewer"], "held for a blocked member")[0]
        self.run_for(150)                                                 # the blocked note goes first
        self.assertEqual(len(self.quiet_notes()), 1)                      # a queued, undeliverable pair is quiet
        self.assertLess(held, self.quiet_notes()[0]["id"])
        self.fake.set_status(self.reviewer, "idle")
        self.keeper.tick()
        self.run_jobs()
        self.keeper.tick()
        self.assertEqual(self.outcome(held, "reviewer")["o"], "delivered")
        self.run_for(130)
        self.assertEqual(len(self.quiet_notes()), 2)                      # by the count, not by message ID

    def pending_quiet_note(self):
        self.run_for(10)
        self.keeper._quiet(factory.team_view(self.hc, self.team), self.clock.monotonic() + 1000)
        return self.quiet_notes()[0]["id"]

    def test_a_quiet_note_for_a_blocked_operator_is_dropped_not_queued(self):
        note = self.pending_quiet_note()
        self.fake.set_status(self.operator, "blocked", "question")
        self.keeper.tick()
        self.assertEqual(self.outcome(note, "operator")["o"], "dropped")

    def test_a_quiet_note_for_a_stopped_team_is_dropped_not_sent(self):
        note = self.pending_quiet_note()
        factory.update_team(self.state_dir, up=False)
        self.keeper.tick()
        self.assertEqual(self.outcome(note, "operator")["o"], "dropped")
        self.assertEqual(self.fake.sent(self.operator), [])

    def test_work_that_starts_in_the_same_tick_drops_the_note(self):
        real = factory.Keeper._quiet

        def quiet_then_work(keeper, view, now):
            real(keeper, view, now)
            self.fake.set_status(self.lead, "working")          # after the note is appended, before the hand-off
        self.run_for(10)
        self.clock.sleep(1000)
        with patch.object(factory.Keeper, "_quiet", quiet_then_work):
            self.keeper.tick()
        note = self.quiet_notes()[0]["id"]
        self.assertEqual(self.outcome(note, "operator")["o"], "dropped")
        self.assertEqual(self.fake.sent(self.operator), [])

    def test_a_note_goes_to_the_operator_the_fresh_view_names(self):
        real = factory.Keeper._quiet

        def quiet_then_move(keeper, view, now):
            real(keeper, view, now)
            self.hc.label(self.operator, {"factory.kind": "operator-lost"})   # a new operator pane took over
            self.moved = self.terminal("operator", kind="operator")[0]
        self.run_for(10)
        self.clock.sleep(1000)
        with patch.object(factory.Keeper, "_quiet", quiet_then_move):
            self.keeper.tick()
        self.run_jobs()
        self.keeper.tick()
        note = self.quiet_notes()[0]["id"]
        outcome = self.outcome(note, "operator")
        self.assertEqual((outcome["o"], outcome["term"]), ("delivered", self.moved))
        self.assertEqual(self.fake.sent(self.operator), [])

    def test_a_quiet_note_no_longer_true_at_hand_off_is_dropped(self):
        self.fake.set_status(self.operator, "blocked", "question")
        self.run_for(130)
        self.assertEqual(self.quiet_notes(), [])                          # not added for a blocked operator
        self.fake.set_status(self.operator, "idle")
        self.clock.sleep(1)
        self.keeper._quiet(factory.team_view(self.hc, self.team), self.clock.monotonic())
        note = self.quiet_notes()[0]
        self.fake.set_status(self.lead, "working")                        # work resumes before the hand-off
        self.keeper.tick()
        self.assertEqual(self.outcome(note["id"], "operator"), {**self.outcome(note["id"], "operator"),
                                                               "o": "dropped", "why": "no longer true"})
        self.assertEqual(self.fake.sent(self.operator), [])

    def test_a_quiet_note_is_never_queued_behind_another_head(self):
        self.run_for(10)
        self.keeper._quiet(factory.team_view(self.hc, self.team), self.clock.monotonic() + 1000)
        note = self.quiet_notes()[0]
        ordinary = self.log.add("lead-dev", ["operator"], "a report")[0]
        self.fake.finish(self.operator)                                   # the operator is gone
        self.keeper.tick()
        self.keeper.tick()
        self.assertEqual(self.outcome(ordinary, "operator")["why"], "gone")
        self.assertEqual(self.outcome(note["id"], "operator")["o"], "dropped")


@fake_only
class ReadyCheckTests(TeamCase):
    """keeper.md §6 step 4 and phase_6.md §4: the owner's ready check while the team is quiet."""

    def setUp(self):
        super().setUp()
        self.operator = self.terminal("operator", kind="operator")[0]
        self.lead = self.terminal("lead-dev")[0]
        self.briefed("lead-dev", self.lead)
        self.board = self.root / "board.txt"
        self.board.write_text("T-1 ship it\n")
        self.check(f"cat {shlex.quote(str(self.board))}")

    def check(self, command):
        conf = self.root / "factory.toml"
        body = re.sub(r'(?m)^ready_check(_every)? = .*\n', "", conf.read_text())
        conf.write_text(f"ready_check = {json.dumps(command)}\nready_check_every = 60\n" + body)

    def records(self, what):
        records = [json.loads(line) for line in self.log.path.read_bytes().splitlines()]
        return [r for r in records if r["t"] == "msg" and r["kind"] == "note" and r["meta"].get("what") == what]

    def run_for(self, seconds, step=10):
        for _ in range(int(seconds / step)):
            self.clock.sleep(step)
            self.keeper.tick()
            self.run_jobs()
        self.keeper.tick()

    def test_stall_ready_work_while_idle(self):
        self.run_for(150)
        self.assertEqual(len(self.records("quiet")), 1)
        self.assertEqual(self.records("ready"), [])                       # the first check waits a full period
        self.run_for(70)
        ready = self.records("ready")
        self.assertEqual([r["text"] for r in ready], ["ready work:\nT-1 ship it"])
        self.assertEqual(ready[0]["meta"]["quiet_id"], self.records("quiet")[0]["id"])
        self.assertTrue(any(text.startswith("[from factory]\nready work:") for text in self.fake.sent(self.operator)))
        self.run_for(200)
        self.assertEqual(len(self.records("ready")), 1)                   # unchanged output: told once

    def test_new_output_wakes_and_none_then_the_same_wakes_again(self):
        self.run_for(220)
        self.board.write_text("T-1 ship it\nT-2 docs\n")
        self.run_for(70)
        self.assertEqual(len(self.records("ready")), 2)
        self.board.write_text("")                                         # no ready work: the report clears
        self.run_for(70)
        self.assertEqual(factory.read_team(self.state_dir)["ready_clear"], self.keeper.box.highest)
        self.board.write_text("T-1 ship it\nT-2 docs\n")                   # the same work comes back
        self.run_for(70)
        self.assertEqual(len(self.records("ready")), 3)

    def test_unknown_results_change_nothing(self):
        for command, line in (("exit 2", "ready check: exit 2"), ("printf '\\377'", "ready check: failed")):
            with self.subTest(command=command):
                self.check(command)
                self.pane.clear()
                self.run_for(300)
                self.assertTrue(any(p.startswith(line) for p in self.pane), self.pane)
                self.assertEqual(self.records("ready"), [])
        with patch.object(factory.subprocess, "run", side_effect=factory.subprocess.TimeoutExpired("sh", 30)):
            self.run_for(70)
        self.assertIn("ready check: timed out", self.pane)

    def test_the_check_runs_in_the_main_checkout_with_output_trimmed(self):
        self.check("pwd; seq 1 30")
        self.run_for(220)
        text = self.records("ready")[0]["text"]
        self.assertEqual(text.splitlines()[1], str(self.root))
        self.assertEqual(len(text.splitlines()), 21)                     # the heading and the first 20 lines

    def test_a_check_that_outlived_an_operator_turn_is_dropped(self):
        self.run_for(150)
        for _ in range(20):
            self.clock.sleep(10)
            self.keeper.tick()
            if self.keeper.ready_job is not None:
                break
            self.run_jobs()
        job = self.jobs.pop()
        self.board.write_text("OLD BEFORE OPERATOR ACTIVITY\n")
        self.fake.set_status(self.operator, "working")               # an operator-only turn
        self.keeper.tick()
        self.fake.set_status(self.operator, "idle")
        self.keeper.tick()
        self.assertIsNotNone(self.keeper.ready_job)                   # the slot stays: no second check yet
        job()
        self.keeper.tick()
        self.assertEqual(self.records("ready"), [])
        self.assertNotIn("ready_clear", factory.read_team(self.state_dir))

    def test_a_failed_config_load_ends_the_interval(self):
        self.run_for(220)
        self.assertEqual(len(self.records("ready")), 1)                    # a report stands
        for _ in range(20):
            self.clock.sleep(10)
            self.keeper.tick()
            if self.keeper.ready_job is not None:
                break
            self.run_jobs()
        job = self.jobs.pop()
        conf = self.root / "factory.toml"
        stamp = conf.stat().st_mtime_ns
        aside = conf.rename(self.root / "factory.toml.aside")
        self.keeper.tick()
        self.assertTrue(self.keeper._cfg_failed)
        aside.rename(conf)
        os.utime(conf, ns=(stamp, stamp))                                  # back unchanged: the same stamp
        self.board.write_text("")                                          # the old check says none
        job()
        self.keeper.tick()
        self.assertNotIn("ready_clear", factory.read_team(self.state_dir))

    def test_a_late_result_for_another_interval_is_dropped(self):
        self.run_for(150)
        for _ in range(20):                                               # until the check is spawned, not run
            self.clock.sleep(10)
            self.keeper.tick()
            if self.keeper.ready_job is not None:
                break
            self.run_jobs()
        self.assertIsNotNone(self.keeper.ready_job)
        job = self.jobs.pop()
        self.fake.set_status(self.lead, "working")                        # real activity: a new interval
        self.keeper.tick()
        self.fake.set_status(self.lead, "idle")
        job()
        self.keeper.tick()
        self.assertEqual(self.records("ready"), [])

    def test_a_broken_factory_toml_stops_the_check(self):
        self.run_for(150)
        conf = self.root / "factory.toml"
        conf.write_text(conf.read_text() + "[broken\n")
        self.run_for(300)
        self.assertEqual(self.records("ready"), [])
        self.assertTrue(any("factory.toml" in p for p in self.pane))

    def test_stall_member_blocked(self):
        self.fake.set_status(self.lead, "blocked", "permission")
        self.keeper.tick()
        self.assertEqual([r["meta"]["what"] for r in self.records("blocked:permission")], ["blocked:permission"])

    def test_stall_member_error(self):
        self.fake.set_status(self.lead, "error", msg="rate limited")
        self.keeper.tick()
        self.assertEqual(self.records("error")[0]["text"], "@lead-dev hit an error: rate limited.")

    def test_stall_member_lost(self):
        self.fake.finish(self.lead, reason="server_stop")
        self.keeper.tick()
        self.assertTrue(self.records("exit")[0]["text"].startswith("@lead-dev hc stopped (state unknown)."))


@fake_only
class KeeperUpgradeTests(TeamCase):
    """phase_6.md §5: the pane title, and draining for a newer build before re-executing in the same terminal."""

    def setUp(self):
        super().setUp()
        self.operator = self.terminal("operator", kind="operator")[0]
        self.lead = self.terminal("lead-dev")[0]
        self.briefed("lead-dev", self.lead)
        self.execs = []
        self.keeper._execv = lambda path, argv: self.execs.append((path, argv))
        self.enterContext(patch.object(factory, "VERSION", "000000000000"))   # the build on disk is newer

    def test_the_title_changes_only_when_it_has_to(self):
        self.keeper._version_at = float("inf")
        first = self.add("lead-dev", "one")
        self.keeper.tick()
        self.keeper.tick()
        self.assertEqual(self.title.getvalue(), "\x1b]2;keeper · 1 pending\x07")
        self.run_jobs()
        self.keeper.tick()
        self.assertEqual(self.outcome(first)["o"], "delivered")
        self.assertEqual(self.title.getvalue(), "\x1b]2;keeper · 1 pending\x07\x1b]2;keeper\x07")

    def test_a_newer_build_drains_then_execs_the_same_keeper(self):
        first = self.add("lead-dev", "one")
        self.keeper._version_at = float("inf")
        self.keeper.tick()                                           # claims the first; its send is held
        self.assertEqual(self.outcome(first)["o"], "sending")
        self.keeper._version_at = 0
        self.clock.sleep(30)
        self.keeper.tick()
        self.assertTrue(self.keeper.draining)
        self.assertTrue(any("installed: finishing what is in flight" in line for line in self.pane))
        second = self.add("lead-dev", "two")
        self.keeper.tick()
        self.assertFalse(self.keeper._drained())                    # the held send is still in flight
        self.assertEqual(self.outcome(second)["why"], "waiting")      # no new claim while draining
        self.run_jobs()
        self.keeper.tick()
        self.assertEqual(self.outcome(first)["o"], "delivered")
        self.assertTrue(self.keeper._drained())
        self.keeper._reexec()
        self.assertEqual(self.execs, [(sys.executable, [sys.executable, str(factory.SCRIPT), "keeper", "--state",
                                                        str(self.keeper.state)])])
        self.assertEqual(self.outcome(second)["why"], "waiting")      # for the new image
        self.assertEqual(self.fake.sent(self.lead).count("[from @pm]\none\n"), 1)

    def test_a_withheld_result_or_a_running_check_holds_the_exec(self):
        self.keeper.draining = "new"
        self.keeper.unsettled = [("lead-dev", 1, self.lead, {"turn": 0})]
        self.assertFalse(self.keeper._drained())
        self.keeper.unsettled, self.keeper.ready_job = [], (1, None, "true")
        self.assertFalse(self.keeper._drained())
        self.keeper.ready_job = None
        self.assertTrue(self.keeper._drained())

    def test_a_failed_exec_goes_on_serving(self):
        def refuse(path, argv):
            raise OSError(8, "Exec format error")
        self.keeper._execv = refuse
        self.keeper.draining = "new"
        self.keeper._reexec()
        self.assertIsNone(self.keeper.draining)
        self.assertTrue(any("could not restart itself" in line for line in self.pane))
        ident = self.add("lead-dev", "after")
        self.keeper.tick()
        self.assertEqual(self.outcome(ident)["o"], "sending")         # claims again

    def test_an_unreadable_build_keeps_the_keeper(self):
        with patch.object(factory.pathlib.Path, "read_bytes", side_effect=OSError("gone")):
            self.keeper.tick()
        self.assertIsNone(self.keeper.draining)


@real_only
class RealMemberNoteTests(TeamCase):
    """phase_6.md §6.4: an actual member episode on the private real core, noted and delivered."""

    def test_a_blocked_member_is_noted_to_the_operator(self):
        operator, operator_log = self.terminal("operator", kind="operator")
        lead, _ = self.terminal("lead-dev")
        self.briefed("lead-dev", lead)
        self.hc.send(lead, "first line\nFAKE: blocks")
        self.state(lead, "blocked")
        self.keeper.tick()
        self.run_jobs()
        self.keeper.tick()
        records = [json.loads(line) for line in self.log.path.read_bytes().splitlines()]
        notes = [r for r in records if r["t"] == "msg" and r["kind"] == "note"]
        self.assertEqual([n["meta"]["what"] for n in notes], ["blocked:permission"])
        self.until(lambda: any(text.startswith("[from factory]\n@lead-dev is blocked (permission)")
                               for text in self.inputs(operator, operator_log)), "the note in the operator's pane")


class KeeperTests(TeamCase):
    def member(self, *, status="idle", briefed=True):
        term, agent_log = self.terminal("lead-dev", status=status)
        if briefed:
            self.briefed("lead-dev", term)
        return term, agent_log

    def test_queued_jobs_keep_their_own_recipient_and_text(self):
        lead, lead_log = self.member()
        reviewer, reviewer_log = self.terminal("reviewer")
        self.briefed("reviewer", reviewer)
        a = self.add("lead-dev", "first")
        b = self.add("reviewer", "second\n\n")
        self.keeper.tick()
        self.assertEqual(self.outcome(a)["o"], "sending")
        self.assertEqual(self.outcome(b, "reviewer")["o"], "sending")
        self.assertEqual(len(self.jobs), 2)
        self.run_jobs(reverse=True)
        self.keeper.tick()
        for term, agent_log, text in ((lead, lead_log, "first\n"), (reviewer, reviewer_log, "second\n\n")):
            self.until(lambda: self.inputs(term, agent_log) == ["[from @pm]\n" + text], "one exact input")
        self.assertEqual(self.outcome(a)["o"], "delivered")
        self.assertEqual(self.outcome(b, "reviewer")["o"], "delivered")
        self.assertEqual(self.keeper.inflight, {})
        self.assertEqual(len(self.pane), 2)
        self.assertRegex(self.pane[0], rf"^\d\d:\d\d:\d\d #{b} @pm -> @reviewer delivered$")
        self.assertNotIn("second", "\n".join(self.pane))

    @fake_only
    def test_held_head_does_not_delay_other_recipients_and_preserves_order(self):
        term, _ = self.member(status="working")
        operator, _ = self.terminal("operator", kind="operator")
        first = self.add("lead-dev")
        second = self.add("lead-dev", "second")
        other = self.log.add("factory", ["operator"], "heads up", kind="note", meta={"what": "error"})[0]
        self.keeper.tick()
        held = self.jobs.pop(0)
        self.run_jobs()
        self.keeper.tick()
        self.keeper.tick()
        self.assertEqual(self.outcome(other, "operator")["o"], "delivered")
        self.assertEqual(self.inputs(operator, None), ["[from factory]\nheads up\n"])
        self.assertEqual(self.outcome(first)["o"], "sending")
        self.assertEqual(self.outcome(second)["why"], "waiting")
        self.assertEqual(sum("sending (held)" in line for line in self.pane), 1)
        self.assertEqual(self.inputs(term, None), [])
        held()
        self.keeper.tick()
        self.run_jobs()
        self.keeper.tick()
        self.assertEqual(self.inputs(term, None), ["[from @pm]\nfirst\n", "[from @pm]\nsecond\n"])

    @fake_only
    def test_blocked_queues_the_head_once_then_delivers(self):
        term, _ = self.member(status="blocked")
        first = self.add("lead-dev")
        second = self.add("lead-dev", "second")
        self.keeper.tick()
        self.keeper.tick()
        self.assertEqual(self.outcome(first)["why"], "blocked")
        self.assertEqual(self.outcome(second)["why"], "waiting")
        self.assertEqual(len(self.pane), 1)
        self.fake.set_status(term, "idle")
        self.keeper.tick()
        self.run_jobs()
        self.keeper.tick()
        self.assertEqual(self.outcome(first)["o"], "delivered")
        self.assertEqual(self.outcome(second)["o"], "sending")

    @fake_only
    def test_shell_is_gone_and_never_receives_input(self):
        term, _ = self.member()
        self.fake.set_status(term, None)
        ident = self.add("lead-dev")
        self.keeper.tick()
        self.assertEqual(self.outcome(ident)["why"], "gone")
        self.assertFalse(self.jobs)

    @fake_only
    def test_operator_null_holds_then_latches_until_marked_again(self):
        operator, _ = self.terminal("operator", kind="operator", status=None)
        first = self.add("operator")
        second = self.add("operator", "second")
        self.keeper.tick()
        self.clock.sleep(factory.LATCH_AFTER - 1)
        self.keeper.tick()
        self.assertEqual(self.outcome(first, "operator")["why"], "waiting")
        self.fake.set_status(operator, "working")
        self.keeper.tick()
        self.assertEqual(self.hc.get(operator)["labels"]["factory.kind"], "operator")
        # Re-observe a new null episode while the first claim is outstanding.
        self.fake.set_status(operator, None)
        self.keeper.tick()
        self.clock.sleep(factory.LATCH_AFTER)
        self.jobs.clear()
        self.keeper.results.put(("operator", first, operator, factory.HcError("finished", "gone")))
        self.keeper.tick()
        self.assertEqual(self.outcome(first, "operator")["why"], "gone")
        self.assertEqual(self.outcome(second, "operator")["why"], "gone")
        self.assertEqual(self.hc.get(operator)["labels"]["factory.kind"], "operator-lost")
        self.fake.set_status(operator, "idle")
        self.keeper = factory.Keeper(self.state_dir, self.hc, self.clock, self.pane.append, self.jobs.append)
        self.keeper.tick()
        self.assertFalse(self.jobs)
        self.hc.label(operator, {"factory.kind": "operator"})
        self.keeper.tick()
        self.run_jobs()
        self.keeper.tick()
        self.run_jobs()
        self.keeper.tick()
        self.assertEqual(self.inputs(operator, None), ["[from @pm]\nfirst\n", "[from @pm]\nsecond\n"])

    @fake_only
    def test_newest_brief_goes_first_and_redelivery_releases_not_ready(self):
        term, _ = self.member(status=None, briefed=False)
        message = self.add("lead-dev")
        superseded = self.add("lead-dev", "old brief", kind="brief")
        brief = self.add("lead-dev", "new brief", kind="brief")
        self.keeper.tick()
        self.assertEqual(self.outcome(superseded)["why"], "superseded")
        self.assertEqual(self.outcome(brief)["why"], "waiting")
        self.assertFalse(self.jobs)
        self.fake.set_status(term, "idle")
        self.keeper.tick()
        self.finish_job(term, brief, factory.HcDown(True, "lost reply"))
        self.keeper.tick()
        self.assertEqual(self.outcome(brief)["o"], "unconfirmed")
        self.assertEqual(self.outcome(message)["why"], "not ready")
        again = self.add("lead-dev", "try brief again", kind="brief")
        self.keeper.tick()
        self.run_jobs()
        self.keeper.tick()
        self.assertEqual(self.outcome(again)["o"], "delivered")
        self.assertEqual(self.outcome(message)["o"], "sending")
        self.run_jobs()
        self.keeper.tick()
        self.assertEqual(self.inputs(term, None), ["[from @pm]\ntry brief again\n", "[from @pm]\nfirst\n"])

    def finish_job(self, term, ident, outcome, name="lead-dev"):
        self.assertEqual(len(self.jobs), 1)
        self.jobs.clear()
        self.keeper.results.put((name, ident, term, outcome))

    @fake_only
    def test_pretyping_failure_retries_without_reordering(self):
        term, _ = self.member()
        first = self.add("lead-dev")
        second = self.add("lead-dev", "later")
        self.keeper.tick()
        self.fake.set_status(term, "blocked")
        self.run_jobs()  # hc refuses, although the earlier view was ready
        self.keeper.tick()
        self.assertEqual(self.outcome(first)["why"], "blocked")
        self.assertEqual(self.outcome(second)["why"], "waiting")
        self.fake.set_status(term, "idle")
        self.keeper.tick()
        self.finish_job(term, first, factory.HcDown(False, "no bytes written"))
        self.keeper.tick()
        self.assertEqual(self.outcome(first)["o"], "sending")
        self.run_jobs()
        self.keeper.tick()
        self.run_jobs()
        self.keeper.tick()
        self.assertEqual(self.inputs(term, None), ["[from @pm]\nfirst\n", "[from @pm]\nlater\n"])

    @fake_only
    def test_note_result_blocked_is_dropped(self):
        operator, _ = self.terminal("operator", kind="operator")
        ident = self.log.add("factory", ["operator"], "quiet", kind="note", meta={"what": "quiet"})[0]
        self.keeper.tick()
        self.fake.set_status(operator, "blocked")
        self.run_jobs()
        self.keeper.tick()
        self.assertEqual(self.outcome(ident, "operator")["why"], "operator could not take it")
        self.assertFalse(self.inputs(operator, None))

    @fake_only
    def test_unsettled_terminal_refusal_requeues_and_the_next_send_delivers_once(self):
        """hc 0.1.20 refuses a fresh terminal whose output has not settled before typing anything (case 8 on a
        real Codex member): the pair waits and is sent again, never unconfirmed."""
        term, _ = self.member()
        refusal = factory.HcError("unavailable", f"{term} is still starting: its output has not settled",
                                  f"nothing was typed; check hc read {term}, then retry")
        ident = self.add("lead-dev")
        self.keeper.tick()
        with patch.object(self.hc, "send", side_effect=[refusal]):
            self.run_jobs()
        self.keeper.tick()                          # settles the refusal and claims the pair again
        records = [(r["o"], r.get("why")) for r in self.log.read_box()[0] if r["t"] == "out" and r["id"] == ident]
        self.assertEqual(records, [("sending", None), ("queued", "waiting"), ("sending", None)])
        self.assertFalse(self.inputs(term, None))
        self.run_jobs()
        self.keeper.tick()
        self.assertEqual(self.outcome(ident)["o"], "delivered")
        self.assertEqual(self.inputs(term, None), ["[from @pm]\nfirst\n"])

    @fake_only
    def test_other_unavailable_replies_stay_unconfirmed(self):
        term, _ = self.member()
        for message in ("the core is shutting down", f"{term} is still starting",
                        "t999 is still starting: its output has not settled"):
            with self.subTest(message=message):
                ident = self.add("lead-dev")
                self.keeper.tick()
                with patch.object(self.hc, "send", side_effect=[factory.HcError("unavailable", message)]):
                    self.run_jobs()
                self.keeper.tick()
                self.assertEqual(self.outcome(ident)["o"], "unconfirmed")

    @fake_only
    def test_malformed_send_result_is_unconfirmed_and_frees_the_recipient(self):
        term, _ = self.member()
        results = ({}, {"turn": "1"}, {"turn": True}, {"turn": 1, "started": "yes"},
                   RuntimeError(), factory.HcDown(True, "lost reply"), {"turn": 2})
        for result in results:
            with self.subTest(result=result):
                ident = self.add("lead-dev")
                self.keeper.tick()
                with patch.object(self.hc, "send", side_effect=[result]):
                    self.run_jobs()
                self.keeper.tick()
                self.assertNotIn("lead-dev", self.keeper.inflight)
                expected = "delivered" if result == {"turn": 2} else "unconfirmed"
                self.assertEqual(self.outcome(ident)["o"], expected)
                if expected == "delivered":
                    self.assertIsNone(self.outcome(ident)["started"])

    @fake_only
    def test_refusal_before_stop_evidence_requeues_and_the_sweep_drops(self):
        term, _ = self.member()
        ident = self.add("lead-dev")
        self.keeper.tick()
        self.fake.finish(term)
        self.run_jobs()
        self.keeper.tick()
        self.assertEqual(self.outcome(ident)["why"], "gone")
        factory.mark_stopped(self.state_dir, [term], self.keeper.box)
        self.hc.rm(term)
        self.assertEqual(self.log.drop_queued(self.keeper.box, ["lead-dev"]), 1)
        self.assertEqual(self.outcome(ident)["o"], "dropped")

    @fake_only
    def test_refusal_after_stop_evidence_is_dropped_directly(self):
        for result in (factory.HcError("finished", "gone"), factory.HcError("not_found", "gone"),
                       factory.HcError("blocked", "permission"), factory.HcDown(False, "unsent")):
            with self.subTest(result=result):
                term, _ = self.member()
                ident = self.add("lead-dev")
                self.keeper.tick()
                self.fake.finish(term)
                factory.mark_stopped(self.state_dir, [term], self.keeper.box)
                self.hc.rm(term)
                self.assertEqual(self.log.drop_queued(self.keeper.box, ["lead-dev"]), 0)
                replacement, _ = self.member()
                self.finish_job(term, ident, result)
                self.keeper.tick()
                self.assertEqual(self.outcome(ident)["why"], "stopped by down")
                self.assertFalse(self.inputs(replacement, None))
                self.assertFalse(self.jobs)
                self.fake.remove(replacement)

    @fake_only
    def test_replaced_terminal_without_stop_evidence_receives_the_message_after_brief(self):
        term, _ = self.member()
        ident = self.add("lead-dev")
        self.keeper.tick()
        self.fake.remove(term)
        replacement, _ = self.member(status=None, briefed=False)
        self.run_jobs()
        with patch.object(self.keeper.log, "locked", wraps=self.keeper.log.locked) as lock:
            self.keeper._drain_results()
            lock.assert_called_once_with(exclusive=True)
        self.assertEqual(self.outcome(ident)["why"], "gone")
        self.keeper.tick()
        self.assertFalse(self.jobs)
        self.briefed("lead-dev", replacement)
        self.fake.set_status(replacement, "idle")
        self.keeper.tick()
        self.run_jobs()
        self.keeper.tick()
        self.assertEqual(self.inputs(replacement, None), ["[from @pm]\nfirst\n"])

    @fake_only
    def test_receipt_and_uncertain_results_ignore_stop_evidence(self):
        term, _ = self.member()
        for result, expected in (({"turn": 10, "started": True}, "delivered"),
                                 (factory.HcDown(True, "lost"), "unconfirmed")):
            ident = self.add("lead-dev")
            self.keeper.tick()
            factory.mark_stopped(self.state_dir, [term], self.keeper.box)
            self.finish_job(term, ident, result)
            self.keeper.tick()
            self.assertEqual(self.outcome(ident)["o"], expected)
        factory.mark_stopped(self.state_dir, ["t99"], self.keeper.box)
        self.assertEqual(factory.read_team(self.state_dir)["stopped"], ["t99"])

    @fake_only
    def test_missing_or_unreadable_metadata_withholds_settlement(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                term, _ = self.member()
                ident = self.add("lead-dev")
                self.keeper.tick()
                self.fake.finish(term)
                factory.mark_stopped(self.state_dir, [term], self.keeper.box)
                self.hc.rm(term)
                self.log.drop_queued(self.keeper.box, ["lead-dev"])
                replacement, _ = self.member()
                later = self.add("lead-dev", "later")
                path = self.state_dir / "team.json"
                data = path.read_bytes()
                if missing:
                    path.unlink()
                else:
                    path.write_text("{")
                self.finish_job(term, ident, factory.HcError("finished", "gone"))
                self.keeper.tick()
                self.keeper.tick()
                self.assertEqual(self.keeper.inflight, {"lead-dev": ident})
                self.assertEqual(len(self.keeper.unsettled), 1)
                self.assertEqual(self.outcome(ident)["o"], "sending")
                self.assertEqual(self.outcome(later)["why"], "waiting")
                self.assertFalse(self.keeper.stop)
                self.assertFalse(self.jobs)
                path.write_bytes(data)
                self.keeper.tick()
                self.run_jobs()
                self.keeper.tick()
                self.assertEqual(self.outcome(ident)["o"], "dropped")
                self.assertEqual(self.outcome(later)["o"], "delivered")
                self.assertEqual(self.inputs(replacement, None), ["[from @pm]\nlater\n"])
                self.fake.remove(replacement)

    @fake_only
    def test_withheld_result_becomes_unconfirmed_on_restart(self):
        term, _ = self.member()
        ident = self.add("lead-dev")
        self.keeper.tick()
        path = self.state_dir / "team.json"
        data = path.read_bytes()
        path.unlink()
        self.finish_job(term, ident, factory.HcError("finished", "gone"))
        self.keeper.tick()
        self.assertTrue(self.keeper.unsettled)
        path.write_bytes(data)
        restart = factory.Keeper(self.state_dir, self.hc, self.clock, self.pane.append, self.jobs.append)
        self.assertTrue(restart._load())
        restart._recover("keeper restarted during the send")
        restart.tick()
        self.assertEqual(self.outcome(ident)["o"], "unconfirmed")
        self.assertFalse(self.jobs)
        self.assertFalse(self.inputs(term, None))

    @fake_only
    def test_wire_text_is_read_after_the_claim(self):
        term, _ = self.member()
        ident = self.add("lead-dev")
        claim = self.keeper.log.out

        def change_before_claim(box, msgid, *args, **kwargs):
            if msgid == ident and args[1] == "sending":
                before = self.log.path.stat()
                self.log.path.write_bytes(self.log.path.read_bytes().replace(b'"first"', b'"fixed"'))
                os.utime(self.log.path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
            return claim(box, msgid, *args, **kwargs)

        with patch.object(self.keeper.log, "out", side_effect=change_before_claim):
            self.keeper.tick()
        self.run_jobs()
        self.keeper.tick()
        self.assertEqual(self.inputs(term, None), ["[from @pm]\nfixed\n"])

    @fake_only
    def test_failed_sending_record_recovers_without_restart(self):
        term, _ = self.member()
        first = self.add("lead-dev")
        second = self.add("lead-dev", "later")
        append = self.keeper.log._append

        def visible_but_unsynced(data):
            _, end = append(data)
            return "visible", end

        with patch.object(self.keeper.log, "_append", side_effect=visible_but_unsynced):
            self.keeper.tick()
        self.assertTrue(self.keeper.damaged)
        self.assertEqual(self.outcome(first)["o"], "sending")
        self.assertEqual(self.outcome(second)["why"], "waiting")
        self.assertFalse(self.jobs)
        self.clock.sleep(59)
        self.keeper.tick()
        self.assertFalse(self.jobs)
        self.clock.sleep(1)
        self.keeper.tick()
        self.assertEqual(self.outcome(first)["o"], "unconfirmed")
        self.assertEqual(self.outcome(second)["o"], "sending")
        self.run_jobs()
        self.keeper.tick()
        self.assertEqual(self.inputs(term, None), ["[from @pm]\nlater\n"])

    @fake_only
    def test_managed_file_is_removed_after_its_last_delivery(self):
        lead, _ = self.member()
        reviewer, _ = self.terminal("reviewer")
        self.briefed("reviewer", reviewer)
        path = self.state_dir / "messages" / "pm" / "done.md"
        path.parent.mkdir(parents=True)
        path.write_text("exact")
        ident = self.log.add("pm", ["lead-dev", "reviewer"], "exact", file=str(path))[0]
        self.keeper.tick()
        held = self.jobs.pop()
        self.run_jobs()
        self.keeper.tick()
        self.assertTrue(path.exists())
        self.assertEqual(self.outcome(ident)["o"], "delivered")
        held()
        self.keeper.tick()
        self.assertFalse(path.exists())
        self.assertEqual(self.inputs(lead, None), self.inputs(reviewer, None))

    @fake_only
    def test_bad_factory_toml_keeps_last_config(self):
        term, _ = self.member()
        self.keeper.tick()
        (self.root / factory.CONF).write_text("[invalid")
        ident = self.add("lead-dev")
        with contextlib.redirect_stderr(io.StringIO()):
            self.keeper.tick()
        self.run_jobs()
        with contextlib.redirect_stderr(io.StringIO()):
            self.keeper.tick()
        self.assertEqual(self.outcome(ident)["o"], "delivered")
        self.assertEqual(self.inputs(term, None), ["[from @pm]\nfirst\n"])
        self.assertEqual(sum(line.startswith("factory.toml:") for line in self.pane), 1)

    @fake_only
    def test_keeper_touches_beat_during_hold_and_unknown_team(self):
        self.member()
        ident = self.add("lead-dev")
        beat = self.state_dir / "keeper.beat"
        path = self.state_dir / "team.json"
        data = path.read_bytes()
        with beat.open("wb") as stream:
            self.keeper._beat_fd = stream.fileno()
            self.keeper.tick()
            self.assertEqual(self.outcome(ident)["o"], "sending")
            path.write_text("{")
            for stamp in (1, 2):
                os.utime(beat, (stamp, stamp))
                self.keeper.tick()
                self.assertGreater(beat.stat().st_mtime, stamp)
                self.assertFalse(self.keeper.stop)
        self.keeper._beat_fd = None
        self.assertEqual(sum(line.startswith("team.json:") for line in self.pane), 1)
        path.write_bytes(data)
        self.run_jobs()
        self.keeper.tick()
        self.assertEqual(self.outcome(ident)["o"], "delivered")

    @fake_only
    def test_second_keeper_exits_without_taking_over(self):
        keeper_term, _ = self.terminal("keeper", kind="keeper")
        (self.state_dir / "keeper.beat").write_text("original")
        with (self.state_dir / "keeper.lock").open("wb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with patch.dict(os.environ, {"HC_TERMINAL_ID": keeper_term}):
                self.assertEqual(self.keeper.run(), 0)
        self.assertEqual(self.pane, ["keeper already running"])
        self.assertEqual((self.state_dir / "keeper.beat").read_text(), "original")
        self.assertFalse(self.hc._closed)

    @fake_only
    def test_startup_damage_keeps_the_beat_and_recovers_after_repair(self):
        keeper_term, _ = self.terminal("keeper", kind="keeper")
        self.member()
        ident = self.add("lead-dev")
        healthy = self.log.path.read_bytes()
        self.log.path.write_bytes(b"{}\n" + healthy)
        tick = self.keeper.tick

        def repair_then_continue():
            self.assertTrue(self.keeper.damaged)
            self.assertFalse(self.jobs)
            self.assertEqual((self.state_dir / "keeper.beat").read_text().split()[2], keeper_term)
            self.log.path.write_bytes(healthy)
            self.clock.sleep(60)
            tick()
            self.run_jobs()
            tick()
            self.keeper.stop = "test completed"

        with (patch.dict(os.environ, {"HC_TERMINAL_ID": keeper_term}),
              patch.object(self.keeper, "tick", side_effect=repair_then_continue),
              patch.object(self.keeper, "_recover", wraps=self.keeper._recover) as recovery):
            self.assertEqual(self.keeper.run(), 0)
        recovery.assert_called_once_with("keeper could not record the send")
        self.assertEqual(self.outcome(ident)["o"], "delivered")
        self.assertTrue(self.hc._closed)

    @fake_only
    def test_stop_prevents_sends_and_run_always_kills_children(self):
        term, _ = self.member()
        ident = self.add("lead-dev")
        self.keeper.stop = "signal"
        self.keeper.tick()
        self.assertEqual(self.outcome(ident)["why"], "waiting")
        self.assertFalse(self.jobs)
        self.keeper.stop = None
        self.keeper.tick()
        self.assertEqual(len(self.jobs), 1)
        self.keeper.stop = "signal"
        self.run_jobs()
        self.assertFalse(self.inputs(term, None))
        self.keeper.stop = None
        keeper_term, _ = self.terminal("keeper", kind="keeper")
        with (patch.dict(os.environ, {"HC_TERMINAL_ID": keeper_term}),
              patch.object(self.keeper, "tick", side_effect=RuntimeError("tick failed"))):
            self.assertEqual(self.keeper.run(), 0)
        self.assertTrue(self.hc._closed)
        self.assertEqual(self.pane[-1], "keeper stopping: tick failed")

    @fake_only
    def test_keeper_lock_and_beat_do_not_follow_links(self):
        keeper_term, _ = self.terminal("keeper", kind="keeper")
        outside = self.root / "outside"
        outside.write_text("keep")
        for filename in ("keeper.lock", "keeper.beat"):
            with self.subTest(filename=filename):
                path = self.state_dir / filename
                path.symlink_to(outside)
                keeper = factory.Keeper(self.state_dir, factory.Hc(), emit=self.pane.append)
                with patch.dict(os.environ, {"HC_TERMINAL_ID": keeper_term}):
                    self.assertEqual(keeper.run(), 0)
                self.assertTrue(path.is_symlink())
                self.assertIn(filename, self.pane[-1])
                self.assertEqual(outside.read_text(), "keep")
                path.unlink()

    @fake_only
    def test_stopped_team_keeps_running_for_reachable_operators_pending_messages(self):
        term, _ = self.terminal("operator", kind="operator", status="blocked")
        ident = self.add("operator")
        factory.update_team(self.state_dir, up=False)
        self.keeper.tick()
        self.assertIsNone(self.keeper.stop)
        self.assertEqual(self.outcome(ident, "operator")["why"], "blocked")
        self.fake.set_status(term, "idle")
        self.keeper.tick()
        self.run_jobs()
        self.keeper.tick()
        self.assertEqual(self.outcome(ident, "operator")["o"], "delivered")
        self.assertEqual(self.keeper.stop, "team is down")


@fake_only
class EnsureKeeperTests(TeamCase):
    def beat(self, terminal, version=factory.VERSION, age=0):
        path = self.state_dir / "keeper.beat"
        path.write_text(f"42 {version} {terminal}\n")
        stamp = self.clock.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def ensure(self, **kwargs):
        return factory.ensure_keeper(self.cfg, self.state_dir, self.hc, factory.View([]), **kwargs)

    def test_starts_missing_keeper_with_labelled_terminal_and_waits_for_beat(self):
        run = self.hc.run

        def start(*args, **kwargs):
            result = run(*args, **kwargs)
            self.beat(result["address"])
            return result

        with (patch.object(factory, "time", self.clock),
              patch.object(self.hc, "run", side_effect=start),
              patch.dict(os.environ, {"HC_BIN": self.hc.bin or factory.HC})):
            terminal = self.ensure()
            self.assertEqual(factory.read_beat(self.state_dir), (42, factory.VERSION, terminal, 0))
        request = self.fake.calls("terminal.run")[0]["params"]
        self.assertEqual(request["argv"], [sys.executable, str(factory.SCRIPT), "keeper", "--state",
                                          str(self.state_dir)])
        self.assertEqual(request["labels"], {"factory.team": self.team, "factory.kind": "keeper",
                                             "factory.dir": str(self.state_dir), "factory.version": factory.VERSION,
                                             "name": "keeper"})
        self.assertEqual(request["cwd"], str(self.root))
        self.assertTrue(request["no_parent"])
        self.assertTrue(request["no_notify"])
        self.assertEqual(request["env"]["HC_BIN"], factory.HC)
        self.assertEqual(self.clock.now, 0)

    def test_leaves_young_keeper_alone_even_if_a_beat_is_missing_or_names_another_terminal(self):
        keeper, _ = self.terminal("keeper", kind="keeper", status=None)
        with patch.object(factory, "time", self.clock):
            self.assertEqual(self.ensure(), keeper)
            self.beat("t999")
            self.assertEqual(self.ensure(), keeper)
        self.assertEqual(self.fake.calls("terminal.run"), [])
        self.assertEqual(self.fake.calls("terminal.kill"), [])

    def test_other_version_is_alive_when_its_beat_is_fresh(self):
        keeper, _ = self.terminal("keeper", kind="keeper", status=None)
        self.clock.sleep(factory.BEAT_STALE + 1)
        self.beat(keeper, version="older")
        with patch.object(factory, "time", self.clock):
            self.assertEqual(self.ensure(), keeper)
        self.assertEqual(self.fake.calls("terminal.kill"), [])

    def test_kills_hung_keeper_by_terminal_id_and_removes_old_finished_records(self):
        keeper, _ = self.terminal("keeper", kind="keeper", status=None)
        finished, _ = self.terminal("keeper", kind="keeper", status=None)
        self.fake.finish(finished)
        unrelated, _ = self.terminal("lead-dev")
        self.clock.sleep(factory.BEAT_STALE + 1)
        self.beat(unrelated)
        with patch.object(factory, "time", self.clock):
            replacement = self.ensure()
        self.assertNotEqual(replacement, keeper)
        self.assertEqual(self.fake.calls("terminal.kill")[0]["params"], {"terminal": keeper})
        self.assertEqual({req["params"]["terminal"] for req in self.fake.calls("terminal.rm")}, {keeper, finished})
        self.assertEqual(self.hc.get(unrelated)["state"], "live")

    def test_reports_keeper_that_exits_at_start_and_removes_it(self):
        run = self.hc.run

        def start(*args, **kwargs):
            result = run(*args, **kwargs)
            self.fake.screen(result["address"], "older output\n\nobserved startup error\n\n")
            self.fake.finish(result["address"])
            return result

        with patch.object(self.hc, "run", side_effect=start):
            with self.assertRaisesRegex(factory.Problem, "the keeper did not start: observed startup error"):
                self.ensure()
        self.assertEqual(self.hc.list(self.team), [])

    def test_leaves_stopped_team_stopped_unless_forced_or_operator_has_pending(self):
        factory.update_team(self.state_dir, up=False)
        self.add("lead-dev")
        self.assertIsNone(self.ensure())
        self.assertFalse(self.fake.calls("terminal.run"))
        with patch.object(factory, "time", self.clock):
            first = self.ensure(force=True)
        self.fake.remove(first)
        self.add("operator")
        with patch.object(factory, "time", self.clock):
            self.assertNotEqual(self.ensure(), first)
        self.assertEqual(len(self.fake.calls("terminal.run")), 2)

    def test_serializes_two_starters(self):
        # Separate descriptors in separate processes, both initially with no keeper in their view.
        script = """import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("factory", sys.argv[1])
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)
f.KEEPER_START = 0.5
print(f.ensure_keeper(f.Config(sys.argv[2]), Path(sys.argv[3]), f.Hc(), f.View([])))
"""
        env = {**os.environ, "HC_BIN": factory.HC}
        argv = [sys.executable, "-c", script, str(SCRIPT), str(self.root), str(self.state_dir)]
        with (subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as first,
              subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as second):
            results = [child.communicate(timeout=10) for child in (first, second)]
            self.assertEqual((first.returncode, second.returncode), (0, 0), results)
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.fake.calls("terminal.run")), 1)

    def test_start_lock_waits_at_most_fifteen_seconds(self):
        with (self.state_dir / "keeper.start").open("wb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with patch.object(factory, "time", self.clock):
                with self.assertRaisesRegex(factory.Problem, "another command is starting the keeper"):
                    self.ensure()
        self.assertGreaterEqual(self.clock.now, 15)
        self.assertLess(self.clock.now, 15.2)
        self.assertFalse(self.fake.calls("terminal.run"))

    def test_start_lock_refuses_a_symlink(self):
        outside = self.root / "outside"
        outside.write_text("keep")
        (self.state_dir / "keeper.start").symlink_to(outside)
        with self.assertRaisesRegex(OSError, "keeper.start"):
            self.ensure()
        self.assertEqual(outside.read_text(), "keep")
        self.assertFalse(self.fake.calls("terminal.run"))

    def test_read_beat_requires_the_full_record(self):
        self.assertIsNone(factory.read_beat(self.state_dir))
        path = self.beat("t1")
        for data in (b"", b"42 too short? too long", b"x version t1", b"\xff"):
            with self.subTest(data=data):
                path.write_bytes(data)
                self.assertIsNone(factory.read_beat(self.state_dir))


class CliTests(TeamCase):
    def prepare_sender(self, name="operator", *, kind="operator"):
        terminal, _ = self.terminal(name, kind=kind)
        return terminal

    def deliver_during_ensure(self, *args):
        self.keeper.tick()
        self.run_jobs()
        self.keeper.tick()
        return "keeper"

    @fake_only
    def test_a_member_say_records_its_session_even_when_refused(self):
        member = self.prepare_sender("lead-dev", kind="member")
        operator = self.prepare_sender()
        threads = {"CODEX_THREAD_ID": "thread-1", "CLAUDE_CODE_SESSION_ID": "s-1", "PI_SESSION_ID": "p-1"}
        with patch.dict(os.environ, threads):
            status, _, err = self.say(member, "operator", "text")          # refused: members send with --file
            self.assertEqual(status, 2, err)
            self.assertEqual(self.say(operator, "operator", "text")[0], 2)
        newest, _ = factory.saved_sessions(self.state_dir)
        self.assertEqual(set(newest), {"lead-dev"})                         # the operator records nothing
        self.assertEqual({k: newest["lead-dev"][k] for k in ("harness", "term", "how", "value")},
                         {"harness": "codex", "term": member, "how": "env", "value": "thread-1"})
        (self.state_dir / factory.SESSIONS).unlink()
        (self.state_dir / factory.SESSIONS).mkdir()                         # cannot be written
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-2"}):
            status, _, err = self.say(member, "operator", "text")
        self.assertIn("warning: could not record your session:", err)
        self.assertIn("nothing sent: members send with --file", err)        # and say went on

    @fake_only
    def test_say_refusals_never_store_or_deliver(self):
        member = self.prepare_sender("lead-dev", kind="member")
        operator = self.prepare_sender()
        keeper = self.prepare_sender("keeper", kind="keeper")
        former = self.prepare_sender("departed", kind="member")
        unlabeled = self.fake.add({})
        nameless = self.fake.add({"factory.team": self.team, "factory.dir": str(self.state_dir),
                                  "factory.kind": "member"})
        bad_file = self.state_dir / "messages" / "operator" / "bad_name.md"
        bad_file.parent.mkdir(parents=True)
        bad_file.write_text("text")
        bad_encoding = self.root / "bad.md"
        bad_encoding.write_bytes(b"\xff")
        self.add("operator")
        before = self.log.path.read_bytes()
        cases = (
            ("", ["lead-dev", "text"], "run up first"),
            ("t9999", ["lead-dev", "text"], "run up first"),
            (unlabeled, ["lead-dev", "text"], "run up first"),
            (nameless, ["operator", "text"], "run up first"),
            (keeper, ["operator", "text"], "the keeper cannot send"),
            (former, ["operator", "text"], "@departed is no longer in factory.toml"),
            (operator, ["lead-dev", "stranger", "text"], "@stranger is not on the team"),
            (operator, ["operator", "text"], "choose another teammate as the recipient"),
            (operator, ["reviewer", "text"], "@reviewer is not running"),
            (member, ["operator", "text"], "members send with --file"),
            (operator, ["lead-dev"], "give a message or --file"),
            (operator, ["lead-dev", "--file", "missing.md"], "cannot read message file missing.md"),
            (operator, ["lead-dev", "--file", str(bad_encoding)], "cannot read message file"),
            (operator, ["lead-dev", " \n "], "give a non-empty message"),
            (operator, ["lead-dev", "é" * 32769], "message is 65538 bytes"),
            (operator, ["lead-dev", "--file", str(bad_file)], "use letters, digits and dashes"),
            (operator, ["lead-dev", "text", "--file", str(bad_file)], "@text is not on the team"),
        )
        for caller, args, reason in cases:
            with self.subTest(caller=caller, reason=reason):
                status, out, err = self.say(caller, *args)
                self.assertEqual((status, out), (2, ""), err)
                self.assertTrue(err.startswith("nothing sent: "), err)
                self.assertIn(reason, err)
                self.assertEqual(self.log.path.read_bytes(), before)
        self.fake.remove(member)
        self.assertIn("no other member is running", self.say(operator, "all", "text")[2])
        self.assertFalse(self.fake.calls("terminal.send"))
        self.assertFalse(self.fake.calls("terminal.run"))

    @fake_only
    def test_invalid_labels_and_metadata_are_refused_without_a_write(self):
        operator = self.prepare_sender()
        self.prepare_sender("lead-dev", kind="member")
        self.add("lead-dev")
        before = self.log.path.read_bytes()
        original = factory.read_team(self.state_dir)
        path = self.state_dir / "team.json"
        for data in ({"v": 1}, {**original, "core": 8}, {**original, "team": "elsewhere"},
                     {**original, "core": self.hc.core[:8] + "-other"}):
            with self.subTest(data=data):
                path.write_text(json.dumps(data))
                status, _, err = self.say(operator, "lead-dev", "text")
                self.assertEqual(status, 2)
                self.assertTrue(err.startswith("nothing sent: "), err)
                self.assertEqual(self.log.path.read_bytes(), before)
        path.write_text(json.dumps(original))
        self.hc.label(operator, {"factory.dir": str(self.root)})
        self.assertIn("not a Factory state directory", self.say(operator, "lead-dev", "text")[2])
        self.assertEqual(self.log.path.read_bytes(), before)
        self.assertFalse(self.fake.calls("terminal.send"))
        self.assertFalse(self.fake.calls("terminal.run"))

    @fake_only
    def test_names_deduplicate_expand_all_and_keep_gone_members_queueable(self):
        operator = self.prepare_sender()
        live = self.prepare_sender("lead-dev", kind="member")
        gone = self.prepare_sender("reviewer", kind="member")
        self.briefed("lead-dev", live)
        self.fake.finish(gone)
        with patch.object(factory, "ensure_keeper", side_effect=self.deliver_during_ensure):
            status, out, err = self.say(operator, "all", "all body")
            self.assertEqual((status, err), (0, ""))
            self.assertEqual(out.splitlines(), ["message 2", "@lead-dev: delivered"])
            status, out, err = self.say(operator, "@reviewer, lead-dev", "reviewer", "body")
            self.assertEqual((status, err), (0, ""))
            self.assertEqual(out.splitlines(), ["message 3", "@reviewer: queued (gone)", "@lead-dev: delivered"])
        self.assertEqual(self.inputs(live, None), ["[from @operator]\nall body\n", "[from @operator]\nbody\n"])

    @fake_only
    def test_say_records_dotdot_path_and_never_removes_the_managed_file(self):
        member = self.prepare_sender("lead-dev", kind="member")
        operator = self.prepare_sender()
        outer = self.root / "nonmanaged"
        (outer / "nested").mkdir(parents=True)
        (outer / "x.md").write_bytes(b"line 1\r\nline 2\n\n")
        directory = self.state_dir / "messages" / "lead-dev"
        directory.mkdir(parents=True)
        (directory / "link").symlink_to(outer / "nested", target_is_directory=True)
        (directory / "x.md").write_text("do not remove")
        recorded = str(directory / "link/../x.md")
        with patch.object(factory, "ensure_keeper", side_effect=self.deliver_during_ensure):
            status, out, err = self.say(member, "operator", "--file", recorded)
        self.assertEqual((status, out, err), (0, "message 1\n@operator: delivered\n", ""))
        self.assertEqual(self.inputs(operator, None), ["[from @lead-dev]\nline 1\r\nline 2\n\n"])
        self.assertEqual(self.log.read()[0][0]["file"], recorded)
        self.assertEqual((directory / "x.md").read_text(), "do not remove")

    @fake_only
    def test_say_keeps_id_when_keeper_fails_or_outcomes_cannot_be_read(self):
        operator = self.prepare_sender()
        self.prepare_sender("lead-dev", kind="member")
        failure = factory.Problem("startup failed", "run factory.py status")
        with patch.object(factory, "ensure_keeper", side_effect=failure):
            status, out, err = self.say(operator, "lead-dev", "text")
            self.assertEqual((status, out), (0, "message 1\n@lead-dev: queued (waiting)\n"))
            self.assertIn("Message 1 is stored", err)
            self.assertNotIn("nothing sent", err)
            with patch.object(factory, "wait_outcomes", side_effect=OSError("cannot read")):
                status, out, err = self.say(operator, "lead-dev", "text")
                self.assertEqual((status, out), (1, "message 2\n"))
                self.assertIn("message 2 is stored; its outcome could not be read", err)
                self.assertIn("Do not resend", err)

    @fake_only
    def test_say_storage_failure_and_warning_outputs(self):
        operator = self.prepare_sender()
        member = self.prepare_sender("lead-dev", kind="member")
        self.briefed("lead-dev", member)
        cases = (
            (factory.LogDamage(self.log.path, 1, "bad"), 2, "nothing sent: "),
            (factory.NotStored(False, "disk full"), 2, "not stored: "),
            (factory.NotStored(True, "failed rollback"), 2, "not stored, and the log may now need repair: "),
            (OSError("storage outcome unknown"), 1, "not confirmed stored: "),
        )
        for error, exit_code, prefix in cases:
            with self.subTest(error=error), patch.object(factory.Log, "add", side_effect=error):
                status, out, err = self.say(operator, "lead-dev", "text")
                self.assertEqual((status, out), (exit_code, ""))
                self.assertTrue(err.startswith(prefix), err)
                if isinstance(error, OSError) or getattr(error, "partial", False):
                    self.assertIn("Do not resend", err)
        self.assertFalse(self.fake.calls("terminal.run"))
        self.assertFalse(self.fake.calls("terminal.send"))
        add = factory.Log.add

        def add_with_warning(log, *args, **kwargs):
            ident, offset, _ = add(log, *args, **kwargs)
            return ident, offset, "stored but disk reported an error. Do not resend."

        with (patch.object(factory.Log, "add", add_with_warning),
              patch.object(factory, "ensure_keeper", side_effect=self.deliver_during_ensure)):
            status, out, err = self.say(operator, "lead-dev", "text")
        self.assertEqual((status, out), (0, "message 2\n@lead-dev: delivered\n"))
        self.assertTrue(err.startswith("warning: stored but disk reported an error"))

    @fake_only
    def test_outcomes_format_and_exit_status(self):
        pairs = {
            "operator": {"o": "queued", "why": "gone"},
            "lead-dev": {"o": "sending"},
            "reviewer": {"o": "unconfirmed", "why": "lost reply"},
            "pm": {"o": "dropped", "why": "stopped by down"},
            "frontend-dev": {"o": "delivered"},
        }
        lines, code = factory.outcome_lines(pairs, "operator", "architect")
        self.assertEqual(code, 1)
        self.assertEqual(lines, [
            "@operator: queued (gone): operator not running; message queued", "@lead-dev: sending",
            "@reviewer: unconfirmed (lost reply)", "@pm: dropped (stopped by down)", "@frontend-dev: delivered",
            "@reviewer, @pm may not have it. Do not resend unless asked; tell @operator.",
        ])
        self.assertNotIn("tell @operator", factory.outcome_lines(pairs, "operator", "operator")[0][-1])

    def test_wait_outcomes_zero_always_reads_and_wait_ends_at_deadline(self):
        ident, offset, _ = self.log.add("operator", ["lead-dev"], "first")
        _, box = self.log.read_box()
        self.log.out(box, ident, "lead-dev", "sending", term="t1")
        with patch.object(self.log, "read", wraps=self.log.read) as read, patch.object(factory, "time", self.clock):
            outcome = factory.wait_outcomes(self.log, ident, ["lead-dev"], offset, timeout=0)
            read.assert_called_once_with(offset)
            self.assertEqual(outcome["lead-dev"]["o"], "sending")
            self.assertEqual(self.clock.now, 0)
            outcome = factory.wait_outcomes(self.log, ident, ["lead-dev"], offset, timeout=0.5)
            self.assertEqual(outcome["lead-dev"]["o"], "sending")
            self.assertEqual(self.clock.now, 0.5)
        self.log.out(box, ident, "lead-dev", "delivered", term="t1", started=True, turn=1)
        with patch.object(factory, "time", self.clock):
            outcome = factory.wait_outcomes(self.log, ident, ["lead-dev"], offset)
            self.assertEqual(outcome["lead-dev"]["o"], "delivered")
            self.assertEqual(self.clock.now, 0.5)

    @real_only
    def test_real_keeper_types_a_say_and_stops_when_team_is_down(self):
        operator = self.prepare_sender()
        member, log = self.terminal("lead-dev")
        self.briefed("lead-dev", member)
        status, out, err = self.say(operator, "lead-dev", "real keeper")
        view = factory.team_view(self.hc, self.team)
        if view.keeper:
            self.addCleanup(self.remove, view.keeper["address"])
        self.assertEqual((status, out, err), (0, "message 2\n@lead-dev: delivered\n", ""))
        self.assertIsNotNone(view.keeper)
        terminal = view.keeper["address"]
        self.until(lambda: self.inputs(member, log) == ["[from @operator]\nreal keeper\n"], "keeper delivery")
        factory.update_team(self.state_dir, up=False)
        self.until(lambda: self.hc.get(terminal)["state"] == "finished", "keeper shutdown")


class LayoutTests(unittest.TestCase):
    @staticmethod
    def pane(terminal, **extra):
        return {"id": f"pane-{terminal}", "type": "block", "content": {"terminal": terminal}, **extra}

    @staticmethod
    def split(ident, *children, kind="row"):
        return {"id": ident, "type": kind, "children": list(children)}

    @classmethod
    def tab(cls, ident, *children):
        return {"id": ident, "name": ident, "root": cls.split(f"root-{ident}", *children)}

    @staticmethod
    def space(ident, *tabs):
        return {"id": ident, "tabs": list(tabs)}

    @staticmethod
    def member(address, **labels):
        return {"address": address, "state": "live", "created_ms": 0,
                "labels": {"factory.kind": "member", "factory.member": address, **labels}}

    def setup_layout(self, count=0, *, keeper=None):
        doc = {"spaces": [self.space("space")]}
        panes = [(f"t{i}", f"@member-{i}") for i in range(count)]
        if keeper:
            panes.append((keeper, "keeper"))
        factory.add_panes(doc, "space", "team", panes, keeper)
        return doc

    @staticmethod
    def children(doc):
        return doc["spaces"][0]["tabs"][0]["root"]["children"]

    def test_new_tab_root_is_a_row(self):
        doc = self.setup_layout(1)
        tab = doc["spaces"][0]["tabs"][0]
        self.assertEqual((tab["id"], tab["name"], tab["root"]["type"]), ("team", "team", "row"))
        self.assertEqual([(node["title"], node["content"]) for node in self.children(doc)],
                         [("@member-0", {"terminal": "t0"})])
        self.assertEqual(factory.where(doc, "t0"), ("space", "team"))
        self.assertIsNone(factory.where(doc, "absent"))

    def test_three_members_three_columns(self):
        doc = self.setup_layout(3)
        self.assertEqual([p["content"]["terminal"] for p in self.children(doc)], ["t0", "t1", "t2"])

    def test_fourth_member_stacks_in_first_column(self):
        doc = self.setup_layout(4)
        first, *other = self.children(doc)
        self.assertEqual(first["type"], "column")
        self.assertEqual([p["content"]["terminal"] for p in first["children"]], ["t0", "t3"])
        self.assertEqual([p["content"]["terminal"] for p in other], ["t1", "t2"])

    def test_nine_members_three_by_three(self):
        doc = self.setup_layout(9)
        self.assertEqual([[p["content"]["terminal"] for p in column["children"]] for column in self.children(doc)],
                         [["t0", "t3", "t6"], ["t1", "t4", "t7"], ["t2", "t5", "t8"]])

    def test_keeper_pane_last_and_narrow_across_edits(self):
        doc = self.setup_layout(0, keeper="keeper")
        factory.add_panes(doc, "space", "team", [("t1", "@one"), ("t2", "@two")], keeper="keeper")
        self.assertEqual([p["content"]["terminal"] for p in self.children(doc)], ["t1", "t2", "keeper"])
        last = self.children(doc)[-1]
        self.assertEqual((last["title"], last["size"]), ("keeper", {"weight": 0.4}))
        factory.add_panes(doc, "space", "team", [("t3", "@three"), ("t4", "@four")], keeper="keeper")
        self.assertIs(self.children(doc)[-1], last)

    def test_owner_restructured_root_is_wrapped_not_changed(self):
        old = self.split("owner-root", self.pane("owner"), kind="column")
        doc = {"spaces": [self.space("space", {"id": "team", "root": old})]}
        factory.add_panes(doc, "space", "team", [("t1", "@one")])
        self.assertIs(self.children(doc)[0], old)
        self.assertEqual(old["children"], [self.pane("owner")])

    def test_fourth_pane_wraps_an_owner_made_row(self):
        owner_row = self.split("nested", self.pane("owner"))
        other_columns = [self.split(key, self.pane(key + "-1"), self.pane(key + "-2"), kind="column")
                         for key in ("second", "third")]
        doc = {"spaces": [self.space("space", self.tab("team", owner_row, *other_columns))]}
        factory.add_panes(doc, "space", "team", [("new", "@new")])
        column = self.children(doc)[0]
        self.assertEqual(column["type"], "column")
        self.assertIs(column["children"][0], owner_row)
        self.assertEqual(owner_row["children"], [self.pane("owner")])
        self.assertEqual(column["children"][1]["content"], {"terminal": "new"})

    def test_remove_prunes_empty_and_collapses_only_changed_nonroot_containers(self):
        one = self.split("one", self.pane("t0"), self.pane("owner"), kind="column")
        empty = self.split("empty", self.split("nested", self.pane("t1"), kind="column"))
        untouched = self.split("untouched", self.pane("other"), kind="column")
        tab = self.tab("team", one, empty, untouched)
        root = tab["root"]
        doc = {"spaces": [self.space("space", tab)]}
        factory.remove_panes(doc, {"tab": "team"}, ["t0", "t1"])
        self.assertEqual(root["children"], [self.pane("owner"), untouched])
        self.assertIs(tab["root"], root)

    def test_last_member_removes_only_the_tracked_tab_and_its_own_empty_space(self):
        for made in (False, True):
            with self.subTest(made=made):
                other = self.space("other-space", self.tab("other"))
                team = self.space("space", self.tab("team", self.pane("t1")))
                doc = {"spaces": [team, other]}
                factory.remove_panes(doc, {"space": "space", "tab": "team", "made_space": made}, ["t1"])
                self.assertEqual(doc["spaces"], [other] if made else [team, other])
                self.assertEqual(team["tabs"], [])

    def test_owner_added_block_keeps_team_tab_when_members_stop(self):
        owner = {"id": "tool", "type": "block", "content": {"tool": "files"}}
        tab = self.tab("team", owner)
        doc = {"spaces": [self.space("space", tab)]}
        factory.add_panes(doc, "space", "team", [("t1", "@one")])
        factory.remove_panes(doc, {"space": "space", "tab": "team", "made_space": True}, ["t1", None])
        self.assertEqual(self.children(doc), [owner])

    def test_remove_panes_only_empties_the_team_tab(self):
        owner_tab = self.tab("owner-tab", self.pane("moved"))
        team = self.tab("team", self.pane("t1"))
        doc = {"spaces": [self.space("space", team, owner_tab)]}
        factory.remove_panes(doc, {"space": "space", "tab": "team", "made_space": True}, ["t1", "moved"])
        self.assertEqual(doc["spaces"][0]["tabs"], [owner_tab])
        self.assertEqual(owner_tab["root"]["children"], [])

    def test_missing_block_is_not_an_error_and_existing_blocks_are_not_duplicated(self):
        doc = self.setup_layout(1)
        original = json.dumps(doc)
        factory.remove_panes(doc, {"tab": "team"}, ["t999"])
        factory.add_panes(doc, "space", "another-tab", [("t0", "@duplicate")])
        self.assertEqual(json.dumps(doc), original)
        self.assertFalse(factory.swap_terminal(doc, "t999", "t0"))

    def test_swap_terminal_keeps_position_and_uses_actual_tab(self):
        block = self.pane("old", title="@owner-moved", size={"weight": 2})
        target = self.tab("owner-tab", self.split("stack", block, kind="column"))
        doc = {"spaces": [self.space("team-space", self.tab("team", self.pane("other"))),
                          self.space("owner-space", target)]}
        self.assertFalse(factory.swap_terminal(doc, "old", "other"))
        self.assertTrue(factory.swap_terminal(doc, "old", "new"))
        self.assertIs(factory.blocks(doc)["new"], block)
        self.assertEqual(block, {**self.pane("old"), "content": {"terminal": "new"},
                                 "title": "@owner-moved", "size": {"weight": 2}})
        self.assertEqual(factory.where(doc, "new"), ("owner-space", "owner-tab"))

    def test_root_stays_container_across_adds_and_removals(self):
        doc = self.setup_layout()
        rng = random.Random(170)
        live = set()
        for _ in range(60):
            term = f"t{rng.randrange(12)}"
            if term in live:
                factory.remove_panes(doc, {"tab": "team"}, [term])
                live.remove(term)
            else:
                factory.add_panes(doc, "space", "team", [(term, term)])
                live.add(term)
            self.assertEqual(set(factory.blocks(doc)), live)
            if live:
                self.assertEqual(doc["spaces"][0]["tabs"][0]["root"]["type"], "row")

    def test_resolve_tab_prefers_saved_tab_in_the_space_that_holds_it(self):
        doc = {"spaces": [self.space("team-space", self.tab("team")),
                          self.space("op-space", self.tab("op-tab", self.pane("operator")))]}
        me = {"address": "operator", "labels": {"herdr.space": "op-space", "herdr.tab": "op-tab"}}
        actual = factory.resolve_tab(doc, {"space": "team-space", "tab": "team", "made_space": True},
                                     factory.View([]), me)
        self.assertEqual(actual, ("team-space", "team", True))

    def test_resolve_tab_counts_distinct_terminals_and_excludes_operator_tabs(self):
        doc = {"spaces": [self.space("space", self.tab("operator-actual", self.pane("operator"), self.pane("t1")),
                                     self.tab("operator-labelled", self.pane("t2"), self.pane("t3")),
                                     self.tab("one", self.pane("t4")), self.tab("team", self.pane("t5"), self.pane("t6")))]}
        me = {"address": "operator", "labels": {"herdr.tab": "operator-labelled", "herdr.space": "space"}}
        view = factory.View([self.member(f"t{i}", **({"herdr.tab": "one"} if i == 4 else {})) for i in range(1, 7)])
        self.assertEqual(factory.resolve_tab(doc, {}, view, me), ("space", "team", False))
        self.assertEqual(factory.resolve_tab(doc, {"tab": "one"}, view, me),
                         ("space", "one", False))
        self.assertEqual(factory.resolve_tab(doc, {"tab": "operator-actual"}, view, me),
                         ("space", "operator-actual", False))

    def test_resolve_tab_can_recover_from_labels_and_propose_without_adding_tab(self):
        doc = {"spaces": [self.space("space", self.tab("existing"))]}
        me = {"address": "operator", "labels": {"herdr.space": "space"}}
        view = factory.View([self.member("t1", **{"herdr.tab": "existing"})])
        self.assertEqual(factory.resolve_tab(doc, {}, view, me), ("space", "existing", False))
        space, tab, made = factory.resolve_tab(doc, {}, factory.View([]), me)
        self.assertEqual((space, made), ("space", False))
        self.assertTrue(tab.startswith("tb"))
        self.assertEqual([t["id"] for t in doc["spaces"][0]["tabs"]], ["existing"])
        fresh = {"spaces": []}
        space, tab, made = factory.resolve_tab(fresh, {}, view, me)
        self.assertTrue(made)
        self.assertEqual(fresh, {"spaces": []})
        factory.add_panes(fresh, space, tab, [("t1", "@one")], folder="/src")
        self.assertEqual(fresh["spaces"][0]["folder"], "/src")

    @fake_only
    def test_edit_layout_retries_with_a_new_document_after_conflict(self):
        clock = unittest.mock.Mock(wraps=time)
        with FakeHc() as fake, patch.object(factory, "time", clock):
            hc = factory.Hc()
            initial = {"spaces": [self.space("space", self.tab("team", self.pane("old")))]}
            hc.doc_set(initial, 0)
            wrote = []

            def change(doc):
                wrote.append(factory.where(doc, "old"))
                if len(wrote) == 1:
                    moved = {"spaces": [self.space("elsewhere", self.tab("moved", self.pane("old"), self.pane("owner")))]}
                    hc.doc_set(moved, 1)
                return factory.swap_terminal(doc, "old", "new")

            self.assertIsNone(factory.edit_layout(hc, change))
            current, version = hc.doc_get()
            self.assertEqual(wrote, [("space", "team"), ("elsewhere", "moved")])
            self.assertEqual(factory.where(current, "new"), ("elsewhere", "moved"))
            self.assertIn("owner", factory.blocks(current))
            self.assertEqual(version, 3)
            self.assertEqual(len(fake.calls("doc.set")), 4)
            clock.sleep.assert_called_once_with(0.05)

    @fake_only
    def test_edit_layout_gives_up_after_its_last_conflict(self):
        clock = unittest.mock.Mock(wraps=time)
        clock.sleep.return_value = None
        with FakeHc() as fake, patch.object(factory, "time", clock):
            hc = factory.Hc()
            fake.doc_conflicts(factory.CAS_TRIES)
            self.assertEqual(factory.edit_layout(hc, lambda doc: True), "another client kept changing it")
            self.assertEqual(len(fake.calls("doc.set")), factory.CAS_TRIES)
            self.assertEqual(clock.sleep.call_count, factory.CAS_TRIES - 1)

    @fake_only
    def test_edit_layout_refuses_an_unexpected_doc(self):
        bad_layouts = ([], {}, {"spaces": [self.space("s", {"id": "t", "root": {"id": "r", "type": "row"}})]})
        with FakeHc() as fake:
            hc = factory.Hc()
            for malformed in bad_layouts:
                with self.subTest(malformed=malformed):
                    _, version = hc.doc_get()
                    hc.doc_set(malformed, version)
                    count = len(fake.calls("doc.set"))
                    edit = unittest.mock.Mock(return_value=True)
                    self.assertEqual(factory.edit_layout(hc, edit), "the herdr-app doc has an unexpected shape")
                    edit.assert_not_called()
                    self.assertEqual(len(fake.calls("doc.set")), count)

    @fake_only
    def test_edit_layout_skips_write_for_noop_and_reports_transport_errors(self):
        with FakeHc() as fake:
            hc = factory.Hc()
            self.assertIsNone(factory.edit_layout(hc, lambda doc: False))
            self.assertFalse(fake.calls("doc.set"))
        for method, error in (("doc_get", factory.HcDown(False, "core unavailable")),
                              ("doc_set", factory.HcError("invalid_params", "cannot edit"))):
            with self.subTest(method=method), patch.object(hc, "doc_get", return_value=(None, 0)):
                with patch.object(hc, method, side_effect=error):
                    self.assertEqual(factory.edit_layout(hc, lambda doc: True), str(error))


class TeamLookupTests(unittest.TestCase):
    def setUp(self):
        self.env = self.enterContext(Env(UI))
        self.enterContext(patch.dict(os.environ, self.env.env, clear=True))
        self.root = self.env.repo
        self.cfg = factory.Config(self.root)
        self.key = factory.team_key(self.root)
        self.core = "fakecore00000000"
        self.args = types.SimpleNamespace(cwd=str(self.root / "sub"))

    def make_state(self, core=None):
        core = self.core if core is None else core
        state = factory.state_dir(self.root, core)
        factory.update_team(state, root=str(self.root), team=self.key, core=core, up=True)
        return state

    def labels(self, state, member="lead-dev"):
        return {"factory.team": self.key, "factory.dir": str(state),
                "factory.kind": "member", "factory.member": member}

    def inbox(self, count=10, for_name=None):
        out, err = io.StringIO(), io.StringIO()
        args = ["inbox", str(count), "--cwd", self.args.cwd]
        if for_name is not None:
            args.extend(["--for", for_name])
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = 0
            try:
                factory.main(args)
            except SystemExit as error:
                code = error.code
        return code, out.getvalue(), err.getvalue()

    def test_two_cache_homes_share_one_log(self):
        state = self.make_state()
        with FakeHc() as fake:
            term = fake.add(self.labels(state))
            with patch.dict(os.environ, {"HC_TERMINAL_ID": term, "XDG_CACHE_HOME": str(self.env.root / "elsewhere")}):
                hc = factory.Hc()
                cfg, resolved, caller = factory.resolve_team(types.SimpleNamespace(cwd="/unrelated"), hc)
                self.assertEqual((cfg.root, resolved, caller["address"]), (self.root, state, term))
                self.assertEqual(hc.expect_core, self.core)
                factory.open_log(resolved).add("operator", ["lead-dev"], "shared text")
        self.assertFalse((self.env.root / "elsewhere").exists())
        self.assertEqual(factory.inbox_rows(factory.open_log(state), "lead-dev")[0][0]["text"], "shared text")

    def test_labelled_state_with_wrong_shape_or_team_is_refused_without_a_write(self):
        state = self.make_state()
        with FakeHc() as fake:
            for labels in (self.labels(state.parent), {**self.labels(state), "factory.team": "other"}):
                term = fake.add(labels)
                with (self.subTest(labels=labels), patch.dict(os.environ, {"HC_TERMINAL_ID": term}),
                      self.assertRaises(factory.Problem)):
                    factory.resolve_team(self.args, factory.Hc())
        self.assertFalse((state / "log.jsonl").exists())

    def test_state_for_live_team_prefers_the_labelled_operator(self):
        state = self.make_state()
        with FakeHc() as fake:
            other = self.env.root / "other-cache" / "factory-skill" / self.key / state.name
            fake.add(self.labels(other), created_ms=2)
            fake.add({**self.labels(state, "operator"), "factory.kind": "operator"}, created_ms=1)
            with patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.env.root / "elsewhere")}):
                self.assertEqual(factory.state_for(self.cfg, factory.Hc()), state)

    def test_inbox_default_name_is_the_callers(self):
        state = self.make_state()
        log = factory.open_log(state)
        log.add("operator", ["lead-dev"], "member only")
        log.add("lead-dev", ["operator"], "operator only")
        with FakeHc() as fake:
            term = fake.add(self.labels(state))
            with patch.dict(os.environ, {"HC_TERMINAL_ID": term}):
                code, output, err = self.inbox()
                self.assertEqual((code, err), (0, ""))
                self.assertIn("member only", output)
                self.assertNotIn("operator only", output)
                fake.stop()
                code, output, err = self.inbox()
                self.assertEqual((code, err), (0, ""))
                self.assertIn("operator only", output)

    def test_offline_selection(self):
        older = self.make_state("11111111old")
        newer = self.make_state("22222222new")
        metadata = self.make_state("33333333meta")
        (older.parent / "alias").symlink_to(metadata, target_is_directory=True)
        for second, candidate in enumerate((older, newer, metadata), 1):
            os.utime(candidate / "team.json", (second, second))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(factory.state_for(self.cfg, None), metadata)
        self.assertEqual(err.getvalue(), f"using {metadata}\n")
        for state in (older, newer):
            factory.open_log(state).add("lead-dev", ["operator"], state.name)
        os.utime(older / "log.jsonl", (5, 5))
        os.utime(newer / "log.jsonl", (6, 6))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(factory.state_for(self.cfg, None), newer)
            os.utime(newer / "log.jsonl", (5, 5))
            self.assertEqual(factory.state_for(self.cfg, None), older)
        (metadata / "log.jsonl").symlink_to(older / "log.jsonl")
        code, output, err = self.inbox()
        self.assertEqual((code, output), (2, ""))
        self.assertIn(str(metadata / "log.jsonl"), err)

    def test_inbox_without_team_state_exits_2(self):
        with patch.object(factory, "Hc", side_effect=AssertionError("offline inbox called hc")):
            code, output, err = self.inbox()
            self.assertEqual((code, output), (2, ""))
            self.assertIn("no team state", err)
            self.make_state()
            self.assertEqual(self.inbox(), (0, "no messages for @operator\n", ""))

    def test_inbox_filters_and_shows_outcome(self):
        log = factory.open_log(self.make_state())
        log.add("operator", ["lead-dev"], "line one\nline two")
        log.add("factory", ["operator"], "note", kind="note")
        log.out(factory.Box(log.path), 1, "lead-dev", "queued", why="blocked")
        original = log.path.read_bytes()
        code, output, err = self.inbox(count=1, for_name="@lead-dev")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Message 1 from @operator", output)
        self.assertIn(": queued (blocked)]\n    line one\n    line two\n", output)
        self.assertNotIn("note", output)
        self.assertIn("Message 2 from factory", self.inbox()[1])
        self.assertEqual(log.path.read_bytes(), original)
        self.assertEqual(self.inbox(count=0)[0], 2)

    def test_pending_lines(self):
        state = self.make_state()
        log = factory.open_log(state)
        self.assertEqual(factory.pending_lines(None, "operator"), ["pending: none"])
        self.assertEqual(factory.pending_lines(state, "operator"), ["pending: none"])
        with patch.object(factory, "utc_now", return_value="2026-11-26T17:00:00Z"):
            log.add("pm", ["operator", "lead-dev"], "oldest")
            log.add("pm", ["operator"], "next")
        log.out(factory.Box(log.path), 1, "operator", "queued", why="blocked")
        now = datetime.datetime.fromisoformat("2026-11-26T17:03:00Z").timestamp()
        self.assertEqual(factory.pending_lines(state, "operator", now), [
            "pending: @operator 2 (oldest 3m, blocked); @lead-dev 1 (oldest 3m, waiting)",
            "read them with: factory.py inbox --for operator",
        ])
        self.assertEqual(len(factory.pending_lines(state, "reviewer", now)), 1)
        with log.path.open("ab") as stream:
            stream.write(b'{"t":"msg","id":3,"at":"2026-11-26T17:00:00Z","from":"pm","to":["operator"],'
                         b'"kind":"say","text":"no ok"}\n')
        self.assertIn("log: 1 uncommitted entry (never delivered)", factory.pending_lines(state, "operator", now))
        with log.path.open("ab") as stream:
            stream.write(b"torn")
        self.assertIn("message log needs repair", factory.pending_lines(state, "operator", now)[0])


class MemberPromptTests(unittest.TestCase):
    def setUp(self):
        self.env = self.enterContext(Env(UI))

    def test_member_prompt_has_label_based_say_and_names_the_outcomes(self):
        prompt = self.env.run("brief", "lead-dev").stdout
        self.assertIn(f"python3 {SCRIPT} say NAME --file", prompt)
        self.assertNotIn("say --cwd", prompt)
        for word in ("delivered", "sending", "queued", "unconfirmed", "dropped", "nothing sent", "not stored",
                     "not confirmed stored"):
            self.assertIn(f"`{word}`", prompt)
        self.assertIn("resend only when asked", prompt)


class LogTests(unittest.TestCase):
    """The v2 message log (message_log.md, phase_plans/phase_3.md §§2-4)."""

    def setUp(self):
        self.base = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.base / "team" / "core"
        self.log = factory.Log(self.state)

    def lines(self):
        return self.log.path.read_bytes().splitlines()

    def box(self):
        records, end = self.log.read()
        box = factory.Box(self.log.path)
        box.apply(records)
        box.offset = end
        return box

    def write_raw(self, *records):
        """Append records as they are, bypassing Log (to build histories, valid or damaged)."""
        if not self.log.path.exists():
            self.log.add("lead-dev", ["operator"], "seed")
        with self.log.path.open("ab") as f:
            for rec in records:
                f.write(rec if isinstance(rec, bytes) else (json.dumps(rec) + "\n").encode())

    def assert_damage(self, line, reason):
        before = self.log.path.read_bytes()
        with self.assertRaises(factory.LogDamage) as raised:
            self.log.read()
        self.assertEqual(raised.exception.line, line)
        self.assertIn(reason, raised.exception.reason)
        with self.assertRaises(factory.LogDamage):
            self.log.add("lead-dev", ["operator"], "after damage")
        self.assertEqual(self.log.path.read_bytes(), before)

    # ids, content, files

    def test_first_id_is_one_and_ids_increase_across_reopen(self):
        self.assertEqual(self.log.add("lead-dev", ["operator"], "one")[0], 1)
        self.assertEqual(factory.Log(self.state).add("reviewer", ["operator", "lead-dev"], "two")[0], 2)
        records, end = self.log.read()
        self.assertEqual([(r["t"], r["id"]) for r in records], [("msg", 1), ("ok", 1), ("msg", 2), ("ok", 2)])
        self.assertEqual(end, self.log.path.stat().st_size)

    def test_add_returns_offset_after_its_commit(self):
        ident, end, warning = self.log.add("lead-dev", ["operator"], "x")
        self.assertIsNone(warning)
        self.assertEqual(self.log.read(end), ([], end))
        self.log.add("pm", ["operator"], "y")
        self.assertEqual([r["t"] for r in self.log.read(end)[0]], ["msg", "ok"])

    def test_text_is_stored_byte_for_byte(self):
        text = 'line one\n\t"quoted" — ünïcode ✓\nends with @reviewer'
        self.log.add("lead-dev", ["operator"], text)
        self.assertEqual(self.box().msgs[1]["text"], text)
        self.log.add("lead-dev", ["operator"], text + "\n")
        self.assertEqual(self.box().msgs[2]["text"], text + "\n")

    def test_message_fields(self):
        self.log.add("factory", ["operator"], "quiet", kind="note", meta={"what": "quiet"})
        self.log.add("lead-dev", ["operator"], "file", file="/abs/messages/lead-dev/a-1.md")
        msgs = self.box().msgs
        self.assertRegex(msgs[1]["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual((msgs[1]["kind"], msgs[1]["meta"]), ("note", {"what": "quiet"}))
        self.assertEqual(msgs[2]["file"], "/abs/messages/lead-dev/a-1.md")

    def test_unserializable_or_nan_meta_touches_nothing(self):
        for meta in ({"x": float("nan")}, {"x": object()}):
            with self.subTest(meta=meta), self.assertRaises(ValueError):
                self.log.add("factory", ["operator"], "note", kind="note", meta=meta)
        self.assertFalse(self.state.exists())

    def test_offset_must_be_a_non_negative_integer(self):
        for offset in (-1, 1.5, True, "0"):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                self.log.read(offset)

    def test_invalid_arguments_raise_before_writing(self):
        for args, kwargs in (
            (("", ["operator"], "x"), {}),
            (("lead-dev", [], "x"), {}),
            (("lead-dev", ["operator", "operator"], "x"), {}),
            (("lead-dev", ["lead-dev"], "x"), {}),
            (("lead-dev", ["operator"], b"x"), {}),
            (("lead-dev", ["operator"], "x"), {"kind": "wait"}),
            (("lead-dev", ["operator"], "x"), {"file": "relative.md"}),
            (("lead-dev", ["operator"], "x"), {"meta": ["not", "object"]}),
        ):
            with self.subTest(args=args, kwargs=kwargs), self.assertRaises(ValueError):
                self.log.add(*args, **kwargs)
        self.assertFalse(self.state.exists())

    def test_uncommitted_entry_uses_up_its_id(self):
        self.write_raw({"t": "msg", "id": 2, "at": "2026-11-26T17:00:00Z", "from": "pm", "to": ["operator"],
                        "kind": "say", "text": "never committed"})
        box = self.box()
        self.assertNotIn(2, box.msgs)
        self.assertEqual((box.uncommitted, box.highest), (1, 2))
        self.assertEqual([m["id"] for m, _ in box.pending()], [1])
        self.assertEqual(self.log.add("pm", ["operator"], "next")[0], 3)
        self.assertEqual(self.box().uncommitted, 1)

    def test_files_are_private(self):
        self.log.add("lead-dev", ["operator"], "x")
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ["log.check", "log.jsonl"])   # no lock file
        for name in ("log.jsonl", "log.check"):
            with self.subTest(name=name):
                self.assertEqual(stat.S_IMODE((self.state / name).stat().st_mode), 0o600)

    def test_symlinked_log_is_refused(self):
        self.log.add("lead-dev", ["operator"], "x")
        outside = self.base / "outside.jsonl"
        self.log.path.rename(outside)
        self.log.path.symlink_to(outside)
        for attempt in (self.log.read, lambda: self.log.add("lead-dev", ["operator"], "y")):
            with self.subTest(attempt=attempt), self.assertRaises(OSError):
                attempt()
        self.assertEqual(len(outside.read_bytes().splitlines()), 2)

    def test_symlinked_state_directory_is_refused(self):
        real = self.base / "elsewhere"
        real.mkdir(mode=0o700)
        self.state.parent.mkdir(parents=True)
        self.state.symlink_to(real)
        with self.assertRaises(OSError):
            self.log.add("lead-dev", ["operator"], "x")
        self.assertEqual(list(real.iterdir()), [])

    def test_reading_creates_nothing(self):
        self.assertEqual(self.log.read(), ([], 0))
        self.assertFalse(self.state.exists())
        self.state.mkdir(parents=True)
        self.assertEqual(self.log.read(), ([], 0))
        self.assertEqual(list(self.state.iterdir()), [])

    def test_reader_does_not_chmod(self):
        self.log.add("lead-dev", ["operator"], "x")
        os.chmod(self.log.path, 0o640)
        self.log.read()
        self.assertEqual(stat.S_IMODE(self.log.path.stat().st_mode), 0o640)

    def test_checkpoint_without_log_is_damage(self):
        self.log.add("lead-dev", ["operator"], "x")
        self.log.path.unlink()
        for attempt in (self.log.read, lambda: self.log.add("lead-dev", ["operator"], "y")):
            with self.subTest(attempt=attempt), self.assertRaisesRegex(factory.LogDamage, "log.check remains"):
                attempt()

    def test_readers_refuse_a_symlinked_state_directory(self):
        self.log.add("lead-dev", ["operator"], "x")
        alias = self.base / "alias"
        alias.symlink_to(self.state)
        with self.assertRaisesRegex(OSError, "symbolic link"):
            factory.Log(alias).read()

    def test_started_must_be_a_bool_or_null(self):
        for started in (1, 0, 1.0):
            with self.subTest(started=started):
                self.setUp()
                self.write_raw({"t": "out", "id": 1, "to": "operator", "o": "sending", "term": "t1",
                                "at": "2026-11-26T17:00:01Z"},
                               {"t": "out", "id": 1, "to": "operator", "o": "delivered", "at": "2026-11-26T17:00:02Z",
                                "term": "t1", "turn": 1, "started": started})
                self.assert_damage(4, "started")

    def test_unsafe_checkpoint_is_an_error_not_a_cache_miss(self):
        self.log.add("lead-dev", ["operator"], "x")
        outside = self.base / "outside.check"
        outside.write_text("{}")
        self.log.check_path.unlink()
        self.log.check_path.symlink_to(outside)
        with self.assertRaises(OSError):
            self.log.add("lead-dev", ["operator"], "y")
        self.assertTrue(self.log.check_path.is_symlink())
        self.assertEqual(outside.read_text(), "{}")

    def test_orphan_checkpoint_is_damage_for_readers(self):
        self.log.add("lead-dev", ["operator"], "x")
        self.log.path.unlink()
        with self.assertRaisesRegex(factory.LogDamage, "log.check remains"):
            self.log.read()

    def test_a_damaged_batch_leaves_the_box_unusable_until_rebuilt(self):
        self.log.add("lead-dev", ["operator"], "x")
        box = self.box()
        sending = {"t": "out", "id": 1, "to": "operator", "o": "sending", "term": "t1", "at": "2026-11-26T17:00:01Z"}
        with self.assertRaises(factory.LogDamage):
            box.apply([sending, sending])
        for use in (lambda: box.outcome(1, "operator"), box.pending, lambda: box.apply([])):
            with self.subTest(use=use), self.assertRaises(factory.LogDamage):
                use()
        self.assertTrue(self.log.out(box, 1, "operator", "sending", term="t1"))   # out rebuilds it from the file
        self.assertEqual(box.outcome(1, "operator")["o"], "sending")

    def test_a_malformed_lineage_is_a_miss(self):
        self.log.add("pm", ["operator"], "one")
        raw = self.log.check_path.read_text()
        check = json.loads(raw)
        self.log.check_path.write_text(raw.replace(json.dumps(check["lineage"]), '"\\ud800"'))
        self.assertEqual(self.log.add("pm", ["operator"], "two")[0], 2)    # one full validation, then a new proof
        self.assertRegex(json.loads(self.log.check_path.read_text())["lineage"], r"^[0-9a-f]{16}$")

    def test_the_state_directory_is_the_lock(self):
        """Writers flock the state directory: another descriptor of it cannot take the lock meanwhile."""
        self.log.add("lead-dev", ["operator"], "x")
        other = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.log.locked(exclusive=True):
                with self.assertRaises(BlockingIOError):
                    factory.fcntl.flock(other, factory.fcntl.LOCK_EX | factory.fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    factory.fcntl.flock(other, factory.fcntl.LOCK_SH | factory.fcntl.LOCK_NB)
            factory.fcntl.flock(other, factory.fcntl.LOCK_SH | factory.fcntl.LOCK_NB)   # readers share
            with self.log.locked(exclusive=False):
                pass
            factory.fcntl.flock(other, factory.fcntl.LOCK_UN)
        finally:
            os.close(other)

    def test_a_held_lock_blocks_writers_with_or_without_a_checkpoint(self):
        self.log.add("lead-dev", ["operator"], "x")
        for checkpoint in (True, False):
            with self.subTest(checkpoint=checkpoint):
                if not checkpoint:
                    self.log.check_path.unlink(missing_ok=True)
                held = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY)
                factory.fcntl.flock(held, factory.fcntl.LOCK_EX)          # another writer, mid-operation
                before = self.log.path.read_bytes()
                results = []
                writer = threading.Thread(target=lambda: results.append(factory.Log(self.state).add("pm", ["x"], "y")))
                writer.start()
                writer.join(0.5)
                self.assertTrue(writer.is_alive())                       # blocked, not appending beside it
                self.assertEqual(self.log.path.read_bytes(), before)
                os.close(held)                                           # the other writer is done
                writer.join(10)
                self.assertEqual(len(results), 1)

    def test_concurrent_first_writers_without_a_checkpoint_all_commit(self):
        """Fresh store, checkpoints unavailable, writers racing from the start: every id once, nothing lost."""
        barrier, errors = threading.Barrier(4), []

        def writer(n):
            log = factory.Log(self.state)
            barrier.wait()
            try:
                for k in range(10):
                    log.add(f"w{n}", ["operator"], str(k))
            except Exception as error:    # collected, so a failure names itself
                errors.append(error)
        with patch.object(factory.Log, "_write_checkpoint", lambda log, last: None):
            threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
        self.assertEqual(errors, [])
        self.assertFalse(self.log.check_path.exists())
        records, _ = self.log.read()
        self.assertEqual([r["id"] for r in records if r["t"] == "msg"], list(range(1, 41)))

    def test_a_filesystem_that_cannot_lock_a_directory_is_named(self):
        for code in (errno.EBADF, errno.ENOLCK, errno.EIO):
            with self.subTest(code=code), \
                    patch.object(factory.fcntl, "flock", side_effect=OSError(code, os.strerror(code))), \
                    self.assertRaisesRegex(OSError, "cannot lock the state directory.*XDG_CACHE_HOME") as raised:
                self.log.add("pm", ["operator"], "x")
            self.assertEqual(raised.exception.errno, code)
            self.assertEqual(raised.exception.__cause__.errno, code)

    def test_a_new_store_is_durable_before_its_first_commit(self):
        """The directory chain is synced by the writer that creates the log, whoever made the directories;
        if that fails nothing is stored, and a retry commits id 1 normally."""
        self.state.mkdir(parents=True, mode=0o700)            # made by a writer that died right after mkdir
        real_fsync, synced = os.fsync, []

        def failing(fd):
            synced.append(os.fstat(fd).st_ino)
            if len(synced) == 1:
                raise OSError(5, "Input/output error")
            return real_fsync(fd)
        with patch.object(factory.os, "fsync", failing), self.assertRaisesRegex(factory.NotStored, "durable"):
            self.log.add("pm", ["operator"], "x")
        self.assertFalse(self.log.path.exists())
        self.assertEqual(synced[0], os.stat(self.state.parent).st_ino)       # the parent's entry first
        synced.clear()
        with patch.object(factory.os, "fsync", lambda fd: (synced.append(os.fstat(fd).st_ino), real_fsync(fd))[1]):
            self.assertEqual(self.log.add("pm", ["operator"], "x"), (1, self.log.path.stat().st_size, None))
        self.assertIn(os.stat(self.state.parent).st_ino, synced)
        self.assertIn(os.stat(self.base).st_ino, synced)

    def test_components_below_the_cache_base_are_never_followed(self):
        """The owner's cache base may be a symbolic link; factory-skill/<team>/<core> below it never are."""
        real_base = self.base / "real-cache"
        real_base.mkdir()
        (self.base / "cache").symlink_to(real_base)
        state = self.base / "cache" / "factory-skill" / "team" / "core"
        log = factory.Log(state, base=self.base / "cache")
        self.assertEqual(log.add("pm", ["operator"], "x")[0], 1)
        self.assertTrue((real_base / "factory-skill" / "team" / "core" / "log.jsonl").exists())
        for name, link in (("factory-skill", self.base / "cache" / "factory-skill"),
                           ("team", self.base / "cache" / "factory-skill" / "team")):
            with self.subTest(component=name):
                elsewhere = self.base / f"elsewhere-{name}"
                shutil.move(str(link), elsewhere)
                link.symlink_to(elsewhere)
                before = sorted(str(p) for p in elsewhere.rglob("*"))
                for attempt in (lambda: log.add("pm", ["operator"], "y"), log.read):
                    with self.assertRaisesRegex(OSError, "symbolic link"):
                        attempt()
                self.assertEqual(sorted(str(p) for p in elsewhere.rglob("*")), before)
                link.unlink()
                shutil.move(str(elsewhere), link)
        with self.assertRaises(ValueError):
            factory.Log(self.base / "other" / "core", base=self.base / "cache")

    def test_a_state_directory_moved_while_locking_is_damage(self):
        self.log.add("pm", ["operator"], "x")
        moved = self.base / "moved"
        real_flock = factory.fcntl.flock

        def move_then_lock(fd, op):
            if op == factory.fcntl.LOCK_EX and not moved.exists():
                self.state.rename(moved)                  # renamed with its log, a new one in its place
                self.state.mkdir(mode=0o700)
            return real_flock(fd, op)
        with patch.object(factory.fcntl, "flock", move_then_lock), \
                self.assertRaisesRegex(factory.LogDamage, "moved or replaced"):
            self.log.add("pm", ["operator"], "y")
        self.assertEqual(len((moved / "log.jsonl").read_bytes().splitlines()), 2)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_a_state_directory_moved_during_the_write_is_reported(self):
        self.log.add("pm", ["operator"], "x")
        moved = self.base / "moved"
        real = factory.Log._append

        def move_after_msg(log, line):
            result = real(log, line)
            if b'"t":"msg"' in line and not moved.exists():
                self.state.rename(moved)
                self.state.mkdir(mode=0o700)
            return result
        with patch.object(factory.Log, "_append", move_after_msg), \
                self.assertRaisesRegex(OSError, "moved during the write; message 2 is in the moved directory"):
            self.log.add("pm", ["operator"], "y")
        committed = [json.loads(line)["id"] for line in (moved / "log.jsonl").read_bytes().splitlines()
                     if json.loads(line)["t"] == "ok"]
        self.assertEqual(committed, [1, 2])                         # committed, in the moved directory

    def test_an_edited_checkpoint_is_a_miss_not_a_wrong_id(self):
        self.log.add("pm", ["operator"], "one")
        self.log.add("pm", ["operator"], "two")
        check = json.loads(self.log.check_path.read_text())
        check["last"] = 1                                 # valid JSON, consistent stat fields, a wrong id
        self.log.check_path.write_text(json.dumps(check))
        self.assertEqual(self.log.add("pm", ["operator"], "three")[0], 3)
        self.assertEqual(len(self.log.read()[0]), 6)

    def test_receipts_need_their_fields(self):
        msg = {"t": "out", "id": 1, "to": "operator", "at": "2026-11-26T17:00:01Z"}
        for history, reason in (
            ([{**msg, "o": "sending"}], "terminal ID of sending"),
            ([{**msg, "o": "sending", "term": "t1"}, {**msg, "o": "delivered", "started": True, "turn": 1}],
             "terminal ID of delivered"),
            ([{**msg, "o": "sending", "term": "t1"}, {**msg, "o": "delivered", "term": "t1", "turn": 1}],
             "started and turn"),
        ):
            with self.subTest(reason=reason):
                self.setUp()
                self.write_raw(*history)
                self.assert_damage(2 + len(history), reason)
        self.setUp()
        self.write_raw({**msg, "o": "sending", "term": "t1"},
                       {**msg, "o": "delivered", "term": "t1", "started": None, "turn": 0})   # null started is fine
        self.assertEqual(self.box().outcome(1, "operator")["o"], "delivered")
        box = self.box()
        for o, extra in (("sending", {}), ("delivered", {"term": "t1"}), ("delivered", {"term": "t1", "turn": 1})):
            with self.subTest(o=o, extra=extra), self.assertRaises(ValueError):
                self.log.out(box, 1, "operator", o, **extra)

    # validation

    def test_incomplete_last_line_is_damage(self):
        self.write_raw(b'{"t":"ok","id":1')
        self.assert_damage(3, "incomplete line")

    def test_blank_line_is_damage(self):
        self.write_raw(b"\n")
        self.assert_damage(3, "blank line")

    def test_nonstandard_json_constants_are_damage(self):
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(constant=constant):
                self.setUp()
                self.write_raw(b'{"t":"msg","id":2,"at":"2026-11-26T17:00:00Z","from":"pm","to":["operator"],'
                               b'"kind":"note","text":"x","meta":{"x":' + constant + b'}}\n', b'{"t":"ok","id":2}\n')
                self.assert_damage(3, "not standard JSON")

    def test_invalid_json_and_utf8_are_damage(self):
        for raw, reason in ((b"{not json}\n", "not JSON"), (b'{"t":"ok","x":"\xff"}\n', "not JSON"),
                            (b"[1,2]\n", "expected a JSON object")):
            with self.subTest(raw=raw):
                self.setUp()
                self.write_raw(raw)
                self.assert_damage(3, reason)

    def test_record_field_damage(self):
        msg = {"t": "msg", "id": 2, "at": "2026-11-26T17:00:00Z", "from": "pm", "to": ["operator"],
               "kind": "say", "text": "x"}
        for field, value, reason in (
            ("t", "note", "expected t of"), ("id", True, "positive integer id"), ("id", 0, "positive integer id"),
            ("at", "yesterday", "UTC at"), ("at", "2026-99-99T99:99:99Z", "UTC at"),
            ("at", "2026-02-30T12:00:00Z", "UTC at"), ("at", "2026-11-26T24:00:00Z", "UTC at"),
            ("from", "", "sender"), ("to", [], "distinct recipients"),
            ("to", ["pm"], "distinct recipients"), ("to", ["a", "a"], "distinct recipients"),
            ("kind", "wait", "kind say"), ("text", None, "expected text"), ("file", "rel.md", "absolute file"),
            ("meta", 3, "meta to be an object"),
        ):
            with self.subTest(field=field, value=value):
                self.setUp()
                self.write_raw({**msg, field: value})
                self.assert_damage(3, reason)

    def test_outcome_field_damage(self):
        out = {"t": "out", "id": 1, "to": "operator", "o": "queued", "why": "blocked", "at": "2026-11-26T17:00:01Z"}
        for changes, reason in (
            ({"o": "lost"}, "known outcome"), ({"why": "tired"}, "queue reason"), ({"to": ""}, "a recipient"),
            ({"o": "dropped", "why": " "}, "reason for dropped"), ({"o": "sending", "term": 7}, "terminal ID"),
            ({"o": "sending", "started": "yes"}, "started"), ({"o": "sending", "turn": True}, "turn"),
            ({"at": None}, "UTC at"), ({"at": "2026-13-01T00:00:00Z"}, "UTC at"),
        ):
            with self.subTest(changes=changes):
                self.setUp()
                self.write_raw({**out, **changes})
                self.assert_damage(3, reason)

    def test_ids_out_of_order_is_damage(self):
        self.write_raw({"t": "msg", "id": 1, "at": "2026-11-26T17:00:00Z", "from": "pm", "to": ["operator"],
                        "kind": "say", "text": "again"})
        self.assert_damage(3, "not above 1")

    def test_ok_must_follow_its_msg(self):
        for extra, line in (([{"t": "ok", "id": 1}], 3), ([{"t": "ok", "id": 7}], 3)):
            with self.subTest(extra=extra):
                self.setUp()
                self.write_raw(*extra)
                self.assert_damage(line, "does not follow its message")

    def test_outcome_references_are_checked(self):
        uncommitted = {"t": "msg", "id": 2, "at": "2026-11-26T17:00:00Z", "from": "pm", "to": ["operator"],
                       "kind": "say", "text": "x"}
        cases = (
            ([{"t": "out", "id": 9, "to": "operator", "o": "sending", "term": "t1", "at": "2026-11-26T17:00:01Z"}], 3,
             "unknown message"),
            ([{"t": "out", "id": 1, "to": "pm", "o": "sending", "term": "t1", "at": "2026-11-26T17:00:01Z"}], 3,
             "non-recipient"),
            ([uncommitted, {"t": "out", "id": 2, "to": "operator", "o": "sending", "term": "t1",
                            "at": "2026-11-26T17:00:01Z"}],
             4, "unknown message"),
        )
        for extra, line, reason in cases:
            with self.subTest(reason=reason, line=line):
                self.setUp()
                self.write_raw(*extra)
                self.assert_damage(line, reason)

    def test_damaged_prefix_blocks_a_valid_later_row(self):
        self.log.add("lead-dev", ["operator"], "seed")
        self.log.add("pm", ["operator"], "second")
        data = bytearray(self.log.path.read_bytes())
        data[data.index(b'"seed"') + 1:data.index(b'"seed"') + 2] = b"\xff"
        self.log.path.write_bytes(bytes(data))
        self.log.path.touch()
        with open(self.log.path, "ab") as f:
            f.write(b'{"t":"out","id":2,"to":"operator","o":"sending","at":"2026-11-26T17:00:01Z"}\n')
        self.assert_damage(1, "not JSON")

    def test_offsets_must_be_line_starts_inside_the_log(self):
        self.log.add("lead-dev", ["operator"], "x")
        size = self.log.path.stat().st_size
        with self.assertRaisesRegex(factory.LogDamage, "truncated"):
            self.log.read(size + 5)
        with self.assertRaisesRegex(factory.LogDamage, "not at the start of a line"):
            self.log.read(3)
        first = len(self.lines()[0]) + 1
        self.assertEqual(self.log.read(first)[0], [{"t": "ok", "id": 1}])

    def test_tail_damage_names_the_absolute_line(self):
        _, end, _ = self.log.add("lead-dev", ["operator"], "x")
        self.write_raw(b"garbage\n")
        with self.assertRaises(factory.LogDamage) as raised:
            self.log.read(end)
        self.assertEqual(raised.exception.line, 3)

    # replay

    def test_incremental_refresh_equals_full_replay(self):
        box = self.box()
        for n in range(3):
            self.log.add("pm", ["operator", "reviewer"], f"m{n}")
            records, end = self.log.read(box.offset)
            box.apply(records)
            box.offset = end
        full = self.box()
        self.assertEqual((box.msgs, box.highest, box.offset), (full.msgs, full.highest, full.offset))

    def test_message_waiting_for_its_commit_spans_batches(self):
        msg = {"t": "msg", "id": 2, "at": "2026-11-26T17:00:00Z", "from": "pm", "to": ["operator"],
               "kind": "say", "text": "x"}
        box = self.box()
        box.apply([msg])
        self.assertEqual((box.uncommitted, 2 in box.msgs), (1, False))
        box.apply([{"t": "ok", "id": 2}])
        self.assertEqual((box.uncommitted, 2 in box.msgs), (0, True))

    def test_default_outcome_and_pending(self):
        self.log.add("pm", ["operator", "reviewer"], "x")
        box = self.box()
        self.assertEqual(box.outcome(1, "operator"), {"o": "queued", "why": "waiting"})
        self.assertEqual([(m["id"], r) for m, r in box.pending()], [(1, "operator"), (1, "reviewer")])
        self.assertEqual([(m["id"], r) for m, r in box.pending("reviewer")], [(1, "reviewer")])


LOG_CHILD = r'''
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("factory", sys.argv[1])
factory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(factory)
log, mode = factory.Log(sys.argv[2]), sys.argv[3]
if mode == "adds":
    for n in range(int(sys.argv[4])):
        log.add("w%s" % os.getpid(), ["operator"], "message %d" % n)
    sys.exit(0)
pause_after, real, calls = int(sys.argv[4]), factory.Log._append, [0]
def paused(self, line):
    result = real(self, line)
    calls[0] += 1
    if calls[0] == pause_after:
        os.write(1, b"paused\n")
        sys.stdin.read()          # the parent kills us here, holding the team lock
    return result
factory.Log._append = paused
log.add("lead-dev", ["operator"], "killed writer")
'''


class LogFaultTests(unittest.TestCase):
    """Append, commit and checkpoint under injected faults (phase_3.md §§3-4, §8 step 2)."""

    def setUp(self):
        self.base = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.base / "team" / "core"
        self.log = factory.Log(self.state)
        self.log.add("lead-dev", ["operator"], "seed")   # a valid checkpoint, so later adds read no log bytes
        self.fsyncs = 0

    def box(self):
        records, end = self.log.read()
        box = factory.Box(self.log.path)
        box.apply(records)
        box.offset = end
        return box

    def faults(self, writes=(), fsync_fail=(), truncate_fails=False, readback_fails=False, dirsync_fail=()):
        """Patch the module's os calls for the log only. writes: per os.write call, an int (bytes to write),
        "zero" or "fail"; later calls write fully. fsync_fail: which fsync calls (1-based) raise."""
        real_write, real_fsync, real_truncate, real_open = os.write, os.fsync, os.ftruncate, factory.private_open
        plan = list(writes)
        stack = contextlib.ExitStack()

        self.failed = False

        def write(fd, data):
            step = plan.pop(0) if plan else None
            if step == "fail":
                self.failed = True
                raise OSError(28, "No space left on device")
            if step == "zero":
                return 0
            return real_write(fd, bytes(data)[:step] if isinstance(step, int) else data)

        def fsync(fd):
            self.fsyncs += 1
            if self.fsyncs in fsync_fail:
                self.failed = True
                raise OSError(5, "Input/output error")
            return real_fsync(fd)

        def ftruncate(fd, size):
            if truncate_fails:
                raise OSError(5, "Input/output error")
            return real_truncate(fd, size)

        def private_open(path, flags, fix_mode=True, dir_fd=None):
            if readback_fails and self.failed and path == "log.jsonl" and flags == os.O_RDONLY:
                raise OSError(5, "Input/output error")
            return real_open(path, flags, fix_mode, dir_fd)
        stack.enter_context(patch.object(factory.os, "write", write))
        stack.enter_context(patch.object(factory.os, "fsync", fsync))
        stack.enter_context(patch.object(factory.os, "ftruncate", ftruncate))
        stack.enter_context(patch.object(factory, "private_open", private_open))
        dirsyncs = []

        def sync_directory(log):
            dirsyncs.append(1)
            if len(dirsyncs) in dirsync_fail:
                self.failed = True
                raise OSError(5, "Input/output error")
        stack.enter_context(patch.object(factory.Log, "_sync_directory", sync_directory))
        return stack

    def committed(self):
        box = self.box()
        return sorted(box.msgs), box.uncommitted

    # the commit matrix

    def test_failed_open_is_not_stored_clean(self):
        before = self.log.path.read_bytes()
        real_open = factory.private_open

        def refuse(path, flags, fix_mode=True, dir_fd=None):
            if path == "log.jsonl" and flags & os.O_WRONLY:
                raise PermissionError(13, "Permission denied")
            return real_open(path, flags, fix_mode, dir_fd)
        with patch.object(factory, "private_open", refuse), self.assertRaises(factory.NotStored) as raised:
            self.log.add("pm", ["operator"], "x")
        self.assertFalse(raised.exception.partial)
        self.assertEqual(self.log.path.read_bytes(), before)

    def test_short_writes_complete_the_line(self):
        with self.faults(writes=(3, 5, 1, 7)):
            self.log.add("pm", ["operator"], "written in pieces")
        self.assertEqual(self.committed(), ([1, 2], 0))

    def test_no_progress_or_a_failed_write_rolls_back(self):
        for writes in ((4, "zero"), (4, 2, "fail"), ("fail",)):
            with self.subTest(writes=writes):
                before = self.log.path.read_bytes()
                with self.faults(writes=writes), self.assertRaises(factory.NotStored) as raised:
                    self.log.add("pm", ["operator"], "x")
                self.assertFalse(raised.exception.partial)
                self.assertEqual(self.log.path.read_bytes(), before)
        self.assertEqual(self.log.add("pm", ["operator"], "next")[0], 2)

    def test_failed_sync_of_a_complete_line_is_rolled_back(self):
        before = self.log.path.read_bytes()
        with self.faults(fsync_fail=(1,)), self.assertRaises(factory.NotStored) as raised:
            self.log.add("pm", ["operator"], "x")
        self.assertFalse(raised.exception.partial)
        self.assertEqual(self.log.path.read_bytes(), before)

    def test_directory_sync_failure(self):
        before = self.log.path.read_bytes()
        with self.faults(dirsync_fail=(1,)), self.assertRaises(factory.NotStored) as raised:
            self.log.add("pm", ["operator"], "msg directory sync fails")
        self.assertFalse(raised.exception.partial)
        self.assertEqual(self.log.path.read_bytes(), before)
        with self.faults(dirsync_fail=(2,), truncate_fails=True):
            ident, _, warning = self.log.add("pm", ["operator"], "ok directory sync fails")
        self.assertEqual((ident, warning), (2, factory.STORED_WARNING))

    def test_truncate_succeeds_but_its_sync_fails(self):
        for failing in ((1, 2), (2, 3)):   # the msg append, then the ok append
            with self.subTest(failing=failing):
                self.setUp()
                before = self.log.path.read_bytes()
                with self.faults(fsync_fail=failing), self.assertRaises(factory.NotStored) as raised:
                    self.log.add("pm", ["operator"], "x")
                self.assertFalse(raised.exception.partial)
                if failing == (1, 2):
                    self.assertEqual(self.log.path.read_bytes(), before)
                else:
                    self.assertEqual(self.committed(), ([1], 1))   # the msg stays, inert

    def test_msg_sync_and_rollback_both_fail(self):
        with self.faults(fsync_fail=(1,), truncate_fails=True), self.assertRaises(factory.NotStored) as raised:
            self.log.add("pm", ["operator"], "x")
        self.assertFalse(raised.exception.partial)
        self.assertIn("uncommitted", raised.exception.detail)
        self.assertEqual(self.log.path.read_bytes().splitlines()[-1][:22], b'{"t":"msg","id":2,"at"')
        box = self.box()
        self.assertEqual((sorted(box.msgs), box.uncommitted, [m["id"] for m, _ in box.pending()]), ([1], 1, [1]))
        self.assertEqual(self.log.add("pm", ["operator"], "next")[0], 3)

    def test_ok_sync_and_rollback_both_fail_is_stored_with_a_warning(self):
        with self.faults(fsync_fail=(2,), truncate_fails=True):
            ident, end, warning = self.log.add("pm", ["operator"], "x")
        self.assertEqual((ident, warning), (2, factory.STORED_WARNING))
        self.assertEqual(end, self.log.path.stat().st_size)
        self.assertEqual([(m["id"], r) for m, r in self.box().pending()], [(1, "operator"), (2, "operator")])

    def test_partial_write_and_failed_rollback_is_damage(self):
        with self.faults(writes=(10, "fail"), truncate_fails=True), self.assertRaises(factory.NotStored) as raised:
            self.log.add("pm", ["operator"], "x")
        self.assertTrue(raised.exception.partial)
        with self.assertRaisesRegex(factory.LogDamage, "incomplete line"):
            self.log.read()
        with self.assertRaises(factory.LogDamage):
            self.log.add("pm", ["operator"], "refused on a damaged log")

    def test_partial_ok_and_failed_rollback_is_damage(self):
        with self.faults(writes=("full", 4, "fail"), truncate_fails=True), \
                self.assertRaises(factory.NotStored) as raised:
            self.log.add("pm", ["operator"], "x")
        self.assertTrue(raised.exception.partial)
        with self.assertRaisesRegex(factory.LogDamage, "incomplete line"):
            self.log.read()

    def test_unreadable_msg_readback_is_not_stored_partial(self):
        with self.faults(fsync_fail=(1,), truncate_fails=True, readback_fails=True), \
                self.assertRaises(factory.NotStored) as raised:
            self.log.add("pm", ["operator"], "x")
        self.assertTrue(raised.exception.partial)
        self.assertEqual(self.committed(), ([1], 1))

    def test_unreadable_ok_readback_reports_unknown_and_recovers_once(self):
        check = self.log.check_path.read_bytes()
        with self.faults(fsync_fail=(2,), truncate_fails=True, readback_fails=True), \
                self.assertRaises(OSError) as raised:
            self.log.add("pm", ["operator"], "x")
        self.assertNotIsInstance(raised.exception, factory.NotStored)
        self.assertIsInstance(raised.exception.__cause__, OSError)
        message = str(raised.exception)
        self.assertIn("message 2", message)
        self.assertIn(str(self.log.path), message)
        self.assertIn(factory.UNKNOWN, message)
        self.assertEqual(self.log.check_path.read_bytes(), check)
        records, _ = self.log.read()
        self.assertEqual([(r["t"], r["id"]) for r in records], [("msg", 1), ("ok", 1), ("msg", 2), ("ok", 2)])
        self.assertEqual(self.committed(), ([1, 2], 0))
        self.assertEqual(self.log.add("pm", ["operator"], "next")[0], 3)

    def test_failed_checkpoint_publication_keeps_the_commit(self):
        denied = OSError(13, "Permission denied")
        with patch.object(factory.os, "replace", side_effect=denied), \
                patch.object(factory.os, "unlink", side_effect=denied):   # its cleanup fails too
            ident, _, warning = self.log.add("pm", ["operator"], "x")
        self.assertEqual((ident, warning), (2, None))
        self.assertEqual(self.committed(), ([1, 2], 0))
        leftover = list(self.state.glob("log.check.*.tmp"))
        self.assertEqual(len(leftover), 1)
        with patch.object(factory.Log, "_snapshot", autospec=True,
                          side_effect=factory.Log._snapshot) as snapshot:
            self.assertEqual(factory.Log(self.state).add("pm", ["operator"], "y")[0], 3)
        self.assertTrue(any(c.args[1:] == (0,) for c in snapshot.call_args_list))   # the stale proof cost a read
        check = json.loads(self.log.check_path.read_text())            # unique temp names: publishing works again
        self.assertEqual((check["size"], check["last"]), (self.log.path.stat().st_size, 3))
        leftover[0].unlink()

    # outcomes under faults

    def test_sending_needs_a_synced_record_but_receipts_do_not(self):
        box = self.box()
        self.log.add("pm", ["operator"], "second")
        with self.faults(fsync_fail=(1,), truncate_fails=True), self.assertRaises(factory.NotStored) as raised:
            self.log.out(box, 1, "operator", "sending", term="t1")
        self.assertIn("not durable", raised.exception.detail)
        offset = box.offset
        self.assertEqual(self.box().outcome(1, "operator")["o"], "sending")   # the record is there
        self.assertFalse(self.log.out(box, 1, "operator", "sending", term="t1"))   # the next refresh sees it
        self.assertGreater(box.offset, offset)
        self.assertTrue(self.log.out(box, 2, "operator", "sending", term="t1"))
        with self.faults(fsync_fail=(1,), truncate_fails=True):
            self.assertTrue(self.log.out(box, 2, "operator", "delivered", term="t1", started=True, turn=3))
        self.assertEqual(self.box().outcome(2, "operator")["o"], "delivered")

    def test_unreadable_outcome_readback_raises(self):
        box = self.box()
        with self.faults(fsync_fail=(1,), truncate_fails=True, readback_fails=True), \
                self.assertRaisesRegex(OSError, "outcome sending for @operator of message 1.*do not resend"):
            self.log.out(box, 1, "operator", "sending", term="t1")

    # the checkpoint

    def test_matching_checkpoint_reads_no_log_bytes(self):
        with patch.object(factory.Log, "_snapshot", side_effect=AssertionError("full read")):
            self.assertEqual(self.log.add("pm", ["operator"], "x")[0], 2)

    def test_stale_or_broken_checkpoint_costs_one_full_validation(self):
        def stale_size():
            with self.log.path.open("ab") as f:
                f.write(b'{"t":"out","id":1,"to":"operator","o":"queued","why":"blocked","at":"2026-11-26T17:00:01Z"}\n')

        def stale_mtime():
            info = self.log.path.stat()
            os.utime(self.log.path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))

        def new_inode():
            copy = self.state / "copy.jsonl"
            shutil.copyfile(self.log.path, copy)
            os.chmod(copy, 0o600)
            os.utime(copy, ns=(self.log.path.stat().st_atime_ns, self.log.path.stat().st_mtime_ns))
            copy.replace(self.log.path)
        for name, change in (("missing", lambda: self.log.check_path.unlink()),
                             ("garbage", lambda: self.log.check_path.write_text("{not json")),
                             ("negative", lambda: self.log.check_path.write_text(
                                 json.dumps({"ino": 1, "size": -1, "mtime_ns": 0, "last": 1}))),
                             ("size", stale_size), ("mtime", stale_mtime), ("inode", new_inode)):
            with self.subTest(name=name):
                change()
                with patch.object(factory.Log, "_snapshot", autospec=True,
                                  side_effect=factory.Log._snapshot) as snapshot:
                    self.log.add("pm", ["operator"], name)
                self.assertTrue(any(c.args[1:] == (0,) for c in snapshot.call_args_list))
                check = json.loads(self.log.check_path.read_text())
                info = self.log.path.stat()
                self.assertEqual((check["ino"], check["size"], check["mtime_ns"]),
                                 (info.st_ino, info.st_size, info.st_mtime_ns))

    def test_one_byte_edit_early_in_a_large_log_blocks_appends(self):
        line = {"t": "msg", "id": 0, "at": "2026-11-26T17:00:00Z", "from": "pm", "to": ["operator"],
                "kind": "say", "text": "x" * 400}
        with self.log.path.open("ab") as f:
            for ident in range(2, 6002):
                f.write((json.dumps({**line, "id": ident}) + "\n").encode())
                f.write(b'{"t":"ok","id":%d}\n' % ident)
        self.log.add("pm", ["operator"], "proves the big log")   # a full validation, then a checkpoint
        self.assertGreater(self.log.path.stat().st_size, 2 * 1024 * 1024)
        info = self.log.path.stat()
        with open(self.log.path, "r+b") as f:
            first, second = self.log.path.read_bytes().splitlines(keepends=True)[:2]
            f.seek(len(first) + len(second) + 5)
            f.write(b"\xff")
        os.utime(self.log.path, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
        before = self.log.path.read_bytes()
        with self.assertRaises(factory.LogDamage) as raised:
            self.log.add("pm", ["operator"], "refused")
        self.assertEqual(raised.exception.line, 3)
        self.assertEqual(self.log.path.read_bytes(), before)

    def test_a_stale_box_is_rebuilt_after_an_in_place_edit(self):
        """A matching checkpoint written after someone else's full validation proves nothing about an older view."""
        self.log.add("pm", ["alice", "other"], "x")
        a = self.box()
        self.assertTrue(self.log.out(a, 2, "alice", "sending", term="t1"))
        data = self.log.path.read_bytes()
        edited = data.replace(b'"to":"alice","o":"sending"', b'"to":"other","o":"sending"')
        self.assertEqual(len(edited), len(data))
        info = self.log.path.stat()
        with open(self.log.path, "r+b") as f:     # same inode, same size
            f.write(edited)
        os.utime(self.log.path, ns=(info.st_atime_ns, info.st_mtime_ns + 1000))
        factory.Log(self.state).add("pm", ["operator"], "b validates and publishes a checkpoint")
        rebuilt = self.log.out(a, 2, "alice", "delivered", term="t1", started=True, turn=1)
        self.assertFalse(rebuilt)                                  # rebuilt from the file: alice is queued
        box = self.box()
        self.assertEqual((box.outcome(2, "alice")["o"], box.outcome(2, "other")["o"]), ("queued", "sending"))

    def test_one_log_shared_by_threads(self):
        """The open state directory belongs to each operation, not to the shared Log."""
        self.log.add("pm", ["operator"], "second")
        results, entered = [], threading.Event()
        with self.log.locked(exclusive=True):
            reader = threading.Thread(target=lambda: (entered.set(), results.append(self.log.read())))
            reader.start()
            entered.wait(5)
            time.sleep(0.2)                    # the reader is now blocked on the shared lock
        reader.join(10)
        self.assertEqual(len(results[0][0]), 4)
        self.assertEqual(self.log.add("pm", ["operator"], "third")[0], 3)
        self.assertEqual(list(pathlib.Path.cwd().glob("log.jsonl")), [])

    # processes

    def child(self, *args):
        return subprocess.Popen([sys.executable, "-c", LOG_CHILD, str(SCRIPT), str(self.state), *args],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def test_concurrent_writers_get_distinct_ids(self):
        children = [self.child("adds", "25") for _ in range(8)]
        for child in children:
            child.communicate(timeout=120)
            self.assertEqual(child.returncode, 0)
        records, _ = self.log.read()
        msgs = [r["id"] for r in records if r["t"] == "msg"]
        self.assertEqual(msgs, list(range(1, 202)))
        for a, b in zip(records[::2], records[1::2]):
            self.assertEqual((a["t"], b["t"], a["id"]), ("msg", "ok", b["id"]))

    def killed(self, pause_after):
        child = self.child("pause", str(pause_after))
        self.assertEqual(child.stdout.readline(), b"paused\n")
        child.kill()
        child.wait(timeout=10)
        child.stdout.close()
        child.stdin.close()

    def test_writer_killed_between_msg_and_ok(self):
        self.killed(1)
        self.assertEqual(self.committed(), ([1], 1))
        self.assertEqual(factory.Log(self.state).add("pm", ["operator"], "after")[0], 3)

    def test_writer_killed_after_its_ok(self):
        self.killed(2)
        self.assertEqual(self.committed(), ([1, 2], 0))
        self.assertEqual(self.log.add("pm", ["operator"], "after")[0], 3)


class LogOutcomeTests(unittest.TestCase):
    """Outcomes, derived facts and message files (phase_3.md §§5-6, §8 step 3)."""

    def setUp(self):
        self.base = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.base / "team" / "core"
        self.log = factory.Log(self.state)

    def box(self):
        records, end = self.log.read()
        box = factory.Box(self.log.path)
        box.apply(records)
        box.offset = end
        return box

    def raw(self, *records):
        with self.log.path.open("ab") as f:
            for rec in records:
                f.write((json.dumps({"at": "2026-11-26T17:00:01Z", **rec}) + "\n").encode())

    # transitions

    def test_stale_boxes_cannot_repeat_a_claim_or_finalize_twice(self):
        self.log.add("pm", ["operator"], "x")
        one, two = self.box(), self.box()
        self.assertTrue(self.log.out(one, 1, "operator", "sending", term="t1"))
        self.assertFalse(self.log.out(two, 1, "operator", "sending", term="t1"))   # refreshed: already claimed
        self.assertEqual(two.outcome(1, "operator")["o"], "sending")
        self.assertTrue(self.log.out(two, 1, "operator", "delivered", term="t1", started=True, turn=2))
        self.assertFalse(self.log.out(one, 1, "operator", "unconfirmed", why="keeper restarted during the send"))
        self.assertEqual(self.box().outcome(1, "operator")["o"], "delivered")

    def test_transition_table(self):
        cases = (
            (("queued", {"why": "blocked"}), ("queued", {"why": "gone"}), True),
            (("queued", {"why": "blocked"}), ("queued", {"why": "blocked"}), False),
            (("sending", {"term": "t1"}), ("queued", {"why": "blocked"}), True),
            (("sending", {"term": "t1"}), ("dropped", {"why": "stopped by down"}), True),
            (("queued", {"why": "gone"}), ("dropped", {"why": "stopped by down"}), True),
            (("queued", {"why": "gone"}), ("delivered", {"term": "t1", "started": None, "turn": 0}), False),
            (("queued", {"why": "gone"}), ("unconfirmed", {"why": "lost"}), False),
            (("sending", {"term": "t1"}), ("sending", {"term": "t1"}), False),
        )
        for first, then, written in cases:
            with self.subTest(first=first, then=then):
                self.setUp()
                self.log.add("pm", ["operator"], "x")
                box = self.box()
                self.assertTrue(self.log.out(box, 1, "operator", first[0], **first[1]))
                self.assertEqual(self.log.out(box, 1, "operator", then[0], **then[1]), written)
        self.setUp()
        self.log.add("pm", ["operator"], "x")
        box = self.box()
        self.log.out(box, 1, "operator", "dropped", why="stopped by down")
        for o, extra in (("queued", {"why": "gone"}), ("sending", {"term": "t1"}), ("dropped", {"why": "again"})):
            with self.subTest(after="dropped", o=o):
                self.assertFalse(self.log.out(box, 1, "operator", o, **extra))

    def test_invalid_outcome_calls_raise(self):
        self.log.add("pm", ["operator"], "x")
        box = self.box()
        for args, kwargs in (((9, "operator", "sending"), {}), ((1, "reviewer", "sending"), {}),
                             ((1, "operator", "lost"), {}), ((1, "operator", "sending"), {"why": "x"}),
                             ((1, "operator", "queued"), {"why": "busy"}), ((1, "operator", "queued"), {}),
                             ((1, "operator", "dropped"), {"why": " "}),
                             ((1, "operator", "delivered"), {"started": 1})):
            with self.subTest(args=args, kwargs=kwargs), self.assertRaises(ValueError):
                self.log.out(box, *args, **kwargs)
        partial = factory.Box.for_message(self.log.path, 1, ["operator"], box.offset)
        with self.assertRaisesRegex(ValueError, "partial"):
            self.log.out(partial, 1, "operator", "sending")

    def test_drop_queued_drops_only_queued_pairs_of_those_names(self):
        self.log.add("pm", ["a", "b"], "one")
        self.log.add("pm", ["a"], "two")
        box = self.box()
        self.log.out(box, 1, "a", "sending", term="t1")
        stale = self.box()
        self.log.out(box, 2, "a", "sending", term="t1")            # after the stale box was built
        self.assertEqual(self.log.drop_queued(stale, {"a"}), 0)    # both of @a's pairs are sending now
        self.assertEqual(self.log.drop_queued(stale, {"a", "b"}), 1)
        self.assertEqual(self.log.drop_queued(stale, {"a", "b"}), 0)
        fresh = self.box()
        self.assertEqual([fresh.outcome(1, "a")["o"], fresh.outcome(1, "b")["o"], fresh.outcome(2, "a")["o"]],
                         ["sending", "dropped", "sending"])

    def test_text_is_dropped_when_final_but_references_still_hold(self):
        self.log.add("pm", ["operator", "reviewer"], "text")
        box = self.box()
        for to in ("operator", "reviewer"):
            self.log.out(box, 1, to, "dropped", why="stopped by down")
        self.assertNotIn("text", box.msgs[1])
        self.assertEqual(box.msgs[1]["to"], ["operator", "reviewer"])
        self.assertFalse(self.log.out(box, 1, "operator", "sending", term="t1"))
        with self.assertRaises(ValueError):
            self.log.out(box, 1, "pm", "sending")
        self.assertEqual(self.log.read()[0][0]["text"], "text")    # the raw records keep it

    # replay of what is on disk

    def test_forbidden_recorded_transitions_are_damage(self):
        sending = {"t": "out", "id": 1, "to": "operator", "o": "sending", "term": "t1"}
        delivered = {"t": "out", "id": 1, "to": "operator", "o": "delivered", "term": "t1", "started": True, "turn": 1}
        cases = (
            ([delivered], 3), ([{"t": "out", "id": 1, "to": "operator", "o": "unconfirmed", "why": "x"}], 3),
            ([sending, sending], 4), ([sending, delivered, delivered], 5),
            ([sending, delivered, {"t": "out", "id": 1, "to": "operator", "o": "queued", "why": "gone"}], 5),
        )
        for history, line in cases:
            with self.subTest(history=history):
                self.setUp()
                self.log.add("pm", ["operator"], "x")
                self.raw(*history)
                before = self.log.path.read_bytes()
                with self.assertRaises(factory.LogDamage) as raised:
                    self.log.read()
                self.assertEqual(raised.exception.line, line)
                with self.assertRaises(factory.LogDamage):
                    self.log.add("pm", ["operator"], "y")
                self.assertEqual(self.log.path.read_bytes(), before)

    def test_a_repeated_queued_reason_replays_as_a_no_op(self):
        self.log.add("pm", ["operator"], "x")
        self.raw({"t": "out", "id": 1, "to": "operator", "o": "queued", "why": "waiting"},
                 {"t": "out", "id": 1, "to": "operator", "o": "queued", "why": "blocked"},
                 {"t": "out", "id": 1, "to": "operator", "o": "queued", "why": "blocked"})
        self.assertEqual(self.box().outcome(1, "operator")["why"], "blocked")

    def test_partial_box_follows_one_message(self):
        _, offset, _ = self.log.add("pm", ["operator", "reviewer"], "x")
        self.log.add("pm", ["operator"], "other")
        full = self.box()
        self.log.out(full, 2, "operator", "sending", term="t1")
        self.log.out(full, 1, "reviewer", "sending", term="t2")
        partial = factory.Box.for_message(self.log.path, 1, ["operator", "reviewer"], offset)
        records, end = self.log.read(offset)
        partial.apply(records)
        self.assertEqual([partial.outcome(1, r)["o"] for r in ("operator", "reviewer")], ["queued", "sending"])
        self.assertEqual(sorted(partial.msgs), [1])
        wrong = factory.Box.for_message(self.log.path, 1, ["operator"], offset)
        with self.assertRaisesRegex(factory.LogDamage, "non-recipient"):
            wrong.apply(records)

    # derived facts

    def test_last_to_and_last_from(self):
        self.log.add("pm", ["lead-dev"], "one")
        self.log.add("operator", ["lead-dev"], "two")
        self.log.add("operator", ["lead-dev"], "three")
        box = self.box()
        for ident in (1, 2):
            self.log.out(box, ident, "lead-dev", "sending", term="t1")
            self.log.out(box, ident, "lead-dev", "delivered", term="t1", started=True, turn=1)
        self.assertEqual(box.last_to("lead-dev")["id"], 2)            # 3 is not delivered
        self.assertEqual(box.last_to("lead-dev", sender="pm")["id"], 1)
        self.assertIsNone(box.last_to("reviewer"))
        self.assertEqual(box.last_from("operator")["id"], 3)
        self.raw({"t": "msg", "id": 4, "from": "operator", "to": ["lead-dev"], "kind": "say", "text": "uncommitted"})
        self.assertEqual(self.box().last_from("operator")["id"], 3)

    def test_briefed_counts_delivered_briefs_only(self):
        for to in ("a", "b", "c"):
            self.log.add("operator", [to], "brief", kind="brief")
        self.log.add("operator", ["d"], "not a brief")
        box = self.box()
        receipt = {"started": True, "turn": 1}
        for ident, to, term, final in ((1, "a", "t1", ("delivered", receipt)),
                                       (2, "b", "t2", ("unconfirmed", {"why": "x"})),
                                       (3, "c", "t3", ("dropped", {"why": "stopped by down"})),
                                       (4, "d", "t4", ("delivered", receipt))):
            self.log.out(box, ident, to, "sending", term=term)
            self.log.out(box, ident, to, final[0], term=term, **final[1])
        self.assertEqual(box.briefed(), {"t1"})

    def test_member_and_team_notes(self):
        def note(meta):
            return self.log.add("factory", ["operator"], "note", kind="note", meta=meta)[0]
        note({"what": "blocked:permission", "about": "a", "term": "t1", "since": 10})
        note({"what": "error", "about": "a", "term": "t1", "since": 20})
        note({"what": "exit", "about": "b", "term": "t2"})
        quiet_old = note({"what": "quiet", "turns": {"a": 1}})
        quiet_new = note({"what": "quiet", "turns": {"a": 2}})
        ready = note({"what": "ready", "digest": "abc"})
        box = self.box()
        self.assertEqual(box.last_note("t1")["what"], "error")
        self.assertEqual(box.last_note("t2"), {"what": "exit", "about": "b", "term": "t2"})
        self.assertIsNone(box.last_note("t9"))
        self.assertEqual(box.quiet_note()["id"], quiet_new)
        self.log.out(box, quiet_new, "operator", "dropped", why="work resumed")
        self.assertEqual(box.quiet_note()["id"], quiet_old)
        self.assertEqual(box.ready_note()["id"], ready)
        self.log.out(box, ready, "operator", "dropped", why="operator could not take it")
        self.assertIsNone(box.ready_note())

    # message files

    def message_file(self, sender, name="op-done-1.md", text="hello"):
        directory = self.state / "messages" / sender
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / name
        path.write_text(text)
        return path

    def deliver(self, box, ident, *recipients):
        for to in recipients:
            self.log.out(box, ident, to, "sending", term="t1")
            self.log.out(box, ident, to, "delivered", term="t1", started=True, turn=1)

    def test_managed_file_rules(self):
        self.log.add("pm", ["operator"], "seed")
        good = self.message_file("lead-dev")
        self.assertTrue(factory.managed_file(self.state, "lead-dev", good))
        outside = self.base / "op-done-1.md"
        outside.write_text("x")
        bad_name = self.message_file("lead-dev", "notes.final.md")
        for path, sender in ((outside, "lead-dev"), (good, "reviewer"), (bad_name, "lead-dev"),
                             (self.state / "messages" / "lead-dev" / ".." / "lead-dev" / good.name, "lead-dev"),
                             (pathlib.Path("messages/lead-dev") / good.name, "lead-dev"),
                             (self.state / "messages" / "lead-dev" / "missing-1.md", "lead-dev")):
            with self.subTest(path=path, sender=sender):
                self.assertFalse(factory.managed_file(self.state, sender, path))
        link = self.state / "messages" / "linked"
        link.symlink_to(self.state / "messages" / "lead-dev")
        self.assertFalse(factory.managed_file(self.state, "linked", link / good.name))

    def test_file_removed_only_when_every_recipient_has_it(self):
        path = self.message_file("lead-dev")
        self.log.add("lead-dev", ["operator", "reviewer"], "x", file=str(path))
        box = self.box()
        self.deliver(box, 1, "operator")
        self.assertIsNone(self.log.remove_delivered_file(box, 1))
        self.assertTrue(path.exists())
        self.deliver(box, 1, "reviewer")
        self.assertIsNone(self.log.remove_delivered_file(box, 1))
        self.assertFalse(path.exists())
        self.assertIsNone(self.log.remove_delivered_file(box, 1))   # already gone: no warning

    def test_unconfirmed_or_dropped_keeps_the_file(self):
        path = self.message_file("lead-dev")
        self.log.add("lead-dev", ["operator", "reviewer"], "x", file=str(path))
        box = self.box()
        self.deliver(box, 1, "operator")
        self.log.out(box, 1, "reviewer", "dropped", why="stopped by down")
        self.assertIsNone(self.log.remove_delivered_file(box, 1))
        self.assertTrue(path.exists())

    def test_a_newer_use_of_the_path_decides(self):
        path = self.message_file("lead-dev")
        self.log.add("lead-dev", ["operator"], "old", file=str(path))
        self.log.add("lead-dev", ["operator"], "new", file=str(path))
        box = self.box()
        self.deliver(box, 1, "operator")
        self.log.remove_delivered_file(box, 1)
        self.assertTrue(path.exists())
        self.deliver(box, 2, "operator")
        self.log.remove_delivered_file(box, 2)
        self.assertFalse(path.exists())

    def test_external_or_symlinked_paths_are_never_removed(self):
        outside = self.base / "op-done-1.md"
        outside.write_text("mine")
        self.log.add("lead-dev", ["operator"], "x", file=str(outside))
        real = self.base / "elsewhere"
        real.mkdir()
        (real / "op-done-2.md").write_text("theirs")
        (self.state / "messages").mkdir(mode=0o700)
        (self.state / "messages" / "pm").symlink_to(real)
        self.log.add("pm", ["operator"], "y", file=str(self.state / "messages" / "pm" / "op-done-2.md"))
        box = self.box()
        self.deliver(box, 1, "operator")
        self.deliver(box, 2, "operator")
        self.assertIsNone(self.log.remove_delivered_file(box, 1))
        self.assertIsNone(self.log.remove_delivered_file(box, 2))
        self.assertEqual(((self.base / "op-done-1.md").read_text(), (real / "op-done-2.md").read_text()),
                         ("mine", "theirs"))

    def test_unlink_failure_is_a_warning(self):
        path = self.message_file("lead-dev")
        self.log.add("lead-dev", ["operator"], "x", file=str(path))
        box = self.box()
        self.deliver(box, 1, "operator")
        with patch.object(factory.os, "unlink", side_effect=PermissionError(13, "Permission denied")):
            warning = self.log.remove_delivered_file(box, 1)
        self.assertIn(str(path), warning)
        self.assertEqual(box.outcome(1, "operator")["o"], "delivered")

    def test_spellings_of_one_path_are_one_file(self):
        """"." and repeated slashes name the same file everywhere; ".." never names a managed one."""
        path = self.message_file("dev", "text.md")
        dotted = str(self.state / "messages" / "dev") + "/./text.md"
        doubled = str(self.state / "messages" / "dev") + "//text.md"
        rooted = "/" + str(path)                                  # "//tmp/...": the same file on POSIX hosts
        for spelling in (dotted, doubled, rooted):
            self.assertTrue(factory.managed_file(self.state, "dev", spelling))
        self.assertEqual(factory.lexical_path(rooted), str(path))
        self.assertEqual(factory.Log("/" + str(self.state)).state, self.state)
        self.assertFalse(factory.managed_file(self.state, "dev",
                                              str(self.state / "messages" / "x" / ".." / "dev" / "text.md")))
        self.log.add("dev", ["operator"], "old", file=str(path))
        self.log.add("dev", ["operator"], "new", file=rooted)     # pending, under another spelling
        box = self.box()
        self.deliver(box, 1, "operator")
        self.assertIsNone(self.log.remove_delivered_file(box, 1))
        self.assertTrue(path.exists())                            # the newer pending use protects it
        rows, _ = self.log.leftovers()                            # and the cold-start sweep agrees
        self.assertEqual([(pathlib.Path(r["path"]).name, r["id"]) for r in rows], [("text.md", 2)])
        self.assertTrue(path.exists())
        self.deliver(box, 2, "operator")
        self.log.remove_delivered_file(box, 2)
        self.assertFalse(path.exists())

    def test_a_relative_state_path_still_matches_absolute_records(self):
        path = self.message_file("dev", "text.md")
        relative = factory.Log(os.path.relpath(self.state))
        relative.add("dev", ["operator"], "x", file=str(path))
        box = self.box()
        self.deliver(box, 1, "operator")
        self.assertIsNone(relative.remove_delivered_file(box, 1))
        self.assertFalse(path.exists())

    def test_partial_boxes_never_widen(self):
        _, offset, _ = self.log.add("pm", ["a"], "one")
        self.log.add("pm", ["a"], "two")
        partial = factory.Box.for_message(self.log.path, 1, ["a"], offset)
        for attempt in (lambda: self.log.drop_queued(partial, {"a"}),
                        lambda: self.log.remove_delivered_file(partial, 1),
                        lambda: self.log.out(partial, 1, "a", "sending")):
            with self.subTest(attempt=attempt), self.assertRaisesRegex(ValueError, "partial"):
                attempt()
        self.assertEqual((sorted(partial.msgs), partial._only), ([1], 1))
        self.assertEqual([m["id"] for m, _ in self.box().pending()], [1, 2])
        with self.assertRaisesRegex(ValueError, "reason"):
            self.log.drop_queued(self.box(), {"a"}, why=" ")

    def test_drop_queued_failing_partway_keeps_earlier_drops(self):
        for n in range(3):
            self.log.add("pm", ["a"], str(n))
        box = self.box()
        real, calls = factory.Log._append, []

        def second_fails(log, line):
            calls.append(line)
            if len(calls) == 2:
                raise factory.NotStored(False, "disk full")
            return real(log, line)
        with patch.object(factory.Log, "_append", second_fails), self.assertRaises(factory.NotStored):
            self.log.drop_queued(box, {"a"})
        fresh = self.box()
        self.assertEqual([fresh.outcome(i, "a")["o"] for i in (1, 2, 3)], ["dropped", "queued", "queued"])
        self.assertEqual(self.log.drop_queued(self.box(), {"a"}), 2)

    def test_leftovers_skip_vanished_entries_and_warn_on_failures(self):
        stay = self.message_file("dev", "stay-1.md")
        gone = self.message_file("dev", "gone-2.md")
        done = self.message_file("dev", "done-3.md")
        self.log.add("dev", ["operator"], "done", file=str(done))
        self.deliver(self.box(), 1, "operator")
        real_scandir, real_unlink = factory.os.scandir, factory.os.unlink

        def vanishing(fd):
            with real_scandir(fd) as it:
                listing = list(it)
            if any(e.name == "gone-2.md" for e in listing) and gone.exists():
                real_unlink(gone)                              # disappears between the listing and its stat
            return contextlib.nullcontext(listing)
        with patch.object(factory.os, "scandir", vanishing), \
                patch.object(factory.os, "unlink", side_effect=PermissionError(13, "Permission denied")):
            rows, warnings = self.log.leftovers()
        self.assertEqual([pathlib.Path(r["path"]).name for r in rows], ["stay-1.md"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("done-3.md", warnings[0])
        self.assertTrue(stay.exists() and done.exists())

    def test_leftovers(self):
        delivered = self.message_file("lead-dev", "a-1.md")
        pending = self.message_file("lead-dev", "b-2.md")
        never = self.message_file("pm", "c-3.md")
        self.message_file("pm", "odd.name.md")
        (self.state / "messages" / "pm" / "link-4.md").symlink_to(never)
        self.log.add("lead-dev", ["operator"], "a", file=str(delivered))
        self.log.add("lead-dev", ["operator", "reviewer"], "b", file=str(pending))
        box = self.box()
        self.deliver(box, 1, "operator")
        self.deliver(box, 2, "operator")
        self.log.out(box, 2, "reviewer", "queued", why="blocked")
        now = time.time() + 180
        rows, warnings = self.log.leftovers(now=now)
        self.assertEqual(warnings, [])
        self.assertFalse(delivered.exists())
        self.assertEqual([(r["sender"], pathlib.Path(r["path"]).name, r["id"]) for r in rows],
                         [("lead-dev", "b-2.md", 2), ("pm", "c-3.md", None), ("pm", "odd.name.md", None)])
        self.assertEqual(rows[0]["outcomes"], {"operator": "delivered", "reviewer": "queued (blocked)"})
        lines = factory.format_leftovers(rows)
        self.assertRegex(lines[0], r"^leftover message file for @lead-dev \(3 min old\): .*b-2\.md: message 2: "
                                   r"@operator delivered, @reviewer queued \(blocked\)$")
        self.assertTrue(lines[1].endswith("c-3.md: never stored"))
        self.assertEqual(lines[-1], "Check before resending or discarding.")
        self.assertEqual(factory.format_leftovers([]), [])


class InboxReaderTests(unittest.TestCase):
    """inbox_rows, the Phase 3 offline reader (phase_3.md §7, §8 step 4). The CLI comes with say in Phase 4."""

    def setUp(self):
        self.base = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.log = factory.Log(self.base / "team" / "core")

    def box(self):
        records, end = self.log.read()
        box = factory.Box(self.log.path)
        box.apply(records)
        box.offset = end
        return box

    def test_default_ten_oldest_first(self):
        for n in range(1, 13):
            self.log.add("pm", ["operator"], f"m{n}")
        rows = factory.inbox_rows(self.log, "operator")
        self.assertEqual([m["text"] for m, _ in rows], [f"m{n}" for n in range(3, 13)])
        self.assertEqual([m["text"] for m, _ in factory.inbox_rows(self.log, "operator", 2)], ["m11", "m12"])

    def test_no_cap_at_a_hundred(self):
        for n in range(120):
            self.log.add("pm", ["operator"], str(n))
        self.assertEqual(len(factory.inbox_rows(self.log, "operator", 105)), 105)
        self.assertEqual(len(factory.inbox_rows(self.log, "operator", 500)), 120)

    def test_each_recipient_sees_its_own_outcome(self):
        self.log.add("pm", ["operator", "reviewer"], "both")
        self.log.add("pm", ["reviewer"], "reviewer only")
        box = self.box()
        self.log.out(box, 1, "operator", "queued", why="blocked")
        self.log.out(box, 1, "reviewer", "sending", term="t2")
        self.log.out(box, 1, "reviewer", "delivered", term="t2", started=True, turn=1)
        self.assertEqual([(m["id"], o["o"], o.get("why")) for m, o in factory.inbox_rows(self.log, "operator")],
                         [(1, "queued", "blocked")])
        self.assertEqual([(m["id"], o["o"]) for m, o in factory.inbox_rows(self.log, "reviewer")],
                         [(1, "delivered"), (2, "queued")])
        self.assertEqual(factory.inbox_rows(self.log, "pm"), [])     # the sender is not a recipient

    def test_original_text_survives_final_outcomes(self):
        text = "first line\nsecond line\n"
        self.log.add("pm", ["operator"], text)
        box = self.box()
        self.log.out(box, 1, "operator", "dropped", why="stopped by down")
        self.assertNotIn("text", box.msgs[1])
        (msg, outcome), = factory.inbox_rows(self.log, "operator")
        self.assertEqual((msg["text"], outcome["o"]), (text, "dropped"))

    def test_uncommitted_messages_are_hidden(self):
        self.log.add("pm", ["operator"], "committed")
        with self.log.path.open("ab") as f:
            f.write(b'{"t":"msg","id":2,"at":"2026-11-26T17:00:00Z","from":"pm","to":["operator"],"kind":"say",'
                    b'"text":"never committed"}\n')
        self.assertEqual([m["id"] for m, _ in factory.inbox_rows(self.log, "operator")], [1])

    def test_empty_store_and_no_match(self):
        self.assertEqual(factory.inbox_rows(self.log, "operator"), [])
        self.assertFalse(self.log.state.exists())
        self.log.add("pm", ["reviewer"], "x")
        self.assertEqual(factory.inbox_rows(self.log, "operator"), [])

    def test_damage_anywhere_raises_before_any_row(self):
        self.log.add("pm", ["operator"], "fine")
        with self.log.path.open("ab") as f:
            f.write(b"garbage\n")
        with self.assertRaises(factory.LogDamage):
            factory.inbox_rows(self.log, "operator")

    def test_invalid_arguments(self):
        for name, count in (("operator", 0), ("operator", -1), ("operator", True), ("operator", 1.0), ("", 10),
                            (None, 10)):
            with self.subTest(name=name, count=count), self.assertRaises(ValueError):
                factory.inbox_rows(self.log, name, count)

    def test_reading_changes_nothing_and_needs_no_hc(self):
        self.log.add("pm", ["operator"], "x")
        os.chmod(self.log.path, 0o640)
        before = {p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in self.log.state.iterdir()}
        with patch.object(factory.Hc, "call", side_effect=AssertionError("hc")):
            self.assertEqual(len(factory.inbox_rows(self.log, "operator")), 1)
        after = {p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in self.log.state.iterdir()}
        self.assertEqual(after, before)


class SessionTests(unittest.TestCase):
    """sessions.jsonl and session_plan (launch_layout.md §3)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = pathlib.Path(self.tmp.name) / "factory-skill" / "team" / "core"
        self.cfg = types.SimpleNamespace(members={
            "claude-dev": {"harness": "claude", "model": "claude-opus-5-5"},
            "codex-dev": {"harness": "codex", "model": "gpt-6.2-sol"},
            "pi-dev": {"harness": "pi", "model": "openai/gpt-6-astra"},
            "agy-dev": {"harness": "agy"}})

    def m(self, name, **over):
        return {"name": name, **self.cfg.members[name], **over}

    def lines(self):
        return [json.loads(line) for line in (self.state / factory.SESSIONS).read_bytes().splitlines()]

    def record(self, member, term, value, var=None):
        var = var or factory.TABLE[self.cfg.members[member]["harness"]]["session_env"]
        return factory.record_session(self.cfg, self.state, member, term, {var: value})

    def test_a_follow_harness_records_each_new_value_and_terminal_once(self):
        with patch.dict(factory.TABLE["claude"], session_follow=True):
            for term, value in (("t1", "aaa"), ("t1", "aaa"), ("t1", "bbb"), ("t2", "bbb")):
                self.assertIsNone(self.record("claude-dev", term, value))
        self.assertEqual([(e["term"], e["value"], e["how"]) for e in self.lines()],
                         [("t1", "aaa", "env"), ("t1", "bbb", "env"), ("t2", "bbb", "env")])
        newest, skipped = factory.saved_sessions(self.state)
        self.assertEqual((newest["claude-dev"]["value"], skipped), ("bbb", 0))
        self.assertEqual(stat.S_IMODE(os.stat(self.state / factory.SESSIONS).st_mode), 0o600)

    def test_without_follow_the_first_capture_of_a_launch_wins_in_either_order(self):
        self.assertFalse(factory.TABLE["codex"].get("session_follow", True))
        m = self.m("codex-dev")
        factory.launch_entry(self.state, m, "t1", "new", None)       # up's fallback first
        self.record("codex-dev", "t1", "thread-a")                   # then the ready line: replaces it
        self.record("codex-dev", "t1", "thread-b")                   # a sub-agent's later value: ignored
        self.assertEqual(factory.saved_sessions(self.state)[0]["codex-dev"]["value"], "thread-a")
        self.record("codex-dev", "t2", "thread-c")                   # the ready line first this time
        factory.launch_entry(self.state, m, "t2", "new", None)       # up's fallback never replaces it
        self.assertEqual([(e["term"], e["how"], e["value"]) for e in self.lines()],
                         [("t1", "new", None), ("t1", "env", "thread-a"), ("t2", "env", "thread-c")])

    def test_a_say_keeps_the_model_its_launch_resolved(self):
        """Phase 8 on a real pi member: up resolved openai/gpt-6-astra to openrouter/openai/gpt-6-astra, the say's
        entry wrote the formation's model, and the next up's provider check started it fresh. Two down/up cycles,
        each resumed by ID with no launch entry for the new terminal (@lead-dev), keep resuming."""
        resolved = self.m("pi-dev", model="openrouter/openai/gpt-6-astra")

        def say(term, value, model=resolved["model"]):
            return factory.record_session(self.cfg, self.state, "pi-dev", term, {"PI_SESSION_ID": value}, model=model)
        with patch.dict(factory.TABLE["pi"], resume_model="provider"):
            factory.launch_entry(self.state, resolved, "t3", "new", None)      # a fresh launch
            say("t3", "pi-1")
            for term in ("t4", "t5"):                                           # resumed by ID: no entry for term
                entry = factory.saved_sessions(self.state)[0]["pi-dev"]
                self.assertTrue(factory.compatible(entry, resolved))
                self.assertTrue(factory.session_plan(resolved, self.state, entry, fresh=False)["resume"])
                say(term, "pi-1")
            self.assertEqual({e["model"] for e in self.lines()}, {"openrouter/openai/gpt-6-astra"})
            factory.launch_entry(self.state, resolved, "t6", "new", None)
            say("t6", "pi-2", model=None)                                       # no label: its launch's entry
            self.assertEqual(self.lines()[-1]["model"], "openrouter/openai/gpt-6-astra")
            say("t9", "pi-3", model=None)                                       # neither: the formation's
            self.assertEqual(self.lines()[-1]["model"], "openai/gpt-6-astra")

    def test_a_fresh_launch_ends_the_previous_conversation(self):
        m = self.m("codex-dev")
        self.record("codex-dev", "t1", "thread-a")
        factory.launch_entry(self.state, m, "t2", "new", None)       # up --fresh, brief delivered, no say yet
        entry = factory.saved_sessions(self.state)[0]["codex-dev"]
        sess = factory.session_plan(m, self.state, entry, fresh=False)
        self.assertEqual((sess["args"], sess["resume"]), ([], False))
        self.assertIn("starts in a new conversation: none was recorded", sess["notice"])

    def test_launch_entry_never_replaces_a_current_session_with_new(self):
        m = self.m("pi-dev")
        for i, (how, value) in enumerate((("id", "abc"), ("dir", "/private/folder"), ("env", "xyz"))):
            with self.subTest(how=how):
                term = f"t{i}"
                factory.launch_entry(self.state, m, term, how, value)
                before = self.lines()
                factory.launch_entry(self.state, m, term, "new", None)
                self.assertEqual(self.lines(), before)
        m = self.m("codex-dev")
        start = threading.Barrier(2)

        def write(how, value):
            start.wait(timeout=5)
            factory.launch_entry(self.state, m, "race", how, value)

        with concurrent.futures.ThreadPoolExecutor(2) as workers:
            calls = [workers.submit(write, how, value) for how, value in (("new", None), ("id", "latest"))]
            for call in calls:
                call.result(timeout=5)
        self.assertEqual(factory.saved_sessions(self.state)[0]["codex-dev"]["value"], "latest")

    def test_values_that_could_be_options_or_too_long_are_not_recorded(self):
        for value in ("-rf", "", "a" * 129, "a b", "x;y", None):
            self.assertIsNone(self.record("codex-dev", "t1", value))
        self.assertFalse((self.state / factory.SESSIONS).exists())
        self.assertIsNone(self.record("codex-dev", "t1", "A1._:-" + "z" * 122))
        self.assertEqual(len(self.lines()), 1)

    def test_a_harness_without_a_variable_records_nothing(self):
        cfg = types.SimpleNamespace(members={"omp-dev": {"harness": "omp", "model": "openai/gpt-6-astra"}})
        self.assertIsNone(factory.record_session(cfg, self.state, "omp-dev", "t1", {"PI_SESSION_ID": "x"}))
        self.assertFalse(self.state.exists())

    def test_damaged_lines_are_skipped_and_counted_and_a_torn_tail_is_completed(self):
        self.record("codex-dev", "t1", "thread-a")
        with open(self.state / factory.SESSIONS, "ab") as f:
            f.write(b'not json\n{"member": "codex-dev", "how": "env", "value": "-x", "harness": "codex",'
                    b' "term": "t9", "at": "x"}\n{"member": "codex-dev", "harn')
        newest, skipped = factory.saved_sessions(self.state)
        self.assertEqual((newest["codex-dev"]["value"], skipped), ("thread-a", 3))
        self.record("codex-dev", "t2", "thread-b")
        data = (self.state / factory.SESSIONS).read_bytes()
        self.assertTrue(data.endswith(b'"harn\n' + data.splitlines()[-1] + b"\n"))
        newest, skipped = factory.saved_sessions(self.state)
        self.assertEqual((newest["codex-dev"]["value"], skipped), ("thread-b", 3))

    def test_concurrent_writers_each_append_one_whole_line(self):
        def worker(i):
            factory.record_session(self.cfg, self.state, "codex-dev", f"t{i}", {"CODEX_THREAD_ID": f"thread-{i}"})
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(e["value"] for e in self.lines()), sorted(f"thread-{i}" for i in range(16)))

    def test_teams_do_not_share_sessions(self):
        other = self.state.parent.parent / "other-team" / "core"
        self.record("codex-dev", "t1", "thread-a")
        self.assertEqual(factory.saved_sessions(other), ({}, 0))
        self.assertFalse(other.exists())                              # a reader creates nothing

    def test_a_failed_write_is_a_warning_and_says_why(self):
        with patch.object(factory.os, "fsync", side_effect=OSError(5, "Input/output error")):
            warning = self.record("codex-dev", "t1", "thread-a")
        self.assertRegex(warning, "^warning: could not record your session: .*Input/output error")

    def test_down_checks_sessions_can_be_saved(self):
        self.assertIsNone(factory.sessions_writable(self.state))
        self.assertTrue((self.state / factory.SESSIONS).exists())
        with patch.object(factory.os, "fsync", side_effect=OSError(28, "No space left on device")):
            self.assertIn("No space left", factory.sessions_writable(self.state))

    def test_session_plan_resumes_only_a_compatible_entry(self):
        entry = {"member": "claude-dev", "harness": "claude", "model": "claude-opus-5-5", "term": "t1",
                 "how": "env", "value": "abc", "at": "x"}
        m = self.m("claude-dev")
        self.assertEqual(factory.session_plan(m, self.state, entry, fresh=False),
                         {"args": ["--resume", "abc"], "resume": True, "assigned": None, "notice": None})
        self.assertEqual(factory.session_plan(m, self.state, entry, fresh=True)["args"], [])
        moved = factory.session_plan({**m, "harness": "codex"}, self.state, entry, fresh=False)
        self.assertEqual((moved["resume"], moved["notice"]),
                         (False, "@claude-dev starts fresh: its saved conversation is for claude claude-opus-5-5"))
        self.assertTrue(factory.session_plan({**m, "model": "claude-sonnet-5-5"}, self.state, entry, False)["resume"])

    def test_provider_harnesses_resume_only_within_a_provider(self):
        m = self.m("pi-dev")
        entry = {"member": "pi-dev", "harness": "pi", "model": "openai/gpt-6-luna", "term": "t1", "how": "env",
                 "value": "abc", "at": "x"}
        self.assertEqual(factory.session_plan(m, self.state, entry, False)["args"], ["--session", "abc"])
        other = factory.session_plan({**m, "model": "z-ai/glm-5.3"}, self.state, entry, False)
        self.assertEqual((other["resume"], other["args"]), (False, []))
        self.assertIn("is for pi openai/gpt-6-luna", other["notice"])

    def test_a_folder_entry_resumes_only_where_the_harness_uses_folders(self):
        entry = {"member": "pi-dev", "harness": "pi", "model": "openai/gpt-6-astra", "term": "t1", "how": "dir",
                 "value": str(self.state / "sessions" / "pi-dev-pi"), "at": "x"}
        self.assertFalse(factory.TABLE["pi"].get("session"))
        sess = factory.session_plan(self.m("pi-dev"), self.state, entry, fresh=False)
        self.assertEqual((sess["resume"], sess["args"]), (False, []))

    def test_opencode_gets_an_id_in_its_own_form_and_records_its_variable(self):
        m = {"name": "oc", "harness": "opencode", "model": "openrouter/z-ai/glm-5.3"}
        sess = factory.session_plan(m, self.state, None, fresh=False)
        self.assertRegex(sess["assigned"], r"^ses_[A-Za-z0-9]{26}$")
        self.assertEqual(sess["args"], ["--session", sess["assigned"]])
        cfg = types.SimpleNamespace(members={"oc": m})
        factory.record_session(cfg, self.state, "oc", "t1", {"OPENCODE_SESSION_ID": "ses_abc"})
        factory.record_session(cfg, self.state, "oc", "t1", {"OPENCODE_SESSION_ID": "ses_def"})   # after /new
        self.assertFalse(factory.TABLE["opencode"]["session_follow"])           # once per launch (#190)
        self.assertEqual(factory.saved_sessions(self.state)[0]["oc"]["value"], "ses_abc")
        again = factory.session_plan(m, self.state, factory.saved_sessions(self.state)[0]["oc"], fresh=False)
        self.assertEqual((again["resume"], again["args"]), (True, ["--session", "ses_abc"]))

    def test_codex_runs_without_its_daemon_fresh_or_resumed(self):
        m = {"name": "x", "role": "lead-dev", "harness": "codex", "model": "gpt-6.2-sol", "thinking": "high",
             "permissions": "full", "args": [], "dir": "/w", "inbox_dir": "/s"}
        self.assertEqual(factory.launch_argv(m, pathlib.Path("/p.md"))[0][0], "--no-daemon")
        argv = factory.launch_argv(m, pathlib.Path("/p.md"), {"args": ["resume", "id1"]})[0]
        self.assertEqual(argv[:3], ["resume", "id1", "--no-daemon"])

    def test_assigned_ids_and_session_folders(self):
        with patch.dict(factory.TABLE["claude"], session="id"):
            sess = factory.session_plan(self.m("claude-dev"), self.state, None, fresh=False)
        self.assertEqual(sess["args"], ["--session-id", sess["assigned"]])
        self.assertEqual(str(uuid.UUID(sess["assigned"])), sess["assigned"])
        folder = self.state / "sessions" / "pi-dev-pi"
        with patch.dict(factory.TABLE["pi"], session="dir"):
            fresh = factory.session_plan(self.m("pi-dev"), self.state, None, fresh=False)
            entry = {"member": "pi-dev", "harness": "pi", "model": "openai/gpt-6-astra", "term": "t1", "how": "dir",
                     "value": str(folder), "at": "x"}
            again = factory.session_plan(self.m("pi-dev"), self.state, entry, fresh=False)
        self.assertEqual(fresh["args"], ["--session-dir", str(folder)])
        self.assertEqual(again["args"], ["--session-dir", str(folder), "--continue"])
        with patch.dict(factory.TABLE["omp"], session="dir"):
            m = {"name": "omp-dev", "harness": "omp", "model": "openai/gpt-6-astra"}
            self.assertEqual(factory.session_plan(m, self.state, None, False)["args"],
                             [f"--session-dir={self.state / 'sessions' / 'omp-dev-omp'}"])

    def test_launch_arguments_stay_separate_items(self):
        prompt = pathlib.Path("/tmp/state dir/it's.md")
        for harness, model, expect in (
                ("claude", "claude-opus-5-5", ["--resume", "id 1"]),
                ("codex", "gpt-6.2-sol", ["resume", "id 1"]),
                ("agy", "gemini-4-neon", ["--conversation", "id 1"]),
                ("opencode", "openrouter/z-ai/glm-5.3", ["--session", "id 1"]),
                ("pi", "openai/gpt-6-astra", ["--session", "id 1"])):
            m = {"name": "x", "role": "lead-dev", "harness": harness, "model": model, "thinking": "high",
                 "permissions": "full", "args": ["--flag", "two words"], "dir": "/w d", "inbox_dir": "/s"}
            argv, _ = factory.launch_argv(m, prompt, {"args": expect, "resume": True})
            at = argv.index(expect[0])
            self.assertEqual(argv[at:at + 2], expect, harness)
            self.assertEqual(argv[-2:], ["--flag", "two words"], harness)
            if harness == "codex":
                self.assertEqual(at, 0)                                  # resume <id> before the options


class ScriptedHc:
    """A stand-in for Hc in readiness tests: each terminal follows a script of (state, status word, screen) steps,
    one per get(); keys are recorded and may move the script on."""

    def __init__(self, scripts, on_keys=None):
        self.scripts = {a: list(s) for a, s in scripts.items()}
        self.at = {a: 0 for a in scripts}
        self.on_keys = on_keys or {}
        self.keys_sent = []

    def step(self, address):
        steps = self.scripts[address]
        return steps[min(self.at[address], len(steps) - 1)]

    def get(self, address):
        state, word, _ = self.step(address)
        self.at[address] += 1
        if state == "error":
            raise factory.HcError("not_found", f"no terminal {address}")
        return {"address": address, "state": state, "exit_code": 2 if state == "finished" else None,
                "status": {"state": word} if word else None}

    def read(self, address):
        return self.step(address)[2]

    def keys(self, address, keys):
        self.keys_sent.append((address, keys))
        self.keys_at = getattr(self, "keys_at", []) + [self.at[address]]   # on which poll
        if address in self.on_keys:
            self.scripts[address] = self.on_keys.pop(address)
            self.at[address] = 0


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ReadyTests(unittest.TestCase):
    """await_ready (launch_layout.md §4), on scripted terminals and fake time."""

    CLAUDE = factory.TABLE["claude"]["dialogs"]
    TRUST = "Do you trust the contents of this project?"

    def ready(self, scripts, dialogs=(), timeout=5, on_keys=None):
        hc = ScriptedHc(scripts, on_keys)
        started = {a: ({"address": a}, dialogs.get(a, []) if isinstance(dialogs, dict) else list(dialogs))
                   for a in scripts}
        return factory.await_ready(hc, started, timeout=timeout, clock=FakeClock()), hc

    def test_idle_without_a_dialog_is_ready_and_nothing_else_is(self):
        out, _ = self.ready({"a": [("live", "working", ""), ("live", "idle", "")],
                             "b": [("live", "done", "")], "c": [("live", None, "")],
                             "d": [("live", "working", "")], "e": [("live", "blocked", "")]})
        self.assertEqual(out, {"a": None, "b": "not ready after 5 s (done)", "c": "not ready after 5 s (no status)",
                               "d": "not ready after 5 s (working)", "e": "not ready after 5 s (blocked)"})

    def test_a_dialog_is_answered_once_whatever_the_status(self):
        screen = "Quick safety check\n  Yes, I trust\n  this folder\n  No, exit"      # wrapped by a narrow pane
        for word in ("blocked", None, "idle"):
            with self.subTest(status=word):
                out, hc = self.ready({"a": [("live", word, screen)]}, self.CLAUDE,
                                     on_keys={"a": [("live", word, screen), ("live", "idle", "")]})
                self.assertEqual(out, {"a": None})
                self.assertEqual(hc.keys_sent, [("a", ["down", "enter"])])

    def test_a_second_dialog_is_answered_while_the_first_is_still_shown(self):
        first, second = "Yes, I trust this folder", "Yes, I accept"
        out, hc = self.ready({"a": [("live", "blocked", first)]}, self.CLAUDE,
                             on_keys={"a": [("live", "blocked", first + "\n" + second)]})
        self.assertEqual(hc.keys_sent, [("a", ["down", "enter"]), ("a", ["down", "enter"])])
        self.assertEqual(out, {"a": f'still asks "{first}" after the answer was sent'})

    def test_a_signal_or_another_end_is_named(self):
        hc = ScriptedHc({"a": [("live", "idle", "")]})
        hc.get = lambda address: {"address": address, "state": "finished", "exit_code": None, "signal": 9,
                                  "status": None}
        out = factory.await_ready(hc, {"a": ({"address": "a"}, [])}, timeout=5, clock=FakeClock())
        self.assertEqual(out, {"a": "was stopped by signal 9"})

    def test_a_dialog_is_answered_on_its_second_poll_not_as_it_is_drawn(self):
        out, hc = self.ready({"a": [("live", "blocked", "Yes, I trust this folder")]}, self.CLAUDE,
                             on_keys={"a": [("live", "idle", "")]})
        self.assertEqual(out, {"a": None})
        self.assertEqual(hc.keys_at, [2])

    def test_a_dialog_still_shown_after_the_answer_is_named(self):
        out, hc = self.ready({"a": [("live", "blocked", "Yes, I trust this folder")]}, self.CLAUDE)
        self.assertEqual(out, {"a": 'still asks "Yes, I trust this folder" after the answer was sent'})
        self.assertEqual(len(hc.keys_sent), 1)

    def test_a_dialog_without_keys_is_left_to_the_owner(self):
        out, hc = self.ready({"a": [("live", "blocked", self.TRUST)]}, [[self.TRUST, []]])
        self.assertEqual(out, {"a": "waits on its trust dialog: accept it in its pane, then run:"
                                    " factory.py brief --send a"})
        self.assertEqual(hc.keys_sent, [])

    def test_an_exit_reports_its_code_and_last_line_and_others_go_on(self):
        out, _ = self.ready({"a": [("finished", None, "starting\nError: bad model\n\n")],
                             "b": [("live", "working", "")] * 3 + [("live", "idle", "")]})
        self.assertEqual(out, {"a": "exited (code 2): Error: bad model", "b": None})

    def test_a_terminal_that_cannot_be_read_is_named(self):
        out, _ = self.ready({"a": [("error", None, "")], "b": [("live", "idle", "")]})
        self.assertEqual(out["b"], None)
        self.assertRegex(out["a"], "^could not be watched: .*no terminal a")

    def test_members_are_polled_together_against_one_deadline(self):
        clock = FakeClock()
        hc = ScriptedHc({"a": [("live", "working", "")] * 6 + [("live", "idle", "")],
                         "b": [("live", "working", "")] * 8 + [("live", "idle", "")]})
        out = factory.await_ready(hc, {"a": ({"address": "a"}, []), "b": ({"address": "b"}, [])}, timeout=60,
                                  clock=clock)
        self.assertEqual(out, {"a": None, "b": None})
        self.assertEqual(clock.now, 4.0)


class StartMemberTests(unittest.TestCase):
    """start_member (launch_layout.md §4) against the fake hc."""

    def setUp(self):
        self.env = self.enterContext(Env())
        self.cfg = factory.Config(self.env.repo)
        self.fake = self.enterContext(FakeHc())
        self.hc = factory.Hc()
        self.state = self.env.root / "cache" / "factory-skill" / "team" / "core"
        self.enterContext(patch.object(factory.shutil, "which", lambda name: f"/opt/bin/{name}"))
        self.path = f"/opt/bin:{os.environ.get('PATH', '')}"      # the fake hc itself still has to start
        self.enterContext(patch.dict(os.environ, {"PATH": self.path}))

    def start(self, name, sess=None):
        m = factory.plan(self.cfg, name)
        return m, factory.start_member(self.cfg, self.state, self.hc, m, "operator", sess)

    def test_the_harness_runs_by_itself_with_its_argv_cwd_env_and_labels(self):
        name = next(n for n, m in self.cfg.members.items() if m["harness"] == "claude")
        m, record = self.start(name, {"args": ["--resume", "abc"], "resume": True})
        run = self.fake.calls("terminal.run")[-1]["params"]
        prompt = self.state / f"{name}.md"
        argv, env = factory.launch_argv({**m, "draft_dir": self.state / "messages" / name, "inbox_dir": self.state},
                                        prompt, {"args": ["--resume", "abc"]})
        self.assertEqual(run["argv"], ["/opt/bin/claude", *argv])
        self.assertEqual((run["cwd"], run["env"]), (m["dir"], {"PATH": self.path, **env}))
        self.assertEqual(run["labels"], {"factory.team": factory.team_key(self.cfg.root), "factory.kind": "member",
                                         "factory.member": name, "factory.dir": str(self.state), "name": "@" + name,
                                         "factory.model": m["model"]})      # a resume by ID too: say's sessions
        self.assertTrue(run["no_parent"])
        self.assertFalse(any(k.startswith("herdr.") for k in record["labels"]))

    def test_files_are_private_and_rewritten_at_each_launch(self):
        name = next(n for n, m in self.cfg.members.items() if m["harness"] == "claude")
        self.start(name)
        prompt, settings = self.state / f"{name}.md", self.state / f"{name}.settings.json"
        self.assertIn(str(self.state / "messages" / name), prompt.read_text())
        self.assertIn(str(prompt), prompt.read_text())
        prompt.write_text("stale")
        self.start(name)
        self.assertNotEqual(prompt.read_text(), "stale")
        for path, mode in ((prompt, 0o600), (settings, 0o600), (self.state / "messages", 0o700),
                           (self.state / "messages" / name, 0o700)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode, path)
        values = json.loads(settings.read_text())
        say = shlex.join(["python3", str(factory.SCRIPT), "say"]) + " *"
        self.assertIn(say, values["sandbox"]["excludedCommands"])
        self.assertIn(f"Bash({say})", values["permissions"]["allow"])
        self.assertEqual(values["permissions"]["additionalDirectories"], [str(self.state / "messages" / name)])

    def test_a_symbolic_link_for_a_message_folder_is_refused_before_anything_starts(self):
        name = next(iter(self.cfg.members))
        (self.state / "messages").mkdir(parents=True)
        (self.state / "messages" / name).symlink_to(self.env.root)
        with self.assertRaisesRegex(factory.Problem, "expected a private directory"):
            self.start(name)
        self.assertEqual(self.fake.calls("terminal.run"), [])

    def test_session_folders_for_dir_harnesses(self):
        name = next(n for n, m in self.cfg.members.items() if m["harness"] == "pi")
        with patch.dict(factory.TABLE["pi"], session="dir"):
            self.start(name)
        folder = self.state / "sessions" / f"{name}-pi"
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)

    def test_start_member_labels_the_session_plan_it_runs(self):
        for name, member in self.cfg.members.items():
            harness = member["harness"]
            for fallback in (False, True):
                for resume in (False, True):
                    with self.subTest(harness=harness, fallback=fallback, resume=resume):
                        table = factory.TABLE[harness]
                        mode = "env" if fallback else table.get("session")
                        with patch.dict(table, session=mode):
                            m = factory.plan(self.cfg, name)
                            entry = {"harness": harness, "model": m["model"],
                                     "how": "dir" if mode == "dir" else "id",
                                     "value": str(factory.session_folder(self.state, m)) if mode == "dir" else "saved"}
                            sess = factory.session_plan(m, self.state, entry if resume else None, False)
                            _, record = self.start(name, sess)
                        raw = record["labels"].get("factory.launch")
                        self.assertEqual(self.fake.calls("terminal.run")[-1]["params"]["labels"].get("factory.launch"),
                                         raw)
                        if resume and mode != "dir":
                            self.assertIsNone(raw)
                            continue
                        how = "dir" if mode == "dir" else "id" if mode == "id" else "new"
                        value = entry["value"] if how == "dir" else sess["assigned"]
                        self.assertEqual(json.loads(raw), {"how": how, "value": value, "fresh": not resume,
                                                           "harness": harness, "model": m["model"]})

    def test_a_missing_binary_or_a_refused_run_is_a_problem(self):
        name = next(iter(self.cfg.members))
        with patch.object(factory.shutil, "which", lambda _: None), \
                self.assertRaisesRegex(factory.Problem, "is not on PATH"):
            self.start(name)
        self.fake.on_run(name, error="spawn_failed")
        with self.assertRaisesRegex(factory.Problem, "hc could not start"):
            self.start(name)


class HcCheckTests(unittest.TestCase):
    """check's offline hc line (hc_client.md, phase_5a.md §4.1)."""

    def setUp(self):
        self.dir = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.environ = {"XDG_CONFIG_HOME": str(self.dir / "config")}

    def hc(self, output, code=0):
        script = self.dir / "hc"
        script.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(output)}\nexit {code}\n")
        script.chmod(0o755)
        self.enterContext(patch.object(factory, "HC", str(script)))

    def config(self, text):
        path = self.dir / "config" / "herdr-core" / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_draft_wait_values(self):
        self.hc("hc 0.1.20")
        path = self.dir / "config" / "herdr-core" / "config.toml"
        warn = f"warning: raise draft_wait to 600 or more in {path} so a report never lands in text you are typing"
        self.assertEqual(factory.hc_check(self.environ), (["hc 0.1.20, draft_wait 60 s", warn], False))
        for text, shown, warned in (("other = 1\n", 60, True), ("draft_wait = 599\n", 599, True),
                                    ("draft_wait = 600\n", 600, False), ("draft_wait = 900\n", 900, False),
                                    ("draft_wait = -5\n", 0, True)):
            self.config(text)
            lines, failed = factory.hc_check(self.environ)
            self.assertEqual((lines[0], len(lines) == 2, failed), (f"hc 0.1.20, draft_wait {shown} s", warned, False))

    def test_a_missing_hc_is_reported_and_not_a_failure(self):
        self.enterContext(patch.object(factory, "HC", str(self.dir / "no-such-hc")))
        lines, failed = factory.hc_check(self.environ)
        self.assertFalse(failed)
        self.assertIn("hc: not found", lines[0])

    def test_an_old_or_unrecognised_hc_or_a_bad_config_fails(self):
        for output, code, expect in (("hc 0.1.19", 0, "too old"), ("herdr 0.9.5", 0, "unrecognised version"),
                                     ("hc 0.1.20", 1, "unrecognised version")):
            self.hc(output, code)
            lines, failed = factory.hc_check(self.environ)
            self.assertTrue(failed, output)
            self.assertIn(expect, lines[0])
        self.hc("hc 0.2.0-dev+abc")
        self.assertFalse(factory.hc_check(self.environ)[1])
        for text, expect in (("draft_wait = [", "cannot read"), ('draft_wait = "600"', "whole number"),
                             ("draft_wait = true", "whole number")):
            self.config(text)
            lines, failed = factory.hc_check(self.environ)
            self.assertTrue(failed, text)
            self.assertIn(expect, lines[0])

    def test_check_prints_the_line_and_its_exit_code(self):
        with Env() as e:
            fake = e.root / "bin" / "hc"
            fake.write_text("#!/bin/sh\necho 'hc 0.1.20'\n")
            fake.chmod(0o755)
            p = e.run("check", HC_BIN=str(fake), XDG_CONFIG_HOME=str(e.root / "config"))
            self.assertIn("hc 0.1.20, draft_wait 60 s", p.stdout)
            self.assertEqual(p.returncode, 0)
            p = e.run("check", check=False, HC_BIN=str(e.root / "missing"), XDG_CONFIG_HOME=str(e.root / "config"))
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("hc: not found", p.stdout)
            fake.write_text("#!/bin/sh\necho 'hc 0.1.2'\n")
            p = e.run("check", check=False, HC_BIN=str(fake), XDG_CONFIG_HOME=str(e.root / "config"))
            self.assertEqual(p.returncode, 2)
            self.assertIn("too old", p.stdout)


class QualifyJudgeTests(unittest.TestCase):
    """The false-pass paths @architect-2 named in the first PR's review."""

    def trials(self, *seconds, idle=True, outcome="delivered"):
        return [{"idle": idle, "seconds": s, "outcome": outcome} for s in seconds]

    def test_latency(self):
        self.assertEqual(live.judge_latency(self.trials(0.7, 0.9, 1.2))[0], "pass")
        self.assertEqual(live.judge_latency(self.trials(0.7, 0.9, 30))[0], "pass")     # one slow trial, reported
        self.assertIn("1 over", live.judge_latency(self.trials(0.7, 0.9, 30))[1])
        self.assertEqual(live.judge_latency(self.trials(30, 30, 30))[0], "fail")       # delivered, but 30 s each
        self.assertEqual(live.judge_latency(self.trials(1, 1, 1, idle=False))[0], "not_run")
        self.assertEqual(live.judge_latency(self.trials(1, 1, 1, outcome="queued"))[0], "fail")
        self.assertEqual(live.judge_latency(self.trials(1, 1))[0], "not_run")

    def test_dialog(self):
        declared = [["Trust and continue", ["enter"]]]
        self.assertEqual(live.judge_dialog(declared, "Trust and continue", True)[0], "pass")
        self.assertEqual(live.judge_dialog(declared, None, True)[0], "not_run")         # up succeeded, none shown
        self.assertEqual(live.judge_dialog(declared, "Trust and continue", False)[0], "fail")
        self.assertEqual(live.judge_dialog([], None, True)[0], "pass")
        owner = [["Do you trust the contents of this project?", []]]
        self.assertEqual(live.judge_dialog(owner, owner[0][0], False)[0], "pass")
        self.assertEqual(live.judge_dialog(owner, owner[0][0], True)[0], "fail")
        self.assertEqual(live.judge_dialog(owner, None, True, suppressed=True)[0], "pass")   # agy's argument, verified
        self.assertEqual(live.judge_dialog(owner, None, True)[0], "not_run")      # no prompt alone proves nothing
        self.assertEqual(live.judge_dialog(owner, None, False, suppressed=True)[0], "not_run")

    def test_blocked(self):
        note = {"about": "lead-dev", "term": "t3", "outcome": "delivered"}
        good = ([note], "lead-dev", "t3", True, False, "delivered", 1)
        self.assertEqual(live.judge_blocked(*good)[0], "pass")
        for index, value in ((0, []), (0, [note, note]), (0, [dict(note, outcome="queued")]),
                             (0, [dict(note, about="pm")]), (0, [dict(note, term="t9")]), (3, False), (4, True),
                             (5, "unconfirmed"), (6, 2), (6, 0)):
            with self.subTest(index=index, value=value):
                bad = list(good)
                bad[index] = value
                self.assertEqual(live.judge_blocked(*bad)[0], "fail")

    def test_member_switch_needs_a_replacement(self):
        good = {"down_exit": 0, "up_exit": 0, "replaced": True}
        self.assertEqual(live.judge_member_switch(True, good, True), "pass")
        for launch in (dict(good, replaced=False), dict(good, down_exit=1, replaced=False), dict(good, up_exit=3),
                       {"down_exit": 1, "up_exit": 2, "replaced": False}):
            with self.subTest(launch=launch):                      # the same session still running recalls its word
                self.assertEqual(live.judge_member_switch(True, launch, True), "fail")
        self.assertEqual(live.judge_member_switch(True, good, False), "fail")
        self.assertEqual(live.judge_member_switch(False, good, True), "not_run")

    def test_switch(self):
        self.assertEqual(live.judge_switch("a", "b"), (True, None))
        self.assertFalse(live.judge_switch("a", "a")[0])                                # switch() did nothing
        self.assertFalse(live.judge_switch("", "b")[0])
        self.assertFalse(live.judge_switch("a", None)[0])


class QualifyReportTests(unittest.TestCase):
    def test_newest_run_wins_and_hand_text_is_kept(self):
        head = subprocess.run(["git", "-C", str(live.ROOT), "rev-parse", "HEAD"], capture_output=True,
                              text=True).stdout.strip()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)

            def run(name, source, status, mode="real"):
                row = {"case": 1, "subcase": "states", "role": "member", "harness": "claude", "status": status,
                       "observed": f"seen at {name}", "proof": {"peer": "codex"}}
                data = {"mode": mode, "source_head": source, "utc": "2026-11-27T00:00:00Z", "rows": [row],
                        "factory_version": "abc", "hc_version": "hc 0.1.20", "rodeo_version": "rodeo 0.2.1",
                        "profiles": [{"role": "member", "harness": "claude", "version": "2.1", "model": "m"}],
                        "trust_entries": [{"harness": "claude", "file": "~/.claude.json", "directory": f"/tmp/{name}"}]}
                (tmp / f"{name}.json").write_text(json.dumps(data))
                return str(tmp / f"{name}.json")
            paths = [run("new", head, "pass"), run("old", "0" * 40, "fail"), run("fake", head, "fail", "fixture")]
            out = tmp / "out"
            out.mkdir()
            (out / "claude.md").write_text(f"# Claude Code\n\nNotes kept.\n\n{live.REPORT_BEGIN}\nold\n"
                                           f"{live.REPORT_END}\n\nAfter, kept.\n")
            live.report(paths, out)
            text = (out / "claude.md").read_text()
            self.assertIn("| 1 | states | pass | seen at new; partner codex |", text)
            self.assertNotIn("seen at old", text)
            self.assertNotIn("\nold\n", text)
            self.assertTrue(text.startswith("# Claude Code\n\nNotes kept."))
            self.assertTrue(text.endswith("After, kept.\n"))
            self.assertIn("1 startup-dialog (no run)", text)
            trust = (out / "trust-entries.md").read_text()
            self.assertIn("`/tmp/new`", trust)
            self.assertIn("`/tmp/old`", trust)
            self.assertNotIn("/tmp/fake", trust)

    def test_the_latest_attempt_wins_across_heads_and_trust_entries_accumulate(self):
        """@reviewer: a later fail on an older head must not hide behind an earlier pass on a newer one; and a
        partial import must keep the cleanup entries an earlier import listed."""
        head = subprocess.run(["git", "-C", str(live.ROOT), "rev-parse", "HEAD"], capture_output=True,
                              text=True).stdout.strip()
        base = subprocess.run(["git", "-C", str(live.ROOT), "rev-list", "--max-parents=0", "HEAD"], capture_output=True,
                              text=True).stdout.split()[0]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)

            def run(name, source, utc, status):
                row = {"case": 2, "subcase": "idle-report", "role": "member", "harness": "codex", "status": status,
                       "observed": f"seen at {name}", "reason": None if status == "pass" else "see proof",
                       "proof": {"turn_completed": True}}
                data = {"mode": "real", "source_head": source, "utc": utc, "rows": [row], "profiles": [],
                        "trust_entries": [{"harness": "codex", "file": "~/.codex/config.toml",
                                           "directory": f"/tmp/factory-qa-{name}/work"}]}
                (tmp / f"{name}.json").write_text(json.dumps(data))
                return str(tmp / f"{name}.json")
            out = tmp / "out"
            first = run("a", head, "2026-11-27T01:00:00Z", "pass")
            later = run("b", base, "2026-11-27T02:00:00Z", "fail")
            live.report([first, later], out)
            text = (out / "codex.md").read_text()
            self.assertIn("| 2 | idle-report | fail | seen at b", text)
            self.assertIn(f"2 idle-report: pass at {head[:7]}, superseded by the fail above", text)
            stale = {"case": 6, "subcase": "member-switch", "role": "member", "harness": "codex", "status": "pass",
                     "proof": {"switched": True, "replaced": False, "down_exit": 1, "up_exit": 0}}
            data = json.loads(pathlib.Path(later).read_text())
            data["rows"].append(stale)
            pathlib.Path(later).write_text(json.dumps(data))
            live.report([first, later], out)
            self.assertIn("| 6 | member-switch | unproved (its proof does not meet today's check)",
                          (out / "codex.md").read_text())
            live.report([later], out)                                     # a partial import: only b's evidence
            trust = (out / "trust-entries.md").read_text()
            self.assertIn("/tmp/factory-qa-a/work", trust)
            self.assertIn("/tmp/factory-qa-b/work", trust)
            live.report([first, later], out)                              # importing again changes nothing
            self.assertEqual((out / "trust-entries.md").read_text(), trust)


class QualifyFrameworkTests(unittest.TestCase):
    """phase_8.md §1 and §5.1: selection, rows, the case rule, exit precedence and checkpoints, with no login."""

    def test_defaults_and_scope(self):
        self.assertEqual(live.select_profiles("codex", None), [("operator", "codex"), ("member", "codex")])
        self.assertEqual(live.select_profiles("pi", None), [("member", "pi")])
        for harness, role, why in (("pi", "operator", "member in this phase"), ("vim", None, "is not one of")):
            with self.assertRaisesRegex(ValueError, why):
                live.select_profiles(harness, role)

    def test_bad_choices_are_refused_before_any_core(self):
        with patch.object(live, "Core", side_effect=AssertionError("no core may start")):
            for args, why in ((("pi", "operator", None), "member in this phase"), (("codex", None, [11]), "1 to 10"),
                              (("cross", "member", None), "no --role")):
                with self.subTest(args=args), self.assertRaisesRegex(ValueError, why):
                    live.qualify(*args)

    def test_the_case_rule_and_exit_precedence(self):
        run = live.Qualification("fixture")
        selected = [("member", "pi", 8)]
        for name in ("dead-member", "unconfirmed-brief"):
            run.row(8, name, "member", "pi", "pass", "x")
        run.row(8, "quit-during-hold", "member", "pi", "measured", "x", observed="late text arrived")
        self.assertEqual((run.case_status("member", "pi", 8), run.exit_code(selected)), ("pass", 0))
        partial = live.Qualification("fixture")
        partial.row(8, "dead-member", "member", "pi", "pass", "x")       # unconfirmed-brief missing
        self.assertEqual((partial.case_status("member", "pi", 8), partial.exit_code(selected)), ("not_run", 2))
        partial.row(8, "unconfirmed-brief", "member", "pi", "fail", "x", reason="retyped")
        self.assertEqual((partial.case_status("member", "pi", 8), partial.exit_code(selected)), ("fail", 1))
        mixed = selected + [("member", "pi", 9)]                          # one fails, one not run: fail wins
        self.assertEqual(partial.exit_code(mixed), 1)

    def test_a_case_stopped_late_keeps_its_earlier_failure(self):
        def case(run, role, harness, adapter_class):
            run.row(6, "resume", role, harness, "fail", "x", reason="the recalled word did not match")
            raise live.NotRun("rodeo is not installed")
        with patch.dict(live.CASE_FUNCTIONS, {6: case}), patch.object(live, "QATeam"):
            evidence, code = live.qualify("codex", "member", [6], fixture=True)
        self.assertEqual([(r["subcase"], r["status"]) for r in evidence["rows"]],
                         [("resume", "fail"), ("member-switch", "not_run")])
        self.assertEqual(code, 1)

    def test_rows_need_reasons_and_stay_bounded(self):
        run = live.Qualification("real")
        for status in ("fail", "not_run"):
            with self.assertRaisesRegex(ValueError, "needs a reason"):
                run.row(1, "states", "member", "pi", status, "x")
        with self.assertRaisesRegex(ValueError, "unknown status"):
            run.row(1, "states", "member", "pi", "passed", "x")
        row = run.row(1, "states", "member", "pi", "pass", "x", observed="y" * 1000)
        self.assertEqual(len(row["observed"]), live.BOUND + 1)

    def test_a_fixture_run_says_so_and_checkpoints_each_case(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "evidence.json"
            with patch.dict(live.CASE_FUNCTIONS, clear=True):             # nothing built: not run, not pass
                evidence, code = live.qualify("codex", "member", [1, 2], str(path), fixture=True)
            self.assertEqual(evidence["mode"], "fixture")
            self.assertEqual(json.loads(path.read_text())["rows"], evidence["rows"])
            self.assertEqual(code, 2)                                     # nothing built yet: not run, not pass
            self.assertEqual(evidence["summary"], {"member codex": {1: "not_run", 2: "not_run"}})
            self.assertEqual(sorted(p.name for p in pathlib.Path(directory).iterdir()), ["evidence.json"])


@real_only
class QualifyFixtureTests(unittest.TestCase):
    """phase_8.md §5.1-2: the qualification cases on fake agents and the private real hc, no login."""

    def test_cases_2_and_9_pass_on_fake_agents_and_say_so(self):
        evidence, code = live.qualify("codex", None, [2, 9], fixture=True)
        commands = "pass" if live.RODEO else "not_run"      # the owner's first prompt goes through rodeo
        self.assertEqual((evidence["mode"], code), ("fixture", 0 if live.RODEO else 2))
        self.assertEqual(sorted((r["role"], r["subcase"], r["status"]) for r in evidence["rows"]),
                         [("member", "guarded", "pass"), ("member", "idle-report", "pass"),
                          ("operator", "idle-report", commands), ("operator", "operator-commands", commands)])

    @real_only
    def test_sigterm_during_a_run_stops_its_cores_and_removes_its_scratch(self):
        """@reviewer: SIGTERM during qualify must run the with-blocks and atexit cleanup, leaving no private core
        or scratch project behind."""
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, TMPDIR=tmp)
            proc = subprocess.Popen([sys.executable, str(live.ROOT / "tests" / "live.py"), "qualify", "codex",
                                     "--role", "member", "--case", "9", "--fixture"], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline and not list(pathlib.Path(tmp).glob("factory-qa-*")):
                    time.sleep(0.2)
                self.assertTrue(list(pathlib.Path(tmp).glob("factory-qa-*")), "the run never started its team")
                time.sleep(3)
                proc.send_signal(signal.SIGTERM)
                self.assertEqual(proc.wait(timeout=90), 1)
            finally:
                if proc.poll() is None:
                    proc.kill()
            left = sorted(p.name for p in pathlib.Path(tmp).iterdir()
                          if p.name.startswith(("factory-qa-", "hc-qualify")))
            self.assertEqual(left, [])

    def test_an_echoed_token_without_the_action_fails(self):
        # The report reaches the operator with the token in it, but no action is asked for: no receipt.
        real_until = live.QATeam.until
        with patch.object(live.FixtureAdapter, "act_append", lambda self, path, tok: f"please note {tok}"), \
                patch.object(live.QATeam, "until", lambda self, predicate, timeout: real_until(self, predicate,
                                                                                               min(timeout, 15))):
            evidence, code = live.qualify("codex", "member", [2], fixture=True)
        row, = evidence["rows"]
        self.assertEqual((row["subcase"], row["status"], code), ("idle-report", "fail", 1))


class LiveToolTests(unittest.TestCase):
    @staticmethod
    def scrubber(host="local-host", user="local-user"):
        core = types.SimpleNamespace(dir="/tmp/factory-scratch-core", id="local-core-id")
        with (patch("socket.gethostname", return_value=host), patch("getpass.getuser", return_value=user),
              patch("os.path.expanduser", return_value=f"/home/{user}")):
            return live.scrubber(core)

    def test_identity_fields_redacted_by_position(self):
        scrub = self.scrubber()
        for value in ("ci", "x", "another-user", 1000):
            with self.subTest(value=value):
                raw = {"core": {"name": str(value)}, "login_session": {"user": value, "id": 42, "gui": None}}
                expected_user = 1 if isinstance(value, int) else "<user>"
                self.assertEqual(scrub(raw), {"core": {"name": "<host>"},
                                              "login_session": {"user": expected_user, "id": 1, "gui": None}})

    def test_leaks_flags_any_unredacted_identity_field(self):
        scrub = self.scrubber()
        self.assertEqual(set(scrub.leaks({"core": {"name": "ci"}, "login_session": {"user": "x", "id": 8}})),
                         {"core.name", "login_session.user", "login_session.id"})

    def test_placeholders_are_not_leaks(self):
        scrub = self.scrubber(host="host", user="user")
        raw = {"core": {"name": "host"}, "login_session": {"user": 1000, "id": 2}, "cwd": "/home/user/host"}
        masked = scrub(raw)
        self.assertEqual(masked["cwd"], "/home/<user>/<host>")
        self.assertEqual(scrub(masked), masked)
        self.assertFalse(scrub.leaks(masked))

    def test_short_names_left_alone_inside_paths(self):
        scrub = self.scrubber(host="ci", user="ci")
        self.assertEqual(scrub("/srv/ci/build"), "/srv/ci/build")
        self.assertFalse(scrub.leaks("/srv/ci/build"))

    def test_protocol_words_that_look_like_names_survive(self):
        scrub = self.scrubber(host="idle", user="live")
        payload = {"state": "live", "reason": "idle", "status": {"state": "idle"},
                   "foreground": {"name": "idle"}, "labels": {"name": "live"}}
        self.assertEqual(scrub(payload), payload)
        self.assertFalse(scrub.leaks(payload))

    def test_scrubbed_fixture_still_conforms(self):
        original = {**HC_FIXTURES["replies"]["hello"]["result"], "login_session": {"id": 51, "user": 103}}
        cleaned = self.scrubber()(original)
        self.assertEqual(cleaned["login_session"], {"id": 1, "user": 1})
        self.assertEqual(HcContract.check_result("hello", cleaned), [])


class ConstantsTests(unittest.TestCase):
    def test_version_is_sha1_of_script(self):
        self.assertEqual(factory.VERSION, hashlib.sha1(SCRIPT.read_bytes()).hexdigest()[:12])


class ConfigTests(unittest.TestCase):
    @staticmethod
    def load_config(settings):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "factory.toml").write_text(settings)
            (root / "FACTORY.md").write_text("# Factory\n")
            return factory.Config(root)

    def test_new_keys_default(self):
        cfg = self.load_config("")
        self.assertEqual((cfg.quiet_grace, cfg.ready_check, cfg.ready_every), (120, None, 300))

    def test_new_keys_accepted(self):
        cfg = self.load_config('quiet_grace = 30\nready_check = "printf todo"\nready_check_every = 60\n')
        self.assertEqual((cfg.quiet_grace, cfg.ready_check, cfg.ready_every), (30, "printf todo", 60))

    def test_invalid_new_keys_are_rejected(self):
        for key, values in {
            "quiet_grace": ("false", "29", "30.5", '"30"'),
            "ready_check": ('""', '"  "', "true", "30"),
            "ready_check_every": ("true", "59", "60.5", '"60"'),
        }.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaisesRegex(SystemExit, rf"factory\.toml: {key}"):
                    self.load_config(f"{key} = {value}\n")

    def test_unknown_top_level_key_still_rejected(self):
        with self.assertRaisesRegex(SystemExit, r"unknown top-level key.*ready_checks"):
            self.load_config("ready_checks = 10\n")

    def test_current_sample_uses_default_ready_check_settings(self):
        cfg = self.load_config((factory.SKILL / "templates/factory.toml").read_text())
        self.assertEqual((cfg.quiet_grace, cfg.ready_check, cfg.ready_every), (120, None, 300))

    @staticmethod
    def files(root):
        return {str(p.relative_to(root)): ("symlink", os.readlink(p)) if p.is_symlink() else p.read_bytes()
                for p in root.rglob("*") if p.is_file() or p.is_symlink()}

    def test_dangling_links_stop_without_writes(self):
        brief = formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]}, operator={"harness": "claude"})
        for name in ("FACTORY.md", "factory.toml", "factory.toml.sample"):
            with self.subTest(name=name), Env(brief) as e:
                link = e.repo / name
                link.unlink(missing_ok=True)
                target = e.root / "missing target"
                link.symlink_to(target)
                before = self.files(e.repo)
                commands = ("init",) if name == "factory.toml.sample" else ("init", "check", "up")
                for command in commands:
                    result = e.run(command, check=False)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(f"{link} is a symbolic link to a missing file", result.stderr)
                    self.assertIn("Repair or remove the link.", result.stderr)
                    self.assertEqual(self.files(e.repo), before)
                    self.assertFalse(target.exists())
                if name == "factory.toml.sample":
                    e.run("check")
                    self.assertEqual(self.files(e.repo), before)
                    self.assertFalse(target.exists())

    def test_valid_formation_link_keeps_its_target(self):
        brief = formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]}, operator={"harness": "claude"})
        with Env(brief) as e:
            link = e.repo / "factory.toml"
            target = e.repo / "settings.toml"
            link.rename(target)
            link.symlink_to(target.name)
            before = self.files(e.repo)
            e.run("check")
            e.run("brief")
            self.assertEqual(self.files(e.repo), before)

    @unittest.skipUnless(GIT, "git is not installed")
    def test_parent_formation_is_a_marker_but_a_sample_is_not(self):
        for marker in ("factory.toml", "factory.toml.sample"):
            with self.subTest(marker=marker), Env(brief=None) as e:
                (e.root / "bin/git").symlink_to(GIT)
                (e.repo / marker).write_text("# parent file\n")
                nested = e.repo / "sub"
                subprocess.run([GIT, "init", "-q", str(nested)], check=True, capture_output=True)
                e.run("init", cwd=nested)
                chosen = e.repo if marker == "factory.toml" else nested
                other = nested if chosen == e.repo else e.repo
                self.assertTrue((chosen / "FACTORY.md").is_file())
                self.assertFalse((other / "FACTORY.md").exists())
                self.assertEqual((e.repo / marker).read_text(), "# parent file\n")

    def test_published_text_has_no_earlier_names(self):
        paths = [factory.SKILL / "SKILL.md", factory.SKILL / "harnesses.toml", factory.SKILL / "scripts/factory.py",
                 factory.SKILL.parent / "README.md"]
        paths.extend(factory.SKILL.parent / "docs" / name for name in ("README.md", "getting-started.md"))
        for directory in ("references", "roles", "templates", "recipes"):
            paths.extend(p for p in (factory.SKILL / directory).iterdir() if p.is_file())
        for path in paths:
            text = path.read_text()
            for name in ("TEAM.md", "team.toml", "team-brief", "a-team", "ateam.py", "ATEAM_"):
                self.assertNotIn(name, text, str(path))
        self.assertIn("name: factory\n", (factory.SKILL / "SKILL.md").read_text())
        self.assertTrue((factory.SKILL / "templates/FACTORY.md").read_text().startswith("# Factory\n"))

    def test_todo_item_format(self):
        first = "- [x] (1) First\n    * verdict: approved"
        second = "- [ ] (2) Second"
        assert_todo_items(self, first + "\n\n" + second)
        assert_todo_items(self, "## Ready\n\n" + second + "\n\n## Done\n\n" + first)
        for bad in (first + "\n" + second, first + "\n\n\n" + second, second + "\n\n    * detail",
                    first + "\n  * misindented detail", first + "\n    - wrong marker",
                    "- [ ] Title", "- [ ] (0) Title", "- [ ] (2)", "- [ ] Title (2)", "- [ ](2) Title",
                    "## Ready\n\n- [ ] (1) Duplicate\n\n## Done\n\n" + first):
            with self.subTest(text=bad), self.assertRaises(AssertionError):
                assert_todo_items(self, bad)

    def test_template_is_consistent(self):
        with Env(TEMPLATE) as e:
            cfg = factory.Config(e.repo)
            for name in cfg.members:
                p = factory.plan(cfg, name)
                self.assertIn(p["harness"], factory.HARNESSES, name)
                self.assertIn(p["model"], factory.TABLE[p["harness"]]["models"], name)  # the template uses recommended models
                self.assertEqual(p["permissions"], "full")
            self.assertEqual(cfg.operator["name"], "operator")
            self.assertTrue({"harness", "model", "thinking"}.isdisjoint(cfg.operator))
            self.assertIn("tracked in `TODO.md`", cfg.body)  # where tasks live is prose in FACTORY.md, not a setting
            todo = (factory.SKILL / "templates/TODO.md").read_text().splitlines()
            self.assertEqual(todo[:2], ["TODO", "===="])
            headings = ["## Ready", "## In progress", "## Backlog", "## Done"]
            self.assertEqual([line for line in todo if line.startswith("## ")], headings)
            tasks = cfg.body.split("## Tasks\n", 1)[1].split("\n## Workflow", 1)[0]
            for heading in headings:
                self.assertIn(heading, tasks)
            self.assertNotIn(" -- ", tasks)
            example = re.search(r"```[^\n]*\n(.*?)\n```", tasks, re.S)
            self.assertIsNotNone(example)
            assert_todo_items(self, example.group(1))
            self.assertRegex(example.group(1), r"\n\n- \[ \] \([1-9][0-9]*\) [^\n]+\Z")  # the second example is title-only
            self.assertRegex(example.group(1), r"(?m)^        (?:\* |\d+\. )")
            self.assertNotIn("tracker", cfg.team)
            self.assertEqual(list(cfg.members), ["lead-dev", "reviewer", "pm"])
            self.assertEqual(cfg.members["lead-dev"]["model"], "gpt-6-astra")
            self.assertEqual(cfg.members["reviewer"]["model"], "gpt-6-luna")
            self.assertIn("## Project", cfg.body)
        self.assertFalse(any("/" in m for m in factory.TABLE["codex"]["models"]), "Codex takes bare model names")
        for h, t in factory.TABLE.items():  # every harness has the same shape, and code to go with it
            self.assertEqual(set(t["permissions"]), set(factory.LEVELS), h)
            self.assertIn(t["prompt"], ("file", "pointer", "message"), h)
            self.assertNotIn("operator_messages", t, h)
            self.assertIn(h, factory.CODE)

    def test_operator_table_and_name(self):
        team = '[members.lead-dev]\nharness = "codex"\nmodel = "gpt-6-luna"\n'
        stock = (factory.SKILL / "templates/FACTORY.md").read_text()  # the shared brief people really get
        with Env(team_brief('[operator]\nname = "boss"\nharness = "claude"\n' + team, stock)) as e:
            brief = e.run("brief").stdout
            self.assertIn("# Team brief (from @boss)", brief)
            self.assertIn("The operator assigns each step", brief)  # the shared brief names the role ...
            self.assertNotIn("@operator", brief)                   # ... never one person's operator
            e.write_brief(team_brief(team, "## Workflow\n\n{operator} and {name} stay as written.\n"))
            self.assertIn("{operator} and {name} stay as written.", e.run("brief").stdout)  # FACTORY.md is sent as it is
            e.write_brief(team_brief('[operator]\nname = "boss"\nharness = "claude"\n' + team, stock))
            prompt = e.run("brief", "lead-dev").stdout
            self.assertIn("led by its operator, @boss", prompt)  # roles and the member prompt follow the name
            self.assertNotIn("@operator", prompt)
            for settings, why in (('[operator]\nname = "Boss!"\n', "[operator] name 'Boss!' must be"),
                                  ('[operator]\nname = "lead-dev"\n', "not a member's name")):
                e.write_brief(team_brief(settings + team))
                result = e.run("check", check=False)
                self.assertEqual(result.returncode, 1, settings)
                self.assertIn(why, result.stderr)

    def test_unknown_top_level_keys_fail(self):
        for key in ("name", "harness", "model", "thinking", "notes", "typo"):
            brief = CATALOG.replace("+++\n", f'+++\n{key} = "misplaced"\n', 1)
            with self.subTest(key=key), Env(brief) as e:
                for command in ("check", "brief"):
                    result = e.run(command, check=False)
                    self.assertEqual(result.returncode, 1, command)
                    self.assertIn(f"{e.repo / 'factory.toml'}: unknown top-level key(s): ['{key}']", result.stderr)
                    self.assertIn("operator fields belong under [operator]", result.stderr)

    def test_member_requires_explicit_harness(self):
        with Env('+++\n[members.lead-dev]\nmodel = "gpt-6-luna"\n+++\n') as e:
            result = e.run("check", "lead-dev", check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("harness", result.stderr + result.stdout)

    def test_brief_and_formation_are_two_files(self):
        team = '[members.lead-dev]\nharness = "codex"\nmodel = "gpt-6-luna"\nworktree = true\n'
        with Env(team_brief(team, "# Team\n\n<!-- for the editor -->\n## Tasks\n\nGitHub Issues on o/r, with `gh issue`.\n"), git=True) as e:
            sent = e.run("brief").stdout
            self.assertIn("GitHub Issues on o/r", sent)       # FACTORY.md is the prose of the brief ...
            self.assertNotIn("\n# Team\n", sent)              # ... without its own title
            self.assertNotIn("for the editor", sent)           # ... and without its comments
            self.assertIn("branch `ada/lead-dev`", sent)       # worktree branches start with the user's name
            self.assertIn("The team brief says where tasks are tracked", e.run("brief", "lead-dev").stdout)
            e.write_brief(team_brief('user = "peter"\n' + team))
            self.assertEqual(factory.plan(factory.Config(e.repo), "lead-dev")["branch"], "peter/lead-dev")
            e.write_brief(team_brief(team))  # a VM or CI user name that someone else's machine may share: a note, not a failure
            generic = e.run("check", LOGNAME="root")
            self.assertIn("note: worktree branches will start with 'root'", generic.stderr)
            self.assertIn("Set `user` in factory.toml", generic.stderr)
            self.assertNotIn("note:", e.run("check").stderr)               # an ordinary system user name
            e.write_brief(team_brief('user = "root"\n' + team))
            self.assertNotIn("note:", e.run("check", LOGNAME="root").stderr)  # chosen in factory.toml
            e.write_brief(team_brief(team.replace("worktree = true\n", "")))
            self.assertNotIn("note:", e.run("check", LOGNAME="root").stderr)  # no branch depends on it
            e.write_brief(team_brief('user = "a b"\n' + team))
            self.assertIn("set `user`", e.run("check", check=False).stderr)
            (e.repo / "factory.toml").unlink()                    # a teammate's clone: the brief is there, the formation is not
            p = e.run("check", check=False)
            self.assertEqual(p.returncode, 1)
            self.assertIn("no factory.toml in", p.stderr)
            self.assertIn("factory.py init", p.stderr)

    def test_brief_validation(self):
        cases = [
            (team_brief('[members.wizard]\nharness = "claude"'), "no role 'wizard'"),
            (team_brief('[members.Bad]\nharness = "claude"\nrole = "pm"'), "role"),
            (team_brief('[tabs]\nteam = "crew"'), "unknown top-level key(s): ['tabs']"),
            (formation({"backend-dev-" + "2" * 25: {"role": "backend-dev", "harness": "codex"}}), "member"),
            (team_brief("members = [unclosed"), "factory.toml:"),
            (team_brief('[members.dev]\nrole = "backend-dev"\nharness = "codex"'), "role"),
        ]
        for brief, error in cases:
            with self.subTest(error=error), Env(brief) as e:
                result = e.run("check", "pm", check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)
        with Env(formation({
            "backend-dev": {"harness": "codex", "model": "gpt-6-luna"},
            "backend-dev-2": {"harness": "codex", "role": "backend-dev", "model": "gpt-6-luna"},
        })) as e:
            self.assertIn("@backend-dev-2", e.run("check", "backend-dev", "backend-dev-2").stdout)

    @unittest.skipUnless(GIT, "git is not installed")
    def test_init(self):
        with Env(brief=None) as e:
            (e.root / "bin/git").symlink_to(GIT)
            subprocess.run([GIT, "init", "-q", str(e.repo)], check=True, capture_output=True)
            out = e.run("init", cwd=e.repo / "sub").stdout     # in a repository: at its root
            self.assertEqual(sorted(p.name for p in e.repo.iterdir() if p.name != ".git"),
                             [".gitignore", "FACTORY.md", "factory.toml", "factory.toml.sample", "sub"])
            self.assertEqual((e.repo / "factory.toml").read_text(), (factory.SKILL / "templates/factory.toml").read_text())
            self.assertEqual((e.repo / ".gitignore").read_text(), "factory.toml\n")
            self.assertEqual(out.count("wrote"), 3)
            e.run("check", "lead-dev")                         # what init writes is a team that checks
            self.assertIn("`## Ready` holds, in the order they will be done, the items the operator may assign "
                          "without asking the user; only a person puts an item there or reorders it, never the team.",
                          e.run("brief").stdout)
            # a teammate: the shared files are there, edited; only their own factory.toml is missing
            (e.repo / "factory.toml.sample").write_text('[members.pm]\nharness = "pi"\nmodel = "z-ai/glm-5.3"\n')
            (e.repo / "FACTORY.md").write_text("# Team\n\nOurs.\n")
            (e.repo / "factory.toml").unlink()
            out = e.run("init").stdout
            self.assertEqual((out.count("kept"), out.count("wrote")), (2, 1))
            self.assertEqual(list(factory.Config(e.repo).members), ["pm"])        # copied from the project's sample
            self.assertEqual((e.repo / "FACTORY.md").read_text(), "# Team\n\nOurs.\n")
            self.assertEqual((e.repo / ".gitignore").read_text(), "factory.toml\n")  # listed once
            self.assertEqual(e.run("init").stdout.count("wrote"), 0)             # nothing is ever replaced
            self.assertNotIn("sets user", out)
            # the project's sample names a user: whose branches these are is said, not guessed
            (e.repo / "factory.toml.sample").write_text('user = "peter"\n[members.pm]\nharness = "pi"\nmodel = "z-ai/glm-5.3"\n')
            (e.repo / "factory.toml").unlink()
            out = e.run("init", LOGNAME="jane").stdout
            self.assertIn("factory.toml.sample sets user = 'peter'", out)
            self.assertIn("change `user` in factory.toml", out)
            # a later negation undoes the ignore line: what git really does decides
            (e.repo / ".gitignore").write_text("factory.toml\n!factory.toml\n")
            self.assertIn("added factory.toml", e.run("init").stdout)
            self.assertEqual((e.repo / ".gitignore").read_text(), "factory.toml\n!factory.toml\nfactory.toml\n")
            self.assertNotIn("added factory.toml", e.run("init").stdout)
            self.assertNotIn("already tracks", e.run("init").stdout)
            subprocess.run([GIT, "-C", str(e.repo), "add", "-f", "factory.toml"], check=True, capture_output=True)
            self.assertIn("git already tracks factory.toml", e.run("init").stdout)  # ignoring it does not untrack it
        with Env(brief=None) as e:
            e.run("init", cwd=e.repo / "sub")                  # no repository: where you are
            self.assertTrue((e.repo / "sub/factory.toml").is_file())

    def test_brief_preview_and_member_prompt(self):
        with Env() as e:
            text = e.run("brief").stdout
            self.assertIn("| **@product** | Claude Code, claude-opus-5-5, medium |", text)
            self.assertIn("No reply needed.", text)
            self.assertIn("factory.py say", text)
            self.assertNotIn("Works in", text)  # no worktrees on this team
            prompt = e.run("brief", "reviewer").stdout
            self.assertIn("run it only when the brief's Review section or the requester calls for the deep lane", prompt)
            self.assertIn(" NAME --file ", prompt)
            self.assertIn("/messages/reviewer/FILENAME", prompt)
            self.assertIn("Start a task only when @operator or the user assigns it to you; an item the brief or "
                          "tracker marks ready is not an assignment. A teammate's request for a review or advice "
                          "needs none.", prompt)
            self.assertIn("You are read-only", prompt)
            self.assertIn("build and run", prompt)  # read-only permits builds/tests
            self.assertIn("## Reports", prompt)
            learned = "learned: <what a later task would otherwise get wrong>"
            self.assertNotIn(learned, prompt)
            self.assertIn("resend only when asked", prompt)
            self.assertNotIn("herdr agent prompt", prompt)
            self.assertNotIn("{instructions}", prompt)
            notes = factory.SKILL / "references/notes.md"
            self.assertIn(f"read `{notes}` before your first task: it covers searching, writing and the "
                          "`learned:` line of a report", prompt)
            self.assertIn(learned, notes.read_text())
            self.assertIn("reviewer.md", prompt)
            self.assertNotIn("{operator}", prompt)

    def test_brief_operator_details_and_fallbacks(self):
        operator = {"harness": "claude", "model": "claude-opus-5-5", "thinking": "medium"}
        agent = "Claude Code, claude-opus-5-5, medium"
        notes = "Decides scope and architecture."
        for brief, expected in (
            (formation(CATALOG_MEMBERS, operator={**operator, "notes": notes}), f"{agent} | {notes}"),
            (formation(CATALOG_MEMBERS, operator=operator), f"{agent} | Operator."),
            (formation(CATALOG_MEMBERS, operator={**operator, "notes": ""}), f"{agent} | Operator."),
            (CATALOG, " | Operator."),
        ):
            with self.subTest(expected=expected), Env(brief) as e:
                self.assertIn(f"| **@operator** | {expected} |", e.run("brief").stdout)

    def test_every_role_has_the_same_four_parts(self):
        for path in sorted((factory.SKILL / "roles").glob("*.md")):
            _, body = factory.parse_role(path)
            heads = [line for line in body.splitlines() if line.startswith("**")]
            self.assertEqual(heads, ["**You own**", "**You output**", "**Done when**", "**Never**"], path.name)
            self.assertNotIn("LSP", body, path.name)  # nothing harness-specific in a role


    def test_bad_role_frontmatter_names_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "x.md"
            p.write_text("---\nthinking: low\noops\n---\nbody\n")
            with self.assertRaisesRegex(ValueError, "x.md:3"):
                factory.parse_role(p)


class ListingTests(unittest.TestCase):
    def test_listed_models_formats(self):
        pi = "provider  model  context\nopenrouter  z-ai/glm-5.3  1M\npax-vllm  nvidia/Q  262K\n"
        self.assertEqual(factory.listed_models(pi), ["openrouter/z-ai/glm-5.3", "pax-vllm/nvidia/Q"])
        self.assertEqual(factory.listed_models("openrouter/z-ai/glm-5.3\nollama/qwen3:8b\n"),
                         ["openrouter/z-ai/glm-5.3", "ollama/qwen3:8b"])
        omp = ("openrouter (2)\n┌──────┬─────┐\n│ model              │ context │\n├─────┼─────┤\n"
               "│ z-ai/glm-5.3       │      1M │\n│ z-ai/glm-5.3-flash │      1M │\n└─────┴─────┘\n"
               "ollama (1)\n┌─────┬─────┐\n│ model    │ context │\n├─────┼─────┤\n│ qwen3:8b │    128K │\n└─────┴─────┘\n")
        self.assertEqual(factory.listed_models(omp), ["openrouter/z-ai/glm-5.3", "openrouter/z-ai/glm-5.3-flash", "ollama/qwen3:8b"])


class E2EFixtureTests(unittest.TestCase):
    def test_local_tracker_fixture(self):
        command = e2e.ready_command(factory.SKILL)
        for empty, count in ((False, 3), (True, 0)):
            with self.subTest(empty=empty), tempfile.TemporaryDirectory() as directory:
                fixture = e2e.make_local(pathlib.Path(directory), factory.SKILL, empty=empty)
                self.assertEqual(e2e.git(fixture.origin, "rev-parse", "main").stdout.strip(), fixture.initial)
                self.assertGreater(e2e.test_run(fixture.root, ci=True), 0)
                self.assertFalse(any(e2e.accepts(fixture.root, item) for item in range(1, 4)))
                ready = subprocess.run(["sh", "-c", command], cwd=fixture.root, capture_output=True, text=True)
                self.assertEqual(ready.returncode, 0 if count else 1)
                self.assertEqual(len(ready.stdout.splitlines()), count)

    def test_empty_successful_suite_does_not_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            e2e.write_project(root)
            (root / "tests/test_calc.py").unlink()
            self.assertEqual(e2e.test_run(root), 0)
            self.assertEqual(e2e.test_run(root, ci=True), 0)


class E2EJudgeTests(unittest.TestCase):
    revision = "a" * 40

    def message(self, ident, sender, recipient, text, committed, **fields):
        return e2e.Message(ident, sender, recipient, text, committed,
                           **{"delivered": committed + 1, "outcome": "delivered", **fields})

    def normal(self, github=False):
        names = ("#31", "#32", "#33") if github else ("(1)", "(2)", "(3)")
        first, second, _ = names
        messages = [
            self.message(1, "operator", "lead-dev", f"Assign {first}", 10, terminal="t2", turn=3, started=True),
            self.message(2, "factory", "operator", "team quiet", 15, kind="note", meta={"what": "quiet"}),
            self.message(3, "operator", "lead-dev", f"What remains on {first}?", 17),
            self.message(4, "operator", "lead-dev", f"Assign {second}", 50, terminal="t2", turn=5, started=True),
            self.message(5, "factory", "operator", "@lead-dev was killed", 55,
                         kind="note", meta={"what": "exit", "term": "t2"}),
            self.message(6, "operator", "lead-dev", "Pick up your work", 60, kind="brief", terminal="t4"),
        ]
        tickets = []
        main_code = set()
        for n, item in enumerate(names):
            revision = chr(ord("a") + n) * 40
            offset = n * 50
            introduced = frozenset({revision, f"earlier-{n}"})
            main_code.update(introduced)
            tickets.append(e2e.Ticket(item, planned=2 + offset, changed=7 + offset, revision=revision,
                                      checked_revision=revision, test_count=2, timely_check=True,
                                      integrated=42 + offset, branch_proof=True, introduced=introduced,
                                      accepted=True, closed=43 + offset, state="Done"))
            messages.append(self.message(7 + n, "reviewer", "operator",
                                         f"status: approve; item {item}; commit {revision}", 40 + offset))
        return e2e.Evidence(tuple(tickets), tuple(messages), frozenset(main_code), ci_count=2, loaded=True, prompts=1,
                            within_budget=True, interrupt=e2e.Fault(first, "t2", 3, 11, 13, 1080),
                            kill=e2e.Fault(second, "t2", 5, 51, 53, 3700), recovered={"t4": 57},
                            github=github, scoped=True)

    def status(self, evidence, name, item=None):
        return next(r["status"] for r in e2e.judge(evidence) if r["name"] == name and r.get("item") == item)

    def test_all_normal_rows_pass_with_multi_commit_reviewed_branch(self):
        for github in (False, True):
            with self.subTest(github=github):
                self.assertTrue(all(r["status"] == "pass" for r in e2e.judge(self.normal(github))))

    def test_each_ticket_needs_plan_approval_check_integration_and_closure(self):
        sample = self.normal()
        for label, changes in (
            ("planned", {"planned": 8}), ("checked", {"test_count": 0}),
            ("checked", {"checked_revision": "b" * 40}), ("integrated", {"branch_proof": False}),
            ("integrated", {"accepted": False}), ("closed", {"closed": 35}),
            ("closed", {"state": "In progress"}),
        ):
            with self.subTest(label=label, changes=changes):
                tickets = (replace(sample.tickets[0], **changes), *sample.tickets[1:])
                self.assertEqual(self.status(replace(sample, tickets=tickets), label, "(1)"), "fail")
        original = sample.messages[6]
        for changes in ({"text": original.text.replace("approve", "changes required")},
                        {"text": original.text.replace("(1)", "(2)")},
                        {"text": original.text.replace(self.revision, "d" * 40)},
                        {"text": original.text + "\nChanges required before integration."}, {"delivered": 43}):
            with self.subTest(changes=changes):
                messages = (*sample.messages[:6], replace(original, **changes), *sample.messages[7:])
                self.assertEqual(self.status(replace(sample, messages=messages), "reviewed", "(1)"), "fail")

    def test_reviewer_can_send_a_formatted_approval_to_the_developer(self):
        sample = self.normal()
        for status in ("approve", "approved"):
            verdict = replace(sample.messages[6], recipient="lead-dev",
                              text=f"- **Status:** `{status}` for item `(1)`. The reviewed commit is `{self.revision}`."
                                   "\nNo changes required.")
            messages = (*sample.messages[:6], verdict, *sample.messages[7:])
            self.assertEqual(self.status(replace(sample, messages=messages), "reviewed", "(1)"), "pass")

    def test_run_rows_reject_direct_code_missing_ci_extra_input_and_wrong_build(self):
        sample = self.normal()
        for name, changes in (
            ("no-direct-code", {"main_code": sample.main_code | {"direct"}}), ("ci-green", {"ci_count": 0}),
            ("one-prompt", {"prompts": 2}), ("one-prompt", {"factory_commands": 1}),
            ("loaded-version", {"loaded": False}), ("within-budget", {"within_budget": False}),
            ("no-unconfirmed", {"messages": (replace(sample.messages[0], outcome="unconfirmed"), *sample.messages[1:])}),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.status(replace(sample, **changes), name), "fail")
        github = self.normal(github=True)
        self.assertEqual(self.status(replace(github, scoped=False), "scoped-writes"), "fail")
        tickets = (replace(github.tickets[0], timely_check=False), *github.tickets[1:])
        self.assertEqual(self.status(replace(github, tickets=tickets), "checked", "#31"), "fail")

    def test_interrupt_needs_silent_turn_and_item_specific_chase_after_note(self):
        sample = self.normal()
        chase = sample.messages[2]
        for messages in ((sample.messages[:2] + sample.messages[3:]),
                         (sample.messages[:2] + (replace(chase, text="Please review (2)"),) + sample.messages[3:]),
                         (sample.messages[:2] + (replace(chase, delivered=14),) + sample.messages[3:])):
            self.assertEqual(self.status(replace(sample, messages=messages), "quiet-then-chase"), "fail")
        for committed in (12, 14):
            report = self.message(12, "lead-dev", "operator", "I sent my progress on (1)", committed, delivered=None)
            self.assertEqual(self.status(replace(sample, messages=(*sample.messages, report)), "quiet-then-chase"),
                             "not_run")
        self.assertEqual(self.status(replace(sample, interrupt=None), "quiet-then-chase"), "not_run")

    def test_exit_note_and_replacement_must_match_the_kill(self):
        sample = self.normal()
        for changes in ({"recovered": {}}, {"recovered": {"t4": 50}},
                        {"messages": (*sample.messages[:4], replace(sample.messages[4], meta={"what": "exit", "term": "x"}),
                                      *sample.messages[5:])},
                        {"messages": (*sample.messages[:5], replace(sample.messages[5], terminal="t2"), *sample.messages[6:])}):
            self.assertEqual(self.status(replace(sample, **changes), "exit-then-up"), "fail")

    def test_empty_board_requires_silence_and_pickup_of_the_moved_item(self):
        sample = e2e.Evidence((e2e.Ticket("(1)", state="In progress"),), (
            self.message(1, "factory", "operator", "team quiet", 2, kind="note", meta={"what": "quiet"}),
            self.message(2, "factory", "operator", "ready work:\n- [ ] (1) Fix calc", 12, kind="note",
                         meta={"what": "ready"}),
            self.message(3, "operator", "lead-dev", "Assign (1)", 14),
        ), loaded=True, prompts=1, within_budget=True, empty=True, moved=10, quiet_seconds=900, ready_seconds=63)
        self.assertTrue(all(r["status"] == "pass" for r in e2e.judge(sample)))
        for what, name in (("quiet", "one-quiet-note"), ("error", "silence")):
            other = self.message(4, "factory", "operator", what, 5, kind="note", meta={"what": what}, delivered=None)
            self.assertEqual(self.status(replace(sample, messages=(*sample.messages, other)), name), "fail")
        self.assertEqual(self.status(replace(sample, quiet_seconds=899), "silence"), "fail")
        self.assertEqual(self.status(replace(sample, ready_seconds=121), "ready-note"), "fail")
        note = replace(sample.messages[1], text="ready work:\n- [ ] (2) Fix calc")
        self.assertEqual(self.status(replace(sample, messages=(sample.messages[0], note, sample.messages[2])),
                                     "ready-note"), "fail")
        other = replace(sample.messages[2], text="Assign (2)")
        self.assertEqual(self.status(replace(sample, messages=(*sample.messages[:2], other)), "item-taken", "(1)"), "fail")
        waiting = replace(sample.tickets[0], state="Ready")
        self.assertEqual(self.status(replace(sample, tickets=(waiting,)), "item-taken", "(1)"), "fail")

    def test_triggers_correlate_receipt_to_the_observed_working_turn(self):
        sample = self.normal()
        first, second, _ = sample.tickets
        term = {"address": "t2", "state": "live", "turn": 3, "status": {"state": "working"}}
        receipt = sample.messages[0]
        self.assertTrue(e2e.should_interrupt(receipt, first, term, 3))
        for msg, terminal, turn, options in (
            (receipt, term, 2, {}), (receipt, {**term, "turn": 4}, 3, {}),
            (replace(receipt, terminal="t3"), term, 3, {}), (replace(receipt, kind="brief"), term, 3, {}),
            (replace(receipt, text="@reviewer joined"), term, 3, {}), (replace(receipt, started=False), term, 3, {}),
            (receipt, term, 3, {"fired": True}), (receipt, term, 3, {"reported": True}),
        ):
            self.assertFalse(e2e.should_interrupt(msg, first, terminal, turn, **options))
        term["turn"] = 5
        receipt = sample.messages[3]
        self.assertTrue(e2e.should_kill(receipt, second, term, 5, first.integrated))
        for options in ({"fired": True}, {"pending": True}):
            self.assertFalse(e2e.should_kill(receipt, second, term, 5, first.integrated, **options))
        self.assertFalse(e2e.should_kill(receipt, second, term, 5, receipt.delivered))


class E2EObserverTests(unittest.TestCase):
    def test_observation_uses_only_log_and_core_reads(self):
        state = pathlib.Path("/scratch/factory-skill/team/core")
        idle = {"address": "t5", "state": "live", "turn": 4, "created_ms": 5, "status": {"state": "idle"},
                "labels": {"factory.dir": str(state), "factory.kind": "member", "factory.member": "lead-dev"}}
        working = {**idle, "status": {"state": "working"}}
        records = [
            {"t": "msg", "id": 1, "from": "operator", "to": ["lead-dev"], "kind": "say", "text": "Start (1)"},
            {"t": "ok", "id": 1},
            {"t": "out", "id": 1, "to": "lead-dev", "o": "sending", "term": "t5"},
            {"t": "out", "id": 1, "to": "lead-dev", "o": "delivered", "term": "t5", "turn": 4, "started": True},
        ]
        log = types.SimpleNamespace(read=lambda offset: ([], 100) if offset == 0 else (records, 400))
        states = iter((idle, working))

        def read_core(method):
            self.assertEqual(method, "terminal.list")
            return {"terminals": [next(states)]}

        evidence = e2e.Evidence((e2e.Ticket("(1)"),))
        obs = e2e.Observer(types.SimpleNamespace(rpc=read_core), types.SimpleNamespace(open_log=lambda _: log),
                           state, evidence)
        with patch.object(e2e.subprocess, "run", side_effect=AssertionError("observer ran a command")):
            obs.collect()
            obs.collect()
        receipt, = evidence.messages
        self.assertEqual(obs.before_claim[(1, "lead-dev")], 4)
        self.assertEqual(obs.offset, 400)
        self.assertTrue(e2e.should_interrupt(receipt, evidence.tickets[0], working, 4))

    def test_rehearsal_and_real_output_stay_distinct(self):
        parser = argparse.ArgumentParser()
        e2e.add_arguments(parser)
        real = parser.parse_args(["--tracker", "github", "--empty", "--operator", "codex"])
        fixture = parser.parse_args(["--tracker", "file", "--rehearse"])
        self.assertTrue(real.empty)
        self.assertEqual(real.operator, "codex")
        with patch.object(e2e, "rehearse", return_value={"mode": "fixture", "status": "pass"}) as rehearsal:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(e2e.run(real, None, None, None, None), 2)
                self.assertEqual(json.loads(output.getvalue())["mode"], "real")
            rehearsal.assert_not_called()
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(e2e.run(fixture, None, None, None, None), 0)
                self.assertEqual(json.loads(output.getvalue())["mode"], "fixture")
            rehearsal.assert_called_once()


class E2ELocalEvidenceTests(unittest.TestCase):
    def test_approval_must_precede_the_local_merge_not_just_its_push(self):
        for late in (False, True):
            with self.subTest(late=late), tempfile.TemporaryDirectory() as directory:
                repo = e2e.make_local(pathlib.Path(directory), factory.SKILL)
                e2e.git(repo.root, "worktree", "add", "-b", "qa/lead-dev", str(repo.developer), repo.initial)
                tickets = tuple(e2e.Ticket(f"({n})") for n in range(1, 4))
                evidence = e2e.Evidence(tickets)
                counter = iter(range(1, 100))
                observer = types.SimpleNamespace(evidence=evidence, collect=lambda: None, stamp=counter.__next__)
                local = e2e.LocalEvidence(repo, observer)
                e2e.move_file_item(repo.root, "(1)", "In progress", "plan: clamp the value and cover its boundaries")
                local.collect()
                e2e.implement_fixture(repo.developer, 1)
                e2e.git(repo.developer, "add", "--", "calc", "tests")
                e2e.git(repo.developer, "commit", "-m", "Implement (1)")
                revision = e2e.git(repo.developer, "rev-parse", "HEAD").stdout.strip()
                local.collect()

                def send_verdict():
                    evidence.messages = (e2e.Message(1, "reviewer", "lead-dev",
                        f"**status:** approve for (1) at `{revision}`", observer.stamp(),
                        delivered=observer.stamp(), outcome="delivered"),)
                    local.collect()

                if not late:
                    send_verdict()
                e2e.git(repo.root, "merge", "--no-ff", "-m", "Integrate (1)", revision)
                local.collect()
                if late:
                    send_verdict()
                merge = e2e.git(repo.root, "rev-parse", "HEAD").stdout.strip()
                e2e.move_file_item(repo.root, "(1)", "Done", f"work: {merge}")
                e2e.git(repo.root, "push", "origin", "main")
                local.collect()
                first = evidence.tickets[0]
                self.assertEqual(first.branch_proof, not late)
                self.assertEqual(first.revision, "" if late else revision)
                self.assertEqual(first.test_count, 0 if late else 2)
                self.assertEqual(first.closed is None, late)


class SkillTextTests(unittest.TestCase):
    @staticmethod
    def pages():
        root = factory.SKILL.parent
        return [root / "README.md", *sorted((root / "docs").glob("*.md")),
                *sorted(factory.SKILL.rglob("*.md")), *sorted(factory.SKILL.rglob("*.toml"))]

    @classmethod
    def examples(cls):
        invocation = re.compile(r"^((?:python3\s+)?(?:\S+/)?factory\.py|/factory)\s+([a-z][a-z-]*)\b(.*)")
        for page in cls.pages():
            fenced = False
            for line in page.read_text().splitlines():
                if line.startswith("```"):
                    fenced = not fenced
                    continue
                spans = [line.split(" #", 1)[0].strip()] if fenced else re.findall(r"`([^`]+)`", line)
                for span in spans:
                    if match := invocation.match(span):
                        yield page, *match.groups()

    @staticmethod
    def help_text(*args):
        return subprocess.run([sys.executable, str(SCRIPT), *args, "--help"], text=True,
                              capture_output=True, check=True).stdout

    def test_no_removed_commands_or_stale_instructions(self):
        removed = re.compile(
            r"\bherdr\b(?!-(?:core|app)\b)|\bHERDR_\w+|(?<!\w)(?:factory\.py|/factory)\s+(?:wait|prompt|continue)\b"
            r"|(?<!\w)--(?:seen|restart|no-brief)\b|\bv1\b", re.IGNORECASE)
        banner = ("> **This checkout is Factory for herdr-core.** Until v2 ships, install the current release from "
                  "the [`v1` branch](https://github.com/pkieltyka/factory/tree/v1) (tag `v1.0.0`).")
        for page in self.pages():
            for number, line in enumerate(page.read_text().splitlines(), 1):
                if page == factory.SKILL.parent / "README.md" and line == banner:
                    continue
                prose = re.sub(r"https?://[^\s)>`]+", "", line)
                self.assertIsNone(removed.search(prose), f"{page}:{number}: {line}")

    def test_commands_named_in_the_text_exist(self):
        commands = set(re.search(r"\{([a-z,-]+)\}", self.help_text())[1].split(","))
        private = {"keeper", "init", "harnesses", "check"}
        for page, invocation, command, _ in self.examples():
            with self.subTest(page=page.name, invocation=invocation, command=command):
                supported = (commands - private) | {"setup"} if invocation == "/factory" else commands
                self.assertIn(command, supported)

    def test_flags_named_in_the_text_exist(self):
        options = {}
        for page, _, command, tail in self.examples():
            flags = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", tail))
            if not flags:
                continue
            if command not in options:
                options[command] = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", self.help_text(command)))
            self.assertFalse(flags - options[command], f"{page}: {command} {tail}")

    def test_status_words_in_leading_are_the_real_ones(self):
        leading = (factory.SKILL / "references/leading.md").read_text()
        table = leading.split("| Status | Action |", 1)[1].split("\n\n", 1)[0]
        states = {"not started", "starting", "idle", "working", "done", "blocked", "error", "gone"}
        hints = {"first brief N is still", "no confirmed brief", "no pane", "operator: not reachable",
                 "keeper: not running"}
        words = {word for row in table.splitlines() if row.startswith("|")
                 for word in re.findall(r"`([^`]+)`", row.split("|", 2)[1])}
        self.assertEqual(words, states | hints)
        source = inspect.getsource(factory.cmd_status) + inspect.getsource(factory._pending_brief_line)
        for hint in hints - {"first brief N is still"}:
            self.assertIn(hint, source)
        self.assertIn("first brief {", source)

    def test_note_table_matches_the_keeper(self):
        leading = (factory.SKILL / "references/leading.md").read_text()
        table = leading.split("| Note | Action |", 1)[1].split("\n\n", 1)[0]
        names = {name.removeprefix("@x ") for row in table.splitlines() if row.startswith("|")
                 for name in re.findall(r"`([^`]+)`", row.split("|", 2)[1])}
        producers = {
            factory.Keeper._episode: {"is blocked", "hit an error"},
            factory.end_cause: {"exited", "was killed", "hc stopped", "was lost", "its harness ended"},
            factory.quiet_text: {"team quiet"},
            factory.Keeper._ready_result: {"ready work"},
        }
        self.assertEqual(names, set().union(*producers.values()))
        for producer, phrases in producers.items():
            for phrase in phrases:
                self.assertIn(phrase, inspect.getsource(producer), producer.__name__)

    def test_outcome_words_are_the_real_ones(self):
        diagnostics = {"nothing sent", "not stored", "not confirmed stored"}
        for name in ("references/leading.md", "templates/member-prompt.md"):
            page = (factory.SKILL / name).read_text()
            for word in set(factory.OUTCOMES) | diagnostics:
                self.assertIn(f"`{word}`", page, name)
        for diagnostic in diagnostics:
            self.assertIn(diagnostic, inspect.getsource(factory.cmd_say))

    def test_template_formation_has_no_active_autonomy_key(self):
        sample = (factory.SKILL / "templates/factory.toml").read_text()
        before_tables = sample.split("\n[operator]", 1)[0]
        parsed = tomllib.loads(sample)
        for key in ("quiet_grace", "ready_check", "ready_check_every"):
            with self.subTest(key=key):
                self.assertNotIn(key, parsed)
                self.assertRegex(before_tables, rf"(?m)^# {key} = ")

    def test_template_ready_check_command(self):
        sample = (factory.SKILL / "templates/factory.toml").read_text()
        setting = next(line[2:] for line in sample.splitlines() if line.startswith("# ready_check = "))
        command = tomllib.loads(setting)["ready_check"]
        template = (factory.SKILL / "templates/TODO.md").read_text()
        item = "- [ ] (7) Check the ready queue"
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "TODO.md"
            for contents, code, output in (
                (None, 2, ""),
                (template, 1, ""),
                (template.replace("## Backlog", f"## Backlog\n\n{item}"), 1, ""),
                (template.replace("## Ready", f"## Ready\n\n{item}"), 0, item + "\n"),
            ):
                if contents is not None:
                    path.write_text(contents)
                with self.subTest(code=code, contents=contents):
                    result = subprocess.run(["sh", "-c", command], cwd=directory, text=True, capture_output=True)
                    self.assertEqual((result.returncode, result.stdout), (code, output))

    def test_template_workflow_is_the_spec_default(self):
        spec = factory.SKILL.parent / "specs/projects/factory/components/cli_skill.md"
        default = spec.read_text().split("Default `## Workflow` for `templates/FACTORY.md`:", 1)[1]
        expected = default.split("```markdown", 1)[1].split("```", 1)[0].strip()
        brief = (factory.SKILL / "templates/FACTORY.md").read_text()
        workflow = brief.split("## Workflow", 1)[1].split("## Notes", 1)[0].strip()
        self.assertEqual(workflow, expected)
        self.assertIn("Only @pm writes the tracker when present. Without @pm, the operator writes it.", workflow)

    def test_team_brief_uses_pane_titles(self):
        with Env() as env:
            brief = env.run("brief").stdout
        self.assertIn("each in its own pane", brief)
        self.assertIn("their pane's title", brief)


class DocumentationTests(unittest.TestCase):
    def test_relative_links_and_headings(self):
        root = factory.SKILL.parent
        pages = [root / "README.md", *sorted((root / "docs").glob("*.md")), factory.SKILL / "SKILL.md"]
        pages += sorted((factory.SKILL / "references").glob("*.md"))
        pages += sorted((factory.SKILL / "recipes").glob("*.md"))
        for page in pages:
            for href in re.findall(r"\]\(([^\s)]+)\)", page.read_text()):
                link = urlsplit(href)
                if link.scheme or link.netloc:
                    continue
                with self.subTest(page=page.name, href=href):
                    target = page.parent / unquote(link.path) if link.path else page
                    self.assertTrue(target.exists(), str(target))
                    if link.fragment:
                        document = target / "README.md" if target.is_dir() else target
                        headings = re.findall(r"^#{1,6} (.+)$", document.read_text(), re.MULTILINE)
                        anchors = [re.sub(r"[^\w -]", "", h.lower()).replace(" ", "-") for h in headings]
                        self.assertIn(unquote(link.fragment), anchors)


class RecipeTests(unittest.TestCase):
    def test_recipes_are_pasteable_sections_linked_from_the_map(self):
        setup = (factory.SKILL / "references/cmd_setup.md").read_text()
        overview = (factory.SKILL.parent / "docs/README.md").read_text()
        recipes = sorted((factory.SKILL / "recipes").glob("*.md"))
        self.assertTrue(recipes)
        for path in recipes:
            with self.subTest(recipe=path.name):
                headings, blocks, block = [], [], None
                for line in path.read_text().splitlines():
                    if line.startswith("```"):
                        if block is None:
                            self.assertEqual(headings[-1], "## FACTORY.md section")
                            block = []
                        else:
                            blocks.append(block)
                            block = None
                    elif block is not None:
                        block.append(line)
                    elif line.startswith("#"):
                        headings.append(line)
                self.assertIsNone(block, "unclosed fence")
                self.assertEqual(len(blocks), 1)
                self.assertTrue(blocks[0][0].startswith("## "))
                section = blocks[0][0]
                self.assertIn(f"`{section}`", setup)
                self.assertTrue(headings[0].startswith(f"# {section[3:]}: "))
                expected = ["## Needs", "## Check", "## FACTORY.md section"]
                if path.name in ("tasks-gh.md", "tasks-todos.md"):
                    expected.append("## Ready check")
                    ready_check = path.read_text().split("## Ready check", 1)[1].split("## Tested with", 1)[0]
                    commands = [span for span in re.findall(r"`([^`]+)`", ready_check)
                                if span.startswith(("board=", "ids="))]
                    self.assertEqual(len(commands), 1)
                self.assertEqual(headings[1:], [*expected, "## Tested with"])
                self.assertIn(f"(../skill/recipes/{path.name})", overview)


class CacheNameTests(unittest.TestCase):
    def test_new_projects_use_factory_cache_with_xdg_or_home(self):
        with Env() as e:
            for xdg in (str(e.root / "cache"), ""):
                with self.subTest(xdg=xdg), patch.dict(os.environ, {**e.env, "XDG_CACHE_HOME": xdg}):
                    state = factory.state_dir(factory.Config(e.repo).root, "core0000aaaa")
                    base = pathlib.Path(xdg) if xdg else e.home / ".cache"
                    self.assertEqual(state.parent.parent, base / "factory-skill")
                    self.assertEqual((state.parent.name, state.name),
                                     (factory.team_key(factory.Config(e.repo).root), "core0000"))
                    self.assertFalse(state.exists())


class InstalledCopyTests(unittest.TestCase):
    def test_skill_runs_without_the_rest_of_the_checkout(self):
        head = (factory.SKILL / "SKILL.md").read_text().split("---\n", 2)[1]
        name = next(line.split(":", 1)[1].strip() for line in head.splitlines() if line.startswith("name:"))
        with Env(brief=None) as e:
            installed = e.root / "installed skills" / name
            shutil.copytree(factory.SKILL, installed, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            script = installed / "scripts" / SCRIPT.name

            def run(*args):
                result = subprocess.run([sys.executable, str(script), *args, "--cwd", str(e.repo)],
                                        cwd=e.repo, env=e.env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn(str(factory.SKILL), result.stdout + result.stderr)
                return result.stdout

            harnesses = run("harnesses")
            self.assertRegex(harnesses, r"codex\s+available: gpt-6\.2-astra, gpt-6\.2-sol,")
            for harness in factory.HARNESSES:
                self.assertRegex(harnesses, rf"{harness}\s+available:")
            run("init")
            self.assertEqual((e.repo / factory.BRIEF).read_text(),
                             (installed / "templates" / factory.BRIEF).read_text())
            self.assertEqual((e.repo / factory.CONF).read_text(),
                             (installed / "templates" / factory.CONF).read_text())
            self.assertIn("ok", run("check"))
            prompt = run("brief", "reviewer")
            self.assertIn("You are @reviewer, the reviewer", prompt)
            self.assertIn("You are read-only", prompt)
            self.assertIn(str(installed / "references/notes.md"), prompt)


@unittest.skipUnless(GIT, "git is not installed")
class GateFixtureTests(unittest.TestCase):
    def test_each_build_is_fresh_and_preserves_existing_fixtures_and_caches(self):
        with tempfile.TemporaryDirectory(prefix="gate1 test ") as directory:
            parent = pathlib.Path(directory)
            env = {**os.environ, "XDG_CACHE_HOME": str(parent / "cache"), "GIT_CONFIG_GLOBAL": os.devnull,
                   "GIT_CONFIG_NOSYSTEM": "1", "PYTHONDONTWRITEBYTECODE": "1"}
            unrelated = parent / "cache/factory-skill/factory-gate1-unrelated/sessions.jsonl"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text("unrelated session")
            fixtures, caches = [], []

            def run(*args, cwd=parent):
                return subprocess.run(args, cwd=cwd, env=env, check=True, capture_output=True, text=True)

            for _ in range(2):
                before = set(parent.glob("factory-gate1.*"))
                run("bash", str(pathlib.Path(__file__).with_name("gate1-setup.sh")), str(factory.SKILL), str(parent))
                created = set(parent.glob("factory-gate1.*")) - before
                self.assertEqual(len(created), 1)
                repo = created.pop()
                fixtures.append(repo)
                with patch.dict(os.environ, env):
                    cache = factory.state_dir(factory.Config(repo).root, "core0000").parent
                self.assertNotIn(cache, caches)
                self.assertFalse(cache.exists())
                caches.append(cache)
                self.assertEqual(run(GIT, "rev-list", "--count", "HEAD", cwd=repo).stdout.strip(), "3")
                self.assertIn("slug: a one-function", (repo / "FACTORY.md").read_text())
                todo = (repo / "TODO.md").read_text()
                self.assertEqual(todo.splitlines()[:2], ["TODO", "===="])
                self.assertEqual([line for line in todo.splitlines() if line.startswith("## ")],
                                 ["## Ready", "## In progress", "## Backlog", "## Done"])
                t1 = run(GIT, "rev-parse", "--short", "HEAD~1", cwd=repo).stdout.strip()
                self.assertNotIn(" -- ", todo)
                self.assertEqual([line for line in todo.splitlines() if line.startswith("- [")], [
                    "- [ ] (3) Command line",
                    "- [ ] (2) max_length (@lead-dev)",
                    f"- [x] (1) Slugify joins words ({t1})",
                    "- [x] (4) Strip accents (0f3a9c1)",
                ])
                assert_todo_items(self, todo)
                self.assertIn("    * verdict: changes required by @reviewer", todo)
                self.assertIn("        2. Add a test where max_length cuts right after a word.", todo)
                self.assertNotEqual(subprocess.run([GIT, "cat-file", "-e", "0f3a9c1"], cwd=repo,
                                                   env=env, capture_output=True).returncode, 0)
                self.assertEqual(set(run(GIT, "status", "--short", cwd=repo).stdout.splitlines()),
                                 {" M README.md", " M slug.py", "?? test_slug_length.py"})
                tests = run(sys.executable, "-m", "unittest", cwd=repo)
                self.assertIn("Ran 3 tests", tests.stderr)
                result = run(sys.executable, "-c",
                             'from slug import slugify; print(slugify("hello world", max_length=6))', cwd=repo)
                self.assertEqual(result.stdout.strip(), "hello-")
                cache.mkdir(parents=True)
                (cache / "sessions.jsonl").write_text("previous fixture session")
                (repo / "preserve-me").write_text("previous fixture")
            self.assertEqual(unrelated.read_text(), "unrelated session")
            for repo, cache in zip(fixtures, caches):
                self.assertEqual((repo / "preserve-me").read_text(), "previous fixture")
                self.assertEqual((cache / "sessions.jsonl").read_text(), "previous fixture session")


@unittest.skipUnless(os.environ.get("FACTORY_LIVE"), "set FACTORY_LIVE=1 (outside any sandbox) to check real harness flags")
class LiveFlags(unittest.TestCase):
    """Harness flags drift between releases. Each installed harness's --help must still name what we pass."""
    FLAGS = {
        "claude": (["--help"], ["--model", "--effort", "--append-system-prompt", "--name", "--settings", "--permission-mode",
                                '"auto"', '"bypassPermissions"', "--resume"]),
        "codex": (["--help"], ["--model", "--config", "--cd", "--approve-for-me", "--add-dir",
                               "--dangerously-bypass-approvals-and-sandbox", "resume"]),
        "pi": (["--help"], ["--model", "--thinking", "--append-system-prompt", "--name", "--session", "--list-models"]),
        "omp": (["--help"], ["--model", "--thinking", "--append-system-prompt", "--resume", "--approval-mode"]),
        "opencode": (["--help"], ["--standalone", "--session", "--auto", "models"]),
        "agy": (["--help"], ["--model", "--mode", "accept-edits", "--settings", "--conversation", "models"]),
    }

    @unittest.skipUnless(shutil.which("agy"), "agy is not installed")
    def test_agy_model_listing(self):
        self.assertTrue(factory.agy_models(), "agy models returned no tab-separated model IDs")

    def test_flags_exist(self):
        for harness, (argv, flags) in self.FLAGS.items():
            if not shutil.which(harness):
                continue
            # Claude's help can truncate when piped; a regular file preserves the full output.
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as output:
                p = subprocess.run([harness, *argv], stdout=output, stderr=subprocess.STDOUT, timeout=60)
                output.seek(0)
                help_text = output.read()
            self.assertEqual(p.returncode, 0, f"{harness} --help failed: {help_text}")
            for f in flags:
                self.assertIn(f, help_text, f"{harness} --help no longer mentions {f}")


if __name__ == "__main__":
    unittest.main()
