import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

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

    def run_basket(self, handler):
        yoto.write_private(ocado.session_path(), session())
        original = httpx.Client
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler), **kwargs)
        with patch.object(ocado.httpx, "Client", side_effect=client):
            return ocado.basket()

    def test_full_trolley_read_uses_only_fixed_read_operations(self):
        seen = []
        def handler(request):
            seen.append((request.method, request.url.path))
            self.assertEqual(request.url.host, "www.ocado.com")
            self.assertIn("test-session=", request.headers["cookie"])
            if request.url.path == "/api/cart/v1/carts/active":
                return httpx.Response(200, json={"items": [{"productId": "p", "quantity": 1}]})
            if request.url.path == "/":
                return httpx.Response(200, text='{"csrf":{"token":"test-csrf"}}')
            self.assertEqual(request.method, "PUT")
            self.assertEqual(json.loads(request.content), ["p"])
            self.assertEqual(request.headers["x-csrf-token"], "test-csrf")
            return httpx.Response(200, json={"products": [{"productId": "p", "name": "Milk"}]})
        result = self.run_basket(handler)
        self.assertEqual(result["items"][0]["name"], "Milk")
        self.assertEqual(seen, [("GET", "/api/cart/v1/carts/active"), ("GET", "/"), ("PUT", ocado.PRODUCTS)])

    def test_expired_challenged_and_html_responses_are_errors_not_empty_baskets(self):
        for code in [202, 301, 401, 403, 429, 500]:
            with self.subTest(code=code), self.assertRaises(ocado.OcadoError) as caught:
                self.run_basket(lambda request: httpx.Response(code, text="TEST_PRIVATE_COOKIE"))
            self.assertNotIn("TEST_PRIVATE_COOKIE", str(caught.exception))
        with self.assertRaises(ocado.OcadoError):
            self.run_basket(lambda request: httpx.Response(200, text="<html>Login</html>"))

    def test_metadata_failure_preserves_items_without_inventing_names(self):
        def handler(request):
            if request.url.path == "/api/cart/v1/carts/active":
                return httpx.Response(200, json={"items": [{"productId": "p", "quantity": 1}]})
            return httpx.Response(403, text="TEST_PRIVATE_COOKIE")
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
