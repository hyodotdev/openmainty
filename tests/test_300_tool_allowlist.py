"""Slice 300: repair-attempt tool allowlist plus no-git-record refusal.

Each broker repair attempt proves, from its own stream, that it
used no tool outside REPAIR_TOOL_ALLOWLIST, and a broker
attempt on a workspace with no "git" record refuses before
spawn. Stream fixtures copy the real E43b shapes (payload /
stream keys, task_lifecycle + tool_result + run_usage) with no
ids, tokens or local paths; the stream kind is the observed
exec.usage (the old "exec" shorthand was never observed and
now refuses). Each test names the protection it
kills so a removal fails it.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openmainty import derived_fix as df  # noqa: E402
from openmainty import m2  # noqa: E402
from openmainty import muse as muse_mod  # noqa: E402
from test_derived_fix import DerivedFixFixtureMixin  # noqa: E402
from test_integration_prepare_issue import git  # noqa: E402

_PINNED = "muse-spark-1.3"
_ABSENT = object()


# Complete envelopes for fixtures. payload_type follows the
# evidence set by name correspondence; record_type uses event
# for records and status for usage. Both are name-inferred
# stand-ins: the guard pins set membership, not the
# per-record mapping. Unknown-kind pins use a valid
# stand-in envelope so each refusal targets the leg under
# test.
def _envelope(kind, event_kind=None):
    if kind == "task_lifecycle":
        payload_type = "task.lifecycle.%s" % (
            event_kind or "proposed")
        if payload_type not in muse_mod.REPAIR_PAYLOAD_TYPES:
            payload_type = "task.lifecycle.proposed"
        return "event", payload_type
    if kind == "run_usage":
        return "status", "run.usage"
    return "event", {
        "command_accepted": "runtime.command.accepted",
        "run_model_configured": "run.model.configured",
        "run_output_delta": "run.output.delta",
        "run_started": "run.lifecycle.started",
        "run_terminal": "run.terminal.completed",
        "session_run_linked": "session.run.linked",
        "task_stream_linked": "task.stream.linked",
        "tool_result": "tool.result",
        "turn_input_user": "turn.input.user",
    }.get(kind, "run.usage")


def _lc_line(task_kind, event_kind="proposed"):
    record_type, payload_type = _envelope(
        "task_lifecycle", event_kind)
    return json.dumps({
        "record_type": record_type,
        "payload_type": payload_type,
        "payload": {"kind": "task_lifecycle",
                    "event": {"kind": event_kind,
                              "task_kind": task_kind}},
        "stream": {"index": 0, "kind": "exec.usage"}})


def _tr_line(corr=_ABSENT, edit=_ABSENT):
    facts = {"outcome": "success"}
    if corr is not _ABSENT:
        facts["tool_name"] = corr
    edits = {"added": 1, "path": "f"}
    if edit is not _ABSENT:
        edits["tool_name"] = edit
    record_type, payload_type = _envelope("tool_result")
    return json.dumps({
        "record_type": record_type,
        "payload_type": payload_type,
        "payload": {"kind": "tool_result",
                    "correlation_facts": facts,
                    "edit_facts": edits},
        "stream": {"index": 1, "kind": "exec.usage"}})


def _model_line():
    return _lc_line("model.meta.response")


def _run_usage_line():
    record_type, payload_type = _envelope("run_usage")
    return json.dumps({
        "record_type": record_type,
        "payload_type": payload_type,
        "payload": {"kind": "run_usage",
                    "model_id": _PINNED,
                    "input_tokens": 5, "output_tokens": 3},
        "stream": {"index": 9, "kind": "exec.usage"}})


def _kind_line(kind):
    record_type, payload_type = _envelope(kind)
    return json.dumps({
        "record_type": record_type,
        "payload_type": payload_type,
        "payload": {"kind": kind},
        "stream": {"index": 0, "kind": "exec.usage"}})


def _named_line(kind, tool_name):
    record_type, payload_type = _envelope(kind)
    return json.dumps({
        "record_type": record_type,
        "payload_type": payload_type,
        "payload": {"kind": kind,
                    "correlation_facts": {
                        "tool_name": tool_name}},
        "stream": {"index": 0, "kind": "exec.usage"}})


def _stream(*lines):
    return ("\n".join(lines) + "\n").encode("utf-8")


class _ProveMixin(object):
    """Drive the real _prove_exec_attempt with honest fixtures."""

    def _prove(self, stream, **usage_kw):
        usage = {"contributor_applied": True,
                 "model": _PINNED,
                 "provider_model": _PINNED + "-contributor"}
        usage.update(usage_kw)
        broker_log = [{"method": "CONNECT",
                       "target": "api.meta.ai:443",
                       "decision": "allowed"}]
        cred = mock.patch(
            "openmainty.preflight._read_worker_credential_trace",
            return_value=({"source": "login",
                           "masks_login": "false"}, None))
        transport = mock.patch(
            "openmainty.preflight._read_worker_transport_trace",
            return_value=({"proxy": 1, "direct": 0}, None))
        with cred, transport:
            err, code = muse_mod._prove_exec_attempt(
                usage, "/owned/canon", broker_log, stream)
        return err, code, usage


class ToolAllowlistAcceptTest(_ProveMixin, unittest.TestCase):
    """Allowed shapes accept and record the names used."""

    def test_allowed_tool_accepts_and_records(self):
        err, code, usage = self._prove(_stream(
            _model_line(),
            _lc_line("tool.write_file"),
            _tr_line(corr="write_file", edit="write_file"),
            _run_usage_line()))
        self.assertIsNone(code)
        self.assertIsNone(err)
        self.assertEqual(usage.get("repair_tools"), ["write_file"])
        self.assertNotIn("refused_tool", usage)

    def test_no_tools_passes(self):
        err, code, usage = self._prove(_stream(
            _model_line(), _run_usage_line()))
        self.assertIsNone(code)
        self.assertIsNone(err)
        self.assertEqual(usage.get("repair_tools"), [])

    def test_name_in_lifecycle_only_counts(self):
        err, code, usage = self._prove(_stream(
            _lc_line("tool.write_file"), _model_line()))
        self.assertIsNone(code)
        self.assertEqual(usage.get("repair_tools"), ["write_file"])

    def test_name_in_correlation_only_counts(self):
        err, code, usage = self._prove(_stream(
            _tr_line(corr="write_file"), _model_line()))
        self.assertIsNone(code)
        self.assertEqual(usage.get("repair_tools"), ["write_file"])

    def test_name_in_edit_only_counts(self):
        err, code, usage = self._prove(_stream(
            _tr_line(edit="write_file"), _model_line()))
        self.assertIsNone(code)
        self.assertEqual(usage.get("repair_tools"), ["write_file"])

    def test_blank_lines_ignored(self):
        err, code, _usage = self._prove(
            _stream(_model_line()) + b"\n\n")
        self.assertIsNone(code)
        self.assertIsNone(err)

    def test_known_kinds_accept(self):
        # Every payload.kind E43b carried must pass; the
        # closed list may only grow on stream evidence.
        lines = [_kind_line(k) for k in (
            "command_accepted", "run_model_configured",
            "run_output_delta", "run_started", "run_terminal",
            "session_run_linked", "task_stream_linked",
            "turn_input_user")]
        lines.append(_lc_line("model.meta.response"))
        lines.append(_run_usage_line())
        lines.append(_tr_line(corr="write_file",
                              edit="write_file"))
        err, code, usage = self._prove(_stream(*lines))
        self.assertIsNone(code)
        self.assertIsNone(err)
        self.assertEqual(usage.get("repair_tools"), ["write_file"])
        self.assertNotIn("refused_tool", usage)
        self.assertNotIn("refused_kind", usage)


class ToolAllowlistRefuseTest(_ProveMixin, unittest.TestCase):
    """Off-list names, nameless records and garbled lines refuse."""

    def _assert_refused(self, stream, needle, refused):
        err, code, usage = self._prove(stream)
        self.assertEqual(code, "exec-tool-unproved")
        self.assertIn(needle, err)
        self.assertEqual(usage.get("refused_tool"), refused)
        return usage

    def test_shell_like_name_refuses(self):
        self._assert_refused(
            _stream(_lc_line("tool.shell_exec"), _model_line()),
            "shell_exec", "shell_exec")

    def test_web_like_name_refuses(self):
        self._assert_refused(
            _stream(_tr_line(edit="web_fetch"), _model_line()),
            "web_fetch", "web_fetch")

    def test_mcp_like_name_refuses(self):
        self._assert_refused(
            _stream(_tr_line(corr="mcp_call"), _model_line()),
            "mcp_call", "mcp_call")

    def test_agent_like_name_refuses(self):
        self._assert_refused(
            _stream(_lc_line("tool.spawn_agent", "started"),
                    _model_line()),
            "spawn_agent", "spawn_agent")

    def test_nameless_tool_result_refuses(self):
        self._assert_refused(
            _stream(_tr_line(), _model_line()),
            "no name", None)

    def test_empty_task_kind_refuses(self):
        self._assert_refused(
            _stream(_lc_line("tool."), _model_line()),
            "no name", None)

    def test_garbled_line_refuses(self):
        self._assert_refused(
            _stream(_model_line()) + b"{nope\n",
            "unparseable", None)

    def test_non_record_line_refuses(self):
        self._assert_refused(
            _stream(_model_line()) + b'"just a string"\n',
            "unparseable", None)

    def test_unreadable_stream_refuses(self):
        err, code, usage = self._prove(None)
        self.assertEqual(code, "exec-tool-unproved")
        self.assertIn("readable", err)
        self.assertEqual(usage.get("repair_tools"), [])
        self.assertIsNone(usage.get("refused_tool"))

    def test_unknown_payload_kind_refuses(self):
        err, code, usage = self._prove(_stream(
            _kind_line("web_result"), _model_line()))
        self.assertEqual(code, "exec-tool-unproved")
        self.assertIn("web_result", err)
        self.assertEqual(usage.get("refused_kind"), "web_result")

    def test_unknown_task_kind_refuses(self):
        for value in ("web.search", "mcp.x", "agent.y"):
            err, code, usage = self._prove(_stream(
                _lc_line(value), _model_line()))
            self.assertEqual(code, "exec-tool-unproved",
                             value)
            self.assertIn(value, err)
            self.assertEqual(usage.get("refused_kind"), value,
                             value)

    def test_tool_name_outside_tool_result_refuses(self):
        err, code, usage = self._prove(_stream(
            _named_line("run_usage", "web_search"),
            _model_line()))
        self.assertEqual(code, "exec-tool-unproved")
        self.assertIn("web_search", err)
        self.assertEqual(usage.get("refused_tool"), "web_search")

    def test_tool_kind_keeps_the_name_it_carried(self):
        err, code, usage = self._prove(_stream(
            _named_line("tool_custom", "web_x"),
            _model_line()))
        self.assertEqual(code, "exec-tool-unproved")
        self.assertIn("tool_custom", err)
        self.assertEqual(usage.get("refused_kind"), "tool_custom")
        self.assertEqual(usage.get("refused_tool"), "web_x")

    def test_record_without_payload_refuses(self):
        line = json.dumps({
            "record_type": "event",
            "payload_type": "run.usage",
            "stream": {"index": 0,
                       "kind": "exec.usage"}})
        err, code, _usage = self._prove(
            _stream(_model_line(), line))
        self.assertEqual(code, "exec-tool-unproved")
        self.assertIn("no payload", err)

    def test_empty_stream_refuses(self):
        err, code, usage = self._prove(b"")
        self.assertEqual(code, "exec-tool-unproved")
        self.assertIn("no records", err)
        self.assertEqual(usage.get("repair_tools"), [])

    def test_blank_only_stream_refuses(self):
        err, code, _usage = self._prove(b"\n\n")
        self.assertEqual(code, "exec-tool-unproved")
        self.assertIn("no records", err)

    def test_non_string_tool_name_refuses(self):
        # A null tool_name refuses even when the same
        # record carries a valid name elsewhere.
        err, code, _usage = self._prove(_stream(
            _tr_line(corr="write_file", edit=None),
            _model_line()))
        self.assertEqual(code, "exec-tool-unproved")
        self.assertIn("no name", err)

    def test_tools_recorded_when_earlier_leg_refuses(self):
        err, code, usage = self._prove(
            _stream(_lc_line("tool.web_x"), _model_line()),
            contributor_applied=False)
        self.assertEqual(code, "exec-contributor-unproved")
        self.assertEqual(usage.get("repair_tools"), [])
        self.assertEqual(usage.get("refused_tool"), "web_x")

    def test_kind_recorded_when_earlier_leg_refuses(self):
        err, code, usage = self._prove(
            _stream(_kind_line("web_result"), _model_line()),
            contributor_applied=False)
        self.assertEqual(code, "exec-contributor-unproved")
        self.assertEqual(usage.get("refused_kind"), "web_result")


class UnseenShapeRefuseTest(_ProveMixin, unittest.TestCase):
    """Root's diagnostics plus tied envelope/lifecycle legs.

    Closed grammar source: Root-E43b-actual-closed-grammar
    evidence (36 records, 0 kindless, every record carrying
    record_type str, payload_type str, stream dict and
    stream kind). Every fixture below carries a valid
    envelope except the leg under test, so each refusal
    targets exactly one leg.
    """

    def _full(self, payload, record_type="event",
              payload_type="run.usage", stream="exec.usage"):
        rec = {}
        if record_type is not _ABSENT:
            rec["record_type"] = record_type
        if payload_type is not _ABSENT:
            rec["payload_type"] = payload_type
        if stream is not _ABSENT:
            if isinstance(stream, dict):
                rec["stream"] = dict(stream)
            elif stream is None:
                rec["stream"] = None
            else:
                rec["stream"] = {"index": 0, "kind": stream}
        rec["payload"] = payload
        return rec

    def _refused_kind(self, obj, needle, want_kind):
        line = json.dumps(obj)
        err, code, usage = self._prove(
            _stream(_model_line(), line))
        self.assertEqual(code, "exec-tool-unproved")
        self.assertIn(needle, err)
        self.assertEqual(usage.get("refused_kind"), want_kind)
        return usage

    def test_empty_payload_object_refuses(self):
        usage = self._refused_kind(
            self._full({}), "no payload kind", None)
        self.assertIsNone(usage.get("refused_tool"))

    def test_kindless_operation_refuses(self):
        self._refused_kind(
            self._full({"operation": "unseen.provider.web"}),
            "no payload kind", None)

    def test_unknown_lifecycle_event_refuses(self):
        self._refused_kind(
            self._full(
                {"kind": "task_lifecycle",
                 "event": {"kind":
                           "unseen_provider_operation"}},
                payload_type="task.lifecycle.proposed"),
            "unseen_provider_operation",
            "unseen_provider_operation")

    def test_unknown_envelope_record_type_refuses(self):
        self._refused_kind(
            self._full({"kind": "run_usage"},
                       record_type="unseen_provider_tool",
                       payload_type="run.usage"),
            "unseen_provider_tool",
            "unseen_provider_tool")

    def test_unknown_payload_type_refuses(self):
        self._refused_kind(
            self._full({"kind": "run_usage"},
                       record_type="status",
                       payload_type="unseen.shape"),
            "unseen.shape", "unseen.shape")

    def test_unknown_event_operation_refuses(self):
        self._refused_kind(
            self._full(
                {"kind": "task_lifecycle",
                 "event": {"kind": "side_effect_intent",
                           "operation":
                           "unseen.provider.web"}},
                payload_type="task.lifecycle.side_effect_intent"),
            "unseen.provider.web",
            "unseen.provider.web")

    def test_task_lifecycle_without_event_refuses(self):
        self._refused_kind(
            self._full({"kind": "task_lifecycle"},
                       payload_type="task.lifecycle.proposed"),
            "no event", None)

    def test_task_lifecycle_null_event_refuses(self):
        obj = self._full({"kind": "task_lifecycle"},
                         payload_type="task.lifecycle.proposed")
        obj["payload"]["event"] = None
        self._refused_kind(obj, "no event", None)

    def test_empty_tool_suffix_borrows_no_name(self):
        # task_kind "tool." refuses even though the record
        # carries a readable allowlisted tool_name elsewhere;
        # the observed name is still recorded, the empty
        # suffix is retained as the refused kind.
        obj = self._full(
            {"kind": "task_lifecycle",
             "event": {"kind": "completed",
                       "task_kind": "tool.",
                       "tool_name": "write_file"}},
            payload_type="task.lifecycle.completed")
        err, code, usage = self._prove(
            _stream(_model_line(), json.dumps(obj)))
        self.assertEqual(code, "exec-tool-unproved")
        self.assertIn("no name", err)
        self.assertEqual(usage.get("refused_kind"), "tool.")
        self.assertEqual(usage.get("repair_tools"), ["write_file"])

    def test_non_lifecycle_list_event_refuses(self):
        obj = self._full({"kind": "run_usage"},
                         record_type="status")
        obj["payload"]["event"] = ["proposed"]
        self._refused_kind(obj, "unexpected event", None)

    def test_non_lifecycle_null_event_refuses(self):
        obj = self._full({"kind": "run_usage"},
                         record_type="status")
        obj["payload"]["event"] = None
        self._refused_kind(obj, "unexpected event", None)

    def test_missing_stream_refuses(self):
        self._refused_kind(
            self._full({"kind": "run_usage"},
                       record_type="status", stream=_ABSENT),
            "no stream", None)

    def test_null_stream_refuses(self):
        self._refused_kind(
            self._full({"kind": "run_usage"},
                       record_type="status", stream=None),
            "not an object", None)

    def test_stream_without_kind_refuses(self):
        self._refused_kind(
            self._full({"kind": "run_usage"},
                       record_type="status", stream={}),
            "no kind", None)

    def test_unobserved_stream_kind_refuses(self):
        # The old "exec" shorthand was never observed (only
        # exec.usage and session were); it refuses and keeps
        # the value.
        self._refused_kind(
            self._full({"kind": "run_usage",
                        "model_id": _PINNED},
                       record_type="status", stream="exec"),
            "exec", "exec")

    def test_missing_record_type_refuses(self):
        self._refused_kind(
            self._full({"kind": "run_usage"},
                       record_type=_ABSENT),
            "no record type", None)

    def test_null_record_type_refuses(self):
        self._refused_kind(
            self._full({"kind": "run_usage"},
                       record_type=None),
            "no record type", None)

    def test_mistyped_record_type_refuses(self):
        self._refused_kind(
            self._full({"kind": "run_usage"},
                       record_type=5),
            "unknown record type", 5)

    def test_missing_payload_type_refuses(self):
        self._refused_kind(
            self._full({"kind": "run_usage"},
                       record_type="status",
                       payload_type=_ABSENT),
            "no payload type", None)

    def test_null_payload_type_refuses(self):
        self._refused_kind(
            self._full({"kind": "run_usage"},
                       record_type="status",
                       payload_type=None),
            "no payload type", None)

    def test_known_operations_accept(self):
        err, code, usage = self._prove(_stream(
            _model_line(),
            json.dumps(self._full(
                {"kind": "task_lifecycle",
                 "event": {"kind": "side_effect_intent",
                           "operation": "tool:write_file"}},
                payload_type="task.lifecycle.side_effect_intent")),
            json.dumps(self._full(
                {"kind": "task_lifecycle",
                 "event": {"kind": "side_effect_intent",
                           "operation":
                           "model.meta.response"}},
                payload_type="task.lifecycle.side_effect_intent"))))
        self.assertIsNone(code)
        self.assertIsNone(err)
        self.assertEqual(usage.get("repair_tools"), [])


class NoGitRecordRefuseTest(DerivedFixFixtureMixin):
    """A broker attempt with no git record refuses before spawn."""

    def _start_flow(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])
        return out["dfix"], started["attempt_id"]

    def test_no_git_record_refuses_before_spawn(self):
        dfix, attempt_id = self._start_flow()
        rec = self._read(dfix)
        work = rec["workspace"]["path"]
        prep_head = rec["workspace"]["prep_head"]
        del rec["workspace"]["git"]
        # A legacy record still validates; only a new attempt
        # on it refuses.
        df._validate_workspace(rec["workspace"], dfix)
        df._write_record(rec)
        with mock.patch("openmainty.muse.exec_source_repair",
                        return_value=(0, b"", {})) as run:
            with m2.op_locked(self.blocked_id):
                rc = df.run_repair_worker(dfix, attempt_id)
        self.assertEqual(rc, 1)
        run.assert_not_called()
        rec = self._read(dfix)
        attempt = next(a for a in rec["source_attempts"]
                       if a.get("id") == attempt_id)
        self.assertEqual(attempt["status"], "refused")
        self.assertIn("[repair-no-git-record]",
                      attempt.get("error") or "")
        head = git("rev-parse", "HEAD", cwd=work)
        self.assertEqual(head, prep_head)
        self.assertTrue(os.path.isdir(work))

    def test_git_record_attempt_reaches_exec(self):
        dfix, attempt_id = self._start_flow()
        with mock.patch("openmainty.muse.exec_source_repair",
                        return_value=(1, b"", {})) as run:
            with m2.op_locked(self.blocked_id):
                rc = df.run_repair_worker(dfix, attempt_id)
        self.assertEqual(rc, 1)
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
