import copy
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
import yaml

from household_tools import ocado_setup
from household_tools.integrations import ocado, ocado_edit as edit, yoto
from test_ocado import session


def order():
    return {"order_id": "order-next", "region_id": "region-one", "delivery": {"start": "2099-04-10T10:00:00Z"},
            "is_editable": True, "is_under_edit": False, "guaranteed_editable_by": "2099-04-09T22:00:00Z",
            "total": "10.00", "items": [{"product_id": "milk", "name": "Milk", "quantity": 2},
                                         {"product_id": "bread", "name": "Bread", "quantity": 1}]}


class EditTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        env = patch.dict(os.environ, {"HERMES_HOME": directory.name, "OCADO_ORDER_EDITING_ENABLED": "true"})
        env.start()
        self.addCleanup(env.stop)
        yoto.write_private(ocado.session_path(), {**session(), "product_names": {"milk": "Milk"}})
        self.order = order()
        mock_order = patch.object(ocado, "next_order", side_effect=lambda: {"order": copy.deepcopy(self.order)})
        mock_order.start()
        self.addCleanup(mock_order.stop)
        self.editing = False
        self.wrong_order = False
        self.cart_changed = False
        self.quantity_failure = None
        self.ignore_quantity_write = False
        self.posts = []
        self.cart = {"milk": 2, "bread": 1}
        original = httpx.Client
        factory = patch.object(edit, "client_for", side_effect=lambda saved:
            original(transport=httpx.MockTransport(self.handler), cookies=ocado.cookie_jar(saved), follow_redirects=False))
        factory.start()
        self.addCleanup(factory.stop)

    def handler(self, request):
        self.assertEqual(request.url.host, "www.ocado.com")
        if request.url.path == "/orders":
            return httpx.Response(200, text='{"csrf":{"token":"TEST_TOKEN"}}')
        if request.url.path == "/graphql":
            self.assertEqual(json.loads(request.content), ocado.CART_CONTEXT_REQUEST)
            return httpx.Response(200, json={"data": {"shopper": {"cart": {
                "isOrderUnderEdit": self.editing, "orderId": "wrong" if self.wrong_order else "order-next"}}}})
        if request.method == "GET":
            self.assertEqual(request.url.path, "/api/cart/v1/carts/active")
            items = self.cart if self.editing else ({"unrelated": 1} if self.cart_changed else {})
            return httpx.Response(200, json={"items": [{"productId": p, "quantity": q} for p, q in items.items() if q]})
        payload = json.loads(request.content)
        self.posts.append((request.url.path, payload))
        if request.url.path == edit.START_EDIT:
            self.editing = True
            return httpx.Response(200, headers={"set-cookie": "edit-session=TEST_EDIT_COOKIE; Domain=.ocado.com; Path=/; Secure"})
        self.assertEqual(request.url.path, edit.APPLY_QUANTITY.split("?")[0])
        if self.quantity_failure == "timeout":
            raise httpx.ReadTimeout("PRIVATE_RESPONSE", request=request)
        if self.quantity_failure:
            return httpx.Response(self.quantity_failure, text="PRIVATE_RESPONSE")
        if not self.ignore_quantity_write:
            for change in payload:
                self.cart[change["productId"]] += change["quantity"]
        return httpx.Response(200)

    def preview(self):
        return edit.prepare([{"product_id": "milk", "quantity": 4}, {"product_id": "bread", "quantity": 0}])

    def test_preview_is_read_only_and_apply_uses_deltas_not_desired_quantities(self):
        preview = self.preview()
        self.assertEqual(self.posts, [])
        self.assertEqual(edit.plan_path(preview["plan_id"]).stat().st_mode & 0o777, 0o600)
        result = edit.apply(preview["plan_id"], confirmed=True)
        self.assertEqual(result["status"], "staged_pending_checkout")
        self.assertEqual(self.posts, [(edit.START_EDIT, {"orderId": "order-next", "regionId": "region-one"}),
            (edit.APPLY_QUANTITY.split("?")[0], [{"productId": "milk", "quantity": 2}, {"productId": "bread", "quantity": -1}])])
        saved = ocado.load_session()
        self.assertEqual(saved["product_names"], {"milk": "Milk"})
        self.assertTrue(any(c["name"] == "edit-session" for c in saved["cookies"]))
        self.assertNotIn("TEST_EDIT_COOKIE", repr(result))
        with self.assertRaisesRegex(ocado.OcadoError, "already been attempted"):
            edit.apply(preview["plan_id"], True)
        self.assertEqual(len(self.posts), 2)

    def test_disabled_unconfirmed_expired_and_invalid_plans_never_write(self):
        preview = self.preview()
        with patch.dict(os.environ, {"OCADO_ORDER_EDITING_ENABLED": "false"}), self.assertRaisesRegex(ocado.OcadoError, "disabled"):
            edit.apply(preview["plan_id"], True)
        with self.assertRaisesRegex(ocado.OcadoError, "explicit user confirmation"):
            edit.apply(preview["plan_id"])
        for invalid in ["../credentials/ocado", "missing", "0" * 32]:
            with self.assertRaises(ocado.OcadoError):
                edit.apply(invalid, True)
        path = edit.plan_path(preview["plan_id"])
        plan = json.loads(path.read_text())
        plan["expires_at"] = time.time() - 1
        yoto.write_private(path, plan)
        with self.assertRaisesRegex(ocado.OcadoError, "expired"):
            edit.apply(preview["plan_id"], True)
        self.assertEqual(self.posts, [])

    def test_stale_order_and_changed_standalone_cart_never_write(self):
        preview = self.preview()
        self.order["items"][0]["quantity"] = 3
        with self.assertRaisesRegex(ocado.OcadoError, "order changed"):
            edit.apply(preview["plan_id"], True)
        self.order = order()
        self.cart_changed = True
        with self.assertRaisesRegex(ocado.OcadoError, "standalone trolley changed"):
            edit.apply(preview["plan_id"], True)
        self.assertEqual(self.posts, [])

    def test_wrong_edit_order_blocks_quantity_write_and_replay(self):
        preview = self.preview()
        self.wrong_order = True
        with self.assertRaisesRegex(ocado.OcadoError, "locked against replay"):
            edit.apply(preview["plan_id"], True)
        self.assertEqual(len(self.posts), 1)
        plan = json.loads(edit.plan_path(preview["plan_id"]).read_text())
        self.assertEqual(plan["state"], "outcome_requires_review")
        with self.assertRaises(ocado.OcadoError):
            edit.apply(preview["plan_id"], True)
        self.assertEqual(len(self.posts), 1)

    def test_changed_edit_cart_baseline_blocks_quantity_write(self):
        preview = self.preview()
        self.cart["milk"] = 3
        with self.assertRaisesRegex(ocado.OcadoError, "quantities differ"):
            edit.apply(preview["plan_id"], True)
        self.assertEqual(len(self.posts), 1)

    def test_unexpected_extra_products_block_writes_and_200_is_not_proof_of_success(self):
        preview = self.preview()
        self.cart["unexpected"] = 1
        with self.assertRaisesRegex(ocado.OcadoError, "quantities differ"):
            edit.apply(preview["plan_id"], True)
        self.assertEqual(len(self.posts), 1)
        self.editing = False
        del self.cart["unexpected"]
        self.posts = []
        preview = self.preview()
        self.ignore_quantity_write = True
        with self.assertRaisesRegex(ocado.OcadoError, "Not all requested draft quantities were verified"):
            edit.apply(preview["plan_id"], True)
        self.assertEqual(len(self.posts), 2)

    def test_unknown_write_outcomes_are_not_retried_or_exposed(self):
        for failure in [202, 403, "timeout"]:
            with self.subTest(failure=failure):
                self.editing = False
                self.posts = []
                self.quantity_failure = failure
                preview = self.preview()
                with self.assertRaises(ocado.OcadoError) as caught:
                    edit.apply(preview["plan_id"], True)
                self.assertNotIn("PRIVATE_RESPONSE", str(caught.exception))
                self.assertIn("locked against replay", str(caught.exception))
                self.assertEqual(len(self.posts), 2)
                with self.assertRaises(ocado.OcadoError):
                    edit.apply(preview["plan_id"], True)
                self.assertEqual(len(self.posts), 2)

    def test_input_constraints_and_noneditable_orders(self):
        for changes in [[], [{"product_id": "milk", "quantity": True}], [{"product_id": "milk", "quantity": 51}],
                        [{"product_id": "milk", "quantity": -1}], [{"product_id": "milk", "quantity": 1}] * 2,
                        [{"product_id": "new", "quantity": 1}], [{"product_id": "milk", "quantity": 2}]]:
            with self.assertRaises(ocado.OcadoError):
                edit.prepare(changes)
        for key, value in [("is_editable", False), ("is_under_edit", True), ("region_id", None),
                           ("guaranteed_editable_by", "2020-01-01T00:00:00Z")]:
            self.order = {**order(), key: value}
            with self.assertRaises(ocado.OcadoError):
                self.preview()
        self.assertEqual(self.posts, [])

    def test_household_lock_and_writer_route_restrictions(self):
        with edit.edit_lock(), self.assertRaisesRegex(ocado.OcadoError, "Another household"):
            self.preview()
        with httpx.Client(transport=httpx.MockTransport(lambda req: self.fail("Unexpected write"))) as client:
            for route, body in [("/checkout", {}), (edit.START_EDIT, {"orderId": "../bad", "regionId": "x"}),
                                (edit.APPLY_QUANTITY, [{"productId": "milk", "quantity": 0}])]:
                with self.assertRaises(ocado.OcadoError):
                    edit.post_edit(client, "test", route, body)

    def test_enable_preserves_settings_backs_up_and_adds_guidance_once(self):
        path = yoto.home() / "config.yaml"
        original = {"model": {"provider": "anthropic"}, "mcp_servers": {"household": {"tools": {"include": ["yoto_playback"]}}}}
        path.write_text(yaml.safe_dump(original))
        soul = yoto.home() / "SOUL.md"
        soul.write_text("Existing personal instructions.")
        ocado_setup.enable_editing()
        ocado_setup.enable_editing()
        current = yaml.safe_load(path.read_text())
        self.assertEqual(current["model"], original["model"])
        entry = current["mcp_servers"]["household"]
        self.assertEqual(entry["env"]["OCADO_ORDER_EDITING_ENABLED"], "true")
        self.assertIn("ocado_apply_order_changes", entry["tools"]["include"])
        self.assertIn("yoto_playback", entry["tools"]["include"])
        self.assertEqual(soul.read_text().count("<!-- ocado-order-editing -->"), 1)
        self.assertTrue(soul.read_text().startswith("Existing personal instructions."))
        self.assertTrue(list(yoto.home().glob("SOUL.before-ocado-*")))
