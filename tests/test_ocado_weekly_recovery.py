from ocado_test_ids import product_id as test_product_id
import json
import unittest
from unittest.mock import patch

from household_tools import recipes
from household_tools.integrations import ocado, ocado_edit as edit, ocado_weekly as weekly, yoto
import test_ocado_weekly as weekly_tests


class WeeklyRecoveryTests(unittest.TestCase):
    handler=weekly_tests.WeeklyTests.handler

    def setUp(self):
        weekly_tests.WeeklyTests.setUp(self)

    def fail_after_server_applies(self, target):
        original=edit.post_edit
        def post(client,token,path,payload):
            original(client,token,path,payload)
            if target=='opening' and path==edit.START_EDIT:
                raise ocado.OcadoError('Ocado editing returned HTTP 500.')
            if target=='adding' and path==edit.APPLY_QUANTITY and payload[0]['quantity']>0:
                raise ocado.OcadoError('Ocado editing returned HTTP 500.')
        with patch.object(edit,'post_edit',side_effect=post), self.assertRaisesRegex(ocado.OcadoError,'HTTP 500'):
            weekly.run('weekly ocado')

    def test_applied_then_500_recovery_uses_live_quantities_instead_of_replaying_deltas(self):
        self.fail_after_server_applies('adding')
        writes=len(self.posts)
        failed=json.loads(weekly.run_path('order-next').read_text())
        self.assertEqual(failed['state'],'needs_review')
        self.assertEqual(failed['failure_http_status'],500)
        self.assertEqual(failed['failed_phase'],'adding_weekly_items')
        recipes.save_recipe('meal-one','Meal one',[
            {'product_id':test_product_id('milk'),'name':'Whole milk 1L','quantity':1},
            {'product_id':test_product_id('extra'),'name':'Recipe product','quantity':2}],confirmed=True)
        result=weekly.run('weekly ocado with Meal one',['Meal one'])
        self.assertEqual(result['status'],'ready_for_checkout')
        self.assertEqual(self.posts[writes][1],[{'productId':test_product_id('milk'),'quantity':-4},{'productId':test_product_id('juice'),'quantity':-1}])
        self.assertEqual({p:q for p,q in self.cart.items() if q},{test_product_id('milk'):5,test_product_id('juice'):1,test_product_id('extra'):2})
        archive=list((yoto.home()/'ocado-weekly-history').glob('*.json'))
        self.assertEqual(json.loads(archive[0].read_text())['failure_http_status'],500)
        current=json.loads(weekly.run_path('order-next').read_text())
        self.assertEqual(current['recovered_from_state'],'needs_review')
        self.assertNotIn('failure_http_status',current)

    def test_session_opened_then_500_can_recover_without_opening_it_again(self):
        self.fail_after_server_applies('opening')
        self.assertTrue(self.editing)
        result=weekly.run('redo weekly ocado')
        self.assertEqual(result['status'],'ready_for_checkout')
        self.assertEqual(sum(route==edit.START_EDIT for route,_ in self.posts),1)

    def test_abandoned_starting_record_recovers_from_current_placeholder(self):
        yoto.write_private(weekly.run_path('order-next'),{
            'state':'starting','order_id':'order-next','old_baseline':{test_product_id('obsolete'):99}})
        self.editing=True
        self.order['is_under_edit']=True
        self.cart={test_product_id('placeholder'):1}
        result=weekly.run('weekly ocado')
        self.assertEqual(result['status'],'ready_for_checkout')
        self.assertEqual(self.posts[0][1],[{'productId':test_product_id('placeholder'),'quantity':-1}])
        current=json.loads(weekly.run_path('order-next').read_text())
        self.assertEqual(current['recovered_from_state'],'starting')

    def test_fresh_read_failure_or_wrong_order_does_not_write_or_erase_failed_record(self):
        self.fail_after_server_applies('adding')
        path=weekly.run_path('order-next')
        before=path.read_bytes()
        writes=len(self.posts)
        with patch.object(edit,'raw_cart',side_effect=ocado.OcadoError('Ocado returned HTTP 500 during fresh read.')):
            with self.assertRaisesRegex(ocado.OcadoError,'fresh read'):
                weekly.run('weekly ocado')
        self.assertEqual(path.read_bytes(),before)
        self.wrong_order=True
        with self.assertRaisesRegex(ocado.OcadoError,'Another order'):
            weekly.run('weekly ocado')
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(len(self.posts),writes)

    def test_persistent_500_stops_each_request_without_automatic_retry_loop(self):
        self.quantity_failure=500
        with self.assertRaisesRegex(ocado.OcadoError,'clearing_order'):
            weekly.run('weekly ocado')
        self.assertEqual(len(self.posts),2)  # Open session, then a single failed quantity call.
        with self.assertRaisesRegex(ocado.OcadoError,'not a permanent lock'):
            weekly.run('weekly ocado')
        self.assertEqual(len(self.posts),3)  # Existing session, one new attempted clear, no loop.
        self.assertEqual(json.loads(weekly.run_path('order-next').read_text())['state'],'needs_review')
