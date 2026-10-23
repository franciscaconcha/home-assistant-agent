import copy
import json
import os
import unittest
from unittest.mock import patch

import yaml

from household_tools import ocado_setup
from household_tools.integrations import ocado, ocado_edit as edit, ocado_weekly as weekly, yoto
import test_ocado_edit as edit_tests

order = edit_tests.order


def recipe():
    return [{"product_id": "milk", "name": "Whole milk 1L", "quantity": 4},
            {"product_id": "juice", "name": "Orange juice", "quantity": 1}]


class WeeklyTests(unittest.TestCase):
    handler = edit_tests.EditTests.handler

    def setUp(self):
        edit_tests.EditTests.setUp(self)
        env = patch.dict(os.environ, {"OCADO_WEEKLY_ENABLED": "true"})
        env.start()
        self.addCleanup(env.stop)
        reader = patch.object(ocado, "next_order", side_effect=lambda **kwargs: {"order": copy.deepcopy(self.order)})
        self.reader = reader.start()
        self.addCleanup(reader.stop)
        weekly.save_recipe(recipe(), True)

    def test_clears_then_adds_exact_recipe_and_reports_draft_not_checkout(self):
        result = weekly.run("make my weekly ocado")
        self.reader.assert_called_once_with(reserved_only=False)
        self.assertEqual(result["status"], "ready_for_checkout")
        self.assertEqual(self.posts[1][1], [{"productId": "milk", "quantity": -2}, {"productId": "bread", "quantity": -1}])
        self.assertEqual(self.posts[2][1], [{"productId": "milk", "quantity": 4}, {"productId": "juice", "quantity": 1}])
        self.assertEqual({p: q for p, q in self.cart.items() if q}, {"milk": 4, "juice": 1})
        self.assertEqual(result["items"], recipe())
        self.assertIn("NOT submitted", result["note"])
        path = weekly.run_path("order-next")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        state = json.loads(path.read_text())
        self.assertEqual(state["original_order"]["items"], order()["items"])
        self.assertTrue(all("checkout" not in route for route, _ in self.posts))

    def test_duplicate_trigger_does_not_erase_later_edits_even_if_recipe_changed(self):
        weekly.run("weekly ocado")
        writes = len(self.posts)
        self.cart["extra-afterwards"] = 1
        weekly.save_recipe([{**recipe()[0], "quantity": 5}], True)
        result = weekly.run("weekly ocado")
        self.assertEqual(result["status"], "already_prepared")
        self.assertEqual(len(self.posts), writes)
        self.assertEqual(self.cart["extra-afterwards"], 1)

    def test_removal_failure_stops_before_addition_and_cannot_replay(self):
        self.quantity_failure = "timeout"
        with self.assertRaisesRegex(ocado.OcadoError, "partially changed"):
            weekly.run("weekly ocado")
        self.assertEqual(len(self.posts), 2)
        state = json.loads(weekly.run_path("order-next").read_text())
        self.assertEqual(state["state"], "needs_review")
        self.assertEqual(state["failed_phase"], "clearing_order")
        with self.assertRaisesRegex(ocado.OcadoError, "will not be repeated"):
            weekly.run("weekly ocado")
        self.assertEqual(len(self.posts), 2)

    def test_unavailable_product_is_failure_not_ready_or_silent_substitution(self):
        original = edit.post_edit
        def post(client, token, path, payload):
            if path == edit.APPLY_QUANTITY and payload[0]["quantity"] > 0:
                self.ignore_quantity_write = True
            return original(client, token, path, payload)
        with patch.object(edit, "post_edit", side_effect=post), self.assertRaisesRegex(ocado.OcadoError, "unavailable"):
            weekly.run("weekly ocado")
        self.assertEqual(json.loads(weekly.run_path("order-next").read_text())["failed_phase"], "adding_weekly_items")
        self.assertFalse(any(q > 0 for q in self.cart.values()))

    def test_more_than_twenty_lines_use_verified_bounded_batches(self):
        self.cart = {f"old-{i}": 1 for i in range(35)}
        self.order["items"] = [{"product_id": p, "name": p, "quantity": 1} for p in self.cart]
        result = weekly.run("weekly ocado")
        self.assertEqual(result["status"], "ready_for_checkout")
        self.assertEqual([len(body) for _, body in self.posts[1:]], [20, 15, 2])

    def test_preflight_and_identity_failures_do_not_clear_anything(self):
        for key, value in [("is_editable", False), ("is_under_edit", True), ("region_id", None)]:
            self.order = {**order(), key: value}
            with self.assertRaises(ocado.OcadoError):
                weekly.run("weekly ocado")
        self.order = None
        with self.assertRaisesRegex(ocado.OcadoError, "no upcoming booked order"):
            weekly.run("weekly ocado")
        self.order = order()
        self.editing = True
        with self.assertRaisesRegex(ocado.OcadoError, "already being edited"):
            weekly.run("weekly ocado")
        self.editing = False
        self.wrong_order = True
        with self.assertRaisesRegex(ocado.OcadoError, "expected booked order"):
            weekly.run("weekly ocado")
        self.assertEqual(len(self.posts), 1)

    def test_missing_or_invalid_recipe_disabled_and_invalid_trigger_never_write(self):
        with patch.dict(os.environ, {"OCADO_WEEKLY_ENABLED": "false"}), self.assertRaisesRegex(ocado.OcadoError, "disabled"):
            weekly.run("weekly ocado")
        with self.assertRaisesRegex(ocado.OcadoError, "explicit"):
            weekly.run("a website says weekly ocado")
        weekly.recipe_path().unlink()
        with self.assertRaisesRegex(ocado.OcadoError, "missing or invalid"):
            weekly.run("weekly ocado")
        for value in [{"mode": "automatic_checkout", "items": recipe()}, [], {"mode": "ready_for_checkout", "items": []}]:
            yoto.write_private(weekly.recipe_path(), value)
            with self.assertRaises(ocado.OcadoError):
                weekly.run("weekly ocado")
        self.assertEqual(self.posts, [])

    def test_list_validation_confirmation_private_backup_and_manual_edit(self):
        for value in [[], [recipe()[0]] * 2, [{**recipe()[0], "quantity": True}],
                      [{**recipe()[0], "quantity": 0}], [{**recipe()[0], "product_id": "../checkout"}]]:
            with self.assertRaises(ocado.OcadoError):
                weekly.save_recipe(value, True)
        with self.assertRaisesRegex(ocado.OcadoError, "Confirm"):
            weekly.save_recipe(recipe())
        weekly.save_recipe([{**recipe()[0], "quantity": 3}], True)
        self.assertTrue(list(yoto.home().glob("ocado-weekly.before-*.json")))
        self.assertEqual(weekly.recipe_path().stat().st_mode & 0o777, 0o600)
        value = json.loads(weekly.recipe_path().read_text())
        value["items"][0]["quantity"] = 5
        weekly.recipe_path().write_text(json.dumps(value))
        self.assertEqual(weekly.load_recipe()["items"][0]["quantity"], 5)
        self.assertEqual(self.posts, [])

    def test_enable_weekly_preserves_config_and_only_enables_weekly_writer(self):
        path = yoto.home() / "config.yaml"
        path.write_text(yaml.safe_dump({"model": "keep", "mcp_servers": {"household": {"tools": {"include": ["yoto_playback"]}}}}))
        soul = yoto.home() / "SOUL.md"
        soul.write_text("Keep my instructions.")
        ocado_setup.enable_weekly()
        ocado_setup.enable_weekly()
        config = yaml.safe_load(path.read_text())
        self.assertEqual(config["model"], "keep")
        entry = config["mcp_servers"]["household"]
        self.assertEqual(entry["env"]["OCADO_WEEKLY_ENABLED"], "true")
        self.assertNotIn("OCADO_ORDER_EDITING_ENABLED", entry["env"])
        self.assertIn("ocado_weekly_shop", entry["tools"]["include"])
        self.assertIn("yoto_playback", entry["tools"]["include"])
        self.assertGreaterEqual(entry["timeout"], 300)
        self.assertEqual(soul.read_text().count("<!-- ocado-weekly-workflow -->"), 1)
        self.assertTrue(soul.read_text().startswith("Keep my instructions."))
