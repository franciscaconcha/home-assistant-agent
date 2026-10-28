import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx
import yaml

from household_tools import yoto_setup
from household_tools.integrations import yoto, yoto_schedule


class YotoScheduleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        env = patch.dict(os.environ, {"HERMES_HOME": directory.name})
        env.start()
        self.addCleanup(env.stop)
        yoto.write_private(yoto.home() / "yoto-players.json", {
            "players": {"Felix": {"device_id": "test-player", "max_volume": 30}}})
        self.player = {"device_id": "test-player", "name": "Test player", "alias": "Felix", "online": True}
        self.config = {"dayTime": "07:00", "nightTime": "19:30", "nightMaxVolumeLimit": "4",
                       "ambientColour": "#ff0000", "alarms": [{"time": "07:15"}], "futureSetting": {"keep": True}}
        self.state = copy.deepcopy(self.config)
        self.puts = []
        self.reads = 0
        self.failure = None
        self.permission_error = False
        self.corrupt = False
        self.fail_readback = False
        self.apply = True
        players = patch.object(yoto, "list_players", side_effect=lambda: [self.player])
        players.start()
        self.addCleanup(players.stop)
        token = patch.object(yoto, "access_token", return_value="PRIVATE_TOKEN")
        token.start()
        self.addCleanup(token.stop)
        for method in ("get", "put"):
            mocked = patch.object(yoto.httpx, method, side_effect=getattr(self, method))
            mocked.start()
            self.addCleanup(mocked.stop)

    def get(self, url, **kwargs):
        self.assertEqual(url, yoto.API + "/device-v2/test-player/config")
        self.reads += 1
        if self.permission_error or (self.fail_readback and self.reads > 1):
            return httpx.Response(403, text="PRIVATE_RESPONSE")
        return httpx.Response(200, json={"device": {"config": copy.deepcopy(self.state),
                            "geoTimezone": "Europe/London", "mac": "PRIVATE_MAC"}})

    def put(self, url, **kwargs):
        self.assertEqual(url, yoto.API + "/device-v2/test-player/config")
        self.puts.append(copy.deepcopy(kwargs["json"]))
        if self.apply:
            self.state = copy.deepcopy(kwargs["json"]["config"])
        if self.corrupt:
            self.state["nightMaxVolumeLimit"] = "16"
        if self.failure == "timeout":
            raise httpx.ReadTimeout("PRIVATE_TOKEN")
        return httpx.Response(self.failure or 200, text="PRIVATE_RESPONSE")

    def test_read_returns_only_times_and_zone_and_allows_offline_player(self):
        self.player["online"] = False
        result = yoto_schedule.get_schedule("Felix")
        self.assertEqual(result["morning_time"], "07:00")
        self.assertEqual(result["bedtime"], "19:30")
        self.assertEqual(result["time_zone"], "Europe/London")
        self.assertNotIn("PRIVATE", repr(result))
        self.assertNotIn("alarms", result)
        self.assertEqual(self.puts, [])

    def test_changes_only_named_time_and_verifies_preserved_settings(self):
        result = yoto_schedule.set_schedule("Felix", bedtime="20:00")
        self.assertEqual(result["status"], "updated")
        self.assertEqual(self.reads, 2)
        self.assertEqual(self.puts, [{"name": "Test player", "config": {**self.config, "nightTime": "20:00"}}])
        self.assertNotIn("PRIVATE", repr(result))
        again = yoto_schedule.set_schedule("Felix", bedtime="20:00")
        self.assertEqual(again["status"], "already_set")
        self.assertEqual(len(self.puts), 1)

    def test_changes_both_times_in_one_write(self):
        result = yoto_schedule.set_schedule("Felix", "08:00", "21:00")
        self.assertEqual(result["morning_time"], "08:00")
        self.assertEqual(result["bedtime"], "21:00")
        self.assertEqual(len(self.puts), 1)

    def test_invalid_times_and_unknown_player_never_write(self):
        for value in ("7am", "7:00", "24:00", "19:60", "07:00\n", True, 700):
            with self.subTest(value=value), self.assertRaises(yoto.YotoError):
                yoto_schedule.set_schedule("Felix", morning_time=value)
        with self.assertRaises(yoto.YotoError):
            yoto_schedule.set_schedule("Felix")
        with self.assertRaises(yoto.YotoError):
            yoto_schedule.set_schedule("unknown", bedtime="20:00")
        self.assertEqual(self.reads, 0)
        self.assertEqual(self.puts, [])
        with self.assertRaisesRegex(yoto.YotoError, "different"):
            yoto_schedule.set_schedule("Felix", bedtime="07:00")
        self.assertEqual(self.puts, [])

    def test_permission_denial_is_actionable_and_no_write(self):
        self.permission_error = True
        with self.assertRaisesRegex(yoto.YotoError, "family:devices:manage") as caught:
            yoto_schedule.set_schedule("Felix", bedtime="20:00")
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(self.puts, [])

    def test_uncertain_response_readback_confirms_without_second_write(self):
        for failure in ("timeout", 500):
            with self.subTest(failure=failure):
                self.failure = failure
                self.state = copy.deepcopy(self.config)
                self.puts = []
                result = yoto_schedule.set_schedule("Felix", bedtime="20:00")
                self.assertEqual(result["status"], "verified_after_uncertain_response")
                self.assertEqual(len(self.puts), 1)

    def test_no_false_success_or_retries_when_write_did_not_apply_or_other_settings_change(self):
        for mode in ("not_applied", "other_changed", "readback_failed"):
            with self.subTest(mode=mode):
                self.state = copy.deepcopy(self.config)
                self.reads, self.puts = 0, []
                self.apply = mode != "not_applied"
                self.corrupt = mode == "other_changed"
                self.fail_readback = mode == "readback_failed"
                with self.assertRaises(yoto.YotoError) as caught:
                    yoto_schedule.set_schedule("Felix", bedtime="20:00")
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.assertEqual(len(self.puts), 1)

    def test_enable_preserves_existing_integrations_aliases_and_soul(self):
        original = {"model": {"provider": "test"}, "whatsapp": {"enabled": True},
                    "mcp_servers": {"household": {"env": {"KEEP": "yes"}, "timeout": 300,
                      "tools": {"include": ["ocado_weekly_shop", "calendar_list_events"],
                                "exclude": ["yoto_get_day_night_times", "other"]}}}}
        path = yoto.home() / "config.yaml"
        path.write_text(yaml.safe_dump(original))
        soul = yoto.home() / "SOUL.md"
        soul.write_text("User instructions")
        allowed = (yoto.home() / "yoto-players.json").read_text()
        yoto_setup.enable_schedule()
        yoto_setup.enable_schedule()
        result = yaml.safe_load(path.read_text())
        self.assertEqual(result["model"], original["model"])
        self.assertEqual(result["whatsapp"], original["whatsapp"])
        entry = result["mcp_servers"]["household"]
        self.assertEqual(entry["timeout"], 300)
        self.assertEqual(entry["env"]["KEEP"], "yes")
        self.assertEqual(entry["tools"]["exclude"], ["other"])
        self.assertEqual(entry["tools"]["include"], ["ocado_weekly_shop", "calendar_list_events"] + yoto_setup.SCHEDULE_TOOLS)
        self.assertEqual(soul.read_text().count("<!-- household-yoto-day-night-v1 -->"), 1)
        self.assertTrue(soul.read_text().startswith("User instructions"))
        self.assertEqual((yoto.home() / "yoto-players.json").read_text(), allowed)
        self.assertEqual(self.puts, [])

    def test_failed_permission_check_does_not_enable_tools(self):
        path = yoto.home() / "config.yaml"
        path.write_text("model: unchanged\n")
        self.permission_error = True
        with self.assertRaises(SystemExit):
            yoto_setup.enable_schedule()
        self.assertEqual(path.read_text(), "model: unchanged\n")
        self.assertEqual(self.puts, [])


if __name__ == "__main__":
    unittest.main()
