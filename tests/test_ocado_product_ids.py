import json
import unittest
from unittest.mock import patch

import httpx

from household_tools import recipes
from household_tools.integrations import ocado, ocado_edit as edit, ocado_weekly as weekly, yoto
from ocado_test_ids import product_id
import test_ocado_weekly as weekly_tests


class ProductIDTests(unittest.TestCase):
    handler=weekly_tests.WeeklyTests.handler

    def setUp(self):
        weekly_tests.WeeklyTests.setUp(self)

    def test_legacy_numeric_id_is_rejected_before_reading_or_clearing_order(self):
        saved=weekly.load_recipe()
        saved['items'].append({'product_id':'12345678','name':'Yoghurt 450g','quantity':1})
        yoto.write_private(weekly.recipe_path(),saved)
        with self.assertRaisesRegex(ocado.OcadoError,'Yoghurt 450g.*internal UUID'):
            weekly.run('weekly ocado')
        self.reader.assert_not_called()
        self.assertEqual(self.posts,[])
        self.assertFalse((yoto.home()/'ocado-weekly-runs').exists())

    def test_saving_bad_id_does_not_replace_good_weekly_list(self):
        before=weekly.recipe_path().read_bytes()
        for bad in ['12345678','https://www.ocado.com/products/example/12345678','not-a-uuid',None,
                    '00000000-0000-0000-0000-00000000000z']:
            with self.subTest(bad=bad), self.assertRaisesRegex(ocado.OcadoError,'internal UUID'):
                weekly.save_recipe([{'product_id':bad,'name':'Product','quantity':1}],True)
        self.assertEqual(weekly.recipe_path().read_bytes(),before)

    def test_bad_recipe_id_stops_before_order_is_touched(self):
        yoto.write_private(recipes.recipe_path('meal-one'),{'id':'meal-one','name':'Meal one',
            'items':[{'product_id':'12345678','name':'Recipe product','quantity':1}]})
        with self.assertRaisesRegex(ocado.OcadoError,'Recipe product.*internal UUID'):
            weekly.run('weekly ocado with Meal one',['Meal one'])
        self.reader.assert_not_called()
        self.assertEqual(self.posts,[])

    def test_uuid_case_normalises_and_duplicates_cannot_hide_in_different_case(self):
        pid=product_id('example')
        clean=weekly.validate_items([{'product_id':pid.upper(),'name':'Example','quantity':1}])
        self.assertEqual(clean[0]['product_id'],pid)
        with self.assertRaisesRegex(ocado.OcadoError,'distinct'):
            weekly.validate_items([{'product_id':value,'name':'Example','quantity':1} for value in [pid,pid.upper()]])

    def test_internal_writer_also_rejects_numeric_id_without_sending_request(self):
        with httpx.Client(transport=httpx.MockTransport(lambda request:self.fail('Unexpected live write'))) as client:
            with self.assertRaises(ocado.OcadoError):
                edit.post_edit(client,'token',edit.APPLY_QUANTITY,[{'productId':'12345678','quantity':1}])
