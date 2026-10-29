import asyncio
import copy
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from household_tools import alexa_import, alexa_setup, occasional
from household_tools.integrations import alexa, ocado, ocado_weekly as weekly, yoto
from ocado_test_ids import product_id
import test_ocado_weekly


def snapshot(name="detergent", version=1):
    return {"list_id": "list-one", "items": [{"id": "item-one", "name": name, "version": version, "status": "ACTIVE"}]}


@unittest.skipIf(sys.version_info < (3, 12), "Alexa connector requires Python 3.12+")
class AlexaProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_pages_are_read_without_lowercasing_and_ids_are_quoted(self):
        pages = [{"itemInfoList": [{"itemId": "one", "itemName": "Fairy Non Bio", "version": 1, "itemStatus": "ACTIVE"}], "nextToken": "page-two"},
                 {"itemInfoList": [{"itemId": "two", "itemName": "Milk", "version": 2, "itemStatus": "COMPLETE"}]}]
        wrapper = SimpleNamespace(session_request=AsyncMock(side_effect=[(None, SimpleNamespace(status=200, json=AsyncMock(return_value=p))) for p in pages]))
        client = alexa.ShoppingSession(wrapper, "https://www.amazon.co.uk")
        result = await client.items("list/one")
        self.assertEqual([row["name"] for row in result], ["Fairy Non Bio", "Milk"])
        calls = wrapper.session_request.call_args_list
        self.assertIn("list%2Fone", str(calls[0].kwargs["url"]))
        self.assertEqual(calls[1].kwargs["input_data"], {"nextToken": "page-two"})
        self.assertTrue(all(call.kwargs["fail_fast"] for call in calls))

    async def test_repeated_pagination_token_or_item_id_stops_instead_of_truncating(self):
        for page in [{"itemInfoList": [], "nextToken": "repeat"},
                     {"itemInfoList": [{"itemId": "same", "itemName": "Milk", "version": 1, "itemStatus": "ACTIVE"}], "nextToken": "repeat"}]:
            wrapper = SimpleNamespace(session_request=AsyncMock(return_value=(None, SimpleNamespace(status=200, json=AsyncMock(return_value=page)))))
            with self.assertRaises(ocado.OcadoError):
                await alexa.ShoppingSession(wrapper, "https://www.amazon.co.uk").items("list")

    async def test_complete_is_version_checked_and_cannot_delete_or_rename(self):
        wrapper = SimpleNamespace(session_request=AsyncMock(return_value=(None, SimpleNamespace(status=200, json=AsyncMock(return_value={})))))
        await alexa.ShoppingSession(wrapper, "https://www.amazon.co.uk").complete("list", snapshot()["items"][0])
        request = wrapper.session_request.call_args.kwargs
        self.assertEqual(str(request["url"]), "https://www.amazon.co.uk/alexashoppinglists/api/v2/lists/list/items/item-one?version=1")
        self.assertEqual(request["input_data"]["itemAttributesToUpdate"], [{"type": "itemStatus", "value": "COMPLETE"}])
        self.assertEqual(request["method"], "PUT")

    async def test_refreshed_session_persists_after_read_failure_without_password_or_otp(self):
        class Session:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        class State:
            def __init__(self, site, email, password, stored):
                self.login_stored_data = dict(stored or {})
                self.retail_site_url = SimpleNamespace(human_repr=lambda: site)
        class Login:
            def __init__(self, wrapper, state): self.state = state
            async def login_mode_stored_data(self):
                self.state.login_stored_data["refresh_token"] = "REFRESHED_PRIVATE_TOKEN"
        async def failed(_): raise RuntimeError("PRIVATE_RESPONSE")
        dependencies = (Session, lambda **kwargs: None, lambda *args: None, State, Login, lambda **kwargs: None)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HERMES_HOME": directory}), \
                patch.object(alexa, "dependencies", return_value=dependencies):
            with self.assertRaises(ocado.OcadoError):
                await alexa.connection(failed, site="https://www.amazon.co.uk", email="private-email", password="PRIVATE_PASSWORD",
                    stored={"device_info": {"device_serial_number": "PRIVATE_SERIAL"}, "site": "https://www.amazon.co.uk"})
            saved = alexa.credentials_path().read_text()
            self.assertIn("REFRESHED_PRIVATE_TOKEN", saved)
            self.assertNotIn("PRIVATE_PASSWORD", saved)
            self.assertNotIn("private-email", saved)
            self.assertEqual(alexa.credentials_path().stat().st_mode & 0o777, 0o600)

    def test_diagnostics_expose_only_fixed_stage_status_and_exception_category(self):
        cases = [("/ap/signin", "Amazon sign-in"), ("/auth/register", "Alexa app registration"),
                 ("/v1/devices/@self/capabilities", "Alexa app capabilities registration"),
                 ("/alexashoppinglists/api/v2/lists/PRIVATE_LIST/items/fetch", "shopping-list access")]
        for path, stage in cases:
            with self.subTest(stage=stage):
                diagnostic = alexa.RequestDiagnostics()
                diagnostic.record("https://www.amazon.co.uk" + path + "?token=PRIVATE_TOKEN&password=PRIVATE_PASSWORD", 400)
                error = diagnostic.failure(ValueError("PRIVATE_RESPONSE PRIVATE_PASSWORD"))
                self.assertIn(stage, str(error))
                self.assertIn("HTTP 400", str(error))
                self.assertNotIn("PRIVATE", str(error))
                self.assertNotIn("amazon.co.uk", repr(vars(diagnostic)))

    async def test_interactive_login_bootstraps_globally_but_stored_sessions_keep_uk_region(self):
        created_sites = []
        class Session:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        class State:
            def __init__(self, site, email, password, stored):
                created_sites.append(site)
                self.login_stored_data = {}
                self.retail_site_url = SimpleNamespace(human_repr=lambda: site)
        class Login:
            def __init__(self, wrapper, state): self.state = state
            async def login_mode_interactive(self, otp):
                self.state.retail_site_url = SimpleNamespace(human_repr=lambda: "https://www.amazon.co.uk")
            async def login_mode_stored_data(self): pass
        async def regional_site(client): return client.site
        dependencies = (Session, lambda **kwargs: None, lambda *args: None, State, Login, lambda **kwargs: None)
        with patch.object(alexa, "dependencies", return_value=dependencies):
            for otp in ("123456", None):
                result = await alexa.connection(regional_site, site="https://www.amazon.co.uk", otp=otp)
                self.assertEqual(result, "https://www.amazon.co.uk")
        self.assertEqual(created_sites, ["https://www.amazon.com", "https://www.amazon.co.uk"])

    def test_login_accepts_combined_or_split_forms_without_sending_password_to_email_only_form(self):
        from bs4 import BeautifulSoup
        _, _, _, State, Login, _ = alexa.dependencies()
        state = State("https://www.amazon.com", "test@example.invalid", "PRIVATE_TEST_PASSWORD", None)
        login = Login(Mock(), state)
        hidden = '<input type="hidden" name="csrf" value="TEST_CSRF">'
        for names in [("email", "password"), ("email",), ("password",), ("otpCode",)]:
            with self.subTest(names=names):
                fields = ''.join(f'<input name="{name}">' for name in names)
                soup = BeautifulSoup('<form name="signIn">' + hidden + fields + '</form>', 'html.parser')
                result = login._get_inputs_from_soup(soup)
                self.assertEqual(result["csrf"], "TEST_CSRF")
                self.assertEqual("email" in result, "email" in names)
                self.assertEqual("password" in result, "password" in names)
                if "password" in names:
                    self.assertEqual(result["password"], "PRIVATE_TEST_PASSWORD")
        soup = BeautifulSoup('<form name="signIn"><input name="email"></form>'
                             '<form><input name="password"></form>', 'html.parser')
        self.assertNotIn("password", login._get_inputs_from_soup(soup))

    async def test_sync_tool_adapter_works_inside_running_mcp_event_loop(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HERMES_HOME": directory}):
            yoto.write_private(alexa.config_path(), {"site": "https://www.amazon.co.uk", "list_id": "list-one"})
            yoto.write_private(alexa.credentials_path(), {"site": "https://www.amazon.co.uk"})
            with patch.object(alexa, "connection", new=AsyncMock(return_value=snapshot())):
                result = alexa.snapshot()
            self.assertEqual(result["items"][0]["name"], "detergent")


class AlexaImportTests(unittest.TestCase):
    handler = test_ocado_weekly.WeeklyTests.handler

    def setUp(self):
        test_ocado_weekly.WeeklyTests.setUp(self)
        occasional.save_item("detergent", "Detergent", [{"product_id": product_id("detergent"), "name": "Approved Fairy bottle"}], confirmed=True)
        self.captured = snapshot()
        mock_read = patch.object(alexa, "snapshot", side_effect=lambda: copy.deepcopy(self.captured))
        self.read_alexa = mock_read.start()
        self.addCleanup(mock_read.stop)
        finish = patch.object(alexa, "complete_snapshot", return_value={"status": "completed"})
        self.complete = finish.start()
        self.addCleanup(finish.stop)

    def test_weekly_imports_saved_preference_then_marks_captured_items_after_cart_verification(self):
        def finish(captured):
            self.assertEqual(self.cart[product_id("detergent")], 1)
            self.assertEqual(json.loads(weekly.run_path("order-next").read_text())["state"], "ready_for_checkout")
            self.assertEqual(captured["items"][0]["name"], "detergent")
            return {"status": "completed"}
        self.complete.side_effect = finish
        result = weekly.run("weekly ocado, including Alexa", include_alexa=True)
        self.assertEqual(result["status"], "ready_for_checkout")
        self.assertEqual(result["alexa"]["status"], "completed")
        self.complete.assert_called_once()

    def test_plain_weekly_does_not_read_or_complete_alexa(self):
        weekly.run("weekly ocado")
        self.read_alexa.assert_not_called()
        self.complete.assert_not_called()

    def test_flag_and_trigger_must_account_for_alexa_and_all_recipe_names(self):
        for trigger, flag in [("weekly ocado, including Alexa", False), ("weekly ocado", True),
                              ("weekly ocado, including Alexa and missing recipe", True)]:
            with self.subTest(trigger=trigger), self.assertRaises(ocado.OcadoError):
                weekly.run(trigger, include_alexa=flag)
        self.assertEqual(self.posts, [])

    def test_unknown_list_entries_or_read_failure_stop_before_order_selection_or_writes(self):
        self.captured = snapshot("unknown strawberries")
        with self.assertRaisesRegex(ocado.OcadoError, "unknown strawberries"):
            weekly.run("weekly ocado, including Alexa", include_alexa=True)
        self.reader.assert_not_called()
        self.assertEqual(self.posts, [])
        self.read_alexa.side_effect = ocado.OcadoError("Alexa unavailable")
        with self.assertRaises(ocado.OcadoError):
            weekly.run("weekly ocado, including Alexa", include_alexa=True)
        self.assertEqual(self.posts, [])
        self.complete.assert_not_called()

    def test_confirmed_alias_to_staple_is_covered_without_extra_packs(self):
        self.captured = snapshot("milk")
        with self.assertRaises(ocado.OcadoError):
            alexa_import.save_mapping("milk", "weekly", product_id("milk"))
        alexa_import.save_mapping("milk", "weekly", product_id("milk"), True)
        result = weekly.run("weekly ocado, including Alexa", include_alexa=True)
        self.assertEqual(self.cart[product_id("milk")], 4)
        self.assertTrue(self.complete.call_args.args[0]["items"][0]["already_covered"])
        self.assertEqual(result["status"], "ready_for_checkout")

    def test_occasional_named_as_extra_and_in_alexa_is_not_added_twice(self):
        result = weekly.run("weekly ocado, detergent and including Alexa", extras=["detergent"], include_alexa=True)
        self.assertEqual(self.cart[product_id("detergent")], 1)
        self.assertEqual(result["status"], "ready_for_checkout")

    def test_failed_ocado_write_never_completes_alexa_entries(self):
        self.quantity_failure = 500
        with self.assertRaises(ocado.OcadoError):
            weekly.run("weekly ocado, including Alexa", include_alexa=True)
        self.complete.assert_not_called()

    def test_alexa_completion_failure_does_not_report_ocado_failure_or_replay_shop(self):
        self.complete.side_effect = RuntimeError("PRIVATE_RESPONSE")
        result = weekly.run("weekly ocado, including Alexa", include_alexa=True)
        self.assertEqual(result["status"], "ready_for_checkout")
        self.assertEqual(result["alexa"]["status"], "pending")
        self.assertNotIn("PRIVATE", repr(result))
        self.assertEqual(json.loads(weekly.run_path("order-next").read_text())["state"], "ready_for_checkout")

    def test_same_order_redo_retains_imported_items_after_alexa_is_empty(self):
        weekly.run("weekly ocado, including Alexa", include_alexa=True)
        self.captured["items"] = []
        self.order["is_under_edit"] = True
        result = weekly.run("weekly ocado, including Alexa", include_alexa=True)
        self.assertEqual(result["status"], "ready_for_checkout")
        self.assertEqual(self.cart[product_id("detergent")], 1)
        self.assertEqual(len(self.complete.call_args.args[0]["items"]), 1)

    def test_changed_configured_list_does_not_mix_old_order_import(self):
        weekly.run("weekly ocado, including Alexa", include_alexa=True)
        before = len(self.posts)
        self.captured["list_id"] = "different-list"
        self.order["is_under_edit"] = True
        with self.assertRaisesRegex(ocado.OcadoError, "Alexa list changed"):
            weekly.run("weekly ocado, including Alexa", include_alexa=True)
        self.assertEqual(len(self.posts), before)

    def test_failed_first_import_uses_fresh_list_without_carrying_removed_unfulfilled_items(self):
        self.quantity_failure = 500
        with self.assertRaises(ocado.OcadoError):
            weekly.run("weekly ocado, including Alexa", include_alexa=True)
        self.quantity_failure = None
        self.captured["items"] = []
        self.order["is_under_edit"] = True
        weekly.run("weekly ocado, including Alexa", include_alexa=True)
        self.assertEqual(self.cart.get(product_id("detergent"), 0), 0)

    def test_failed_redo_preserves_previously_verified_completed_import_on_recovery(self):
        weekly.run("weekly ocado, including Alexa", include_alexa=True)
        self.captured["items"] = []
        self.order["is_under_edit"] = True
        self.quantity_failure = 500
        with self.assertRaises(ocado.OcadoError):
            weekly.run("weekly ocado, including Alexa", include_alexa=True)
        self.quantity_failure = None
        weekly.run("weekly ocado, including Alexa", include_alexa=True)
        self.assertEqual(self.cart[product_id("detergent")], 1)


class AlexaCompletionTests(unittest.TestCase):
    def test_renamed_or_version_changed_entries_and_new_items_remain_untouched(self):
        captured = snapshot()
        captured["items"] += [{"id": "renamed", "name": "Original", "version": 1, "status": "ACTIVE"},
                              {"id": "changed", "name": "Changed", "version": 1, "status": "ACTIVE"}]
        live = [copy.deepcopy(captured["items"][0]),
                {"id": "renamed", "name": "New name", "version": 2, "status": "ACTIVE"},
                {"id": "changed", "name": "Changed", "version": 2, "status": "ACTIVE"},
                {"id": "new", "name": "New item", "version": 1, "status": "ACTIVE"}]
        async def complete(list_id, row):
            next(item for item in live if item["id"] == row["id"])["status"] = "COMPLETE"
        client = SimpleNamespace(items=AsyncMock(side_effect=lambda _: copy.deepcopy(live)), complete=AsyncMock(side_effect=complete))
        with patch.object(alexa, "load_config", return_value={"list_id": "list-one"}), \
             patch.object(alexa, "run", side_effect=lambda op: asyncio.run(op(client))):
            result = alexa.complete_snapshot(captured)
        self.assertEqual(result["completed"], ["detergent"])
        self.assertEqual(result["left_unchanged"], ["Original", "Changed"])
        self.assertEqual(live[-1]["status"], "ACTIVE")
        client.complete.assert_called_once()

    def test_private_session_failure_never_echoes_tokens(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with patch.dict(os.environ, {"HERMES_HOME": directory.name}):
            yoto.write_private(alexa.config_path(), {"site": "https://www.amazon.co.uk", "list_id": "list-one"})
            yoto.write_private(alexa.credentials_path(), {"site": "https://www.amazon.co.uk", "refresh_token": "SECRET"})
            with patch.object(alexa, "connection", side_effect=RuntimeError("SECRET")), self.assertRaises(ocado.OcadoError) as caught:
                alexa.run(None)
            self.assertNotIn("SECRET", str(caught.exception))


class AlexaSetupTests(unittest.TestCase):
    def test_enabling_is_read_only_and_preserves_other_integrations(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HERMES_HOME": directory}):
            original = {"model": {"provider": "test"}, "mcp_servers": {"household": {
                "tools": {"include": ["ocado_weekly_shop", "calendar_list_events", "yoto_set_mode_now"]}, "timeout": 900}}}
            path = yoto.home() / "config.yaml"
            path.write_text(json.dumps(original))
            with patch.object(alexa, "snapshot", return_value=snapshot()):
                alexa_setup.enable()
                alexa_setup.enable()
            saved = json.loads(path.read_text())
            self.assertEqual(saved["model"], original["model"])
            entry = saved["mcp_servers"]["household"]
            self.assertEqual(entry["timeout"], 900)
            self.assertEqual(entry["tools"]["include"], original["mcp_servers"]["household"]["tools"]["include"] + alexa_setup.TOOLS)
            self.assertEqual((yoto.home() / "SOUL.md").read_text().count("<!-- household-alexa-import-v1 -->"), 1)
