"""Unit coverage: harness envelopes never leak into context or drafts."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import runner  # noqa: E402

TERMINAL = ('{"schema_version":1,"record_type":"event",'
            '"payload_type":"run.terminal.completed","payload":'
            '{"kind":"run_terminal","text":"Fix done.\\n\\nFILES CHANGED\\n- ios/Sound.swift"}}')
LIFECYCLE = ('{"schema_version":1,"record_type":"event",'
             '"payload":{"kind":"task_lifecycle","task_id":"abc"}}')
FRAGMENT = 'ycle.completed","payload_schema_version":1,"payload":{"kind":"task_lifecycle"'
STATUS_PARTIAL = ('{"schema_version":1,"record_type":"status",'
                  '"payload":{"kind":"status","text":"- ios"}}')


class HumanTextTests(unittest.TestCase):
    def test_drops_envelopes_keeps_terminal_text(self):
        raw = "\n".join(["plain line", LIFECYCLE, TERMINAL, FRAGMENT, "tail line"])
        out = runner.human_text(raw)
        self.assertIn("plain line", out)
        self.assertIn("tail line", out)
        self.assertIn("Fix done.", out)
        self.assertIn("ios/Sound.swift", out)
        for smell in ("schema_version", "task_lifecycle", "task_id", "record_type"):
            self.assertNotIn(smell, out)

    def test_streamed_fragments_duplicated_in_terminal_are_dropped(self):
        raw = "\n".join(["FILES CHANGED", "- ios", TERMINAL, "genuinely new line"])
        out = runner.human_text(raw)
        self.assertNotIn("- ios\n", out)
        self.assertIn("genuinely new line", out)
        self.assertIn("Fix done.", out)

    def test_partial_envelope_text_yields_to_terminal(self):
        raw = "\n".join([STATUS_PARTIAL, TERMINAL])
        out = runner.human_text(raw)
        self.assertEqual(out.strip(), "Fix done.\n\nFILES CHANGED\n- ios/Sound.swift")

    def test_partial_envelope_text_kept_without_terminal(self):
        out = runner.human_text(STATUS_PARTIAL)
        self.assertIn("- ios", out)

    def test_plain_text_passes_through(self):
        raw = "FILES CHANGED\n- a.ts\n\n{\"not\": \"an envelope\"}"
        self.assertEqual(runner.human_text(raw), raw)

    def test_empty_and_none(self):
        self.assertEqual(runner.human_text(""), "")
        self.assertEqual(runner.human_text(None), "")

    def test_tail_is_cleaned(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "fix.log.txt"
            p.write_text("x" * 5000 + "\n" + TERMINAL + "\n" + LIFECYCLE, encoding="utf-8")
            tail = runner._tail(p)
            self.assertLessEqual(len(tail), 3000)
            self.assertIn("Fix done.", tail)
            self.assertNotIn("schema_version", tail)

    def test_wrapped_midword_fragment_is_dropped(self):
        frag = ('n_stream\\":{\\"kind\\":\\"run\\",\\"id\\":\\"abc\\"},'
                '\\"text\\":\\" need\\"}}')
        out = runner.human_text("\n".join(["## DUPLICATE", frag, "No duplicate found."]))
        self.assertNotIn("n_stream", out)
        self.assertIn("## DUPLICATE", out)
        self.assertIn("No duplicate found.", out)

    def test_tail_drops_partial_first_line(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.log.txt"
            p.write_text("z" * 100 + "\nline two\nline three", encoding="utf-8")
            self.assertEqual(runner._tail(p, n=20), "line two\nline three")
            self.assertEqual(runner._tail(p, n=10000),
                             "z" * 100 + "\nline two\nline three")


if __name__ == "__main__":
    unittest.main()
