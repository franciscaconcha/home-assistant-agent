import base64
import hashlib
import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import yaml

from household_tools.integrations import yoto
from household_tools import yoto_setup


class YotoTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, {"HERMES_HOME": self.directory.name})
        self.env.start()
        self.addCleanup(self.env.stop)

    def tokens(self, expires=3600):
        yoto.write_private(yoto.token_path(), {
            "client_id": "test-client", "access_token": "test-access",
            "refresh_token": "test-refresh", "expires_at": time.time() + expires,
        })

    def allowed(self, maximum=30, device_id="player-one"):
        yoto.write_private(yoto.home() / "yoto-players.json", {
            "players": {"bedroom": {"device_id": device_id, "max_volume": maximum}},
        })

    def test_pkce_and_state(self):
        verifier = "test-verifier"
        query = parse_qs(urlparse(yoto_setup.login_url("test-client", verifier, "state")).query)
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(query["code_challenge"], [expected])
        self.assertEqual(query["redirect_uri"], [yoto_setup.CALLBACK])
        self.assertEqual(yoto_setup.callback_value("/callback?state=state&code=abc", "state"), "abc")
        for path in ["/callback?state=wrong&code=abc", "/other?state=state&code=abc",
                     "/callback?state=state&error=access_denied"]:
            with self.assertRaises(yoto.YotoError):
                yoto_setup.callback_value(path, "state")

    def test_missing_login_is_actionable(self):
        with self.assertRaisesRegex(yoto.YotoError, "make yoto-login"):
            yoto.access_token()

    def test_refresh_is_rotated_once_across_concurrent_calls(self):
        self.tokens(expires=-10)
        response = httpx.Response(200, json={
            "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 7200,
        })
        with patch.object(yoto.httpx, "post", return_value=response) as post:
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(list(pool.map(lambda _: yoto.access_token(), range(2))), ["new-access"] * 2)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["data"]["refresh_token"], "test-refresh")
        self.assertEqual(json.loads(yoto.token_path().read_text())["refresh_token"], "new-refresh")
        self.assertEqual(yoto.token_path().stat().st_mode & 0o777, 0o600)

    def test_refresh_failure_keeps_saved_token_and_hides_body(self):
        self.tokens(expires=-10)
        before = yoto.token_path().read_text()
        with patch.object(yoto.httpx, "post", return_value=httpx.Response(400, text="SECRET")):
            with self.assertRaisesRegex(yoto.YotoError, "HTTP 400") as caught:
                yoto.access_token()
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(yoto.token_path().read_text(), before)

    def test_player_discovery_minimises_data_and_attaches_alias(self):
        self.tokens()
        self.allowed()
        response = httpx.Response(200, json={"devices": [{
            "deviceId": "player-one", "name": "Bedroom", "online": True, "description": "private",
        }]})
        with patch.object(yoto.httpx, "get", return_value=response) as get:
            self.assertEqual(yoto.list_players(), [{"device_id": "player-one", "name": "Bedroom",
                              "online": True, "alias": "bedroom", "max_volume": 30}])
        self.assertEqual(get.call_args.args[0], yoto.API + "/device-v2/devices/mine")

    def test_unknown_and_unsafe_aliases_fail_before_network(self):
        with patch.object(yoto, "list_players") as devices:
            with self.assertRaises(yoto.YotoError):
                yoto.playback("all", "pause")
            self.allowed(device_id="../other/command")
            with self.assertRaises(yoto.YotoError):
                yoto.playback("bedroom", "pause")
            devices.assert_not_called()

    def test_volume_ceiling_and_offline_player(self):
        self.allowed()
        player = {"device_id": "player-one", "online": True}
        with patch.object(yoto, "list_players", return_value=[player]), patch.object(yoto, "mqtt_request") as send:
            for volume in [-1, 101, 31, True, 2.5]:
                with self.assertRaises(yoto.YotoError):
                    yoto.set_volume("bedroom", volume)
            send.assert_not_called()
            yoto.set_volume("bedroom", 20)
            self.assertEqual(send.call_args.args, ("player-one", "volume/set", '{"volume": 20}'))
            player["online"] = False
            with self.assertRaisesRegex(yoto.YotoError, "offline"):
                yoto.playback("bedroom", "resume")

    def test_action_allowlist(self):
        with patch.object(yoto, "selected_player") as select:
            with self.assertRaises(yoto.YotoError):
                yoto.playback("bedroom", "reboot")
            select.assert_not_called()

    def test_mqtt_subscribes_before_status_request_and_closes(self):
        client = Mock()
        success = SimpleNamespace(is_failure=False)
        def start():
            client.on_connect(client, None, None, success, None)
        def subscribe(topic, qos):
            client.on_subscribe(client, None, 1, [success], None)
            return yoto.mqtt.MQTT_ERR_SUCCESS, 1
        def publish(topic, payload, qos):
            client.on_message(client, None, SimpleNamespace(
                topic="device/player-one/data/status", payload=b'{"status":{"batteryLevel":80}}'))
        client.loop_start.side_effect = start
        client.subscribe.side_effect = subscribe
        client.publish.side_effect = publish
        with patch.object(yoto.mqtt, "Client", return_value=client), patch.object(yoto, "access_token", return_value="test"):
            self.assertEqual(yoto.mqtt_request("player-one", "status/request", "", True), {"batteryLevel": 80})
        client.publish.assert_called_once_with("device/player-one/command/status/request", "", qos=1)
        client.disconnect.assert_called_once()
        client.loop_stop.assert_called_once()

    def test_mqtt_delivery_does_not_claim_device_execution(self):
        client = Mock()
        client.loop_start.side_effect = lambda: client.on_connect(
            client, None, None, SimpleNamespace(is_failure=False), None)
        client.publish.return_value.is_published.return_value = True
        with patch.object(yoto.mqtt, "Client", return_value=client), patch.object(yoto, "access_token", return_value="test"):
            result = yoto.mqtt_request("player-one", "card/pause", "{}")
        self.assertTrue(result["sent"])
        self.assertFalse(result["execution_confirmed"])
        client.publish.assert_called_once_with("device/player-one/command/card/pause", "{}", qos=1)

    def test_configuration_preserves_model_whatsapp_and_other_tools(self):
        path = yoto.home() / "config.yaml"
        original = {"model": {"provider": "anthropic"}, "whatsapp": {"group_policy": "pairing"},
                    "mcp_servers": {"household": {"tools": {"include": ["existing_tool"]},
                                                   "env": {"EXISTING_SETTING": "keep"}},
                                    "other": {"command": "other-command"}}}
        path.write_text(yaml.safe_dump(original))
        with patch.object(yoto, "list_players", return_value=[{"device_id": "player-one", "name": "Bedroom", "online": True}]), \
                patch("builtins.input", side_effect=["1", "bedroom", "25"]):
            yoto_setup.configure()
        current = yaml.safe_load(path.read_text())
        self.assertEqual(current["model"], original["model"])
        self.assertEqual(current["whatsapp"], original["whatsapp"])
        self.assertEqual(current["mcp_servers"]["other"], original["mcp_servers"]["other"])
        self.assertIn("existing_tool", current["mcp_servers"]["household"]["tools"]["include"])
        self.assertIn("yoto_playback", current["mcp_servers"]["household"]["tools"]["include"])
        self.assertEqual(current["mcp_servers"]["household"]["env"], {
            "EXISTING_SETTING": "keep", "HERMES_HOME": self.directory.name,
        })
        self.assertEqual(json.loads((yoto.home() / "yoto-players.json").read_text())
                         ["players"]["bedroom"]["max_volume"], 25)
        self.assertEqual(len(list(yoto.home().glob("config.before-yoto-*.yaml"))), 1)


if __name__ == "__main__":
    unittest.main()
