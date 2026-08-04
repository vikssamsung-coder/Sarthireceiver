"""Regression tests for receiver failures that previously caused data loss."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import dump_flows as df
import extractor
import flow_engine
import intake_queue as iq
import sarthi_receiver
import updater


class ReceiverRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "flows.sqlite3"
        self.seen = self.root / "seen.sqlite3"
        df.init_db(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def _type_with_step(self, key="feed", *, save_folder=None, target=None,
                        working_dir=None):
        df.upsert_dump_type(key, key, save_folder=str(save_folder) if save_folder else None,
                            db_path=self.db)
        df.set_recognition(key, [{"mode": "all", "conditions": [
            {"field": "subject", "op": "contains", "value": key}
        ]}], db_path=self.db)
        df.set_steps(key, [{
            "step_name": "build", "kind": "python",
            "target_path": str(target or self.root / "unused.py"),
            "args_json": [], "working_dir": str(working_dir) if working_dir else None,
            "on_failure": "stop",
        }], db_path=self.db)

    def test_zip_slip_member_cannot_escape_destination(self):
        archive = self.root / "feed.zip"
        dest = self.root / "data"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("../data_evil/stolen.csv", "secret")
            z.writestr("valid.csv", "a\n1\n")

        result = extractor.extract_dump(archive, dest, log=lambda _m: None)

        self.assertEqual(result["primary"], dest / "valid.csv")
        self.assertFalse((self.root / "data_evil" / "stolen.csv").exists())

    def test_missing_input_fails_before_any_step_runs(self):
        self._type_with_step(save_folder=self.root / "saved")
        calls = []

        ok, _ = flow_engine.run_dump_flow(
            "b1", "feed", self.root / "missing.csv", db_path=self.db,
            run_python=lambda *args: calls.append(args) or True,
            log=lambda _m: None,
        )

        self.assertFalse(ok)
        self.assertEqual(calls, [])
        self.assertEqual(df.list_runs(1, db_path=self.db)[0]["status"], "failed")

    def test_default_runner_honours_configured_working_directory(self):
        work = self.root / "work"
        work.mkdir()
        marker = self.root / "cwd.txt"
        script = self.root / "write_cwd.py"
        script.write_text(
            "import os, pathlib\n"
            f"pathlib.Path({str(marker)!r}).write_text(os.getcwd())\n",
            encoding="utf-8",
        )
        self._type_with_step(target=script, working_dir=work)

        ok, _ = flow_engine.run_dump_flow(
            "b2", "feed", "", db_path=self.db, log=lambda _m: None)

        self.assertTrue(ok)
        self.assertEqual(Path(marker.read_text()).resolve(), work.resolve())

    def test_failed_email_is_not_marked_seen(self):
        src = self.root / "feed.csv"
        src.write_text("a\n1\n", encoding="utf-8")
        self._type_with_step(save_folder=self.root / "saved")

        with mock.patch.object(flow_engine, "run_dump_flow", return_value=(False, [])):
            result = sarthi_receiver.handle_email(
                "entry-1", "feed", "", "sender@example.com", [src],
                db_path=self.db, seen_path=self.seen, log=lambda _m: None)

        self.assertEqual(result, "failed")
        self.assertFalse(sarthi_receiver.is_seen(self.seen, "entry-1"))

    def test_released_intake_claim_clears_claim_timestamp(self):
        job_id = iq.enqueue(self.root / "feed.csv", entry_id="entry-2", db_path=self.db)
        with sqlite3.connect(self.db) as c:
            c.execute(
                "UPDATE intake_queue SET status='claimed', "
                "claimed_at=datetime('now','-2 hours') WHERE id=?", (job_id,))

        self.assertEqual(iq.release_stale(30, db_path=self.db), 1)
        row = iq.list_jobs(db_path=self.db)[0]
        self.assertEqual(row["status"], "queued")
        self.assertIsNone(row["claimed_at"])

    def test_updater_skips_archive_path_outside_destination(self):
        archive = self.root / "update.zip"
        with zipfile.ZipFile(archive, "w") as z:
            self._write_required_update_files(z)
            z.writestr("Sarthireceiver-main/good.py", "print('ok')")
            z.writestr("Sarthireceiver-main/../../escaped.py", "bad")
        payload = archive.read_bytes()
        destination = self.root / "install"

        with mock.patch.object(updater, "_fetch_zip", return_value=payload):
            written = updater.update_from_github(
                destination, log=lambda _m: None)

        self.assertIn("good.py", written)
        self.assertTrue((destination / "good.py").is_file())
        self.assertFalse((self.root / "escaped.py").exists())

    @staticmethod
    def _write_required_update_files(z):
        for rel in updater.REQUIRED_CODE_FILES:
            z.writestr(f"Sarthireceiver-main/{rel.as_posix()}", f"content for {rel}")

    def test_updater_replaces_receiver_and_evaluator_and_preserves_data(self):
        destination = self.root / "install"
        evaluator = destination / "client_intelligence_pipeline" / "phase2_intelligence.py"
        evaluator.parent.mkdir(parents=True)
        evaluator.write_text("old evaluator", encoding="utf-8")
        (destination / "receiver.sqlite3").write_text("database", encoding="utf-8")
        (destination / "secrets.toml").write_text("secret", encoding="utf-8")

        archive = self.root / "complete-update.zip"
        with zipfile.ZipFile(archive, "w") as z:
            self._write_required_update_files(z)
            z.writestr("Sarthireceiver-main/new_receiver_module.py", "new code")
            z.writestr("Sarthireceiver-main/receiver.sqlite3", "must not overwrite")
            z.writestr("Sarthireceiver-main/secrets.toml", "must not overwrite")

        with mock.patch.object(updater, "_fetch_zip", return_value=archive.read_bytes()):
            written = updater.update_from_github(destination, log=lambda _m: None)

        self.assertIn("app.py", written)
        self.assertIn("client_intelligence_pipeline/phase2_intelligence.py", written)
        self.assertEqual(evaluator.read_text(encoding="utf-8"),
                         "content for client_intelligence_pipeline/phase2_intelligence.py")
        self.assertEqual((destination / "receiver.sqlite3").read_text(), "database")
        self.assertEqual((destination / "secrets.toml").read_text(), "secret")

    def test_updater_removes_only_obsolete_previously_managed_code(self):
        destination = self.root / "install"
        destination.mkdir()
        obsolete = destination / "obsolete_module.py"
        local = destination / "local_notes.txt"
        obsolete.write_text("old", encoding="utf-8")
        local.write_text("keep", encoding="utf-8")
        updater._write_manifest(destination, {Path("obsolete_module.py")})

        archive = self.root / "update-with-deletion.zip"
        with zipfile.ZipFile(archive, "w") as z:
            self._write_required_update_files(z)

        with mock.patch.object(updater, "_fetch_zip", return_value=archive.read_bytes()):
            updater.update_from_github(destination, log=lambda _m: None)

        self.assertFalse(obsolete.exists())
        self.assertTrue(local.exists())

    def test_updater_rejects_incomplete_archive_before_overwrite(self):
        destination = self.root / "install"
        destination.mkdir()
        app_file = destination / "app.py"
        app_file.write_text("installed app", encoding="utf-8")
        archive = self.root / "incomplete.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("wrong-main/app.py", "partial update")

        with mock.patch.object(updater, "_fetch_zip", return_value=archive.read_bytes()):
            with self.assertRaisesRegex(ValueError, "archive is incomplete"):
                updater.update_from_github(destination, log=lambda _m: None)

        self.assertEqual(app_file.read_text(encoding="utf-8"), "installed app")


if __name__ == "__main__":
    unittest.main()
