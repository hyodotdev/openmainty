"""Test package bootstrap (M2.15b): the real-home guard is on for
the whole suite. Minimal on purpose — fixture helpers live in
tests/_home.py, never here."""
import os

os.environ.setdefault("OPENMAINTY_TEST_GUARD", "1")
