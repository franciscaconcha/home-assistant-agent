import json
import unittest
from unittest.mock import patch

import httpx

from household_tools import occasional, recipes
from household_tools.integrations import ocado,ocado_edit as edit,ocado_weekly as weekly,yoto
from ocado_test_ids import product_id
import test_ocado_weekly as weekly_tests


def option(label, **kwargs):
    return {'product_id':product_id(label),'name':label,**kwargs}


def quote(current,original=None,available=True):
    return {'available':available,'current':current,'original':original,'currency':'GBP'}


class OccasionalTests(unittest.TestCase):
    handler=weekly_tests.WeeklyTests.handler

    def setUp(self):
        weekly_tests.WeeklyTests.setUp(self)

    def save(self,item_id='extra-item',name='Extra item',products=None,**kwargs):
        return occasional.save_item(item_id,name,products or [option('extra')],confirmed=True,**kwargs)

    def test_private_save_alias_lookup_backup_and_manual_edits(self):
        self.assertEqual(occasional.list_items(),[])
        self.save(aliases=['extra'])
        self.assertEqual(occasional.get_item('EXTRA')['id'],'extra-item')
        self.assertEqual(occasional.path().stat().st_mode & 0o777,0o600)
        self.save(quantity=2)
        self.assertEqual(len(list((yoto.home()/'occasional-backups').glob('*.json'))),1)
        value=json.loads(occasional.path().read_text())
        value['items'][0]['quantity']=3
        occasional.path().write_text(json.dumps(value))
        self.assertEqual(recipes.shopping_plan(extras=['Extra item'])['extras'][0]['product']['quantity'],3)
        self.assertEqual(self.posts,[])

    def test_comma_request_adds_only_requested_extra_and_can_mix_recipes_in_either_order(self):
        self.save(aliases=['extra'])
        self.save('unused-item','Unused item',[option('unused')])
        result=weekly.run('weekly ocado, extra',extras=['extra'])
        self.assertEqual(result['status'],'ready_for_checkout')
        self.assertEqual(self.cart[product_id('extra')],1)
        self.assertNotIn(product_id('unused'),self.cart)
        recipes.save_recipe('meal-one','Meal one',[{'product_id':product_id('ingredient'),'name':'Ingredient','quantity':2}],confirmed=True)
        result=weekly.run('weekly ocado, extra and Meal one',['Meal one'],extras=['extra'])
        self.assertEqual(self.cart[product_id('ingredient')],2)
        self.assertEqual(result['extras'][0]['requested_name'],'extra')
        result=weekly.run('weekly ocado')
        self.assertEqual(result['extras'],[])
        self.assertEqual(self.cart.get(product_id('extra'),0),0)
        self.assertEqual(self.cart.get(product_id('ingredient'),0),0)

    def test_shared_product_quantities_sum_across_staples_recipes_and_extras(self):
        self.save(products=[option('milk')],quantity=2)
        recipes.save_recipe('meal-one','Meal one',[{'product_id':product_id('milk'),'name':'Milk','quantity':1}],confirmed=True)
        plan=recipes.shopping_plan(['Meal one'],extras=['Extra item'])
        milk=next(item for item in plan['breakdown'] if item['product_id']==product_id('milk'))
        self.assertEqual(milk['quantity'],7)
        self.assertEqual([x['quantity'] for x in milk['sources']],[4,1,2])

    def test_unknown_unmapped_duplicate_and_omitted_extras_stop_before_order_read(self):
        occasional.save_item('draft','Draft',[{'product_id':None,'name':'Choose a product'}])
        self.save(aliases=['extra'])
        for trigger,extras in [('weekly ocado, unknown',['unknown']),('weekly ocado, Draft',['Draft']),
                               ('weekly ocado, extra and Extra item',['extra','Extra item']),
                               ('weekly ocado, extra and unknown',['extra']),('weekly ocado, extra',None)]:
            with self.subTest(trigger=trigger), self.assertRaises(ocado.OcadoError):
                weekly.run(trigger,extras=extras)
        self.reader.assert_not_called()
        self.assertEqual(self.posts,[])

    def test_lowest_price_compares_bottle_prices_not_value_per_wash(self):
        self.save(products=[option('small'),option('large')],selection='lowest_price')
        with patch.object(occasional,'price_quotes',return_value={product_id('small'):quote('3.00'),product_id('large'):quote('6.00')}):
            plan=recipes.shopping_plan(extras=['Extra item'])
        selected=plan['extras'][0]
        self.assertEqual(selected['product']['product_id'],product_id('small'))
        self.assertEqual(selected['price'],'3.00')

    def test_discount_rule_filters_then_chooses_lowest_qualifying_pack_price(self):
        self.save(products=[option('discount-a'),option('discount-b'),option('not-half-price')],selection='discount',minimum_discount_percent=45)
        quotes={product_id('discount-a'):quote('5.50','10.00'),product_id('discount-b'):quote('7.00','14.00'),
                product_id('not-half-price'):quote('4.00','5.00')}
        with patch.object(occasional,'price_quotes',return_value=quotes):
            selected=recipes.shopping_plan(extras=['Extra item'])['extras'][0]
        self.assertEqual(selected['product']['product_id'],product_id('discount-a'))
        self.assertEqual(selected['discount_percent'],'45.00')

    def test_unavailable_option_can_be_replaced_only_by_another_saved_eligible_option(self):
        self.save(products=[option('small'),option('large')],selection='lowest_price')
        with patch.object(occasional,'price_quotes',return_value={product_id('small'):quote('3.00',available=False),product_id('large'):quote('6.00')}):
            selected=recipes.shopping_plan(extras=['Extra item'])['extras'][0]
        self.assertEqual(selected['product']['product_id'],product_id('large'))

    def test_blocked_unknown_price_or_no_qualifying_offer_stops_before_any_order_change(self):
        self.save(products=[option('extra')],selection='discount')
        for data in [None,{product_id('extra'):quote('9.00','10.00')},{product_id('extra'):quote(None,'10.00')},
                     {product_id('extra'):quote('5.00',available=False)}]:
            kwargs={'side_effect':ocado.OcadoError('HTTP 202 price challenge')} if data is None else {'return_value':data}
            with self.subTest(data=data),patch.object(occasional,'price_quotes',**kwargs),self.assertRaises(ocado.OcadoError):
                weekly.run('weekly ocado, Extra item',extras=['Extra item'])
        self.reader.assert_not_called()
        self.assertEqual(self.posts,[])

    def test_named_flavour_uses_saved_brand_pack_and_does_not_rotate_or_price_shop(self):
        self.save('spread','Spread',[option('red',aliases=['red spread']),option('purple',aliases=['purple spread'])],selection='named')
        with patch.object(occasional,'price_quotes',side_effect=AssertionError('No price lookup needed')):
            plan=recipes.shopping_plan(extras=['red spread'])
        self.assertEqual(plan['extras'][0]['product']['product_id'],product_id('red'))
        self.assertEqual(plan['extras'][0]['requested_name'],'red spread')
        with self.assertRaisesRegex(ocado.OcadoError,'Specify the flavour'):
            recipes.shopping_plan(extras=['Spread'])
        plan=recipes.shopping_plan(extras=['red spread','purple spread'])
        self.assertEqual(len(plan['extras']),2)

    def test_mapped_named_variant_works_even_when_another_variant_is_still_a_draft(self):
        occasional.save_item('spread','Spread',[option('red',aliases=['red spread']),
            {'product_id':None,'name':'Choose purple product','aliases':['purple spread']}],selection='named')
        self.assertEqual(occasional.get_item('red spread')['status'],'ready')
        self.assertEqual(occasional.list_items()[0]['status'],'partially_ready')
        self.assertEqual(recipes.shopping_plan(extras=['red spread'])['extras'][0]['product']['product_id'],product_id('red'))
        with self.assertRaises(ocado.OcadoError):
            recipes.shopping_plan(extras=['purple spread'])

    def test_optional_redo_preview_preserves_selected_named_variant(self):
        weekly.run('weekly ocado')
        self.save('spread','Spread',[option('red',aliases=['red spread']),option('purple',aliases=['purple spread'])],selection='named')
        plan=weekly.prepare_redo(extras=['purple spread'])
        result=weekly.apply_redo(plan['plan_id'],True)
        self.assertEqual(result['extras'][0]['product']['product_id'],product_id('purple'))
        self.assertEqual(self.cart[product_id('purple')],1)
        self.assertNotIn(product_id('red'),self.cart)

    def test_conflicting_aliases_and_invalid_numeric_ids_do_not_replace_existing_data(self):
        self.save(aliases=['extra'])
        before=occasional.path().read_bytes()
        for kwargs in [{'item_id':'another','name':'Another','products':[option('another')],'aliases':['extra']},
                       {'products':[{'product_id':'12345678','name':'Not a UUID'}]},
                       {'products':[option('a'),option('b')],'selection':'fixed'}]:
            with self.assertRaises(ocado.OcadoError):self.save(**kwargs)
        self.assertEqual(occasional.path().read_bytes(),before)

    def test_price_lookup_is_bounded_read_only_and_uses_primary_price_shapes(self):
        requests=[]
        def handler(request):
            requests.append((request.method,request.url.path))
            if request.url.path=='/orders':return httpx.Response(200,text='{"csrf":{"token":"PRIVATE_TOKEN"}}')
            self.assertEqual(request.method,'PUT')
            self.assertEqual(request.url.path,ocado.PRODUCTS)
            self.assertEqual(json.loads(request.content),[product_id('priced')])
            return httpx.Response(200,json={'products':[{'productId':product_id('priced'),'available':True,
                'price':{'current':{'amount':'5.00','currency':'GBP'},'original':{'amount':'10.00','currency':'GBP'}}}]})
        original=httpx.Client
        with patch.object(edit,'client_for',side_effect=lambda saved:original(transport=httpx.MockTransport(handler))):
            result=occasional.price_quotes([product_id('priced')])
        self.assertEqual(result[product_id('priced')],quote('5.00','10.00'))
        self.assertEqual(requests,[('GET','/orders'),('PUT',ocado.PRODUCTS)])
        self.assertNotIn('PRIVATE_TOKEN',repr(result))
        with httpx.Client(transport=httpx.MockTransport(lambda request:self.fail('Unexpected request'))) as client:
            for ids in [[],['12345678'],[product_id(str(i)) for i in range(101)]]:
                with self.assertRaises(ocado.OcadoError):ocado.read_request(client,'PUT',ocado.PRODUCTS,json=ids)
