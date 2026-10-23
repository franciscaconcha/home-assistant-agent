import copy
import tempfile
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import httpx

from household_tools.integrations import ocado, yoto
from test_ocado import session


def upcoming(order_id="order-next", start="2099-04-10T10:00:00+01:00", end="2099-04-10T11:00:00+01:00",
             reserved=True, editable=True):
    return {"orderId": order_id, "status": "CONFIRMED", "isRecurring": reserved,
        "prices": {"total": {"amount": "42.50", "currency": "GBP"}},
        "editInfo": {"isEditable": editable, "isUnderEdit": False, "edited": False,
                     "guaranteedEditableBy": "2099-04-09T22:00:00Z", "restrictions": []},
        "slot": {"slotId": "slot-test", "type": "STANDARD",
                 "timeWindow": {"start": start, "end": end, "timeZone": "Europe/London"}}}


def detail(order_id="order-next"):
    return {"entities": {"order": {order_id: {"orderId": order_id,
        "address": {"private": "DO_NOT_RETURN"}, "paymentInfo": {"private": "DO_NOT_RETURN"},
        "groupedProducts": {"products": [{"productId": "product-one", "name": "Milk", "quantity": 2,
            "prices": {"unit": {"amount": "1.70"}, "total": {"amount": "3.40"}}}]}}}}}


class OrderSelectionTests(unittest.TestCase):
    def test_nearest_reserved_order_includes_true_editability_without_skipping_it(self):
        next_order = upcoming(editable=False)
        later = upcoming("order-later", "2099-04-17T10:00:00+01:00", "2099-04-17T11:00:00+01:00")
        oneoff = upcoming("oneoff", "2099-04-09T10:00:00+01:00", "2099-04-09T11:00:00+01:00", reserved=False)
        now = datetime(2099, 4, 8, tzinfo=timezone.utc)
        chosen = ocado.select_next_order([later, oneoff, next_order], now=now)
        self.assertEqual(chosen["order_id"], "order-next")
        self.assertFalse(chosen["is_editable"])
        self.assertEqual(ocado.select_next_order([next_order, oneoff], reserved_only=False, now=now)["order_id"], "oneoff")

    def test_no_matching_order_is_explicit(self):
        now = datetime(2099, 5, 1, tzinfo=timezone.utc)
        self.assertIsNone(ocado.select_next_order([upcoming()], now=now))
        self.assertIsNone(ocado.select_next_order([]))
        self.assertIsNone(ocado.select_next_order([upcoming(reserved=False)]))

    def test_timestamps_require_unambiguous_zone(self):
        local = ocado.order_timestamp("2099-04-10T10:00:00", "Europe/London")
        self.assertIsNotNone(local.utcoffset())
        with self.assertRaises(ocado.OcadoError):
            ocado.order_timestamp("2099-04-10T10:00:00")
        for value in ["not-a-date", None]:
            with self.assertRaises(ocado.OcadoError):
                ocado.order_timestamp(value)

    def test_invalid_order_identity_and_delivery_fail_before_selection(self):
        for value in [upcoming("../checkout"), {**upcoming(), "slot": None}, {"isRecurring": True}]:
            with self.assertRaises(ocado.OcadoError):
                ocado.select_next_order([value])

    def test_detail_is_whitelisted_and_matches_selected_identity(self):
        items = ocado.order_items(detail(), "order-next")
        self.assertEqual(items, [{"product_id": "product-one", "name": "Milk", "quantity": 2,
                                  "unit_price": "1.70", "line_total": "3.40"}])
        self.assertNotIn("DO_NOT_RETURN", repr(items))
        with self.assertRaises(ocado.OcadoError):
            ocado.order_items(detail("different-order"), "order-next")


class OrdersHTTPTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        env = patch.dict(os.environ, {"HERMES_HOME": directory.name})
        env.start()
        self.addCleanup(env.stop)
        yoto.write_private(ocado.session_path(), session())

    def run_query(self, handler):
        original = httpx.Client
        with patch.object(ocado.httpx, "Client", side_effect=lambda **kwargs:
                          original(transport=httpx.MockTransport(handler), **kwargs)):
            return ocado.next_order()

    def test_live_order_reader_uses_exact_query_and_does_not_read_or_change_active_cart(self):
        requests = []
        def handler(request):
            requests.append((request.method, request.url.path))
            self.assertEqual(request.url.host, "www.ocado.com")
            if request.url.path == "/orders":
                return httpx.Response(200, text='{"csrf":{"token":"TEST_PRIVATE_TOKEN"}}')
            if request.url.path == "/graphql":
                import json
                self.assertEqual(json.loads(request.content), ocado.UPCOMING_REQUEST)
                self.assertEqual(request.headers["x-csrf-token"], "TEST_PRIVATE_TOKEN")
                return httpx.Response(200, json={"data": {"ordersUpcoming": [upcoming()]}})
            self.assertEqual(request.url.path, "/api/order/v6/orders/order-next")
            return httpx.Response(200, json=detail())
        result = self.run_query(handler)
        self.assertEqual(result["order"]["items"][0]["name"], "Milk")
        self.assertEqual(requests, [("GET", "/orders"), ("POST", "/graphql"),
                                    ("GET", "/api/order/v6/orders/order-next")])
        self.assertNotIn("TEST_PRIVATE_TOKEN", repr(result))
        self.assertNotIn("DO_NOT_RETURN", repr(result))

    def test_graphql_errors_or_missing_order_list_are_not_no_orders(self):
        for response in [{"errors": [{"message": "private server details"}]}, {"data": {}}]:
            def handler(request):
                if request.url.path == "/orders":
                    return httpx.Response(200, text='{"csrf":{"token":"test"}}')
                return httpx.Response(200, json=response)
            with self.assertRaises(ocado.OcadoError):
                self.run_query(handler)

    def test_unavailable_details_do_not_erase_valid_order_summary(self):
        def handler(request):
            if request.url.path == "/orders":
                return httpx.Response(200, text='{"csrf":{"token":"test"}}')
            if request.url.path == "/graphql":
                return httpx.Response(200, json={"data": {"ordersUpcoming": [upcoming()]}})
            return httpx.Response(202)
        result = self.run_query(handler)
        self.assertEqual(result["order"]["order_id"], "order-next")
        self.assertIsNone(result["order"]["items"])
        self.assertIn("HTTP 202", result["order"]["warning"])

    def test_no_matching_reserved_order_does_not_fetch_detail_or_create_trolley(self):
        def handler(request):
            if request.url.path == "/orders":
                return httpx.Response(200, text='{"csrf":{"token":"test"}}')
            self.assertEqual(request.url.path, "/graphql")
            return httpx.Response(200, json={"data": {"ordersUpcoming": []}})
        self.assertIsNone(self.run_query(handler)["order"])

    def test_only_the_exact_read_query_is_permitted_for_graphql(self):
        mutation = copy.deepcopy(ocado.UPCOMING_REQUEST)
        mutation["query"] = "mutation EditOrder { editOrder { orderId } }"
        with httpx.Client(transport=httpx.MockTransport(lambda request: self.fail("Unexpected request"))) as client:
            for payload in [mutation, {**ocado.UPCOMING_REQUEST, "variables": {"orderId": "other"}}, {}]:
                with self.assertRaises(ocado.OcadoError):
                    ocado.read_request(client, "POST", "/graphql", json=payload)
            for path in ["/api/order/v6/orders/../checkout", "/api/order/v6/orders/id/edit"]:
                with self.assertRaises(ocado.OcadoError):
                    ocado.read_request(client, "GET", path)
