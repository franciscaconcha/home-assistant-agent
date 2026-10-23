import json
import os
import tempfile
import time
import unittest
import sys
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import httpx
import yaml

from household_tools import ocado_setup
from household_tools.integrations import ocado, yoto


def session():
    return {"cookies": [{"name": "test-session", "value": "TEST_PRIVATE_COOKIE", "domain": ".ocado.com",
                         "path": "/", "secure": True, "expires": time.time() + 3600}],
            "user_agent": "TestBrowser/1.0"}


class OcadoTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, {"HERMES_HOME": self.directory.name})
        env.start()
        self.addCleanup(env.stop)

    def test_session_requires_usable_ocado_cookies(self):
        value = session()
        value["cookies"].extend([
            {"name": "unrelated", "value": "ignore", "domain": ".example.com"},
            {"name": "suffix-trap", "value": "ignore", "domain": "evilocado.com"},
            {"name": "expired", "value": "ignore", "domain": ".ocado.com", "expires": 1},
            {"name": "non-finite", "value": "ignore", "domain": ".ocado.com", "expires": float("nan")},
        ])
        result = ocado.validated_session(value)
        self.assertEqual([c["name"] for c in result["cookies"]], ["test-session"])
        for bad in [{}, {"cookies": []}, {"cookies": [{"name": "x", "value": "x", "domain": "evilocado.com"}]}]:
            with self.assertRaises(ocado.OcadoError):
                ocado.validated_session(bad)

    def test_missing_session_is_actionable(self):
        with self.assertRaisesRegex(ocado.OcadoError, "make ocado-login"):
            ocado.basket()

    def test_private_session_saved_without_cross_domain_data(self):
        yoto.write_private(ocado.session_path(), session())
        value = ocado.load_session()
        self.assertEqual(len(value["cookies"]), 1)
        self.assertEqual(ocado.session_path().stat().st_mode & 0o777, 0o600)

    def test_unknown_schema_is_not_empty_trolley(self):
        for cart in [{}, {"items": None}, {"items": "login"}, {"items": [{"productId": "p", "quantity": None}]}]:
            with self.assertRaises(ocado.OcadoError):
                ocado.normalise_trolley(cart, [])
        self.assertEqual(ocado.normalise_trolley({"items": []}, [])["items"], [])

    def test_exact_price_and_unknown_values(self):
        cart = {"items": {"p": {"productId": "p", "quantity": 2, "finalPrice": {"amount": "3.40"}}},
                "totals": {"subTotal": {"amount": "3.40", "currency": "GBP"}}}
        result = ocado.normalise_trolley(cart, [{"productId": "p", "name": "Milk"}])
        self.assertEqual(result["subtotal"], "3.40")
        self.assertEqual(result["items"][0], {"product_id": "p", "name": "Milk", "quantity": 2, "line_total": "3.40"})
        self.assertIsNone(ocado.normalise_trolley({"items": [{"productId": "p", "quantity": 1}]}, [])["items"][0]["name"])
        for amount in [None, "invalid", "NaN", "Infinity", -1, True]:
            self.assertIsNone(ocado.amount(amount))

    def run_basket(self, handler, saved=None):
        yoto.write_private(ocado.session_path(), saved or session())
        original = httpx.Client
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler), **kwargs)
        with patch.object(ocado.httpx, "Client", side_effect=client):
            return ocado.basket()

    def test_snapshot_names_need_only_live_trolley_request(self):
        seen = []
        def handler(request):
            seen.append((request.method, request.url.path))
            self.assertEqual(request.url.host, "www.ocado.com")
            self.assertIn("test-session=", request.headers["cookie"])
            if request.url.path == "/api/cart/v1/carts/active":
                return httpx.Response(200, json={"items": [{"productId": "p", "quantity": 1}]})
            self.fail("The challenged product-information and homepage routes must not be called.")
        saved = {**session(), "product_names": {"p": "Milk"}, "captured_at": 12345}
        result = self.run_basket(handler, saved)
        self.assertEqual(result["items"][0]["name"], "Milk")
        self.assertEqual(result["names_captured_at"], 12345)
        self.assertIn("not live", result["name_source"])
        self.assertEqual(seen, [("GET", "/api/cart/v1/carts/active")])

    def test_expired_challenged_and_html_responses_are_errors_not_empty_baskets(self):
        for code in [202, 301, 401, 403, 429, 500]:
            with self.subTest(code=code), self.assertRaises(ocado.OcadoError) as caught:
                self.run_basket(lambda request: httpx.Response(code, text="TEST_PRIVATE_COOKIE"))
            self.assertNotIn("TEST_PRIVATE_COOKIE", str(caught.exception))
        with self.assertRaises(ocado.OcadoError):
            self.run_basket(lambda request: httpx.Response(200, text="<html>Login</html>"))

    def test_missing_snapshot_names_are_unknown_without_remote_metadata_attempts(self):
        def handler(request):
            if request.url.path == "/api/cart/v1/carts/active":
                return httpx.Response(200, json={"items": [{"productId": "p", "quantity": 1}]})
            self.fail("Must not fetch product names from a challenged endpoint.")
        result = self.run_basket(handler)
        self.assertEqual(result["total_quantity"], 1)
        self.assertIsNone(result["items"][0]["name"])
        self.assertIn("warning", result)
        self.assertNotIn("TEST_PRIVATE_COOKIE", str(result))

    def test_mutating_and_arbitrary_routes_are_denied(self):
        with httpx.Client(transport=httpx.MockTransport(lambda request: self.fail("unexpected request"))) as client:
            for method, path in [("POST", "/api/cart/v1/carts/active/apply-quantity"),
                                 ("GET", "https://example.com"), ("POST", "/checkout")]:
                with self.assertRaises(ocado.OcadoError):
                    ocado.read_request(client, method, path)

    def test_browser_page_names_handle_escaped_quotes_and_braces(self):
        value = {"p": {"productId": "p", "name": 'Milk "organic" {1L}'}}
        html = '<script>{"productEntities": ' + json.dumps(value) + ', "unrelated": "private"}</script>'
        self.assertEqual(ocado.page_product_names(html), {"p": 'Milk "organic" {1L}'})
        self.assertEqual(ocado.page_product_names('"productEntities": invalid'), {})

    def test_snapshot_names_are_validated_and_matched_by_id(self):
        saved = {**session(), "product_names": {"p": "Milk", "wrong": 123}}
        self.assertEqual(ocado.validated_session(saved)["product_names"], {"p": "Milk"})
        result = self.run_basket(lambda request: httpx.Response(200, json={
            "items": [{"productId": "new-product", "quantity": 2}],
        }), saved)
        self.assertIsNone(result["items"][0]["name"])
        self.assertEqual(result["items"][0]["quantity"], 2)
        self.assertIn("1 item lines", result["warning"])

    def test_manual_browser_sync_saves_only_successful_first_party_product_names(self):
        manager, browser, context, page = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        manager.__enter__.return_value.chromium.launch.return_value = browser
        browser.new_context.return_value = context
        context.new_page.return_value = page
        context.cookies.return_value = session()["cookies"]
        page.evaluate.return_value = "TestBrowser/1.0"
        callbacks = {}
        context.on.side_effect = lambda event, callback: callbacks.update({event: callback})
        def content():
            for url, code, key in [(ocado.ORIGIN + ocado.PRODUCTS, 200, "valid"),
                                    ("https://example.com" + ocado.PRODUCTS, 200, "wrong-origin"),
                                    (ocado.ORIGIN + ocado.PRODUCTS, 202, "challenged")]:
                callbacks["response"](SimpleNamespace(url=url, status=code,
                    json=lambda key=key: {"products": [{"productId": key, "name": "Milk"}]}))
            return '<script>{"productEntities":{"p":{"productId":"embedded","name":"Bread"}}}</script>'
        page.content.side_effect = content
        fake = SimpleNamespace(sync_playwright=lambda: manager, Error=RuntimeError)
        with patch.dict(sys.modules, {"playwright.sync_api": fake}), patch("builtins.input", return_value=""):
            ocado_setup.login()
        saved = ocado.load_session()
        self.assertEqual(saved["product_names"], {"valid": "Milk", "embedded": "Bread"})
        self.assertEqual(ocado.session_path().stat().st_mode & 0o777, 0o600)
        browser.close.assert_called_once()

    def test_configuration_preserves_yoto_and_whatsapp(self):
        yoto.write_private(ocado.session_path(), session())
        path = yoto.home() / "config.yaml"
        original = {"model": {"provider": "anthropic"}, "whatsapp": {"group_policy": "pairing"},
            "mcp_servers": {"household": {"tools": {"include": ["yoto_list_players", "yoto_playback"]},
                                         "env": {"OTHER_SETTING": "keep"}}}}
        path.write_text(yaml.safe_dump(original))
        ocado_setup.configure()
        current = yaml.safe_load(path.read_text())
        self.assertEqual(current["model"], original["model"])
        self.assertEqual(current["whatsapp"], original["whatsapp"])
        household = current["mcp_servers"]["household"]
        self.assertIn("yoto_playback", household["tools"]["include"])
        self.assertIn("ocado_basket", household["tools"]["include"])
        self.assertEqual(household["env"], {"HERMES_HOME": self.directory.name, "OTHER_SETTING": "keep"})
