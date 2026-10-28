import copy
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import test_yoto_schedule
from household_tools.integrations import yoto, yoto_override as override


class YotoOverrideTests(unittest.TestCase):
    get = test_yoto_schedule.YotoScheduleTests.get
    put = test_yoto_schedule.YotoScheduleTests.put

    def setUp(self):
        test_yoto_schedule.YotoScheduleTests.setUp(self)
        self.current = datetime(2026, 10, 28, 12, 0, 30, tzinfo=timezone.utc)
        clock = patch.object(override, "now", side_effect=lambda: self.current)
        clock.start()
        self.addCleanup(clock.stop)
        yoto.write_private(override.heartbeat_path(), {"heartbeat": time.time()})

    def test_moon_saves_usual_schedule_then_restores_at_next_boundary_after_restart(self):
        result = override.switch_now("moon")
        self.assertEqual(result["status"], "temporary_settings_verified")
        self.assertEqual(result["restore_at"], "2026-10-28T19:30:00+00:00")
        record = override.load_record("test-player")
        self.assertEqual(record["usual"], {"dayTime": "07:00", "nightTime": "19:30"})
        self.assertEqual(self.state["nightTime"], "12:00")
        # A worker starts with only disk state, and preserves later unrelated app edits.
        self.state["ambientColour"] = "#123456"
        self.current = datetime(2026, 10, 28, 19, 31, tzinfo=timezone.utc)
        override.run_due()
        self.assertEqual(self.state["nightTime"], "19:30")
        self.assertEqual(self.state["ambientColour"], "#123456")
        self.assertIsNone(override.load_record("test-player"))
        self.assertEqual(len(self.puts), 2)

    def test_sun_at_night_restores_at_usual_morning_and_repeats_keep_original(self):
        self.current = datetime(2026, 10, 28, 22, 0, tzinfo=timezone.utc)
        first = override.switch_now("morning", "Felix")
        second = override.switch_now("sun", "Felix")
        self.assertEqual(first["restore_at"], "2026-10-29T07:00:00+00:00")
        self.assertEqual(second["usual_morning"], "07:00")
        self.assertEqual(len(self.puts), 1)
        self.assertEqual(override.mode_at(self.state, self.current), "sun")

    def test_opposite_shortcut_returns_to_normal_early(self):
        override.switch_now("night")
        result = override.switch_now("sun")
        self.assertEqual(result["status"], "usual_schedule_restored")
        self.assertEqual(self.state, self.config)
        self.assertIsNone(override.load_record("test-player"))

    def test_already_matching_normal_mode_makes_no_change(self):
        result = override.switch_now("sun")
        self.assertEqual(result["status"], "already_in_requested_mode")
        self.assertEqual(self.puts, [])
        self.assertIsNone(override.load_record("test-player"))

    def test_missing_worker_or_offline_player_never_changes_settings(self):
        override.heartbeat_path().unlink()
        with self.assertRaisesRegex(yoto.YotoError, "restore worker"):
            override.switch_now("moon")
        yoto.write_private(override.heartbeat_path(), {"heartbeat": time.time() - 120})
        with self.assertRaises(yoto.YotoError):
            override.switch_now("moon")
        yoto.write_private(override.heartbeat_path(), {"heartbeat": time.time()})
        self.player["online"] = False
        with self.assertRaisesRegex(yoto.YotoError, "offline"):
            override.switch_now("moon")
        self.assertEqual(self.puts, [])
        self.assertEqual(self.reads, 0)

    def test_restore_leaves_later_app_schedule_change_intact(self):
        override.switch_now("moon")
        self.state["nightTime"] = "21:00"
        result = override.restore("Felix")
        self.assertEqual(result["status"], "cancelled_after_external_change")
        self.assertEqual(self.state["nightTime"], "21:00")
        self.assertEqual(len(self.puts), 1)
        self.assertIsNone(override.load_record("test-player"))

    def test_failed_verification_keeps_recovery_record_and_worker_reads_before_restoring(self):
        self.fail_readback = True
        with self.assertRaises(yoto.YotoError):
            override.switch_now("moon")
        self.assertIsNotNone(override.load_record("test-player"))
        self.assertEqual(len(self.puts), 1)
        self.fail_readback = False
        self.current = datetime(2026, 10, 28, 20, 0, tzinfo=timezone.utc)
        override.run_due()
        self.assertEqual(self.state, self.config)
        self.assertIsNone(override.load_record("test-player"))

    def test_restore_failure_keeps_task_until_read_verified(self):
        override.switch_now("moon")
        self.current = datetime(2026, 10, 28, 20, 0, tzinfo=timezone.utc)
        self.permission_error = True
        override.run_due()
        self.assertIsNotNone(override.load_record("test-player"))
        self.assertEqual(len(self.puts), 1)
        self.permission_error = False
        self.player["online"] = False
        override.run_due()
        self.assertEqual(self.state, self.config)

    def test_restore_record_exists_before_remote_change(self):
        def checked_put(*args, **kwargs):
            record = override.load_record("test-player")
            self.assertEqual(record["usual"], override.times(self.config))
            return self.put(*args, **kwargs)
        with patch.object(yoto.httpx, "put", side_effect=checked_put):
            override.switch_now("moon")

    def test_dst_and_overnight_boundaries(self):
        cases = [
            ("2026-03-29T00:45:00+00:00", "2026-03-29T18:30:00+00:00"),
            ("2026-10-25T00:45:00+00:00", "2026-10-25T01:30:00+00:00"),
        ]
        for value, expected in cases:
            self.assertEqual(override.next_boundary({"dayTime": "01:30", "nightTime": "19:30"},
                "Europe/London", datetime.fromisoformat(value)), expected)

    def test_worker_rechecks_due_time_and_player_identity_under_restore_lock(self):
        override.switch_now("moon")
        result = override.restore("Felix", due_only=True, expected_device_id="test-player")
        self.assertEqual(result["status"], "not_due")
        with self.assertRaises(yoto.YotoError):
            override.restore("Felix", due_only=True, expected_device_id="different-player")
        self.assertEqual(len(self.puts), 1)
        self.assertIsNotNone(override.load_record("test-player"))


if __name__ == "__main__":
    unittest.main()
