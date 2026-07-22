"""Regression tests for generated Outlook VBA and its JSON enqueue protocol."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import dump_flows as df
import vba_generator as vg


class VbaGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "flows.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def _feed(self, key, priority, conditions, with_steps=True):
        df.upsert_dump_type(key, key, sort_order=priority, db_path=self.db)
        df.set_recognition(key, [{"mode": "all", "conditions": conditions}],
                           db_path=self.db)
        if with_steps:
            df.set_steps(key, [{"step_name": "process", "kind": "python",
                                "target_path": r"C:\Sarthi\process.py"}],
                         db_path=self.db)

    def test_first_match_priority_and_attachment_field(self):
        self._feed("specific", 10, [
            {"field": "sender", "op": "is", "value": "a@example.com"},
            {"field": "subject", "op": "contains", "value": "order book"},
        ])
        self._feed("general", 20, [
            {"field": "subject", "op": "contains", "value": "order book"},
        ])
        self._feed("cube", 30, [
            {"field": "attachment", "op": "contains",
             "value": "_calllog history.xlsx"},
        ])

        code = vg.generate(db_path=self.db, all_in_one=True)

        self.assertLess(code.index("If Watch_specific"), code.index("If Watch_general"))
        self.assertIn("Then Exit Sub", code)
        self.assertIn('InStr(attachNames, "_calllog history.xlsx")', code)
        self.assertNotIn('InStr(subjectText, "_calllog history.xlsx")', code)
        self.assertIn("Rule overlap: general includes messages matched", code)

    def test_safe_event_job_protocol_and_result_logging_are_generated(self):
        self._feed("feed", 10, [
            {"field": "subject", "op": "contains", "value": "feed"},
        ])
        code = vg.generate(db_path=self.db, all_in_one=True)

        for required in ("InitializeInboxWatcher", "TestSelectedEmail",
                         "If Item Is Nothing Then Exit Sub", "--job-file",
                         "Select Case rc", "Case 2", "DUPLICATE skipped",
                         "IsDataAttachment", "WriteUtf8", "NO MATCH"):
            self.assertIn(required, code)
        self.assertNotIn("att.Size > 4096", code)
        self.assertNotIn("On Error Resume Next", code)
        self.assertNotIn("Attribute VB_Name", code)

    def test_feed_with_rules_but_no_steps_is_blocked(self):
        self._feed("broken", 10, [
            {"field": "subject", "op": "contains", "value": "broken"},
        ], with_steps=False)
        code = vg.generate(db_path=self.db, all_in_one=True)
        self.assertIn("BLOCKED - HAS ROUTING RULE BUT NO STEPS: broken", code)
        self.assertNotIn("Watch_broken", code)

    def test_json_job_returns_distinct_duplicate_exit_code(self):
        runner = Path(__file__).with_name("run_direct.py")

        def invoke(job_name):
            job = self.root / job_name
            job.write_text(json.dumps({
                "enqueue": True, "delete_after_read": True, "file": "",
                "subject": "special % & | ! ^ (subject)",
                "sender": "a@example.com", "entry_id": "same:1",
                "dump_type": "feed",
            }), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(runner), "--job-file", str(job),
                 "--db", str(self.db)], capture_output=True, text=True)

        first = invoke("one.json")
        second = invoke("two.json")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(json.loads(first.stdout)["status"], "queued")
        self.assertEqual(second.returncode, 2, second.stdout + second.stderr)
        self.assertEqual(json.loads(second.stdout)["status"], "duplicate")


if __name__ == "__main__":
    unittest.main()
