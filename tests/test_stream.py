"""Durable coverage: exec-stream delta parsing + streaming chat turns.

Delta fixtures are verbatim `muse exec --json` lines observed from a real
run (see tests/testdata/exec-lines.jsonl). No quota, no network. Run with:
  python3 -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import chat, muse, pcconfig, repos, runner  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402

PROFILE = {
    "name": "fakerepo",
    "github": "example/fakerepo",
    "baseBranch": "main",
    "knowledge": [],
    "checks": [],
    "e2e": [],
    "release": {},
    "announce": {"medium": False, "discord": False, "slack": False},
}

FIXTURE = Path(__file__).resolve().parent / "testdata" / "exec-lines.jsonl"


def fixture_lines():
    lines = []
    for raw in FIXTURE.read_text(encoding="utf-8").splitlines():
        if raw and not raw.startswith("#"):
            lines.append(raw)
    return lines


class ParseDeltaTests(unittest.TestCase):
    def test_verbatim_fixture_shapes(self):
        kinds = [muse.parse_delta_line(line) for line in fixture_lines()]
        self.assertEqual(kinds[0], ("delta", "CHAT_OK"))
        self.assertEqual(kinds[1], ("terminal", "CHAT_OK"))
        self.assertEqual(kinds[2][0], "none")
        self.assertEqual(kinds[3], ("none", ""))

    def test_malformed_lines_are_none(self):
        self.assertEqual(muse.parse_delta_line(""), ("none", ""))
        self.assertEqual(muse.parse_delta_line("plain text"), ("none", ""))
        self.assertEqual(muse.parse_delta_line('{"payload": 42}'), ("none", ""))
        self.assertEqual(muse.parse_delta_line('{"payload": {"kind": "run_output_delta"}}'),
                         ("none", ""))


class StubStreamExecutor:
    """ emits scripted chunks, then returns scripted usage. """

    def __init__(self, chunks, terminal_text=None, quota=False):
        self.chunks = list(chunks)
        self.terminal_text = terminal_text
        self.quota = quota

    def muse_step_stream(self, step, prompt, workdir, on_delta, timeout=1200):
        if self.quota:
            raise runner.QuotaHit("fake quota exhausted")
        for chunk in self.chunks:
            on_delta(chunk)
        usage = {"fake": True, "tokens_measurable": False, "tokens": None}
        if self.terminal_text is not None:
            usage["terminal_text"] = self.terminal_text
        return 0, "(raw)", usage


class SendStreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))
        self.checkout = Path(self.tmp.name) / "checkout"
        self.checkout.mkdir()
        repos.save(dict(PROFILE))
        pc = pcconfig.load()
        pc["checkouts"]["fakerepo"] = str(self.checkout)
        pcconfig.save(pc)

    def test_incremental_chunks_concatenate(self):
        session = chat.create("fakerepo", "", "", {})
        seen = []
        turn = chat.send_stream(session, repos.load("fakerepo"), "hi?",
                                StubStreamExecutor(["Hel", "lo"]),
                                self.checkout, seen.append)
        self.assertEqual(turn["text"], "Hello")
        self.assertEqual("".join(seen), "Hello")
        self.assertEqual(turn["usage"]["text_source"], "stream-live")

    def test_cumulative_chunks_replace(self):
        session = chat.create("fakerepo", "", "", {})
        turn = chat.send_stream(session, repos.load("fakerepo"), "hi?",
                                StubStreamExecutor(["Hel", "Hello"]),
                                self.checkout, lambda c: None)
        self.assertEqual(turn["text"], "Hello")

    def test_terminal_text_wins(self):
        session = chat.create("fakerepo", "", "", {})
        turn = chat.send_stream(session, repos.load("fakerepo"), "hi?",
                                StubStreamExecutor(["par", "tial"], terminal_text="FINAL"),
                                self.checkout, lambda c: None)
        self.assertEqual(turn["text"], "FINAL")

    def test_quota_records_wait_and_reraises(self):
        session = chat.create("fakerepo", "", "", {})
        with self.assertRaises(runner.QuotaHit):
            chat.send_stream(session, repos.load("fakerepo"), "hi?",
                             StubStreamExecutor([], quota=True),
                             self.checkout, lambda c: None)
        self.assertEqual(chat.load(session["id"])["waitReason"],
                         "subscription-quota in chat")

    def test_usage_honesty(self):
        session = chat.create("fakerepo", "", "", {})
        turn = chat.send_stream(session, repos.load("fakerepo"), "hi?",
                                StubStreamExecutor(["x"]),
                                self.checkout, lambda c: None)
        self.assertIsNone(turn["usage"]["tokens"])


if __name__ == "__main__":
    unittest.main()
