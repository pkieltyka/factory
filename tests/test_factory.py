"""Tests for factory.py with fake herdr and harnesses, plus an optional private hc core.

Two test classes touch real tools and skip when they cannot: HerdrContract reads the installed herdr's API
schema, and LiveFlags (FACTORY_LIVE=1, outside any sandbox) greps each installed harness's --help for the
flags factory.py passes."""
import importlib.util
import contextlib
from concurrent.futures import ThreadPoolExecutor
import errno
import hashlib
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import tomllib
import types
import unittest
import uuid
from urllib.parse import unquote, urlsplit
from unittest.mock import call, patch

if __package__:
    from . import fake_hc, live
else:
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

    def test_log_has_every_input(self):
        term, log = self.launch("silent")
        self.hc.send(term, "first\nsecond")
        self.hc.keys(term, ["enter"])
        self.hc.send(term, "third")
        self.until(lambda: self.inputs(term, log) == ["first\nsecond", "", "third"], "all input")
        self.assertTrue(all(entry["at"] > 0 for entry in map(json.loads, log.read_text().splitlines())))


FAKE_HERDR = textwrap.dedent(r'''
    #!/usr/bin/env python3
    # A herdr stand-in. Like herdr: `pane list` carries labels but no agent names, `agent list` carries names
    # but no labels; a pane keeps its label but loses its agent_session when its agent exits; a prompt to a
    # blocked agent is refused; errors are JSON on stderr with exit status 1.
    import json, os, sys, textwrap
    path = os.environ["FAKE_HERDR"]
    s = json.load(open(path))
    s["calls"].append(sys.argv[1:])
    a = sys.argv[1:]
    panes = s["panes"]
    def save(): json.dump(s, open(path, "w"))
    def out(result):
        save(); print(json.dumps({"id": "1", "result": result})); sys.exit(0)
    def fail(code, message=""):
        save(); print(json.dumps({"id": "1", "error": {"code": code, "message": message}}), file=sys.stderr); sys.exit(1)
    def find(target):
        return panes.get(target) or next((p for p in panes.values() if p.get("agent") and p.get("name") == target), None)
    def view(p, kind):
        hide = {"pane": ("name", "rect"), "agent": ("label", "rect")}[kind]
        return {k: v for k, v in p.items() if k not in hide}
    def opt(name):
        return a[a.index(name) + 1] if name in a else None
    def new_pane(tab, rect):
        s["next"] += 1
        pid = f"w1:p{s['next']}"
        panes[pid] = {"pane_id": pid, "tab_id": tab, "workspace_id": "w1", "label": None, "agent_status": "unknown",
                      "foreground_cwd": opt("--cwd"), "cwd": opt("--cwd"), "rect": rect, "env": [a[i + 1] for i, x in enumerate(a) if x == "--env"]}
        return panes[pid]
    if a == ["integration", "status"]:
        if os.environ.get("FAKE_INTEGRATION_FAIL"):
            fail("status_failed", "cannot read integration status")
        current = "\n".join(f"{h}: current (v1) (/hooks/{h})"
                            for h in ("claude", "codex", "pi", "omp", "opencode", "antigravity-cli"))
        save(); print(os.environ.get("FAKE_INTEGRATION_STATUS", current)); sys.exit(0)
    if a[:2] == ["agent", "list"]:
        if os.environ.get("FAKE_ROSTER_FAIL"):
            fail("roster_failed")
        change = s.get("on_second_poll")
        if change:
            s["polls"] = s.get("polls", 0) + 1
            if s["polls"] == 2:
                target = next(k for k, p in panes.items() if p.get("label") == change["name"])
                if change.get("close"):
                    del panes[target]
                elif change.get("exit"):
                    panes[target].update(agent=None, name=None, agent_status="unknown", agent_session=None)
                else:
                    panes[target]["agent_status"] = change["status"]

        out({"type": "agent_list", "agents": [view(p, "agent") for p in panes.values() if p.get("agent")]})
    if a[:2] == ["agent", "get"]:
        if os.environ.get("FAKE_GET_FAIL"):
            fail("PermissionDenied", "Operation not permitted: socket blocked")
        p = find(a[2])
        out({"type": "agent_info", "agent": view(p, "agent")}) if p and p.get("agent") else fail("agent_not_found")
    if a[:2] == ["agent", "rename"]:
        find(a[2])["name"] = a[3]; out({"type": "agent_info", "agent": view(find(a[3]), "agent")})
    if a[:2] == ["pane", "list"]:
        out({"type": "pane_list", "panes": [view(p, "pane") for p in panes.values()]})
    if a[:2] == ["pane", "get"]:
        p = panes.get(a[2])
        out({"type": "pane_info", "pane": view(p, "pane")}) if p else fail("pane_not_found")
    if a[:2] == ["pane", "close"]:
        if a[2] == os.environ.get("FAKE_CLOSE_FAIL"):
            fail("close_refused", "could not close pane")
        del panes[a[2]]
        change = s.pop("after_close", None)
        if change:
            panes[change["pane"]].update(change["fields"])
        out({"type": "pane_closed"})
    if a[:2] == ["pane", "rename"]:
        panes[a[2]]["label"] = None if a[3] == "--clear" else a[3]; out({"type": "pane_info", "pane": view(panes[a[2]], "pane")})
    if a[:2] == ["tab", "rename"]:
        s["tabs"][a[2]] = a[3]; out({"type": "tab_info", "tab": {"tab_id": a[2], "label": a[3]}})
    if a[:2] == ["tab", "create"]:
        tab = f"w1:t{len(s['tabs']) + 1}"
        s["tabs"][tab] = opt("--label")
        out({"type": "tab_created", "tab": {"tab_id": tab, "label": opt("--label")},
             "root_pane": view(new_pane(tab, {"x": 0, "y": 0, "width": 200, "height": 50}), "pane")})
    if a[:2] == ["pane", "split"]:
        r = panes[a[2]]["rect"]
        if opt("--direction") == "right":
            r["width"] //= 2; new = {**r, "x": r["x"] + r["width"]}
        else:
            r["height"] //= 2; new = {**r, "y": r["y"] + r["height"]}
        out({"type": "pane_info", "pane": view(new_pane(panes[a[2]]["tab_id"], new), "pane")})
    if a[:2] == ["pane", "layout"]:
        tab = panes[opt("--pane")]["tab_id"]
        out({"type": "pane_layout", "layout": {"tab_id": tab, "panes": [
            {"pane_id": p["pane_id"], "rect": p["rect"]} for p in panes.values() if p["tab_id"] == tab]}})
    if a[:2] == ["pane", "process-info"]:
        p = panes[opt("--pane")]
        procs = p.get("procs")  # a test's own foreground processes; else the harness as factory started it
        if procs is None:
            start = next((c for c in reversed(s["calls"]) if c[:2] == ["agent", "start"] and c[c.index("--pane") + 1] == p["pane_id"]), None)
            procs = [{"pid": 1, "name": p["agent"], "argv": [p["agent"], *start[start.index("--") + 1:]]}] if start and p.get("agent") else []
        out({"type": "pane_process_info", "process_info": {"pane_id": p["pane_id"], "foreground_processes": procs}})
    if a[:2] == ["pane", "read"]:
        asks = panes[a[2]].get("dialog", [""])[0]
        if width := int(os.environ.get("FAKE_DIALOG_WRAP", "0")):  # a narrow pane wraps the question
            asks = "\n".join(textwrap.wrap(asks, width))
        save(); print(f"...\n{asks}  [{a[2]}]"); sys.exit(0)  # plain text, like herdr
    if a[:2] == ["pane", "send-keys"]:
        if os.environ.get("FAKE_DIALOG_STUCK"):
            fail("input_failed", "keypress was not delivered")
        if panes[a[2]].pop("dialog", [0, 0])[1] == a[3:]:  # the keys that accept
            panes[a[2]]["agent_status"] = "idle"
        out({"type": "ok"})
    if a[:2] == ["agent", "start"]:
        name, kind, pane = a[2], opt("--kind"), opt("--pane")
        how = dict(x.split(":")[::-1] for x in os.environ.get("FAKE_START_FAIL", "").split(",") if x).get(name)
        if panes[pane].get("agent"):
            fail("pane_busy")
        if how == "dead":
            fail("timeout", "agent did not appear")
        panes[pane].update(agent=kind, name=name, agent_status="idle" if how in (None, "banner") else "blocked")
        if how == "dialog":  # a first-run question the harness table knows
            panes[pane]["dialog"] = {
                "codex": ["> 1. Trust and continue  2. Back", ["enter"]],
                "agy": ["Do you trust the contents of this project?\n> Yes, I trust this folder", ["enter"]],
            }.get(kind, ["> No, exit\n  Yes, I trust this folder", ["down", "enter"]])
            if os.environ.get("FAKE_DIALOG_TEXT"):  # a question the harness table does not know
                panes[pane]["dialog"][0] = os.environ["FAKE_DIALOG_TEXT"]
            if kind == "codex" or os.environ.get("FAKE_DIALOG_WRAP"):  # herdr 0.9.5 calls Codex, and agy in a
                panes[pane]["agent_status"] = how = "idle"  # narrow pane, ready while its trust question is up
        if how in ("blocked", "banner", "dialog"):  # a banner clears by itself: the agent is idle a moment later
            fail("agent_not_ready", "agent is waiting on a dialog")
        out({"type": "agent_started", "agent": view(panes[pane], "agent")})
    if a[:2] == ["agent", "prompt"]:
        p = find(a[2])
        if not p or not p.get("agent"):
            fail("agent_not_found")
        if p.get("label") == os.environ.get("FAKE_UNCONFIRMED"):
            out({})  # herdr returned without confirming the submission
        if p.get("agent_status") == "blocked" or p.get("label") in os.environ.get("FAKE_PROMPT_FAIL", "").split(","):
            fail("agent_blocked", "agent is waiting on an approval")
        out({"type": "agent_prompted", "agent": view(p, "agent")})
    fail("unexpected", " ".join(a))
''').lstrip()

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
    """A temp repo with a ten-role fixture brief by default, a fake herdr, fake harnesses,
    a Codex model cache, and the operator (a claude agent) in pane w1:p1 of tab w1:t1."""

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
        scripts = {"herdr": FAKE_HERDR, "pi": FAKE_PI, "omp": FAKE_OMP, "opencode": FAKE_OPENCODE, "agy": FAKE_AGY,
                   "claude": "#!/bin/sh\n", "codex": "#!/bin/sh\n"}
        for name, body in scripts.items():
            if name == "herdr" or name in harnesses:
                (bin_ / name).write_text(body)
                (bin_ / name).chmod(0o755)
        self.fake = self.root / "herdr.json"
        operator = {"pane_id": "w1:p1", "tab_id": "w1:t1", "workspace_id": "w1", "label": None, "agent": "claude",
                "name": None, "agent_status": "working", "rect": {"x": 0, "y": 0, "width": 200, "height": 50}}
        self.write_fake({"calls": [], "next": 1, "tabs": {"w1:t1": "1"}, "panes": {"w1:p1": operator}})
        self.env = {**os.environ, "HERDR_BIN": str(bin_ / "herdr"), "FAKE_HERDR": str(self.fake),
                    "HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1", "HERDR_TAB_ID": "w1:t1", "HERDR_WORKSPACE_ID": "w1",
                    "PATH": str(bin_), "HOME": str(self.home), "XDG_CACHE_HOME": str(self.root / "cache"),
                    "FAKE_MODELS": models, "FACTORY_SETTLE": "0.2"}
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

    def write_fake(self, s):
        self.fake.write_text(json.dumps(s))

    def fake_state(self):
        return json.loads(self.fake.read_text())

    def run(self, *args, check=True, cwd=None, **env):
        command = list(args)
        command[1:1] = ["--cwd", str(cwd or self.repo)]
        p = subprocess.run([sys.executable, str(SCRIPT), *command],
                           capture_output=True, text=True, env={**self.env, **env})
        if check and p.returncode:
            raise AssertionError(p.stderr + p.stdout)
        return p

    def say(self, sender, *args, **kwargs):
        return self.run("say", *args, HERDR_PANE_ID=self.pane_of(sender)["pane_id"], **kwargs)

    def calls(self, *prefix):
        return [c for c in self.fake_state()["calls"] if c[:len(prefix)] == list(prefix)]

    def starts(self):
        return {c[2]: c for c in self.calls("agent", "start")}

    def prompts(self):
        return [(c[2], c[3]) for c in self.calls("agent", "prompt")]

    def pane_of(self, name):
        return next(p for p in self.fake_state()["panes"].values() if p.get("label") == name)

    def update(self, pane_label, **fields):
        fs = self.fake_state()
        next(p for p in fs["panes"].values() if p.get("label") == pane_label).update(fields)
        self.write_fake(fs)

    def set_session(self, name, ref):
        self.update(name, agent_session=ref)

    def kill(self, *names):
        """Members' agents exit. Like herdr, their panes keep the label and lose the session reference;
        a command that looked at the team before (here: status) has noted the session."""
        self.run("status")
        fs = self.fake_state()
        for p in fs["panes"].values():
            if p.get("label") in names:
                p.update(agent=None, name=None, agent_status="unknown", agent_session=None)
        self.write_fake(fs)

    def close(self, *names):
        fs = self.fake_state()
        fs["panes"] = {k: p for k, p in fs["panes"].items() if p.get("label") not in names}
        self.write_fake(fs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.tmp.cleanup()


def native(call):
    return call[call.index("--") + 1:]


def flag(args, name):
    return args[args.index(name) + 1]


def kind(call):
    return flag(call, "--kind")


class UpTests(unittest.TestCase):
    def test_up_warns_once_for_each_integration_it_will_use(self):
        members = {n: CATALOG_MEMBERS[n] for n in ("lead-dev", "reviewer", "pm", "frontend-dev")}
        members["designer"] = {"harness": "agy"}
        status = textwrap.dedent("""\
            claude: outdated (v2 < v3) (/hooks/claude)
            codex: not installed (/hooks/codex)
            pi: current (v2) (/hooks/pi)
            opencode: needs repair (v13) (/hooks/opencode)
            antigravity-cli: needs repair (v4) (/hooks/antigravity-cli)
            """)
        with Env(formation(members)) as e:
            p = e.run("up", "--fresh", "--no-brief", FAKE_INTEGRATION_STATUS=status)
            self.assertEqual(set(e.starts()), set(members))
            self.assertEqual(p.stderr.count("Codex herdr integration not installed"), 1)
            self.assertIn("Claude Code herdr integration outdated (v2 < v3)", p.stderr)
            self.assertIn("Antigravity CLI herdr integration needs repair (v4)", p.stderr)
            for target in ("claude", "codex", "antigravity-cli"):
                self.assertIn(f"Ask the user before running `herdr integration install {target}`", p.stderr)
            self.assertNotIn("pi herdr integration", p.stderr)
            self.assertNotIn("OpenCode", p.stderr)
            e.run("up", "--no-brief", FAKE_INTEGRATION_STATUS=status)  # already running: no query
            self.assertEqual(e.calls("integration"), [["integration", "status"]])
            calls = e.calls()
            self.assertLess(calls.index(["integration", "status"]), calls.index(e.calls("pane", "rename")[0]))

    def test_up_integration_status_failure_and_unknown_rows_only_warn(self):
        brief = formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]})
        cases = (
            ({"FAKE_INTEGRATION_FAIL": "1"}, "could not check herdr integrations (status_failed:"),
            ({"FAKE_INTEGRATION_STATUS": "pi: current (v2) (/hooks/pi)"}, "could not verify the Codex herdr integration"),
            ({"FAKE_INTEGRATION_STATUS": "codex: new state (v2) (/hooks/codex)"}, "could not verify the Codex herdr integration"),
        )
        for env, warning in cases:
            with self.subTest(env=env), Env(brief) as e:
                p = e.run("up", "--no-brief", **env)
                self.assertIn(warning, p.stderr)
                self.assertIn("Run `herdr integration status` to verify", p.stderr)
                self.assertNotIn("integration install", p.stderr)
                self.assertEqual(e.calls("integration"), [["integration", "status"]])
                self.assertIn("lead-dev", e.starts())

    def test_ui_team_launches_in_the_team_tab_and_is_briefed(self):
        with Env(UI) as e:
            self.assertEqual(factory.Config(e.repo).operator["name"], "operator")  # the default name
            e.run("up", cwd=e.repo / "sub")  # the team's files are found from a subdirectory too
            fs = e.fake_state()
            self.assertEqual((fs["panes"]["w1:p1"]["name"], fs["panes"]["w1:p1"]["label"]), ("operator", "operator"))
            self.assertEqual(fs["tabs"], {"w1:t1": "factory operator", "w1:t2": "factory team"})  # the operator's tab, and one for the team
            tab = e.calls("tab", "create")
            self.assertEqual(len(tab), 1)
            self.assertEqual((flag(tab[0], "--cwd"), flag(tab[0], "--workspace")), (str(e.repo), "w1"))
            self.assertTrue(all("--no-focus" in c and flag(c, "--cwd") == str(e.repo) for c in tab + e.calls("pane", "split")))
            labels = {p["label"]: (k, p["tab_id"]) for k, p in fs["panes"].items()}
            self.assertEqual(labels, {"operator": ("w1:p1", "w1:t1"), "lead-dev": ("w1:p2", "w1:t2"), "frontend-dev": ("w1:p3", "w1:t2"),
                                      "reviewer": ("w1:p4", "w1:t2"), "pm": ("w1:p5", "w1:t2")})
            sizes = {(p["rect"]["width"], p["rect"]["height"]) for p in fs["panes"].values() if p["tab_id"] == "w1:t2"}
            self.assertEqual(sizes, {(100, 25)})  # four members: a 2 x 2 grid

            starts = e.starts()
            self.assertEqual({n: kind(c) for n, c in starts.items()},
                             {"lead-dev": "codex", "frontend-dev": "claude", "reviewer": "codex", "pm": "pi"})
            cache = e.root / "cache/factory-skill"
            for name in ("lead-dev", "reviewer"):  # Codex: never stops to ask, never hard read-only
                argv = native(starts[name])
                self.assertEqual(flag(argv, "-m"), {"lead-dev": "gpt-6-astra", "reviewer": "gpt-6-luna"}[name])
                self.assertEqual(argv[-1], "--dangerously-bypass-approvals-and-sandbox")
                pointer = tomllib.loads(next(v for v in argv if v.startswith("developer_instructions=")))["developer_instructions"]
                self.assertIn(f"You are @{name}", pointer)
                prompt_file = pathlib.Path(pointer.split(" are in ")[1].split(". Read")[0])
                self.assertTrue(str(prompt_file).startswith(str(cache)) and prompt_file.exists())  # outside the repo
            fe = native(starts["frontend-dev"])
            settings = json.loads(pathlib.Path(flag(fe, "--settings")).read_text())
            self.assertIn(f"Bash(python3 {SCRIPT} say *)", settings["permissions"]["allow"])
            self.assertIn(f"python3 {SCRIPT} say *", settings["sandbox"]["excludedCommands"])
            self.assertNotIn("herdr agent prompt *", settings["sandbox"]["excludedCommands"])
            sock = os.environ.get("HERDR_SOCKET_PATH")
            expected_network = {"allowUnixSockets": [sock]} if sys.platform == "darwin" and sock else {"allowAllUnixSockets": True}
            self.assertEqual(settings["sandbox"]["network"], expected_network)
            self.assertEqual(flag(fe, "--permission-mode"), "bypassPermissions")  # the default never stops to ask
            self.assertTrue(settings["skipDangerousModePermissionPrompt"])  # nor at its first-run warning
            self.assertNotIn("--disallowedTools", fe)
            prompt = pathlib.Path(flag(fe, "--append-system-prompt-file")).read_text()
            self.assertIn(str(e.repo / "FACTORY.md"), prompt)
            self.assertNotIn("{instructions}", prompt)
            self.assertEqual(flag(native(starts["pm"]), "--model"), "openrouter/deepseek/deepseek-v4.1-flash-1017")

            status = e.run("status").stdout  # names come from herdr's agent list, not its pane list
            self.assertNotIn("unnamed", status)
            sent = dict(e.prompts())
            self.assertEqual(set(sent), {"lead-dev", "frontend-dev", "reviewer", "pm"})  # one message each
            self.assertTrue(sent["frontend-dev"].startswith("# Team brief (from @operator)"))  # its instructions are its system prompt
            self.assertTrue(sent["reviewer"].startswith("[from @operator]: Your standing instructions"))  # Codex gets them here too
            self.assertIn("You are read-only", sent["reviewer"])
            for text in sent.values():
                for needle in ("| **@lead-dev** | Codex, gpt-6-astra, high |", "## Workflow", READY):
                    self.assertIn(needle, text)
                self.assertNotIn("+++", text)
            self.assertEqual(sorted(p.name for p in e.repo.iterdir()), ["FACTORY.md", "factory.toml", "sub"])  # nothing else

    def test_ten_members_stay_a_balanced_grid(self):
        with Env() as e:
            e.run("up", "--no-brief")
            team = [p for p in e.fake_state()["panes"].values() if p["tab_id"] == "w1:t2"]
            self.assertEqual(len(team), 10)
            self.assertEqual(len(e.calls("tab", "create")), 1)
            areas = [p["rect"]["width"] * p["rect"]["height"] for p in team]
            self.assertLessEqual(max(areas) / min(areas), 2.2)  # no pane is squeezed: each split halves the largest
            self.assertGreaterEqual(min(p["rect"]["width"] for p in team), 50)
            self.assertGreaterEqual(min(p["rect"]["height"] for p in team), 12)

    def test_typed_command_fits_the_macos_line_limit(self):
        # herdr types `<harness> <argv>` into the pane's shell, and macOS limits that line to 1024 bytes.
        deep = "/Users/someone/Development/some-organization/a-rather-long-repository-name-for-a-project"
        with Env() as e:
            cfg = factory.Config(e.repo)
            cfg.root = pathlib.Path(deep)
            resume = {"agent": "codex", "kind": "id", "value": "01a1cbc6-e708-7623-986f-cc2e07efa28f"}
            for name in cfg.members:
                m = {**factory.plan(cfg, name), "permissions": "full", "branch": "ada/" + name}
                m["model"] = "openrouter/" + m["model"] if m["harness"] == "pi" else m["model"]
                argv, _ = factory.launch_argv(m, pathlib.Path("/Users/someone/.cache/factory-skill") / cfg.cache.name / cfg.session_dir.name / f"{name}.md", resume)
                self.assertLess(len(shlex.join([m["harness"], *argv]).encode("utf-8")), 900, name)

    def test_failed_check_launches_nothing_and_explains_the_fix(self):
        with Env(team_brief('''
            [members.backend-dev]
            harness = "pi"
            model = "z-ai/glm-9"
            [members.lead-dev]
            harness = "codex"
            model = "gpt-6-luna"
        ''')) as e:
            p = e.run("up", check=False)
            self.assertEqual(p.returncode, 2)
            self.assertIn("Cannot launch: 1 member(s)", p.stdout)
            self.assertIn("pi has no model z-ai/glm-9 configured", p.stdout)
            self.assertIn("OpenRouter key", p.stdout)
            self.assertEqual(e.calls(), [["agent", "get", e.env["HERDR_PANE_ID"]], ["agent", "list"], ["pane", "list"]])

    def test_up_can_be_run_again_after_a_partial_launch(self):
        with Env(UI) as e:
            p = e.run("up", check=False, FAKE_START_FAIL="dead:frontend-dev,blocked:pm,banner:reviewer,dialog:lead-dev")
            self.assertEqual(p.returncode, 3)
            self.assertIn("started @reviewer", p.stdout)  # a start-up notice that clears is not a failure
            self.assertIn("started @lead-dev", p.stdout)  # a question the harness table knows is answered
            self.assertEqual([c[3:] for c in e.calls("pane", "send-keys")], [["enter"]])
            self.assertIn("@frontend-dev did not come up: timeout: agent did not appear", p.stdout)  # herdr's reason is shown
            self.assertIn("factory.py up frontend-dev", p.stdout)
            self.assertIn("@pm launch incomplete: agent_not_ready", p.stdout)
            self.assertIn("Brief withheld.", p.stdout)
            self.assertEqual(sorted(e.starts()), ["frontend-dev", "lead-dev", "pm", "reviewer"])  # it went on past both
            self.assertEqual({n for n, _ in e.prompts()}, {"lead-dev", "reviewer"})  # only those that came up were briefed

            e.update("pm", agent_status="idle")  # the user answered pm's dialog
            before = len(e.prompts())
            out = e.run("up", "frontend-dev").stdout
            self.assertIn("started @frontend-dev", out)
            later = dict(e.prompts()[before:])
            self.assertIn("starting in a new session", later.pop("frontend-dev"))
            self.assertEqual(set(later), {"lead-dev", "reviewer", "pm"})
            for text in later.values():
                self.assertTrue(text.startswith("[from @operator]: Joined the team: @frontend-dev (frontend-dev, Claude Code"))
            self.assertEqual(len(e.calls("tab", "create")), 1)  # the failed member's pane was reused
            self.assertEqual(len([p for p in e.fake_state()["panes"].values() if p["label"] == "frontend-dev"]), 1)
            e.run("brief", "--send", "pm")
            self.assertIn("# Team brief", e.prompts()[-1][1])
            self.assertEqual(e.prompts()[-1][0], "pm")

    def test_startup_dialog_must_disappear_before_briefing(self):
        with Env() as e:
            result = e.run("up", "lead-dev", check=False,
                           FAKE_START_FAIL="dialog:lead-dev", FAKE_DIALOG_STUCK="1")
            self.assertEqual(result.returncode, 3)
            self.assertIn("still asks", result.stdout)
            self.assertIn("input_failed", result.stdout)
            self.assertEqual(e.prompts(), [])

    def test_wrapped_startup_question_is_answered_once_before_the_brief(self):
        # A narrow pane wraps agy's trust question and herdr calls agy ready. Unanswered, the question took the
        # Enter that ends the brief, and the brief was lost (gate-010).
        agy = formation({"pm": {"harness": "agy"}}, operator={"harness": "claude"})
        for width in ("27", "4"):
            with self.subTest(width=width), Env(agy) as e:
                e.run("up", FAKE_START_FAIL="dialog:pm", FAKE_DIALOG_WRAP=width)
                calls = e.fake_state()["calls"]
                keys = [i for i, c in enumerate(calls) if c[:2] == ["pane", "send-keys"]]
                prompts = [i for i, c in enumerate(calls) if c[:3] == ["agent", "prompt", "pm"]]
                self.assertEqual([calls[i][3:] for i in keys], [["enter"]])
                self.assertEqual(len(prompts), 1)
                self.assertLess(keys[0], prompts[0])
        with self.subTest("a question the table does not know"), Env(agy) as e:
            e.run("up", FAKE_START_FAIL="dialog:pm", FAKE_DIALOG_WRAP="27",
                  FAKE_DIALOG_TEXT="Do you trust the contents of this?")
            self.assertEqual(e.calls("pane", "send-keys"), [])
        with self.subTest("the question stays after its answer"), Env(agy) as e:
            result = e.run("up", check=False, FAKE_START_FAIL="dialog:pm", FAKE_DIALOG_WRAP="27", FAKE_DIALOG_STUCK="1")
            self.assertEqual(result.returncode, 3)
            self.assertIn("still asks", result.stdout)
            self.assertEqual(len(e.calls("pane", "send-keys")), 1)
            self.assertEqual(e.prompts(), [])

    def test_lead_detection_preserves_socket_error(self):
        with Env() as e:
            result = e.run("up", "lead-dev", check=False, FAKE_GET_FAIL="1")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Operation not permitted", result.stderr)
            self.assertNotIn("herdr sees no agent", result.stderr)
            self.assertEqual(e.starts(), {})

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
            result = e.run("up", "reviewer")
            self.assertIn("claude-future-9 is not among", result.stderr)
            self.assertEqual(flag(native(e.starts()["reviewer"]), "--model"), "claude-future-9")

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
            e.run("up", "reviewer")
            self.assertEqual(native(e.starts()["reviewer"]), ["--standalone", "--auto"])
            content = json.loads(e.pane_of("reviewer")["env"][0].split("=", 1)[1])
            self.assertEqual(content, {"model": "openrouter/z-ai/glm-5.3#high"})
            name, text = e.prompts()[0]
            self.assertIn("You are @reviewer", text)  # OpenCode gets its instructions as a message
            self.assertIn("# Team brief", text)

    def test_agy_default_launch_trust_message_and_resume(self):
        with Env(formation({"pm": {"harness": "agy"}}, operator={"harness": "claude"})) as e:
            e.run("up", FAKE_START_FAIL="dialog:pm")
            args = ["--model", "gemini-4-neon-high", "--mode", "accept-edits",
                    "--settings", '{"toolPermission":"always-proceed"}']
            self.assertEqual(kind(e.starts()["pm"]), "agy")
            self.assertEqual(native(e.starts()["pm"]), args)
            self.assertEqual([c[3:] for c in e.calls("pane", "send-keys")], [["enter"]])
            brief = dict(e.prompts())["pm"]
            self.assertIn("You are @pm", brief)
            self.assertIn("Antigravity CLI, gemini-4-neon, high", brief)
            self.assertIn("factory.py say", brief)
            self.assertEqual(e.say("pm", "operator", "report").stdout.strip(), "queued message 1 for @operator")
            e.set_session("pm", {"agent": "agy", "kind": "id", "value": "conversation-94"})
            e.kill("pm")
            e.run("up", "pm")
            self.assertEqual(native(e.starts()["pm"]), args[:2] + ["--conversation", "conversation-94"] + args[2:])

    def test_agy_model_variants_and_guarded_permissions(self):
        cases = {"pm": ("gemini-4-neon", "low", "gemini-4-neon-low"),
                 "reviewer": ("gemini-4-neon-low", None, "gemini-4-neon-low"),
                 "designer": ("gemini-pro-orchestrator-neon", None, "gemini-pro-orchestrator-neon")}
        members = {name: {"harness": "agy", "model": model, "permissions": "guarded",
                          **({"thinking": level} if level else {})} for name, (model, level, _) in cases.items()}
        with Env(formation(members)) as e:
            e.run("up")
            for name, (_, _, wanted) in cases.items():
                self.assertEqual(native(e.starts()[name]), ["--model", wanted, "--mode", "accept-edits",
                                 "--settings", '{"toolPermission":"always-proceed"}'])
            brief = dict(e.prompts())["pm"]
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
            result = e.run("up", check=False)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(e.starts(), {})
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
            e.run("up", "lead-dev", "frontend-dev", "reviewer", "pm", "--no-brief", FAKE_START_FAIL="dialog:frontend-dev")
            starts = {n: native(c) for n, c in e.starts().items()}
            self.assertEqual(starts["lead-dev"][-4:], ["--approve-for-me", "-c", "sandbox_workspace_write.network_access=true", "--search"])
            self.assertNotIn("-s", starts["lead-dev"])  # --approve-for-me cannot be combined with -s
            self.assertEqual(flag(starts["frontend-dev"], "--permission-mode"), "auto")
            self.assertNotIn("network", json.loads(pathlib.Path(flag(starts["frontend-dev"], "--settings")).read_text())["sandbox"])
            self.assertEqual([c[3:] for c in e.calls("pane", "send-keys")], [["down", "enter"]])  # Claude's trust question
            self.assertEqual(starts["reviewer"][-1], "--dangerously-bypass-approvals-and-sandbox")
            self.assertEqual(starts["pm"][-2:], ["--approval-mode", "yolo"])  # omp has no guard that lets messages through
            e.write_brief(team_brief('''
            permissions = "yolo"
            [members.pm]
            harness = "claude"
        '''))
            self.assertIn("must be one of full, guarded", e.run("check", "pm", check=False).stderr)

    def test_claude_members_disable_prompt_suggestions_on_start_and_resume(self):
        members = {"frontend-dev": {**CATALOG_MEMBERS["frontend-dev"], "permissions": "full"},
                   "designer": {**CATALOG_MEMBERS["designer"], "permissions": "guarded"}}
        with Env(formation(members)) as e:
            e.run("up", "--no-brief")
            for name in members:
                e.set_session(name, {"agent": "claude", "kind": "id", "value": f"saved-{name}"})
            e.run("up", "--restart")
            for name in members:
                fresh, resumed = [native(call) for call in e.calls("agent", "start", name)]
                self.assertNotIn("--resume", fresh)
                self.assertEqual(flag(resumed, "--resume"), f"saved-{name}")
                for argv in (fresh, resumed):
                    self.assertIs(json.loads(pathlib.Path(flag(argv, "--settings")).read_text())["promptSuggestionEnabled"], False)

    def test_claude_socket_settings_follow_platform_and_permission_level(self):
        for platform in ("linux", "darwin"):
            for socket_path in ("", "/tmp/test-herdr.sock"):
                with self.subTest(platform=platform, socket=socket_path), patch.object(factory.sys, "platform", platform), \
                        patch.dict(os.environ, {"HERDR_SOCKET_PATH": socket_path}):
                    full = json.loads(factory.claude_settings("full"))["sandbox"]
                    expected = {"allowUnixSockets": [socket_path]} if platform == "darwin" and socket_path else {"allowAllUnixSockets": True}
                    self.assertEqual(full["network"], expected)
                    self.assertNotIn("network", json.loads(factory.claude_settings("guarded"))["sandbox"])

    def test_new_members_never_rename_existing_members(self):
        with Env() as e:
            e.run("up", "backend-dev", "pm", "--no-brief")
            first = e.pane_of("backend-dev")
            original_settings = factory.Config(e.repo).members["backend-dev"]
            original_prompt = pathlib.Path(flag(native(e.starts()["backend-dev"]), "--append-system-prompt"))
            original_text = original_prompt.read_text()
            for number in (2, 3):
                name = f"backend-dev-{number}"
                members = {**CATALOG_MEMBERS, **{f"backend-dev-{i}": {
                    "role": "backend-dev", "harness": "codex", "model": "gpt-6-luna",
                } for i in range(2, number + 1)}}
                e.write_brief(formation(members))
                original_brief = (e.repo / "factory.toml").read_bytes()
                self.assertIn(f"started @{name}", e.run("up", name).stdout)
                self.assertEqual((e.repo / "factory.toml").read_bytes(), original_brief)
                self.assertEqual(factory.Config(e.repo).members["backend-dev"], original_settings)
                self.assertEqual({k: e.pane_of("backend-dev")[k] for k in ("pane_id", "name", "label", "cwd")},
                                 {k: first[k] for k in ("pane_id", "name", "label", "cwd")})
                self.assertEqual(original_prompt.read_text(), original_text)
            self.assertEqual(e.calls("agent", "rename"), [["agent", "rename", "w1:p1", "operator"]])
            self.assertNotIn("backend-dev-1", factory.Config(e.repo).members)

    def test_existing_member_settings_only_come_from_the_brief(self):
        with Env(git=True) as e:
            original = (e.repo / "factory.toml").read_bytes()
            for args in (("--role", "reviewer"), ("--model", "gpt-6-luna"), ("--harness", "claude"),
                         ("--thinking", "low"), ("--worktree",), ("--team", "ui")):
                p = e.run("up", "lead-dev", *args, check=False)
                self.assertEqual(p.returncode, 2)
                self.assertIn("unrecognized arguments", p.stderr)
            self.assertEqual((e.repo / "factory.toml").read_bytes(), original)
            self.assertEqual(e.starts(), {})

    def test_up_reports_brief_delivery_failure_on_start_and_recovery(self):
        with Env() as e:
            p = e.run("up", "lead-dev", check=False, FAKE_PROMPT_FAIL="lead-dev")
            self.assertEqual(p.returncode, 3)
            self.assertIn("NOT delivered to @lead-dev: agent_blocked", p.stdout)
            self.assertEqual(len(e.prompts()), 1)
            e.kill("lead-dev")
            p = e.run("up", "lead-dev", check=False, FAKE_UNCONFIRMED="lead-dev")
            self.assertEqual(p.returncode, 3)
            self.assertIn("delivery unconfirmed to @lead-dev: unexpected reply from herdr", p.stdout)
            self.assertNotIn("brief --send", p.stdout)
            self.assertEqual(len(e.prompts()), 2)
            self.assertEqual(e.run("brief", "--send", "lead-dev").returncode, 0)

    def test_say_reports_who_did_not_get_the_message(self):
        with Env() as e:
            e.run("up", "lead-dev", "pm", "--no-brief")
            note = e.root / "note.md"
            note.write_text("Priorities changed: ship the list view first.\n")
            e.run("say", "all", "--file", str(note))
            e.run("say", "@pm", "add a todo: ship it")
            sent = e.prompts()
            self.assertIn(("lead-dev", "[from @operator]: Priorities changed: ship the list view first.\n"), sent)
            self.assertIn(("pm", "[from @operator]: add a todo: ship it\n"), sent)
            self.assertIn("not running: reviewer", e.run("say", "reviewer", "hi", check=False).stdout)
            e.update("pm", agent_status="blocked")  # herdr refuses a prompt to an agent waiting on an approval
            p = e.run("say", "all", "status?", check=False)
            self.assertEqual(p.returncode, 1)
            self.assertIn("submitted your message to @lead-dev", p.stdout)
            self.assertIn("not sent to @pm: agent_blocked", p.stdout)

    def test_say_terminates_text_and_files_without_changing_the_payload(self):
        with Env() as e:
            e.run("up", "lead-dev", "--no-brief")
            note = e.root / "note.md"
            for message, expected in (("Ask @pm.", "Ask @pm.\n"),
                                      ("Read $herdr", "Read $herdr\n"),
                                      ("Keep this line.\nAsk @pm.", "Keep this line.\nAsk @pm.\n"),
                                      ("Ask @pm.\n", "Ask @pm.\n"),
                                      ("Keep trailing space. \n\n", "Keep trailing space. \n\n")):
                for source in ("text", "file"):
                    with self.subTest(message=message, source=source):
                        note.write_text(message)
                        args = (message,) if source == "text" else ("--file", str(note))
                        e.run("say", "lead-dev", *args)
                        self.assertEqual(e.prompts()[-1], ("lead-dev", "[from @operator]: " + expected))

    def test_delivery_preserves_the_briefs_final_newline(self):
        with Env() as e:
            e.run("up", "--no-brief")
            preview = e.run("brief").stdout
            e.run("brief", "--send", "frontend-dev")  # Claude gets the brief without a standing-instructions prefix
            self.assertEqual(dict(e.prompts())["frontend-dev"] + "\n", preview)  # CLI print adds one newline

    def test_member_draft_delivers_literal_verdict_and_retries_only_refused_peer(self):
        members = {**CATALOG_MEMBERS, "reviewer": {**CATALOG_MEMBERS["reviewer"], "worktree": True}}
        with Env(formation(members), git=True) as e:
            e.run("up", "reviewer", "lead-dev", "pm", "--no-brief")
            member = {**e.env, "HERDR_PANE_ID": e.pane_of("reviewer")["pane_id"]}
            prompt = e.run("prompt", "reviewer").stdout
            send, = re.findall(r"```bash\n([^\n]+)\n```", prompt)
            directory = pathlib.Path(shlex.split(send)[-1]).parent
            self.assertIn(f"new file in `{directory}`", prompt)
            self.assertIn("Never overwrite a pending draft", prompt)
            def run(command):
                return subprocess.run([shutil.which("bash"), "-c", command], cwd=e.pane_of("reviewer")["cwd"],
                                      env=member, text=True, capture_output=True, check=False)
            draft = directory / "lead-dev-review-105-1.md"
            other = directory / "pm-review-105-2.md"
            text = ('Review: "the reviewer\'s patch" keeps $HOME and `status` literal.\n' * 11) + "Ask @pm"
            draft.write_text(text)
            other.write_text("Second verdict")
            calls = len(e.calls("agent", "rename"))
            baseline = e.prompts()
            old = "herdr agent prompt lead-dev '[from @reviewer]: " + "old verdict " * 55
            self.assertNotEqual(run(old).returncode, 0)
            self.assertEqual(e.prompts(), baseline)
            e.update("pm", agent_status="blocked")
            first = run(send.replace(" NAME ", " all ").replace("FILENAME", draft.name))
            self.assertNotEqual(first.returncode, 0, first.stderr)
            expected = "[from @reviewer]: " + text + "\n"
            self.assertEqual(e.prompts().count(("lead-dev", expected)), 1)
            self.assertNotIn(("reviewer", expected), e.prompts())
            self.assertEqual(draft.read_text(), text)
            retry = next(line.split(": ", 1)[1] for line in first.stdout.splitlines() if line.startswith("Retry once"))
            self.assertNotIn("lead-dev", shlex.split(retry))
            e.update("pm", agent_status="idle")
            self.assertEqual(run(retry).returncode, 0)
            self.assertFalse(draft.exists())
            self.assertTrue(other.exists())
            self.assertNotEqual(run(retry).returncode, 0)
            self.assertEqual(e.prompts().count(("lead-dev", expected)), 1)
            self.assertEqual(e.prompts()[-1], ("pm", expected))
            self.assertEqual(len(e.calls("agent", "rename")), calls)
            member["FAKE_UNCONFIRMED"] = "pm"
            uncertain = run(send.replace(" NAME ", " pm ").replace("FILENAME", other.name))
            self.assertNotEqual(uncertain.returncode, 0)
            self.assertIn("delivery unconfirmed to @pm", uncertain.stdout)
            self.assertNotIn("Retry once", uncertain.stdout)
            self.assertTrue(other.exists())

    def test_session_local_drafts_instructions_settings_and_cold_start(self):
        with Env(formation({"architect": CATALOG_MEMBERS["architect"]}, operator={"harness": "claude"})) as e:
            fresh = e.fake.read_bytes()
            drafts, instructions = [], []
            for session in ("team/one", "team-one"):  # identical readable prefix and roster
                e.fake.write_bytes(fresh)
                e.run("up", "--no-brief", HERDR_SESSION=session)
                launch = e.starts()["architect"]
                prompt = pathlib.Path(flag(launch, "--append-system-prompt-file"))
                instructions.append((prompt, prompt.read_text()))
                settings = json.loads(pathlib.Path(flag(launch, "--settings")).read_text())
                directory = pathlib.Path(settings["permissions"]["additionalDirectories"][0])
                self.assertIn(str(directory), prompt.read_text())
                self.assertIn(str(directory), settings["sandbox"]["filesystem"]["allowWrite"])
                self.assertIn(f"python3 {SCRIPT} say *", settings["sandbox"]["excludedCommands"])
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
                path = directory / "message-same.md"
                path.write_text(session)
                drafts.append(path)
            first, second = drafts
            self.assertNotEqual(first, second)
            self.assertNotEqual(instructions[0][0], instructions[1][0])
            for path, original in instructions:
                self.assertEqual(path.read_text(), original)
            e.kill("architect")
            cold = e.run("up", "--no-brief", HERDR_SESSION="team-one").stdout
            self.assertIn(str(second), cold)
            self.assertNotIn(str(first), cold)
            e.say("architect", "operator", "--file", str(first), HERDR_SESSION="team-one")
            self.assertTrue(first.exists())  # an external --file is never consumed
            e.say("architect", "operator", "--file", str(second), HERDR_SESSION="team-one")
            self.assertFalse(second.exists())
            self.assertTrue(first.exists())

    def test_cold_up_warns_about_unreadable_drafts_and_lists_readable_siblings(self):
        members = {name: CATALOG_MEMBERS[name] for name in ("architect", "reviewer")}
        with Env(formation(members)) as e, patch.dict(os.environ, e.env), \
                patch.object(factory, "HERDR", e.env["HERDR_BIN"]):
            drafts = factory.Config(e.repo).messages
            unreadable = drafts / "architect"
            readable = drafts / "reviewer" / "pending-review.md"
            unreadable.mkdir(parents=True)
            readable.parent.mkdir()
            readable.write_text("A pending review")
            iterdir = pathlib.Path.iterdir

            def scan(path):
                if path == unreadable:
                    raise PermissionError("permission denied")
                return iterdir(path)

            out, err = io.StringIO(), io.StringIO()
            with patch.object(pathlib.Path, "iterdir", scan), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                factory.main(["up", "--cwd", str(e.repo), "--no-brief"])

            self.assertIn(f"warning: cannot scan drafts in {unreadable}: permission denied", err.getvalue())
            self.assertIn(str(readable), out.getvalue())
            self.assertIn("leftover draft for @reviewer", out.getvalue())
            self.assertEqual(set(e.starts()), set(members))
            self.assertEqual(readable.read_text(), "A pending review")

    def test_brief_send_all_matches_implicit_broadcast(self):
        with Env() as e:
            e.run("up", "lead-dev", "pm", "--no-brief")
            e.run("brief", "--send")
            expected = e.prompts()
            self.assertEqual([name for name, text in expected], ["lead-dev", "pm"])
            e.run("brief", "--send", "all")
            self.assertEqual(e.prompts(), expected + expected)

    def test_member_queues_for_configured_operator_without_a_pane(self):
        with Env(formation({"reviewer": CATALOG_MEMBERS["reviewer"]}, operator={"name": "lead"})) as e:
            e.run("up", "--no-brief")
            with patch.dict(os.environ, e.env):
                cfg = factory.Config(e.repo)
                draft = cfg.messages / "reviewer/message-one.md"
                inbox = factory.Inbox(cfg)
            text = "Reviewer's report with `literal` $ and a final @lead"
            draft.write_text(text)
            e.close("lead")
            self.assertIn("queued message 1 for @lead", e.say("reviewer", "lead", "--file", str(draft)).stdout)
            self.assertFalse(e.prompts())
            self.assertFalse(draft.exists())
            row = json.loads(inbox.path.read_text())
            self.assertEqual((row["from"], row["text"]), ("reviewer", text))


    def test_draft_name_validation_is_scoped_to_the_sender_folder(self):
        with Env(UI) as e:
            e.run("up", "--no-brief")
            with patch.dict(os.environ, e.env):
                directory = factory.Config(e.repo).messages / "reviewer"
            for name in ("review notes.md", "review_name.txt", "review.one.txt", "review", "review$.md"):
                draft = directory / name
                draft.write_text("verdict")
                result = e.say("reviewer", "operator", "lead-dev", "--file", str(draft), check=False)
                self.assertIn("letters, digits and dashes", result.stderr)
                self.assertEqual(result.returncode, 1)
                self.assertTrue(draft.exists())
            self.assertFalse(e.prompts())
            self.assertNotIn("unread", e.run("status").stdout)
            external = e.root / "release notes.md"
            external.write_text("preserved")
            e.run("say", "lead-dev", "--file", str(external))
            self.assertEqual(e.prompts(), [("lead-dev", "[from @operator]: preserved\n")])
            self.assertTrue(external.exists())

    def test_say_recipient_lists_accept_spaces_and_commas(self):
        with Env(UI) as e:
            e.run("up", "reviewer", "lead-dev", "--no-brief")
            with patch.dict(os.environ, e.env):
                cfg = factory.Config(e.repo)
                inbox = factory.Inbox(cfg)
                directory = cfg.messages / "reviewer"
            message = "@operator Please record the verdict. @lead-dev Please check the fix."
            for i, targets in enumerate((("operator", "lead-dev"), ("operator,lead-dev",), ("all",)), 1):
                with self.subTest(targets=targets):
                    draft = directory / f"multi-{i}.md"
                    draft.write_text(message)
                    result = e.say("reviewer", *targets, "--file", str(draft))
                    self.assertIn("submitted your message to @lead-dev", result.stdout)
                    if targets != ("all",):
                        self.assertIn(f"queued message {i} for @operator", result.stdout)
                    self.assertFalse(draft.exists())
            self.assertEqual(e.prompts(), [("lead-dev", "[from @reviewer]: " + message + "\n")] * 3)
            self.assertEqual([row["text"] for row in map(json.loads, inbox.path.read_text().splitlines())], [message] * 2)

    def test_mixed_send_reports_each_target_without_replaying_a_queue(self):
        with Env(UI) as e:
            e.run("up", "--no-brief")
            with patch.dict(os.environ, e.env):
                cfg = factory.Config(e.repo)
                draft = cfg.messages / "reviewer/mixed-one.txt"
                inbox = factory.Inbox(cfg)
            draft.write_text("first verdict")
            e.update("pm", agent_status="blocked")
            result = e.say("reviewer", "operator", "pm", "--file", str(draft), check=False)
            self.assertIn("queued message 1 for @operator", result.stdout)
            self.assertIn("not sent to @pm: agent_blocked", result.stdout)
            self.assertEqual(result.returncode, 1)
            retry = shlex.split(next(line.split(": ", 1)[1] for line in result.stdout.splitlines()
                                     if line.startswith("Retry once")))
            self.assertEqual(retry[-3:], ["pm", "--file", str(draft)])
            self.assertTrue(draft.exists())
            e.update("pm", agent_status="idle")
            env = {**e.env, "HERDR_PANE_ID": e.pane_of("reviewer")["pane_id"]}
            self.assertEqual(subprocess.run(retry, env=env, capture_output=True, text=True, check=False).returncode, 0)
            self.assertFalse(draft.exists())
            self.assertEqual([row["id"] for row in map(json.loads, inbox.path.read_text().splitlines())], [1])
            self.assertEqual(e.prompts()[-1], ("pm", "[from @reviewer]: first verdict\n"))

            draft.write_text("second verdict")
            e.update("pm", agent_status="blocked")
            result = e.say("reviewer", "operator", "pm", "lead-dev", "--file", str(draft),
                           check=False, FAKE_UNCONFIRMED="lead-dev")
            self.assertEqual(result.returncode, 1)
            self.assertIn("queued message 2 for @operator", result.stdout)
            self.assertIn("not sent to @pm: agent_blocked", result.stdout)
            self.assertIn("delivery unconfirmed to @lead-dev", result.stdout)
            self.assertNotIn("Retry once", result.stdout)
            self.assertEqual(draft.read_text(), "second verdict")

    def test_herdr_required_and_brief_missing(self):
        with Env() as e:
            self.assertEqual(e.run("status", "--all", check=False).returncode, 2)
            for command in ("up", "check", "prompt"):
                missing = e.run(command, "ghost", check=False)
                self.assertEqual(missing.returncode, 1)
                self.assertIn("not a member", missing.stderr)
                self.assertNotIn("Traceback", missing.stderr)
            status = e.run("status").stdout
            self.assertEqual(status.count("not started"), len(CATALOG_MEMBERS) + 1)
            self.assertIn("No member of factory.toml has a pane in this herdr session. "
                          "Run `factory.py up` to start or resume its members.", status)
            for command in ("cost", "show"):
                self.assertEqual(e.run(command, check=False).returncode, 2)
            for option in ("--dry-run", "--trust-models", "--effort"):
                self.assertEqual(e.run("up", "lead-dev", option, check=False).returncode, 2)
            for cmd in (["up", "pm"], ["status"], ["wait"]):
                self.assertIn("not inside herdr", e.run(*cmd, check=False, HERDR_ENV="").stderr)
        with Env(brief=None) as e:
            self.assertIn("Run `/factory setup`", e.run("check", "pm", check=False).stderr)


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
            out = e.run("up", "backend-dev", "backend-dev-2", "frontend-dev", "reviewer").stdout
            trees = e.root / "repo.worktrees"
            self.assertEqual(sorted(p.name for p in trees.iterdir()), ["ada-backend-dev", "feature-login"])  # one is shared
            self.assertEqual(out.count("created worktree"), 2)
            branch = lambda d: subprocess.run([GIT, "-C", str(d), "rev-parse", "--abbrev-ref", "HEAD"],
                                              capture_output=True, text=True).stdout.strip()
            self.assertEqual((branch(trees / "ada-backend-dev"), branch(trees / "feature-login")),
                             ("ada/backend-dev", "feature/login"))
            where = {n: e.pane_of(n)["cwd"] for n in ("backend-dev", "backend-dev-2", "frontend-dev", "reviewer")}
            self.assertEqual(where, {"backend-dev": str(trees / "ada-backend-dev"), "backend-dev-2": str(trees / "feature-login"),
                                     "frontend-dev": str(trees / "feature-login"), "reviewer": str(e.repo)})
            prompt = e.run("prompt", "backend-dev").stdout
            self.assertIn("Commit your work on `ada/backend-dev`", prompt)
            self.assertIn("`git merge main`", prompt)
            self.assertIn("agree who commits it for you", prompt)
            self.assertNotIn("Never run `git commit`", prompt)
            staging = "Stage only task files, never `git add -A` or `git add .`"
            self.assertIn(staging, prompt)
            self.assertNotIn("Do not stage files", prompt)
            shared = e.run("prompt", "reviewer").stdout
            self.assertIn("Do not stage files. Never run `git commit`, `git merge` or `git push`. "
                          "@operator stages only task files and commits.", shared)
            self.assertNotIn(staging, shared)
            brief = next(t for n, t in e.prompts() if n == "reviewer")
            self.assertIn("| branch `feature/login` in `" + str(trees / "feature-login") + "` |", brief)
            self.assertIn("`git diff main...<branch>`", brief)
            self.assertIn("| the main checkout |", brief)
            e.kill("backend-dev")
            self.assertNotIn("created worktree", e.run("up", "backend-dev").stdout)  # the checkout is kept

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
            e.run("up", "reviewer-2", "--no-brief")  # the existing reviewer keeps its name
            self.assertTrue((e.root / "repo.worktrees/ada-reviewer-2").is_dir())
            self.assertIs(factory.Config(e.repo).members["reviewer-2"]["worktree"], True)
        with Env(team_brief(self.BRIEF)) as e:  # not a git repository
            p = e.run("up", "backend-dev", check=False)
            self.assertEqual(p.returncode, 2)
            self.assertIn("a worktree needs a git repository with a commit", p.stdout)
            self.assertEqual(e.calls(), [["agent", "get", e.env["HERDR_PANE_ID"]], ["agent", "list"], ["pane", "list"]])

    def test_distinct_branches_cannot_share_a_worktree_path(self):
        brief = formation({
            'backend-dev': {'harness': 'codex', 'model': 'gpt-6-luna', 'worktree': 'feature/a'},
            'backend-dev-2': {'role': 'backend-dev', 'harness': 'codex', 'model': 'gpt-6-luna', 'worktree': 'feature-a'},
        })
        with Env(brief, git=True) as e:
            for command in ("check", "up"):
                result = e.run(command, "backend-dev", "backend-dev-2", check=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                for needle in ("worktree", "feature/a", "feature-a"):
                    self.assertIn(needle, result.stdout)
            self.assertEqual(e.starts(), {})
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
                result = e.run("up", "lead-dev", check=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("worktree", result.stdout)
                self.assertEqual(e.starts(), {})

    def test_branch_names_git_rejects_fail_the_check(self):
        team = ('[members.reviewer]\nharness = "codex"\nmodel = "gpt-6-luna"\n'
                '[members.lead-dev]\nharness = "codex"\nmodel = "gpt-6-luna"\nworktree = {}\n')
        cases = [(f'user = "{u}"\n', "true") for u in (".ada", "ada..x", "ada.lock", "-ada")] + [("", '"feature//x"'), ("", '"x.lock"')]
        for settings, worktree in cases:
            with self.subTest(settings=settings, worktree=worktree), Env(team_brief(settings + team.format(worktree)), git=True) as e:
                for command in ("check", "up"):
                    p = e.run(command, check=False)
                    self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                    self.assertIn("is not a branch name git accepts", p.stdout)
                    self.assertIn("Change `user`, or this member's `worktree`", p.stdout)
                self.assertEqual(e.starts(), {})  # the reviewer, checked first, was not started either
        with Env(team_brief(team.format("true")), git=True) as e:  # a running member is not closed for a bad restart
            e.run("up", "--no-brief")
            pane = e.pane_of("lead-dev")["pane_id"]
            e.write_brief(team_brief('user = ".ada"\n' + team.format("true")))
            p = e.run("up", "lead-dev", "--restart", check=False)
            self.assertEqual(p.returncode, 2)
            self.assertEqual(e.calls("pane", "close"), [])
            self.assertEqual(e.pane_of("lead-dev")["pane_id"], pane)

    def test_up_moves_member_to_newly_configured_worktree(self):
        with Env(git=True) as e:
            e.run("up", "lead-dev", "--no-brief")
            old_id = e.pane_of("lead-dev")["pane_id"]
            e.set_session("lead-dev", {"agent": "codex", "kind": "id", "value": "saved-codex"})
            e.kill("lead-dev")
            e.write_brief(CATALOG.replace('[members.lead-dev]', '[members.lead-dev]\nworktree = true'))
            e.run("up", "lead-dev")
            current = e.pane_of("lead-dev")
            self.assertNotEqual(current["pane_id"], old_id)
            self.assertEqual(current["cwd"], str(e.root / "repo.worktrees/ada-lead-dev"))
            self.assertIsNone(e.fake_state()["panes"][old_id]["label"])
            self.assertEqual(native(e.starts()["lead-dev"])[:2], ["resume", "saved-codex"])
            # Codex restores its saved tool cwd unless explicitly overridden, even in a new pane.
            self.assertEqual(flag(native(e.starts()["lead-dev"]), "-C"), current["cwd"])

    def test_up_uses_foreground_directory_when_shell_moved(self):
        with Env(git=True) as e:
            e.run("up", "lead-dev", "--no-brief")
            old_id = e.pane_of("lead-dev")["pane_id"]
            e.kill("lead-dev")
            e.update("lead-dev", foreground_cwd=str(e.repo / "sub"))
            e.run("up", "lead-dev")
            self.assertNotEqual(e.pane_of("lead-dev")["pane_id"], old_id)
            self.assertEqual(e.pane_of("lead-dev")["cwd"], str(e.repo))


class InboxTests(unittest.TestCase):
    def prepare(self, e):
        with patch.dict(os.environ, e.env), factory.Inbox(factory.Config(e.repo)) as inbox:
            inbox.save_state()
            return inbox

    def send(self, e, text="done", **options):
        return e.say("lead-dev", "operator", text, **options)

    def seed(self, e, text="done", sender="lead-dev"):
        with patch.dict(os.environ, e.env):
            outcome, number = factory.queue_message(factory.Config(e.repo), sender, text)
        self.assertEqual(outcome, "queued", number)
        return number

    def invoke(self, e):
        with patch.dict(os.environ, e.env):
            return factory.queue_message(factory.Config(e.repo), "lead-dev", "literal '$herdr' @pm.")

    def test_inbox_and_generated_text_for_every_member_harness(self):
        members = {"lead-dev": {**CATALOG_MEMBERS["lead-dev"], "permissions": "guarded"},
                   "architect": CATALOG_MEMBERS["architect"], "pm": CATALOG_MEMBERS["pm"],
                   "reviewer": {"harness": "omp", "model": "openai/gpt-6-astra", "thinking": "high"},
                   "backend-dev": {"harness": "opencode", "model": "openrouter/z-ai/glm-5.3", "thinking": "high"}}
        with Env(formation(members, operator={"harness": "claude"})) as e:
            self.assertIn("through the inbox", e.run("up").stdout)
            for name in members:
                prompt = e.run("prompt", name).stdout
                self.assertIn("including @operator", prompt)
                self.assertIn("Report queued for @<requester>", prompt)
                self.assertIn("queued message N", prompt)
                self.assertIn(f"say --cwd {e.repo} NAME --file", prompt)
                self.assertNotIn("inbox send", prompt)
            brief = e.run("brief").stdout
            self.assertIn("factory.py say", brief)
            for name, settings in members.items():
                e.set_session(name, {"agent": settings["harness"], "kind": "id", "value": f"saved-{name}"})
            e.run("up", "--restart")
            for name, text in e.prompts()[-len(members):]:
                self.assertIn("Send it with the say command", text, name)
                self.assertNotIn("Send it: `herdr agent prompt operator", text)
            starts = {c[2]: c for c in e.calls("agent", "start")}
            claude = starts["architect"]
            settings = json.loads(pathlib.Path(claude[claude.index("--settings") + 1]).read_text())
            prefix = f"python3 {SCRIPT} say *"
            self.assertIn(prefix, settings["sandbox"]["excludedCommands"])
            self.assertIn(f"Bash({prefix})", settings["permissions"]["allow"])
            codex = starts["lead-dev"]
            with patch.dict(os.environ, e.env):
                cache = str(factory.Config(e.repo).cache)
            self.assertEqual(codex[codex.index("--add-dir") + 1], cache)
            self.assertIn("Use this command for teammates", e.run("prompt", "lead-dev", HERDR_ENV="").stdout)

    def test_inbox_and_instructions_ignore_configured_harness(self):
        with Env(UI) as e:
            prompt, brief = e.run("prompt", "lead-dev").stdout, e.run("brief").stdout
            unset = e.run("up").stdout
            self.assertIn("through the inbox", unset)
            self.assertIn("wait in a native background task", unset)
            e.set_session("lead-dev", {"agent": "codex", "kind": "id", "value": "saved"})
            e.run("up", "lead-dev", "--restart")
            closing = dict(e.prompts())["lead-dev"].split("We are picking up")[1]
            e.write_brief(formation({n: CATALOG_MEMBERS[n] for n in ("lead-dev", "frontend-dev", "reviewer", "pm")},
                                    operator={"harness": "codex"}))
            e.update("operator", agent="codex")
            e.run("up", "lead-dev", "--restart")
            self.assertEqual(prompt, e.run("prompt", "lead-dev").stdout)
            # The Roles row changes with configuration; the generated messaging sentence does not.
            self.assertEqual(brief.split("## Talking to each other")[1],
                             e.run("brief").stdout.split("## Talking to each other")[1])
            self.assertEqual(closing, dict(e.prompts())["lead-dev"].split("We are picking up")[1])
            e.write_brief(formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]}, operator={"harness": "claude"}))
            e.update("operator", agent="codex")
            self.assertIn("wait in the foreground", e.run("up").stdout)
            e.update("operator", agent="claude")
            self.assertIn("wait in a native background task", e.run("up").stdout)
            e.write_brief(formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]}, operator={"harness": "unknown"}))
            self.assertIn("[operator] harness 'unknown' is not one of", e.run("check", check=False).stderr)
        self.assertNotIn("inbox", factory.claude_settings("guarded"))

    def test_stale_operator_sample_keeps_invoking_codex_pane(self):
        members = {"architect": CATALOG_MEMBERS["architect"]}
        with Env(formation(members, operator={"harness": "claude"})) as e:
            local = e.repo / factory.CONF
            sample = e.repo / factory.SAMPLE
            local.rename(sample)
            e.run("init")
            e.update(None, agent="codex")
            stale = e.run("up").stdout
            self.assertIn("through the inbox", stale)
            self.assertIn("wait in the foreground", stale)
            self.assertIn("not qualified for unattended idle delivery", stale)
            self.assertEqual(e.pane_of("operator")["pane_id"], e.env["HERDR_PANE_ID"])
            self.assertEqual(e.pane_of("operator")["agent"], "codex")
            self.assertEqual(list(e.starts()), ["architect"])
            self.assertEqual(kind(e.starts()["architect"]), "claude")
            self.assertEqual(flag(native(e.starts()["architect"]), "--model"), members["architect"]["model"])
            self.assertEqual(len(e.calls("tab", "create")), 1)  # the member tab

            # Setup reconciles the local operator fields. Raw up leaves the sample alone.
            local.write_text(local.read_text().replace('harness = "claude"', 'harness = "codex"', 1))
            corrected = e.run("up").stdout
            self.assertIn("through the inbox", corrected)
            self.assertIn("wait in the foreground", corrected)
            self.assertEqual(len(e.calls("agent", "start")), 1)
            self.assertEqual(tomllib.loads(sample.read_text())["operator"]["harness"], "claude")

    def test_up_supports_pi_and_omp_foreground_operators(self):
        for harness in ("pi", "omp"):
            with self.subTest(harness=harness), Env(UI) as e:
                e.update(None, agent=harness)
                result = e.run("up", "lead-dev", "--no-brief")
                self.assertIn("wait in the foreground during active work", result.stdout)
                self.assertIn("not qualified for unattended idle delivery", result.stdout)
                self.assertEqual(e.pane_of("operator")["agent"], harness)
                self.assertEqual(list(e.starts()), ["lead-dev"])

    def test_up_supports_agy_background_operator(self):
        with Env(UI) as e:
            e.update(None, agent="agy")
            result = e.run("up", "lead-dev", "--no-brief")
            self.assertIn("wait in a native background task", result.stdout)
            self.assertEqual(e.pane_of("operator")["agent"], "agy")
            self.assertEqual(list(e.starts()), ["lead-dev"])


    def test_worktree_uses_explicit_main_root_and_rejects_invalid_messages(self):
        with Env(formation({"lead-dev": {**CATALOG_MEMBERS["lead-dev"], "worktree": True}},
                           operator={"harness": "claude"}), git=True) as e:
            e.run("up")
            with patch.dict(os.environ, e.env):
                cfg = factory.Config(e.repo)
                worktree = factory.plan(cfg, "lead-dev")["dir"]
                inbox = factory.Inbox(cfg)
            command = [sys.executable, str(SCRIPT), "say", "--cwd", str(e.repo), "operator", "hi"]
            env = {**e.env, "HERDR_PANE_ID": e.pane_of("lead-dev")["pane_id"]}
            calls = e.prompts()
            for i in (1, 2):
                result = subprocess.run(command, cwd=worktree, env=env, capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"queued message {i}", result.stdout)
            before = inbox.path.read_bytes()
            self.assertNotEqual(e.say("lead-dev", "operator", "  ", check=False).returncode, 0)
            self.assertEqual(inbox.path.read_bytes(), before)
            self.assertEqual(e.prompts(), calls)

    def test_payloads_replay_acknowledgement_and_ids(self):
        with Env() as e:
            inbox = self.prepare(e)
            text = "line one\nUnicode: café 日本語\n@pm.\n"
            inbox.path.write_text(json.dumps({"id": 1, "at": "2026-11-19T00:00:00Z",
                                              "from": "lead-dev", "text": text}) + "\n")
            e.run("up", "lead-dev", "reviewer", "--no-brief")
            note = e.root / "message.txt"
            note.write_text(text)
            self.assertIn("queued message 2", e.say("reviewer", "operator", "--file", str(note)).stdout)
            self.assertEqual(e.prompts(), [])
            first = e.run("wait").stdout
            self.assertEqual(first, e.run("wait").stdout)
            self.assertIn("    Unicode: café 日本語", first)
            self.assertIn("[Message 1 from @lead-dev at ", first)
            self.assertIn("[Message 2 from @reviewer at ", first)
            self.assertNotIn("#1", first)
            self.assertIn("2 unread messages. After you have acted on or recorded these messages:", first)
            self.assertIn("wait --seen 2", first)
            rows = [json.loads(s) for s in inbox.path.read_text().splitlines()]
            self.assertEqual([r["text"] for r in rows], [text, text])
            self.assertRegex(rows[0]["at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            e.run("wait", "--seen", "2", "--timeout", "0", check=False)
            self.assertEqual(inbox.path.read_bytes(), b"")
            self.assertIn("queued message 3", self.send(e).stdout)
            self.assertIn("1 unread message.", e.run("wait").stdout)
            self.assertIn("; 1 unread message (", e.run("status").stdout)
            e.run("wait", "--seen", "1", "--timeout", "0")  # an old acknowledgement cannot move seen backwards
            before = (inbox.path.read_bytes(), inbox.state_path.read_bytes())
            self.assertEqual(e.run("wait", "--seen", "9", check=False).returncode, 2)
            self.assertEqual(before, (inbox.path.read_bytes(), inbox.state_path.read_bytes()))
            self.assertIn("1 unread", e.run("status").stdout)

    def test_acknowledged_history_recovers_text_without_watching_or_writing(self):
        with Env() as e:
            self.assertIn("inbox reprint recent acknowledged inbox messages", " ".join(e.run("--help").stdout.split()))
            self.assertIn("Reread them with factory.py inbox", " ".join(e.run("wait", "--help").stdout.split()))
            inbox = self.prepare(e)
            original = inbox.state_path.read_bytes()  # Old state has no history field.
            self.assertEqual(e.run("inbox").stdout, "no acknowledged messages kept\n")
            self.assertEqual(inbox.state_path.read_bytes(), original)
            self.assertFalse(inbox.path.exists())
            text = "A report accidentally acknowledged\n" + "a" * 500 + "\nDecision: approve café 日本語"
            self.seed(e, text)
            self.seed(e, "still unread")
            e.run("wait", "--seen", "1", "--timeout", "0")
            before = inbox.path.read_bytes(), inbox.state_path.read_bytes(), e.calls()
            result = e.run("inbox")
            self.assertIn("[Message 1 from @lead-dev at ", result.stdout)
            self.assertIn("    " + text.replace("\n", "\n    "), result.stdout)
            self.assertNotIn("still unread", result.stdout)
            self.assertNotIn("wait --seen", result.stdout)
            self.assertEqual(e.run("inbox", "seen", check=False).returncode, 2)
            self.assertEqual((inbox.path.read_bytes(), inbox.state_path.read_bytes(), e.calls()), before)
            self.assertIn("no acknowledged messages kept", e.run("inbox", HERDR_SESSION="other").stdout)
            for count in ("0", "-1", "invalid"):
                self.assertNotEqual(e.run("inbox", count, check=False).returncode, 0)

    def test_history_is_bounded_ordered_and_acknowledged_only_once(self):
        with Env() as e:
            inbox = self.prepare(e)
            rows = [{"id": n, "from": "lead-dev", "at": "2026-11-18T12:00:00Z", "text": f"report {n}"}
                    for n in range(1, 107)]
            inbox.path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            e.run("wait", "--seen", "105")
            history = json.loads(inbox.state_path.read_text())["history"]
            self.assertEqual([r["id"] for r in history], list(range(6, 106)))
            before = inbox.path.read_bytes(), inbox.state_path.read_bytes()
            for seen in (105, 104):
                e.run("wait", "--seen", str(seen))
            for count, expected in ((None, range(101, 106)), ("2", range(104, 106)), ("200", range(6, 106))):
                result = e.run("inbox", *([] if count is None else [count]))
                ids = [line.split()[1] for line in result.stdout.splitlines() if line.startswith("[Message ")]
                self.assertEqual(ids, [str(n) for n in expected])
            self.assertEqual((inbox.path.read_bytes(), inbox.state_path.read_bytes()), before)

    def test_failed_acknowledgement_keeps_text_unread_and_retry_archives_it(self):
        with Env() as e:
            inbox = self.prepare(e)
            self.seed(e, "recover me")
            before = inbox.path.read_bytes(), inbox.state_path.read_bytes()
            with patch.dict(os.environ, e.env), factory.Inbox(factory.Config(e.repo)) as current:
                with patch.object(current, "replace", side_effect=OSError("state save failed")), self.assertRaises(OSError):
                    current.acknowledge(1)
            self.assertEqual((inbox.path.read_bytes(), inbox.state_path.read_bytes()), before)
            self.assertIn("recover me", e.run("wait").stdout)
            e.run("wait", "--seen", "1", "--timeout", "0")
            self.assertIn("recover me", e.run("inbox").stdout)

    def test_history_damage_and_unavailable_state_fail_without_writes(self):
        row = {"id": 1, "from": "lead-dev", "at": "time", "text": "old"}
        for history in ({}, [None], [{**row, "id": True}], [row, row], [{**row, "text": None}],
                        [{**row, "id": 2}], [{**row, "id": n} for n in range(1, 102)]):
            with self.subTest(history=history), Env() as e:
                inbox = self.prepare(e)
                self.seed(e, "unread")
                inbox.state_path.write_text(json.dumps({"seen": 1, "history": history}))
                before = inbox.path.read_bytes(), inbox.state_path.read_bytes()
                for command in (("inbox",), ("wait", "--seen", "1")):
                    result = e.run(*command, check=False)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("inbox needs repair", result.stderr)
                self.assertEqual((inbox.path.read_bytes(), inbox.state_path.read_bytes()), before)
        with Env() as e:
            inbox = self.prepare(e)
            target = e.root / "state"
            inbox.state_path.rename(target)
            inbox.state_path.symlink_to(target)
            before = target.read_bytes()
            result = e.run("inbox", check=False)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(target.read_bytes(), before)

    def test_concurrent_sends_and_acknowledgement(self):
        with Env() as e:
            inbox = self.prepare(e)
            self.seed(e)
            inode = inbox.lock_path.stat().st_ino
            waiter = subprocess.Popen([sys.executable, str(SCRIPT), "wait", "--cwd", str(e.repo), "--seen", "1"],
                                      env=e.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            with patch.dict(os.environ, e.env), ThreadPoolExecutor(max_workers=6) as pool:
                cfg = factory.Config(e.repo)
                results = list(pool.map(lambda n: factory.queue_message(cfg, "lead-dev", str(n)), range(6)))
            waiter.communicate(timeout=15)
            self.assertTrue(all(outcome == "queued" for outcome, _ in results))
            rows = [json.loads(s) for s in inbox.path.read_text().splitlines()]
            self.assertEqual([r["id"] for r in rows], list(range(2, 8)))
            self.assertEqual({r["text"] for r in rows}, {str(n) for n in range(6)})
            self.assertEqual([r["id"] for r in json.loads(inbox.state_path.read_text())["history"]], [1])
            self.assertEqual(inode, inbox.lock_path.stat().st_ino)

    def test_queue_distinguishes_a_refusal_from_an_uncertain_append(self):
        with Env() as e:
            self.prepare(e)
            with patch.object(factory.Inbox, "private_open", side_effect=PermissionError("denied")):
                self.assertEqual(self.invoke(e), ("refused", "denied"))
            with patch.object(factory.os, "fsync", side_effect=OSError("failed fsync")):
                self.assertEqual(self.invoke(e), ("unconfirmed", "failed fsync"))
            self.assertEqual(e.calls(), [])

    def test_queue_accepts_missing_acknowledgement_state(self):
        with Env() as e:
            self.assertEqual(self.invoke(e), ("queued", 1))
            self.assertIn("literal '$herdr' @pm.", e.run("wait", "--timeout", "0").stdout)


    def test_short_writes_and_private_modes(self):
        with Env() as e:
            inbox = self.prepare(e)
            inbox.directory.chmod(0o755)
            inbox.state_path.chmod(0o644)
            inbox.lock_path.chmod(0o644)
            write = os.write
            with patch.object(factory.os, "write", side_effect=lambda fd, data: write(fd, data[:3])):
                self.assertEqual(self.invoke(e), ("queued", 1))
            self.assertEqual(inbox.directory.stat().st_mode & 0o777, 0o700)
            for path in (inbox.path, inbox.state_path, inbox.lock_path):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(inbox.path.read_text())["text"], "literal '$herdr' @pm.")

    def test_interrupted_acknowledgement_is_reconciled_on_next_send(self):
        with Env() as e:
            inbox = self.prepare(e)
            self.seed(e, "one")
            self.seed(e, "two")
            with patch.dict(os.environ, e.env), factory.Inbox(factory.Config(e.repo)) as current:
                replace = current.replace
                def interrupt(path, data):
                    if path == current.path:
                        raise OSError("crash after state commit")
                    return replace(path, data)
                with patch.object(current, "replace", side_effect=interrupt), self.assertRaises(OSError):
                    current.acknowledge(1)
            before = inbox.path.read_bytes()
            self.assertNotIn("    one", e.run("wait").stdout)
            self.assertEqual(before, inbox.path.read_bytes())
            self.assertEqual(3, self.seed(e, "three"))
            self.assertEqual([r["id"] for r in map(json.loads, inbox.path.read_text().splitlines())], [2, 3])
            e.run("wait", "--seen", "1", "--timeout", "0")
            history = e.run("inbox").stdout
            self.assertEqual(history.count("[Message 1 from"), 1)
            self.assertIn("    one", history)

    def test_damage_refuses_send_and_seen_but_lifecycle_wait_survives(self):
        for damaged in ("inbox", "state"):
            with self.subTest(damaged=damaged), Env(UI) as e:
                e.run("up", "--no-brief")
                inbox = self.prepare(e)
                self.send(e)
                path = inbox.path if damaged == "inbox" else inbox.state_path
                path.write_bytes(b'{"torn":')
                before = (inbox.path.read_bytes(), inbox.state_path.read_bytes())
                failure = self.send(e, check=False)
                self.assertEqual(failure.returncode, 1)
                self.assertIn("not sent to @operator", failure.stdout)
                self.assertFalse(e.prompts())
                self.assertEqual(e.run("wait", "--seen", "1", check=False).returncode, 2)
                result = e.run("wait", "lead-dev", "--interval", "0.01")
                self.assertEqual(result.stdout.count("inbox needs repair"), 1)
                self.assertIn("@lead-dev stopped (idle)", result.stdout)
                self.assertIn("inbox needs repair", e.run("status").stdout)
                self.assertEqual(before, (inbox.path.read_bytes(), inbox.state_path.read_bytes()))
                fake = e.fake_state()
                fake["panes"] = {"w1:p1": fake["panes"]["w1:p1"]}
                e.write_fake(fake)
                result = e.run("up")
                self.assertIn("inbox needs repair", result.stdout)
                self.assertIn("started @lead-dev", result.stdout)
                self.assertIn("Use this command for teammates", e.run("prompt", "lead-dev").stdout)
                self.assertEqual(before, (inbox.path.read_bytes(), inbox.state_path.read_bytes()))

    def test_unavailable_inbox_degrades_but_cache_directories_may_be_symlinks(self):
        brief = formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]}, operator={"harness": "claude"})
        with Env(brief) as e:
            inbox = self.prepare(e)
            target = e.root / "external-state"
            inbox.state_path.rename(target)
            inbox.state_path.symlink_to(target)
            before = target.read_bytes()
            self.assertIn("inbox unavailable:", e.run("up").stdout)
            self.assertIn("Use this command for teammates", e.run("prompt", "lead-dev").stdout)
            self.assertIn("Use the standalone `factory.py say`", e.run("brief").stdout)
            self.assertIn("operator messages: unavailable", e.run("status").stdout)
            waiting = e.run("wait", "--timeout", "0", check=False)
            self.assertEqual(waiting.returncode, 2)
            self.assertIn(str(inbox.state_path), waiting.stderr)
            self.assertTrue(inbox.state_path.is_symlink())
            self.assertEqual(target.read_bytes(), before)
        for parent in (False, True):
            with self.subTest(parent=parent), Env(brief) as e:
                inbox = self.prepare(e)
                path = inbox.directory.parent if parent else inbox.directory
                target = e.root / "linked-cache"
                path.rename(target)
                path.symlink_to(target, target_is_directory=True)
                target.chmod(0o755)
                self.assertIn("through the inbox", e.run("up").stdout)
                self.assertIn("queued message 1", self.send(e).stdout)
                self.assertTrue(path.is_symlink())
                self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    def test_message_file_read_errors_are_clean_and_do_not_queue(self):
        with Env() as e:
            e.run("up", "lead-dev", "--no-brief")
            inbox = self.prepare(e)
            before = inbox.state_path.read_bytes()
            missing, invalid = e.root / "missing", e.root / "invalid-utf8"
            invalid.write_bytes(b"\xff\xfe")
            for path in (missing, invalid, e.root):
                result = e.say("lead-dev", "operator", "--file", str(path), check=False)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"cannot read message file {path}:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertNotIn("herdr agent prompt", result.stderr)
                self.assertFalse(inbox.path.exists())
                self.assertEqual(inbox.state_path.read_bytes(), before)
            self.assertEqual(e.prompts(), [])

    def test_cold_retention_clears_only_acknowledged_in_current_session(self):
        brief = formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]}, operator={"harness": "claude"})
        for acknowledge in (False, True):
            with self.subTest(acknowledge=acknowledge), Env(brief) as e:
                inbox = self.prepare(e)
                self.seed(e, "earlier message")
                if acknowledge:
                    e.run("wait", "--seen", "1", "--timeout", "0")
                with patch.dict(os.environ, {**e.env, "HERDR_SESSION": "another/session"}):
                    with factory.Inbox(factory.Config(e.repo)) as other:
                        other.save_state()
                    other.path.write_bytes(b"untouched")
                before_other = (other.path.read_bytes(), other.state_path.read_bytes())
                inode = inbox.lock_path.stat().st_ino
                result = e.run("up")
                self.assertEqual(inbox.lock_path.stat().st_ino, inode)
                self.assertEqual(before_other, (other.path.read_bytes(), other.state_path.read_bytes()))
                state = json.loads(inbox.state_path.read_text())
                self.assertEqual(state["seen"], 1 if acknowledge else 0)
                if acknowledge:
                    self.assertEqual(inbox.path.read_bytes(), b"")
                    self.assertNotIn("unread messages from the earlier team", result.stdout)
                    self.assertIn("earlier message", e.run("inbox").stdout)
                    self.assertIn("queued message 2", self.send(e).stdout)
                else:
                    self.assertIn("1 unread message from the earlier team", result.stdout)
                    self.assertIn("earlier message", e.run("wait").stdout)

    def test_partial_restart_and_failed_query_do_not_clear(self):
        brief = formation({"lead-dev": CATALOG_MEMBERS["lead-dev"], "reviewer": CATALOG_MEMBERS["reviewer"]},
                          operator={"harness": "claude"})
        with Env(brief) as e:
            e.run("up", "lead-dev")
            inbox = self.prepare(e)
            self.send(e)
            e.run("wait", "--seen", "1", "--interval", "0.01", "--timeout", "0", check=False)
            inode = inbox.lock_path.stat().st_ino
            for command in (("up", "reviewer"), ("up", "--restart")):
                e.run(*command)
                self.assertTrue(inbox.path.exists())
                self.assertEqual(json.loads(inbox.state_path.read_text())["seen"], 1)
                self.assertEqual(inbox.lock_path.stat().st_ino, inode)
            # Even a restart with no members live must not clear acknowledged transport state.
            fake = e.fake_state()
            fake["panes"] = {"w1:p1": fake["panes"]["w1:p1"]}
            e.write_fake(fake)
            e.run("up", "--restart")
            self.assertEqual(json.loads(inbox.state_path.read_text())["seen"], 1)
            before = (inbox.path.read_bytes(), inbox.state_path.read_bytes())
            args = types.SimpleNamespace(cwd=str(e.repo), names=[], restart=False, fresh=False, timeout=1000, no_brief=False)
            with patch.dict(os.environ, e.env), patch.object(factory, "ensure_operator", return_value="operator"), \
                    patch.object(factory, "Team", side_effect=SystemExit("query failed")):
                with self.assertRaisesRegex(SystemExit, "query failed"):
                    factory.cmd_up(args)
            self.assertEqual(before, (inbox.path.read_bytes(), inbox.state_path.read_bytes()))

    def test_named_watch_wakes_on_any_sender_and_sessions_are_isolated(self):
        with Env(UI) as e:
            e.run("up", "--no-brief")
            inbox = self.prepare(e)
            e.update("lead-dev", agent_status="working")
            watcher = subprocess.Popen([sys.executable, str(SCRIPT), "wait", "lead-dev", "--cwd", str(e.repo),
                                        "--interval", "0.05", "--timeout", "5"], env=e.env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.seed(e, "question", sender="reviewer")
            out, err = watcher.communicate(timeout=10)
            self.assertEqual(watcher.returncode, 0, err)
            self.assertIn("from @reviewer", out)
            other = e.run("wait", "--interval", "0.01", "--timeout", "0.1", check=False, HERDR_SESSION="other")
            self.assertNotIn("question", other.stdout)
            self.assertIn("question", e.run("wait").stdout)


class StockTransportTests(unittest.TestCase):
    def test_snapshot_ack_and_errors_without_herdr_metadata(self):
        with Env(UI) as e:
            e.run("up", "lead-dev", "--no-brief")
            for i, report in enumerate(("first report", "second report"), 1):
                self.assertIn(f"queued message {i}", e.say("lead-dev", "operator", report).stdout)
            e.run("down")
            e.close("operator")
            calls = e.calls()
            blocked = {"FAKE_ROSTER_FAIL": "1", "FAKE_GET_FAIL": "1"}
            with patch.dict(os.environ, e.env):
                inbox = factory.Inbox(factory.Config(e.repo))
            self.assertFalse(inbox.state_path.exists())
            before = inbox.path.read_bytes()
            self.assertIn("second report", e.run("wait", **blocked).stdout)
            first = e.run("wait", "--timeout", "0", **blocked).stdout
            self.assertIn("first report", first)
            self.assertIn("second report", first)
            invalid = e.run("wait", "--timeout", "0", "--seen", "3", check=False, **blocked)
            self.assertEqual(invalid.returncode, 2)
            self.assertIn("invalid inbox id 3", invalid.stderr)
            self.assertFalse(inbox.state_path.exists())
            self.assertEqual(inbox.path.read_bytes(), before)
            remaining = e.run("wait", "--timeout", "0", "--seen", "1", **blocked).stdout
            self.assertNotIn("first report", remaining)
            self.assertIn("second report", remaining)
            self.assertIn("wait --seen 2 --timeout 0", remaining)
            self.assertEqual(e.run("wait", "--timeout", "0", "--seen", "2", **blocked).stdout, "")
            self.assertIn("first report", e.run("inbox", **blocked).stdout)
            inbox.path.write_text('{"torn":')
            damaged = e.run("wait", "--timeout", "0", check=False, **blocked)
            self.assertEqual(damaged.returncode, 2)
            self.assertIn("inbox needs repair", damaged.stderr)
            self.assertEqual(e.calls(), calls)

    def test_send_to_codex_only_appends_and_consumes_draft(self):
        with Env(UI) as e:
            e.update(None, agent="codex")
            e.run("up", "lead-dev", "--no-brief")
            e.update("operator", agent_session={"agent": "codex", "kind": "id",
                                                "value": "01234567-89ab-cdef-0123-456789abcdef"},
                     procs=[{"name": "codex", "argv": ["codex"]}])
            native_calls = e.root / "codex-invoked"
            (e.root / "bin/codex").write_text(f"#!/bin/sh\nprintf called > {shlex.quote(str(native_calls))}\n")
            with patch.dict(os.environ, e.env):
                draft = factory.Config(e.repo).messages / "lead-dev/report.md"
            draft.write_text("a Codex operator report")
            before = len(e.calls())
            result = e.say("lead-dev", "operator", "--file", str(draft))
            self.assertEqual(result.stdout, "queued message 1 for @operator\n")
            self.assertEqual(result.stderr, "")
            self.assertFalse(draft.exists())
            self.assertFalse(native_calls.exists())
            self.assertEqual(e.calls()[before:], [["agent", "list"], ["pane", "list"]])
            snapshot = e.run("wait", "--timeout", "0").stdout
            self.assertEqual(snapshot.count("[Message 1 from @lead-dev"), 1)
            self.assertIn("a Codex operator report", snapshot)


class WaitTests(unittest.TestCase):
    def test_wait_deadline_uses_the_first_verified_operator(self):
        codex = {"agent": "codex", "pane_id": "w1:p1"}
        pi = {"agent": "pi", "pane_id": "w1:p1"}
        omp = {"agent": "omp", "pane_id": "w1:p1"}
        claude = {"agent": "claude", "pane_id": "w1:p1"}
        opencode = {"agent": "opencode", "pane_id": "w1:p1"}

        def roster(operator, named_pane):
            return types.SimpleNamespace(
                agent_panes={"captain": named_pane} if named_pane else {},
                pane={"captain": operator, "lead-dev": {"pane_id": "w1:p2"}}.get,
                status=lambda _name: "working",
            )

        cases = (
            ("Codex", codex, "w1:p1", None, 60, 60),
            ("pi", pi, "w1:p1", None, 60, 60),
            ("omp", omp, "w1:p1", None, 240, 240),
            ("Claude", claude, "w1:p1", None, 1800, None),
            ("OpenCode", opencode, "w1:p1", None, 1800, None),
            ("missing pane", None, "w1:p1", None, 1800, None),
            ("unbound Codex pane", codex, None, None, 1800, None),
            ("mismatched Codex", codex, "w1:p2", None, 1800, None),
            ("unbound pi pane", pi, None, None, 1800, None),
            ("mismatched pi", pi, "w1:p2", None, 1800, None),
            ("unbound omp pane", omp, None, None, 1800, None),
            ("mismatched omp", omp, "w1:p2", None, 1800, None),
            ("explicit Codex", codex, "w1:p1", 12, 12, 12),
            ("explicit pi", pi, "w1:p1", 12, 12, 12),
            ("explicit omp", omp, "w1:p1", 12, 12, 12),
            ("explicit background", claude, "w1:p1", 12, 12, 12),
        )
        brief = formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]},
                          operator={"name": "captain", "harness": "codex"})
        for case, operator, named_pane, explicit, named_default, unnamed_default in cases:
            for names, expected in ((["@lead-dev"], named_default), ([], unnamed_default)):
                with self.subTest(case=case, names=names), Env(brief) as e, patch.dict(os.environ, e.env):
                    cfg = factory.Config(e.repo)
                    args = types.SimpleNamespace(cwd=str(e.repo), names=names, seen=None, timeout=explicit, interval=0)
                    now = 0
                    ticks = iter((59, 1800) if expected is None else (expected - 1, expected))

                    def advance(_):
                        nonlocal now
                        now = next(ticks)
                        if expected is None and now == 1800:
                            self.assertEqual(factory.queue_message(cfg, "lead-dev", "still listening"), ("queued", 1))

                    first = roster(operator, named_pane)
                    # Later polls cannot start or reset the invocation's default deadline.
                    later = roster(claude if expected == 60 else codex, "w1:p1")
                    clock = types.SimpleNamespace(monotonic=lambda: now, sleep=advance)
                    out = io.StringIO()
                    with patch.object(factory, "Team", side_effect=[first, later, later]) as team, \
                            patch.object(factory, "time", clock), contextlib.redirect_stdout(out):
                        if expected is None:
                            factory.cmd_wait(args)
                            self.assertIn("still listening", out.getvalue())
                            self.assertEqual(team.call_count, 2)
                        else:
                            with self.assertRaises(SystemExit) as exited:
                                factory.cmd_wait(args)
                            self.assertEqual(exited.exception.code, 4)
                            self.assertIn(f"timeout after {expected}s", out.getvalue())
                            self.assertEqual(team.call_count, 3)

    def test_quiet_watch_ignores_idle_until_message_or_inbox_failure(self):
        for harness, event in (("claude", "message"), ("claude", "damage"), ("claude", "unavailable"),
                               ("agy", "message")):
            brief = formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]}, operator={"harness": harness})
            with self.subTest(harness=harness, event=event), Env(brief) as e:
                e.update(None, agent=harness)
                e.run("up", "--no-brief")
                e.update("lead-dev", agent_status="working")
                state = e.fake_state()
                state["on_second_poll"] = {"name": "lead-dev", "status": "done"}
                e.write_fake(state)
                sleeps = []
                with patch.dict(os.environ, e.env):
                    inbox = factory.Inbox(factory.Config(e.repo))
                def after_poll(_):
                    sleeps.append(e.fake_state()["polls"])
                    if len(sleeps) == 2:
                        if event == "message":
                            self.assertEqual(factory.queue_message(factory.Config(e.repo), "lead-dev",
                                                                   "report after idle"), ("queued", 1))
                        elif event == "damage":
                            inbox.path.write_bytes(b'{"torn":')
                        else:
                            inbox.path.symlink_to(e.root / "missing")
                    elif len(sleeps) > 2:
                        self.fail("watch did not return after message or inbox error")
                args = types.SimpleNamespace(cwd=str(e.repo), names=[], seen=None, timeout=None, interval=0)
                out, err = io.StringIO(), io.StringIO()
                # Patch factory's binding: changing time.sleep also intercepts subprocess timeout polling.
                with patch.dict(os.environ, e.env), patch.object(factory, "HERDR", e.env["HERDR_BIN"]), \
                        patch.object(factory, "time", types.SimpleNamespace(sleep=after_poll)), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    if event == "message":
                        factory.cmd_wait(args)
                    else:
                        with self.assertRaises(SystemExit) as ex:
                            factory.cmd_wait(args)
                        self.assertEqual(ex.exception.code, 2)
                self.assertEqual(sleeps, [1, 2])  # The done transition did not end the watch.
                self.assertNotIn("stopped", out.getvalue())
                if event == "message":
                    self.assertIn("report after idle", out.getvalue())
                else:
                    self.assertIn(str(inbox.path), err.getvalue())

    def test_quiet_watch_returns_problems_but_named_watch_keeps_lifecycle(self):
        brief = formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]}, operator={"harness": "claude"})
        for change, expected in (({"exit": True}, "gone"), ({"close": True}, "closed"),
                                 ({"status": "blocked"}, "blocked"), ({"status": "idle"}, "stopped")):
            with self.subTest(change=change), Env(brief) as e:
                e.run("up", "--no-brief")
                e.update("lead-dev", agent_status="working")
                state = e.fake_state()
                state["on_second_poll"] = {"name": "lead-dev", **change}
                e.write_fake(state)
                names = ["lead-dev"] if expected == "stopped" else []
                result = e.run("wait", *names, "--interval", "0.01", "--timeout", "2")
                self.assertIn(expected, result.stdout)

    def test_quiet_timeout_shows_unknown_and_working_members(self):
        brief = formation({n: CATALOG_MEMBERS[n] for n in ("lead-dev", "reviewer")}, operator={"harness": "claude"})
        with Env(brief) as e:
            e.run("up", "--no-brief")
            e.update("lead-dev", agent_status="unknown")
            e.update("reviewer", agent_status="working")
            result = e.run("wait", "--interval", "0.01", "--timeout", "0.2", check=False)
            self.assertEqual(result.returncode, 4)
            self.assertIn("still working: @reviewer; status unknown: @lead-dev", result.stdout)
            e.update("reviewer", agent_status="done")
            result = e.run("wait", "--interval", "0.01", "--timeout", "0.2", check=False)
            self.assertEqual(result.returncode, 4)
            self.assertIn("nobody is working; status unknown: @lead-dev", result.stdout)

    def test_wait_returns_when_the_lead_is_needed(self):
        with Env() as e:
            e.update(None, agent="codex")
            self.assertIn("not started: any member", e.run("wait", check=False).stderr)
            e.run("up", "lead-dev", "reviewer", "pm", "--no-brief")
            for name in ("lead-dev", "reviewer", "pm"):
                e.update(name, agent_status="working")
            p = e.run("wait", "--interval", "0.05", "--timeout", "0.2", check=False)
            self.assertEqual(p.returncode, 4)
            self.assertIn("still working: @lead-dev, @reviewer, @pm", p.stdout)

            e.update("reviewer", agent_status="blocked")
            out = e.run("wait").stdout
            self.assertIn("@reviewer is blocked: it needs an answer", out)
            e.update("reviewer", agent_status="working")
            e.kill("pm")
            self.assertIn("@pm is gone: its agent exited. `factory.py up pm`", e.run("wait").stdout)

            e.run("up", "pm")
            e.update("pm", agent_status="working")
            e.update("lead-dev", agent_status="idle")  # a watched member stopped while others still work
            self.assertIn("@lead-dev stopped (idle)", e.run("wait", "lead-dev", "--interval", "0.05").stdout)
            p = e.run("wait", "--interval", "0.05", "--timeout", "0.2", check=False)
            self.assertEqual(p.returncode, 4)  # without names, others working means: keep waiting
            for name in ("reviewer", "pm"):
                e.update(name, agent_status="idle")
            self.assertIn("nobody is working: @lead-dev idle, @reviewer idle, @pm idle", e.run("wait", "--interval", "0.05").stdout)

    def test_default_wait_notices_a_finished_member_while_others_work(self):
        with Env() as e:
            e.update(None, agent="codex")
            e.run("up", "lead-dev", "reviewer", "--no-brief")
            e.update("lead-dev", agent_status="working")
            e.update("reviewer", agent_status="working")
            state = e.fake_state()
            state["on_second_poll"] = {"name": "lead-dev", "status": "done"}
            e.write_fake(state)
            result = e.run("wait", "--interval", "0.01", "--timeout", "2")
            self.assertIn("@lead-dev stopped (done)", result.stdout)
            self.assertEqual(e.pane_of("reviewer")["agent_status"], "working")

    def test_named_wait_ignores_other_members(self):
        for change in (*({"status": s} for s in ("idle", "done", "blocked", "unknown")),
                       {"close": True}, {"exit": True}):
            with self.subTest(change=change), Env() as e:
                e.run("up", "lead-dev", "reviewer", "--no-brief")
                for name in ("lead-dev", "reviewer"):
                    e.update(name, agent_status="working")
                state = e.fake_state()
                state["on_second_poll"] = {"name": "reviewer", **change}
                e.write_fake(state)
                result = e.run("wait", "@lead-dev", "--interval", "0.01", "--timeout", "1", check=False)
                self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
                self.assertIn("still working: @lead-dev", result.stdout)
                self.assertNotIn("reviewer", result.stdout)
                self.assertGreaterEqual(e.fake_state()["polls"], 2)  # the unrelated transition really happened

    def test_named_wait_notices_selected_members(self):
        changes = [({"status": s}, s) for s in ("idle", "done", "blocked", "unknown")]
        for change, expected in changes + [({"close": True}, "closed"), ({"exit": True}, "gone")]:
            with self.subTest(change=change), Env() as e:
                e.run("up", "lead-dev", "reviewer", "pm", "--no-brief")
                for name in ("lead-dev", "reviewer", "pm"):
                    e.update(name, agent_status="working")
                state = e.fake_state()
                state["on_second_poll"] = {"name": "reviewer", **change}
                e.write_fake(state)
                result = e.run("wait", "lead-dev", "@reviewer", "--interval", "0.01", "--timeout", "2")
                self.assertIn("@reviewer", result.stdout)
                self.assertIn(expected, result.stdout)
                self.assertNotIn("@pm", result.stdout)
        with Env() as e:
            e.run("up", "lead-dev", "--no-brief")
            e.update("lead-dev", agent_status="working")
            for name in ("reviewer", "operator"):
                result = e.run("wait", "lead-dev", name, "--timeout", "0.2", "--interval", "0.01", check=False)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"not started: {name}", result.stderr)

    def test_wait_reports_disappearance_and_unknown(self):
        for change, expected in (({"close": True}, "closed"), ({"status": "unknown"}, "unknown")):
            with self.subTest(change=change), Env() as e:
                e.update(None, agent="codex")
                e.run("up", "lead-dev", "reviewer", "--no-brief")
                e.update("lead-dev", agent_status="working")
                e.update("reviewer", agent_status="working")
                state = e.fake_state()
                state["on_second_poll"] = {"name": "lead-dev", **change}
                e.write_fake(state)
                result = e.run("wait", "--interval", "0.01", "--timeout", "2")
                self.assertIn("@lead-dev", result.stdout)
                self.assertIn(expected, result.stdout)
                self.assertNotIn("@lead-dev stopped", result.stdout)
                self.assertNotIn("nobody is working", result.stdout)

    def test_terminal_reads_and_keys_are_limited_to_known_startup_dialogs(self):
        with Env() as e:
            launched = e.run("up", "lead-dev", "pm", check=False, FAKE_START_FAIL="dialog:lead-dev,blocked:pm")
            self.assertEqual(launched.returncode, 3)
            self.assertIn("@pm launch incomplete", launched.stdout)
            pane = e.pane_of("lead-dev")["pane_id"]
            self.assertIn("@pm blocked:", e.run("status").stdout)
            for state, expected in (("blocked", "@pm is blocked"), ("unknown", "@pm: status unknown"),
                                    ("idle", "@pm stopped (idle)")):
                e.update("pm", agent_status=state)
                self.assertIn(expected, e.run("wait", "pm", "--interval", "0", "--timeout", "20").stdout)
            failed = e.run("brief", "--send", "pm", check=False, FAKE_UNCONFIRMED="pm")
            self.assertIn("delivery unconfirmed to @pm", failed.stdout)
            self.assertIn("queued message 1", e.say("lead-dev", "operator", "a report").stdout)
            self.assertIn("a report", e.run("wait").stdout)
            e.run("wait", "--seen", "1", "--timeout", "0", check=False)
            self.assertIn("a report", e.run("inbox").stdout)
            e.run("down", "lead-dev", "pm")

            read = ["pane", "read", pane, "--source", "visible"]
            terminal_calls = [c for c in e.calls() if len(c) > 1 and c[0] in ("pane", "agent", "terminal")
                              and c[1] in ("read", "snapshot", "wait-output", "send-keys")]
            self.assertEqual(terminal_calls, [read, ["pane", "send-keys", pane, "enter"], read])


class UpRecoveryTests(unittest.TestCase):
    def test_launch_matrix(self):
        for flags in ((), ("--fresh",), ("--restart",), ("--restart", "--fresh")):
            with self.subTest(flags=flags), Env(UI) as e:
                # One live member, two closed panes (matching and incompatible sessions), one newcomer.
                e.run("up", "lead-dev", "frontend-dev", "pm", "--no-brief")
                for name, agent in (("lead-dev", "codex"), ("frontend-dev", "claude"), ("pm", "codex")):
                    e.set_session(name, {"agent": agent, "kind": "id", "value": f"saved-{name}"})
                e.run("status")
                e.close("frontend-dev", "pm")
                before = e.pane_of("lead-dev")["pane_id"]
                out = e.run("up", *flags).stdout
                restarted = "--restart" in flags
                fresh = "--fresh" in flags
                targets = ["frontend-dev", "reviewer", "pm"]
                if restarted:
                    targets.insert(0, "lead-dev")
                self.assertEqual([c[2] for c in e.calls("agent", "start")[3:]], targets)
                self.assertEqual(e.calls("pane", "close"), [["pane", "close", before]] if restarted else [])
                prompts = dict(e.prompts())
                self.assertEqual(len(e.prompts()), 4)
                for name in targets:
                    resumed = name in ("lead-dev", "frontend-dev") and not fresh
                    self.assertEqual(f"saved-{name}" in native(e.starts()[name]), resumed)
                    self.assertIn(f"{'resumed' if resumed else 'started'} @{name} (", out)
                    closing = prompts[name].rstrip().rsplit("\n\n", 1)[-1]
                    if resumed:
                        self.assertIn("picking up where we left off", closing)
                        self.assertNotIn("`ready`", closing)
                    elif name == "reviewer":
                        self.assertEqual(closing, READY)
                    else:
                        self.assertEqual(closing, "You are starting in a new session and know nothing of the work so far. " + READY)
                if not restarted:
                    self.assertIn("Joined the team: @reviewer", prompts["lead-dev"])
                    self.assertNotIn("@frontend-dev", prompts["lead-dev"])
                    self.assertNotIn("@pm", prompts["lead-dev"])

    def test_restart_with_no_saved_session_sends_no_join(self):
        with Env(UI) as e:
            e.run("up", "lead-dev", "pm", "--no-brief")
            e.run("up", "pm", "--restart")
            self.assertEqual([name for name, _ in e.prompts()], ["pm"])
            self.assertIn("starting in a new session", e.prompts()[0][1])

    def test_launch_reports_name_the_operator_and_manual_briefs_ask_nothing(self):
        with Env(formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]}, operator={"name": "boss"})) as e:
            e.run("up")
            self.assertTrue(e.prompts()[0][1].endswith(READY.replace("@operator", "@boss") + "\n"))
            self.assertEqual(len(e.prompts()), 1)
            e.run("up")  # already running: nothing sent
            e.run("brief", "--send", "lead-dev")
            self.assertEqual(len(e.prompts()), 2)
            self.assertTrue(e.prompts()[1][1].endswith("That is the whole team brief. No reply needed.\n"))
            e.set_session("lead-dev", {"agent": "codex", "kind": "id", "value": "saved"})
            e.run("up", "lead-dev", "--restart")
            self.assertEqual(len(e.prompts()), 3)
            self.assertIn("send @boss a new one-line pickup report for this launch, even if you sent one before.",
                          e.prompts()[2][1])

    def test_up_names_running_members_without_their_launch_flags(self):
        restored = {"lead-dev": [{"pid": 7, "name": "codex", "argv": ["codex", "resume", "cx-1"]}],
                    "frontend-dev": [{"pid": 8, "name": ".claude-wrapped", "argv": ["/bin/claude", "--resume", "c",
                                                                                     "--permission-mode", "auto"]}]}
        cases = {
            "matching": ({}, None),
            "missing flags": (restored, "lead-dev frontend-dev"),
            "unrelated foreground process": ({"lead-dev": [{"pid": 9, "name": "bash", "argv": ["bash"]}]}, None),
            "ambiguous": ({"lead-dev": restored["lead-dev"] * 2}, None),
            "no data": ({"lead-dev": []}, None),
            "no argv": ({"lead-dev": [{"pid": 7, "name": "codex"}]}, None),
        }
        for case, (procs, named) in cases.items():
            with self.subTest(case), Env(UI) as e:
                e.run("up", "lead-dev", "frontend-dev", "pm", "--no-brief")
                for name, p in procs.items():
                    e.update(name, procs=p)
                before = len(e.calls())
                out = e.run("up", "lead-dev", "frontend-dev", "pm").stdout
                self.assertIn("already running: lead-dev, frontend-dev, pm", out)
                # pi has no permission flags, so its process is never read
                read = [c[3] for c in e.calls("pane", "process-info")]
                self.assertEqual(read, [e.pane_of("lead-dev")["pane_id"], e.pane_of("frontend-dev")["pane_id"]])
                if named:
                    self.assertIn("running without their factory launch flags, as after a herdr restart:"
                                  f" {named.replace(' ', ', ')}. Run factory.py up {named} --restart", out)
                    self.assertNotIn("--resume", out)  # no raw argv
                else:
                    self.assertNotIn("launch flags", out)
                calls = e.calls()[before:]
                self.assertFalse([c for c in calls if c[:2] in (["pane", "close"], ["agent", "start"], ["agent", "prompt"])])

    def test_launch_flags_check_needs_args_too_and_skips_restarts(self):
        with Env(formation({"lead-dev": {**CATALOG_MEMBERS["lead-dev"], "args": ["-c", "x=1"]}})) as e:
            e.run("up", "--no-brief")
            argv = native(e.starts()["lead-dev"])
            e.update("lead-dev", procs=[{"pid": 7, "name": "codex", "argv": ["codex", *[a for a in argv if a != "x=1"]]}])
            self.assertIn("Run factory.py up lead-dev --restart", e.run("up").stdout)
            e.run("up", "--restart")
            self.assertNotIn("launch flags", e.run("up").stdout)  # the relaunch is checked by its own argv
            self.assertEqual(len(e.calls("pane", "process-info")), 2)

    def test_taken_name_prevents_an_earlier_restart(self):
        with Env(UI) as e:
            e.run("up", "lead-dev", "pm", "--no-brief")
            state = e.fake_state()
            state["panes"][e.pane_of("pm")["pane_id"]]["name"] = "reviewer"
            e.write_fake(state)
            before = len(e.calls())
            p = e.run("up", "lead-dev", "reviewer", "--restart", check=False)
            self.assertEqual(p.returncode, 2)
            self.assertIn("outside this team", p.stderr)
            calls = e.calls()[before:]
            self.assertEqual(calls, [["agent", "get", e.env["HERDR_PANE_ID"]], ["agent", "list"], ["pane", "list"]])

    def test_operator_pane_with_an_old_member_label_is_not_a_restart_target(self):
        with Env(formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]}, operator={"harness": "claude"})) as e:
            state = e.fake_state()
            pane = state["panes"]["w1:p1"]
            pane.update(label="lead-dev", name="lead-dev",
                        agent_session={"agent": "claude", "kind": "id", "value": "operator-session"})
            e.write_fake(state)
            self.assertIn("through the inbox", e.run("up", "--restart").stdout)
            self.assertEqual(e.calls("pane", "close"), [])
            self.assertEqual(e.pane_of("operator")["pane_id"], "w1:p1")
            self.assertNotEqual(e.pane_of("lead-dev")["pane_id"], "w1:p1")
            self.assertIn(READY, dict(e.prompts())["lead-dev"])

    def test_continue_stub_never_launches(self):
        with Env(UI) as e:
            for args in ((), ("pm", "--restart", "--fresh")):
                p = e.run("continue", *args, check=False)
                self.assertEqual(p.returncode, 2)
                self.assertEqual((p.stdout + p.stderr).splitlines(), ["use factory.py up [names] [--fresh] [--restart]"])
            self.assertEqual(e.calls(), [])

    def test_no_member_panes_resumes_noted_sessions(self):
        marker = "This brief applies to the resumed team."
        with Env(UI + "\n" + marker + "\n") as e:
            e.run("up", "--no-brief")
            e.set_session("lead-dev", {"agent": "codex", "kind": "id", "value": "cx-1"})
            e.set_session("pm", {"agent": "pi", "kind": "path", "value": "/s/p.jsonl"})
            e.run("status")
            e.close("lead-dev", "frontend-dev", "reviewer", "pm")
            out = e.run("up").stdout
            self.assertEqual(len(e.calls("agent", "start")), 8)
            by = {n: native(c) for n, c in e.starts().items()}
            self.assertEqual(by["lead-dev"][:2], ["resume", "cx-1"])
            self.assertEqual(flag(by["pm"], "--session"), "/s/p.jsonl")
            for name in ("frontend-dev", "reviewer"):
                self.assertTrue({"resume", "--resume", "--session"}.isdisjoint(by[name]))
            sent = dict(e.prompts())
            self.assertEqual(len(sent), 4)
            for message in sent.values():
                self.assertEqual(message.count(marker), 1)
                closing = message.rstrip().rsplit("\n\n", 1)[-1]
                self.assertNotIn("re-read", closing.lower())
            for name in ("lead-dev", "pm"):
                self.assertIn("picking up where we left off", sent[name])
                self.assertNotIn("No reply needed", sent[name])
            for name in ("frontend-dev", "reviewer"):
                self.assertTrue(sent[name].endswith(READY + "\n"))
                self.assertIn(f"started @{name}", out)

    def test_bare_up_recovers_exited_and_closed_members_without_disturbing_live_members(self):
        with Env(UI) as e:
            e.run("up", "--no-brief")
            for name in ("lead-dev", "reviewer"):
                e.set_session(name, {"agent": "codex", "kind": "id", "value": f"saved-{name}"})
            e.kill("lead-dev")
            e.close("reviewer")
            e.run("up")
            self.assertEqual([c[2] for c in e.calls("agent", "start")[4:]], ["lead-dev", "reviewer"])
            self.assertEqual([n for n, _ in e.prompts()], ["lead-dev", "reviewer"])

    def test_up_without_members_says_so(self):
        with Env(formation({})) as e:
            result = e.run("up", check=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn("no members in factory.toml; add one, then run `factory.py up`", result.stderr)
            self.assertEqual(e.starts(), {})

    def test_cold_up_checks_every_member_before_launch(self):
        with Env(UI.replace('model = "gpt-6-luna"', 'model = "missing-model"')) as e:
            result = e.run("up", check=False)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(e.starts(), {})
            self.assertEqual(e.calls("tab", "create"), [])

    def test_failed_fresh_launch_is_not_reported_as_fresh(self):
        with Env(UI) as e:
            result = e.run("up", check=False, FAKE_START_FAIL="dead:lead-dev")
            self.assertEqual(result.returncode, 3)
            self.assertNotIn("started @lead-dev (", result.stdout)
            for name in ("frontend-dev", "reviewer", "pm"):
                self.assertIn(f"started @{name} (", result.stdout)
            self.assertEqual({n for n, _ in e.prompts()}, {"frontend-dev", "reviewer", "pm"})

    def test_brings_members_back_in_their_panes_and_sessions(self):
        with Env() as e:
            e.run("up", "lead-dev", "frontend-dev", "pm", "--no-brief")
            e.set_session("lead-dev", {"source": "herdr:codex", "agent": "codex", "kind": "id", "value": "cx-1"})
            e.set_session("frontend-dev", {"source": "herdr:claude", "agent": "claude", "kind": "id", "value": "cl-1"})
            e.set_session("pm", {"source": "herdr:pi", "agent": "pi", "kind": "path", "value": "/s/2026_pi-1.jsonl"})
            self.assertIn("already running", e.run("up", "lead-dev", "frontend-dev", "pm").stdout)
            self.assertEqual(e.prompts(), [])
            self.assertEqual(e.calls("agent", "rename"), [["agent", "rename", "w1:p1", "operator"]])
            e.kill("lead-dev", "frontend-dev", "pm")
            panes_before = {n: e.pane_of(n)["pane_id"] for n in ("lead-dev", "frontend-dev", "pm")}
            # The brief changed while they were gone: lead-dev moves to Luna.
            e.write_brief(CATALOG.replace('model = "gpt-6-astra"\nthinking = "high"',
                                           'model = "gpt-6-luna"\nthinking = "medium"'))
            self.assertIn("`factory.py up lead-dev frontend-dev pm`", e.run("status").stdout)

            before = len(e.prompts())
            e.run("up", "lead-dev", "frontend-dev", "pm")
            by = {c[2]: c for c in e.calls("agent", "start")[3:]}
            self.assertEqual({n: flag(c, "--pane") for n, c in by.items()}, panes_before)  # same panes, no new ones
            self.assertEqual(len(e.calls("tab", "create")) + len(e.calls("pane", "split")), 3)
            self.assertEqual(native(by["lead-dev"])[:2], ["resume", "cx-1"])
            self.assertEqual(flag(native(by["lead-dev"]), "-C"), str(e.repo))
            self.assertEqual(flag(native(by["lead-dev"]), "-m"), "gpt-6-luna")  # current settings apply
            self.assertEqual(flag(native(by["frontend-dev"]), "--resume"), "cl-1")
            self.assertEqual(flag(native(by["pm"]), "--session"), "/s/2026_pi-1.jsonl")
            sent = e.prompts()[before:]
            self.assertEqual(sorted(n for n, _ in sent), ["frontend-dev", "lead-dev", "pm"])  # one message each
            for _, text in sent:
                self.assertIn("picking up where we left off", text)
                self.assertIn("Send it with the say command", text)
                self.assertIn("A reply in your own pane does not reach @operator", text)
                self.assertIn("# Team brief", text)
                self.assertNotIn("No reply needed", text)

            e.kill("pm")
            before = len(e.prompts())
            e.run("up", "pm")  # one member comes back: only it is re-briefed
            self.assertEqual([n for n, _ in e.prompts()[before:]], ["pm"])

    def test_restart_checks_before_closing_and_reapplies_settings(self):
        with Env(UI) as e:
            e.run("up", "--no-brief")
            old = e.pane_of("lead-dev")["pane_id"]
            others = {n: {k: v for k, v in e.pane_of(n).items() if k != "rect"}
                      for n in ("operator", "reviewer", "frontend-dev", "pm")}
            e.set_session("lead-dev", {"agent": "codex", "kind": "id", "value": "restart-session"})
            e.write_brief(UI.replace('model = "gpt-6-astra"', 'model = "missing-model"'))
            failed = e.run("up", "lead-dev", "--restart", check=False)
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(e.calls("pane", "close"), [])
            self.assertEqual(e.pane_of("lead-dev")["pane_id"], old)
            e.write_brief(UI.replace('model = "gpt-6-astra"', 'model = "gpt-6-luna"'))
            before = len(e.prompts())
            e.run("up", "lead-dev", "--restart")
            argv = native(e.starts()["lead-dev"])
            self.assertEqual(argv[:2], ["resume", "restart-session"])
            self.assertEqual(flag(argv, "-m"), "gpt-6-luna")
            self.assertEqual(flag(argv, "-C"), str(e.repo))
            self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
            self.assertEqual(e.calls("pane", "close"), [["pane", "close", old]])
            self.assertNotIn(old, e.fake_state()["panes"])
            self.assertNotEqual(e.pane_of("lead-dev")["pane_id"], old)
            self.assertEqual([n for n, _ in e.prompts()[before:]], ["lead-dev"])
            self.assertIn("Send it with the say command", e.prompts()[-1][1])
            self.assertIn("picking up where we left off", e.prompts()[-1][1])
            self.assertEqual({n: {k: v for k, v in e.pane_of(n).items() if k != "rect"} for n in others}, others)
            e.run("up", "lead-dev", "--fresh", "--restart")
            self.assertNotIn("resume", native(e.starts()["lead-dev"]))
            self.assertIn("starting in a new session", e.prompts()[-1][1])

    def test_closed_pane_or_changed_harness_starts_fresh(self):
        with Env() as e:
            e.run("up", "pm", "reviewer", "--no-brief")
            e.set_session("pm", {"agent": "pi", "kind": "path", "value": "/s/p.jsonl"})
            e.kill("pm")
            e.close("reviewer")
            e.write_brief(CATALOG.replace('model = "deepseek/deepseek-v4.1-flash-1017"', 'model = "gpt-6-sol"')
                          .replace('[members.pm]\nharness = "pi"', '[members.pm]\nharness = "codex"'))
            out = e.run("up", "pm", "reviewer").stdout
            self.assertIn("started @reviewer", out)
            by = {c[2]: native(c) for c in e.calls("agent", "start")[2:]}
            self.assertNotIn("resume", by["pm"])  # a pi session can't open in codex
            self.assertNotIn("resume", by["reviewer"])
            self.assertEqual(e.pane_of("reviewer")["pane_id"], "w1:p4")  # a new, labeled pane
            self.assertEqual(e.pane_of("reviewer")["tab_id"], e.pane_of("pm")["tab_id"])  # in the team tab
            self.assertIn("starting in a new session", dict(e.prompts())["pm"])
            self.assertTrue(dict(e.prompts())["pm"].endswith(READY + "\n"))  # a changed harness starts fresh
            self.assertTrue(dict(e.prompts())["reviewer"].endswith(READY + "\n"))

    def test_opencode_member_gets_a_new_pane_and_the_old_label_is_cleared(self):
        with Env(formation({
            'reviewer': {'harness': 'opencode', 'model': 'z-ai/glm-5.3'},
            'pm': {'harness': 'pi', 'model': 'z-ai/glm-5.3'},
        })) as e:
            e.run("up", "pm", "reviewer", "--no-brief")
            e.set_session("reviewer", {"agent": "opencode", "kind": "id", "value": "ses_1"})
            e.kill("reviewer")
            e.run("up", "reviewer")
            labeled = [p for p in e.fake_state()["panes"].values() if p["label"] == "reviewer"]
            self.assertEqual([p["pane_id"] for p in labeled], ["w1:p4"])  # its environment is set at creation
            self.assertEqual(native(e.calls("agent", "start")[-1]), ["--standalone", "--session", "ses_1", "--auto"])

    def test_failed_resume_says_how_to_start_fresh(self):
        with Env() as e:
            e.run("up", "pm", "reviewer", "--no-brief")
            e.set_session("pm", {"agent": "pi", "kind": "id", "value": "old"})
            e.kill("pm", "reviewer")
            p = e.run("up", check=False, FAKE_START_FAIL="dead:pm")
            self.assertEqual(p.returncode, 3)
            self.assertIn("factory.py up pm --fresh", p.stdout)
            self.assertIn("started @reviewer", p.stdout)  # the others still came back

    def test_duplicate_session_observations_keep_latest_resume(self):
        with Env() as e:
            e.run("up", "lead-dev", "--no-brief")
            for value in ("first", "second"):
                e.set_session("lead-dev", {"agent": "codex", "kind": "id", "value": value})
                e.run("status")
            history = next((e.root / "cache/factory-skill").glob("*/sessions.jsonl"))
            lines = history.read_text().splitlines()
            with history.open("a") as f:
                f.write(lines[-1] + "\n")
            e.kill("lead-dev")
            e.run("up", "lead-dev")
            self.assertEqual(native(e.starts()["lead-dev"])[:2], ["resume", "second"])


class DownTests(unittest.TestCase):
    def test_down_hints_at_repair_only_when_no_session_was_recorded(self):
        names = ("architect", "designer", "lead-dev", "reviewer")
        members = {name: {"harness": "opencode", "model": "openrouter/z-ai/glm-5.3"} for name in names}
        with Env(formation(members)) as e:
            e.run("up", "--no-brief")
            e.set_session("architect", {"agent": "opencode", "kind": "id", "value": "compatible"})
            e.set_session("designer", {"agent": "claude", "kind": "id", "value": "older"})
            e.run("status")  # save both older refs
            for name in ("architect", "designer"):
                e.set_session(name, None)
            p = e.run("down", FAKE_INTEGRATION_STATUS="opencode: needs repair (v13) (/hooks/opencode)")
            hints = [line for line in p.stdout.splitlines() if "may have prevented session recording" in line]
            self.assertEqual(len(hints), 2)
            self.assertIn("@lead-dev", hints[0])
            self.assertIn("@reviewer", hints[1])
            self.assertIn("no compatible session recorded for @designer: up will start it fresh\n", p.stdout)
            self.assertNotIn("no compatible session recorded for @architect", p.stdout)
            self.assertEqual(e.calls("integration"), [["integration", "status"]] * 2)  # one for up, one for down

    def test_down_records_sessions_for_up_and_preserves_messages(self):
        with Env(UI) as e:
            e.run("up", "lead-dev", "pm", "--no-brief")
            e.set_session("lead-dev", {"agent": "codex", "kind": "id", "value": "old"})
            e.run("status")
            e.set_session("lead-dev", {"agent": "codex", "kind": "id", "value": "latest"})
            e.set_session("pm", {"agent": "pi", "kind": "path", "value": "/sessions/pm.jsonl"})
            e.update("lead-dev", agent_status="working")
            e.update("pm", agent_status="blocked")
            e.say("lead-dev", "operator", "acknowledged report")
            e.run("wait", "--seen", "1", "--timeout", "0")
            self.assertIn("queued message", e.say("lead-dev", "operator", "unread report").stdout)
            with patch.dict(os.environ, e.env):
                cfg = factory.Config(e.repo)
                draft = cfg.messages / "pm" / "pending.md"
                draft.write_text("unsent report")
                instruction = cfg.session_dir / "pm.md"
                with factory.Inbox(cfg) as inbox:
                    self.assertEqual([row["text"] for row in inbox.unread], ["unread report"])
                    preserved = {p: p.read_bytes() for p in (draft, instruction, inbox.path, inbox.state_path)}
            operator = e.pane_of("operator")
            targets = [e.pane_of(name)["pane_id"] for name in ("lead-dev", "pm")]
            out = e.run("down").stdout
            self.assertIn("stopped @lead-dev (was working)", out)
            self.assertIn("stopped @pm (was blocked)", out)
            self.assertIn("@reviewer was not running", out)
            self.assertIn(str(draft), out)
            self.assertEqual(e.calls("pane", "close"), [["pane", "close", pane] for pane in targets])
            self.assertEqual(e.pane_of("operator"), operator)
            self.assertEqual({p: p.read_bytes() for p in preserved}, preserved)
            self.assertNotIn("stopped @", e.run("down").stdout)

            # A plain up with current arguments resumes the sessions just saved by down.
            e.write_brief(formation({"lead-dev": {**CATALOG_MEMBERS["lead-dev"], "args": ["--no-alt-screen"]},
                                     "pm": CATALOG_MEMBERS["pm"]}))
            e.run("up")
            codex = native(e.starts()["lead-dev"])
            self.assertEqual(codex[:2], ["resume", "latest"])
            self.assertIn("--no-alt-screen", codex)
            self.assertEqual(flag(native(e.starts()["pm"]), "--session"), "/sessions/pm.jsonl")
            self.assertEqual([n for n, _ in e.prompts()], ["lead-dev", "pm"])
            for _, brief in e.prompts():
                self.assertIn("picking up where we left off", brief)

    def test_down_selects_names_and_leaves_empty_panes_and_caller(self):
        with Env(UI) as e:
            e.run("up", "lead-dev", "frontend-dev", "pm", "--no-brief")
            e.kill("frontend-dev")
            empty = e.pane_of("frontend-dev")
            e.set_session("pm", {"agent": "pi", "kind": "id", "value": "not-stopping"})
            other = e.pane_of("pm")
            e.update("operator", label="reviewer", agent=None, name=None)  # invoking shell has a stale member label
            caller = e.pane_of("reviewer")
            out = e.run("down", "@lead-dev", "lead-dev", "frontend-dev", "reviewer").stdout
            self.assertEqual(out.count("stopped @lead-dev"), 1)
            self.assertIn("no compatible session recorded for @lead-dev: up will start it fresh", out)
            self.assertIn("@frontend-dev was not running", out)
            self.assertIn("@reviewer was not running", out)
            self.assertEqual(e.pane_of("frontend-dev"), empty)
            self.assertEqual(e.pane_of("reviewer"), caller)
            self.assertEqual(e.pane_of("pm"), other)
            self.assertEqual(len(e.calls("pane", "close")), 1)
            with patch.dict(os.environ, e.env):
                self.assertNotIn("pm", factory.past_sessions(factory.Config(e.repo)))

    def test_down_validates_before_closing_anything(self):
        with Env(UI) as e:
            e.run("up", "lead-dev", "pm", "--no-brief")
            cases = [("not a member", ("lead-dev", "unknown"), {}),
                     ("not a member", ("lead-dev", "operator"), {}),
                     ("not inside herdr", (), {"HERDR_ENV": "0"}),
                     ("HERDR_PANE_ID is not set", (), {"HERDR_PANE_ID": ""}),
                     ("not a member pane", (), {"HERDR_PANE_ID": e.pane_of("pm")["pane_id"]})]
            for message, args, env in cases:
                with self.subTest(message=message, args=args):
                    p = e.run("down", *args, check=False, **env)
                    self.assertNotEqual(p.returncode, 0)
                    self.assertIn(message, p.stderr)
            self.assertEqual(e.calls("pane", "close"), [])

    def test_down_leaves_mismatched_pane_and_continues_after_refusal(self):
        with Env(UI) as e:
            e.run("up", "lead-dev", "reviewer", "pm", "--no-brief")
            e.update("reviewer", name="unrelated")
            unrelated = e.pane_of("reviewer")
            lead = e.pane_of("lead-dev")["pane_id"]
            pm = e.pane_of("pm")["pane_id"]
            p = e.run("down", check=False, FAKE_CLOSE_FAIL=lead)
            self.assertEqual(p.returncode, 1)
            self.assertIn("could not stop @reviewer: pane label and agent name", p.stderr)
            self.assertIn("could not stop @lead-dev: close_refused", p.stderr)
            self.assertIn("stopped @pm", p.stdout)
            self.assertEqual(e.calls("pane", "close"), [["pane", "close", pane] for pane in (lead, pm)])
            self.assertEqual(e.pane_of("reviewer"), unrelated)
            self.assertIn("stopped @lead-dev", e.run("down", "lead-dev").stdout)

    def test_down_leaves_members_running_when_sessions_cannot_be_recorded(self):
        with Env(UI) as e:
            e.run("up", "lead-dev", "--no-brief")
            e.set_session("lead-dev", {"agent": "codex", "kind": "id", "value": "cx-1"})
            with patch.dict(os.environ, e.env):
                (factory.Config(e.repo).cache / "sessions.jsonl").mkdir()
            p = e.run("down", check=False)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("could not record member sessions", p.stderr)
            self.assertEqual(e.calls("pane", "close"), [])

    def test_down_checks_each_target_again_before_closing(self):
        for fields, error in (({"agent": None, "name": None}, False),
                              ({"name": "unrelated", "label": "other"}, True)):
            with self.subTest(fields=fields), Env(UI) as e:
                e.run("up", "lead-dev", "pm", "--no-brief")
                lead = e.pane_of("lead-dev")["pane_id"]
                pm = e.pane_of("pm")
                state = e.fake_state()
                state["after_close"] = {"pane": pm["pane_id"], "fields": fields}
                e.write_fake(state)
                p = e.run("down", "lead-dev", "pm", check=False)
                self.assertEqual(p.returncode, int(error))
                if error:
                    self.assertIn("could not stop @pm: member identity changed", p.stderr)
                else:
                    self.assertIn("@pm was not running", p.stdout)
                self.assertEqual(e.calls("pane", "close"), [["pane", "close", lead]])
                self.assertEqual(e.fake_state()["panes"][pm["pane_id"]], {**pm, **fields})

    def test_down_resumes_with_an_interrupted_history_and_same_session_value(self):
        with Env(UI) as e:
            e.run("up", "lead-dev", "--no-brief")
            current = {"agent": "codex", "kind": "id", "value": "same"}
            e.set_session("lead-dev", current)
            with patch.dict(os.environ, e.env):
                cfg = factory.Config(e.repo)
                stale = {"member": "lead-dev", **current, "agent": "claude", "kind": "path"}
                (cfg.cache / "sessions.jsonl").write_text(json.dumps(stale) + '\n{"member":')
            e.run("down", "lead-dev")
            with patch.dict(os.environ, e.env):
                self.assertEqual(factory.past_sessions(cfg)["lead-dev"], {"member": "lead-dev", **current})
            e.run("up", "lead-dev")
            self.assertEqual(native(e.starts()["lead-dev"])[:2], ["resume", "same"])


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
            with self.log._locked(exclusive=True):
                with self.assertRaises(BlockingIOError):
                    factory.fcntl.flock(other, factory.fcntl.LOCK_EX | factory.fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    factory.fcntl.flock(other, factory.fcntl.LOCK_SH | factory.fcntl.LOCK_NB)
            factory.fcntl.flock(other, factory.fcntl.LOCK_SH | factory.fcntl.LOCK_NB)   # readers share
            with self.log._locked(exclusive=False):
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
        for code in (errno.EBADF, errno.ENOLCK):
            with self.subTest(code=code), \
                    patch.object(factory.fcntl, "flock", side_effect=OSError(code, os.strerror(code))), \
                    self.assertRaisesRegex(OSError, "cannot lock the state directory.*XDG_CACHE_HOME") as raised:
                self.log.add("pm", ["operator"], "x")
            self.assertEqual(raised.exception.errno, code)

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
        with self.log._locked(exclusive=True):
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

    def test_v1_sample_loads_unchanged(self):
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
                    for command in (("check",), ("up", "--no-brief")):
                        e.run(*command)
                    self.assertIn("queued message 1", e.say("lead-dev", "operator", "report").stdout)
                    self.assertEqual(self.files(e.repo), before)
                    self.assertFalse(target.exists())

    def test_valid_formation_link_keeps_its_target_and_allows_reports(self):
        brief = formation({"lead-dev": CATALOG_MEMBERS["lead-dev"]}, operator={"harness": "claude"})
        with Env(brief) as e:
            link = e.repo / "factory.toml"
            target = e.repo / "settings.toml"
            link.rename(target)
            link.symlink_to(target.name)
            before = self.files(e.repo)
            e.run("check")
            e.run("up", "--no-brief")
            self.assertIn("queued message 1", e.say("lead-dev", "operator", "report").stdout)
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
            self.assertIn(t.get("operator_wait"), ("background", "foreground", None), h)
            self.assertNotIn("operator_messages", t, h)
            self.assertIn(h, factory.CODE)

    def test_operator_table_and_name(self):
        team = '[members.lead-dev]\nharness = "codex"\nmodel = "gpt-6-luna"\n'
        stock = (factory.SKILL / "templates/FACTORY.md").read_text()  # the shared brief people really get
        with Env(team_brief('[operator]\nname = "boss"\nharness = "claude"\n' + team, stock)) as e:
            e.run("up")
            pane = e.fake_state()["panes"]["w1:p1"]
            self.assertEqual((pane["name"], pane["label"]), ("boss", "boss"))  # its @name, pane label and agent name
            self.assertRegex(e.run("status").stdout, r"@boss +operator")
            brief = dict(e.prompts())["lead-dev"]
            self.assertIn("# Team brief (from @boss)", brief)
            self.assertIn("1. The operator assigns tasks", brief)  # the shared brief names the role ...
            self.assertNotIn("@operator", brief)                   # ... never one person's operator
            e.write_brief(team_brief(team, "## Workflow\n\n{operator} and {name} stay as written.\n"))
            self.assertIn("{operator} and {name} stay as written.", e.run("brief").stdout)  # FACTORY.md is sent as it is
            e.write_brief(team_brief('[operator]\nname = "boss"\nharness = "claude"\n' + team, stock))
            prompt = e.run("prompt", "lead-dev").stdout
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
            self.assertIn("The team brief says where tasks are tracked", e.run("prompt", "lead-dev").stdout)
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
            prompt = e.run("prompt", "reviewer").stdout
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
            self.assertIn("printed retry command once", prompt)
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

    def test_session_id_from_ref(self):
        self.assertEqual(factory.session_id({"kind": "id", "value": "abc"}), "abc")
        path = "/x/rollout-2026-11-11T13-02-52-01a1cbc6-e708-7623-986f-cc2e07efa28f.jsonl"
        self.assertEqual(factory.session_id({"kind": "path", "value": path}), "01a1cbc6-e708-7623-986f-cc2e07efa28f")

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


class DocumentationTests(unittest.TestCase):
    def test_relative_links_and_headings(self):
        root = factory.SKILL.parent
        for page in [root / "README.md", *sorted((root / "docs").glob("*.md"))]:
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
                self.assertEqual(headings[1:], ["## Needs", "## Check", "## FACTORY.md section", "## Tested with"])
                self.assertIn(f"(../skill/recipes/{path.name})", overview)


class CacheNameTests(unittest.TestCase):
    def test_new_projects_use_factory_cache_with_xdg_or_home(self):
        with Env() as e:
            for xdg in (str(e.root / "cache"), ""):
                with self.subTest(xdg=xdg), patch.dict(os.environ, {**e.env, "XDG_CACHE_HOME": xdg}):
                    cache = factory.Config(e.repo).cache
                    base = pathlib.Path(xdg) if xdg else e.home / ".cache"
                    (base / "a-team" / cache.name).mkdir(parents=True)
                    self.assertEqual(factory.Config(e.repo).cache, cache)
                    self.assertEqual(cache.parent, base / "factory-skill")
                    self.assertFalse(cache.exists())


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
            prompt = run("prompt", "reviewer")
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
                    cache = factory.Config(repo).cache
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


class HerdrContract(unittest.TestCase):
    """The fields factory.py reads must exist where it reads them. The fake above follows the same split."""

    @unittest.skipUnless(shutil.which("herdr"), "herdr is not installed")
    def test_agy_member_kind(self):
        help_text = subprocess.run(["herdr", "agent"], capture_output=True, text=True)
        self.assertRegex(help_text.stderr + help_text.stdout, r"kinds: [^\n]*\bagy\b")

    def test_pane_and_agent_records(self):
        if not shutil.which("herdr"):
            self.skipTest("herdr is not installed")
        p = subprocess.run(["herdr", "api", "schema", "--json"], capture_output=True, text=True)
        if p.returncode:
            self.skipTest("herdr api schema is not available")
        defs = {}

        def walk(o):
            if isinstance(o, dict):
                defs.update(o.get("$defs", {}))
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(json.loads(p.stdout))
        pane, agent = defs["PaneInfo"]["properties"], defs["AgentInfo"]["properties"]
        for field in ("pane_id", "tab_id", "label", "agent", "agent_status", "agent_session"):
            self.assertIn(field, pane)
        self.assertNotIn("name", pane)  # which is why Team reads `agent list` as well
        for field in ("pane_id", "name", "agent"):
            self.assertIn(field, agent)
        self.assertEqual(set(defs["AgentSessionInfo"]["properties"]), {"agent", "kind", "source", "value"})
        self.assertIn("text", defs["PaneReadResult"]["properties"])
        self.assertIn("foreground_processes", defs["PaneProcessInfo"]["properties"])
        self.assertLessEqual({"name", "argv"}, set(defs["PaneProcessInfoProcess"]["properties"]))
        self.assertEqual(set(defs["ErrorBody"]["properties"]), {"code", "message"})
        for params, field in (("TabCreateParams", "env"), ("PaneSplitParams", "env"), ("AgentStartParams", "args")):
            self.assertIn(field, defs[params]["properties"])


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
