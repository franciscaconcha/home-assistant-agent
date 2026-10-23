import json
import time
import unittest
from unittest.mock import patch

from household_tools.integrations import ocado, ocado_weekly as weekly, yoto
from household_tools import recipes
import test_ocado_weekly as weekly_tests


class WeeklyRedoTests(unittest.TestCase):
    handler = weekly_tests.WeeklyTests.handler

    def setUp(self):
        weekly_tests.WeeklyTests.setUp(self)
        weekly.run('weekly ocado')
        self.initial_writes=len(self.posts)
        recipes.save_recipe('meal-one','Meal one',[
            {'product_id':'extra','name':'Recipe product','quantity':2}],confirmed=True)

    def preview(self):
        return weekly.prepare_redo(['Meal one'])

    def test_preview_then_confirmed_redo_reuses_correct_edit_trolley_and_archives_run(self):
        self.cart['later-addition']=1
        preview=self.preview()
        self.assertEqual(len(self.posts),self.initial_writes)
        removed=next(c for c in preview['changes'] if c['product_id']=='later-addition')
        self.assertEqual((removed['before'],removed['after']),(1,0))
        result=weekly.apply_redo(preview['plan_id'],True)
        self.assertEqual(result['status'],'ready_for_checkout')
        self.assertEqual({p:q for p,q in self.cart.items() if q},{'milk':4,'juice':1,'extra':2})
        # Only quantity writes were needed; the existing edit session was not reopened.
        self.assertTrue(all('/apply-quantity' in route for route,_ in self.posts[self.initial_writes:]))
        archive=list((yoto.home()/'ocado-weekly-history').glob('*.json'))
        self.assertEqual(len(archive),1)
        self.assertEqual(archive[0].stat().st_mode & 0o777,0o600)
        self.assertEqual(json.loads(weekly.redo_path(preview['plan_id']).read_text())['state'],'applied')
        writes=len(self.posts)
        with self.assertRaisesRegex(ocado.OcadoError,'already attempted'):
            weekly.apply_redo(preview['plan_id'],True)
        self.assertEqual(len(self.posts),writes)
        self.assertEqual(weekly.run('weekly ocado')['status'],'ready_for_checkout')
        self.assertGreater(len(self.posts),writes)
        self.assertEqual({p:q for p,q in self.cart.items() if q},{'milk':4,'juice':1})

    def test_unconfirmed_expired_and_stale_cart_preview_do_not_write(self):
        preview=self.preview()
        with self.assertRaisesRegex(ocado.OcadoError,'explicit user confirmation'):
            weekly.apply_redo(preview['plan_id'])
        self.cart['milk']=5
        with self.assertRaisesRegex(ocado.OcadoError,'trolley or editing session changed'):
            weekly.apply_redo(preview['plan_id'],True)
        path=weekly.redo_path(preview['plan_id'])
        plan=json.loads(path.read_text())
        plan['expires_at']=time.time()-1
        yoto.write_private(path,plan)
        with self.assertRaisesRegex(ocado.OcadoError,'expired'):
            weekly.apply_redo(preview['plan_id'],True)
        self.assertEqual(len(self.posts),self.initial_writes)

    def test_changed_recipes_order_and_previous_run_require_fresh_confirmation(self):
        preview=self.preview()
        recipes.save_recipe('meal-one','Meal one',[
            {'product_id':'extra','name':'Recipe product','quantity':3}],confirmed=True)
        with self.assertRaisesRegex(ocado.OcadoError,'changed since the redo preview'):
            weekly.apply_redo(preview['plan_id'],True)
        preview=self.preview()
        self.order['total']='999.00'
        with self.assertRaisesRegex(ocado.OcadoError,'changed since the redo preview'):
            weekly.apply_redo(preview['plan_id'],True)
        preview=self.preview()
        path=weekly.run_path('order-next')
        previous=json.loads(path.read_text())
        previous['prepared_at']+=1
        yoto.write_private(path,previous)
        with self.assertRaisesRegex(ocado.OcadoError,'changed since the redo preview'):
            weekly.apply_redo(preview['plan_id'],True)
        self.assertEqual(len(self.posts),self.initial_writes)

    def test_wrong_order_or_uncertain_previous_run_cannot_be_redone(self):
        self.wrong_order=True
        with self.assertRaisesRegex(ocado.OcadoError,'Another order'):
            self.preview()
        self.wrong_order=False
        path=weekly.run_path('order-next')
        previous=json.loads(path.read_text())
        previous['state']='needs_review'
        yoto.write_private(path,previous)
        with self.assertRaisesRegex(ocado.OcadoError,'manual review'):
            self.preview()
        self.assertEqual(len(self.posts),self.initial_writes)

    def test_redo_can_open_a_fresh_edit_session_after_prior_order_was_saved(self):
        self.editing=False
        # Saved order details and next editing trolley now reflect the successful preparation.
        self.order['items']=[{'product_id':'milk','name':'Milk','quantity':4},
                             {'product_id':'juice','name':'Juice','quantity':1}]
        preview=self.preview()
        weekly.apply_redo(preview['plan_id'],True)
        self.assertEqual(self.posts[self.initial_writes][0],weekly.edit.START_EDIT)
        self.assertEqual(self.cart['extra'],2)

    def test_uncertain_redo_stops_and_locks_plan_and_run_against_replay(self):
        preview=self.preview()
        self.quantity_failure='timeout'
        with self.assertRaisesRegex(ocado.OcadoError,'partially changed'):
            weekly.apply_redo(preview['plan_id'],True)
        writes=len(self.posts)
        with self.assertRaises(ocado.OcadoError):
            weekly.apply_redo(preview['plan_id'],True)
        with self.assertRaisesRegex(ocado.OcadoError,'manual review'):
            self.preview()
        self.assertEqual(len(self.posts),writes)
        self.assertEqual(json.loads(weekly.run_path('order-next').read_text())['state'],'needs_review')
