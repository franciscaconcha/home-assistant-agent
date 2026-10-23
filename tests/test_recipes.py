import json
import os
import tempfile
import unittest
from unittest.mock import patch

from household_tools import recipes
from household_tools.integrations import ocado, ocado_weekly as weekly, yoto


def item(pid='shared', quantity=1):
    return {'product_id':pid,'name':'Confirmed product pack','quantity':quantity}


class RecipeTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        env=patch.dict(os.environ, {'HERMES_HOME':directory.name,'OCADO_WEEKLY_ENABLED':'true'})
        env.start()
        self.addCleanup(env.stop)
        weekly.save_recipe([item('shared',4),item('staple',1)],True)

    def save(self, recipe_id='meal-one', name='Meal one', items=None, **kwargs):
        return recipes.save_recipe(recipe_id,name,items if items is not None else [item()],confirmed=True,**kwargs)

    def test_private_collection_names_aliases_and_backup(self):
        self.assertEqual(recipes.list_recipes(),[])
        self.save(aliases=['Dinner one'],servings=4,notes='One preparation.')
        self.assertEqual(recipes.get_recipe('DINNER ONE')['id'],'meal-one')
        self.assertEqual(recipes.get_recipe('meal-one')['servings'],4)
        path=recipes.recipe_path('meal-one')
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.save(items=[item(quantity=2)])
        self.assertEqual(len(list((yoto.home()/'recipe-backups').glob('meal-one-*.json'))),1)
        self.assertEqual(len(recipes.list_recipes()),1)
        self.assertEqual(recipes.get_recipe('Meal one')['items'][0]['quantity'],2)

    def test_drafts_can_be_saved_but_never_shopped_until_products_resolved(self):
        saved=recipes.save_recipe('meal-one','Meal one',[item(None)],notes='Choose an exact product.')
        self.assertEqual(saved['recipe']['status'],'needs_products')
        self.assertEqual(recipes.list_recipes()[0]['status'],'needs_products')
        with patch.object(ocado,'next_order',side_effect=AssertionError('Must not contact Ocado')):
            with self.assertRaisesRegex(ocado.OcadoError,'still needs exact Ocado products'):
                weekly.run('weekly ocado, with meal one',['Meal one'])
        with self.assertRaisesRegex(ocado.OcadoError,'Confirm'):
            recipes.save_recipe('meal-one','Meal one',[item()])
        self.save()
        self.assertEqual(recipes.shopping_plan(['Meal one'])['items'][0]['quantity'],5)

    def test_plan_sums_staples_and_each_recipe_by_product_id_without_changing_files(self):
        before=weekly.recipe_path().read_bytes()
        self.save(items=[item('shared',2),item('unique',1)],servings=4)
        self.save('meal-two','Meal two',[item('shared',1),item('other',2)],servings=2)
        plan=recipes.shopping_plan(['Meal one','meal-two'])
        self.assertEqual({i['product_id']:i['quantity'] for i in plan['items']},
                         {'shared':7,'staple':1,'unique':1,'other':2})
        line=next(i for i in plan['breakdown'] if i['product_id']=='shared')
        self.assertEqual(line['sources'],[{'source':'weekly staples','quantity':4},
            {'source':'Meal one','quantity':2},{'source':'Meal two','quantity':1}])
        self.assertEqual([r['servings'] for r in plan['recipes']],[4,2])
        self.assertEqual(weekly.recipe_path().read_bytes(),before)
        self.assertEqual(recipes.get_recipe('Meal one')['items'][0]['quantity'],2)

    def test_unselected_recipes_are_not_added_and_distinct_products_are_not_merged(self):
        self.save(items=[item('different-product',1)])
        self.save('meal-two','Meal two',[item('not-selected',1)])
        plan=recipes.shopping_plan(['Meal one'])
        self.assertEqual({i['product_id'] for i in plan['items']},{'shared','staple','different-product'})
        self.assertEqual(recipes.shopping_plan()['items'],weekly.load_recipe()['items'])

    def test_unknown_duplicate_ambiguous_and_invalid_requests_fail_before_ocado(self):
        self.save(aliases=['dinner'])
        with self.assertRaisesRegex(ocado.OcadoError,'ambiguous'):
            self.save('meal-two','Meal two',aliases=['dinner'])
        self.assertEqual(len(recipes.list_recipes()),1)
        for request in [['typo'],['Meal one','dinner'],['Meal one']*21,[None],'Meal one']:
            with patch.object(ocado,'next_order',side_effect=AssertionError('Must not contact Ocado')):
                with self.assertRaises(ocado.OcadoError):
                    weekly.run('weekly ocado',request)
        with patch.object(ocado,'next_order',side_effect=AssertionError('Must not contact Ocado')):
            with self.assertRaisesRegex(ocado.OcadoError,'request names recipes'):
                weekly.run('weekly ocado, with unknown recipe')
            with self.assertRaisesRegex(ocado.OcadoError,'not resolved'):
                weekly.run('weekly ocado, with Meal one and an unknown meal',['Meal one'])
        # Manual alias collisions also fail; they are never resolved by picking a file.
        value=recipes.get_recipe('Meal one')
        value.pop('status')
        value.update(id='meal-two',name='Meal two')
        yoto.write_private(recipes.recipe_path('meal-two'),value)
        with self.assertRaisesRegex(ocado.OcadoError,'ambiguous'):
            recipes.shopping_plan(['dinner'])

    def test_full_phrase_can_use_aliases_commas_and_names_containing_and(self):
        self.save(name='Meal and sides',aliases=['Dinner'])
        self.save('meal-two','Meal two')
        for phrase in ['Meal and sides and Meal two','dinner, meal two','DINNER & MEAL TWO']:
            result=recipes.shopping_plan(['meal-one','Meal two'],phrase)
            self.assertEqual(len(result['recipes']),2)

    def test_invalid_files_ids_and_quantity_overflow_fail_closed(self):
        for rid in ['../outside','Meal one','',None]:
            with self.assertRaises(ocado.OcadoError):
                recipes.recipe_path(rid)
        for bad in [[item(quantity=True)],[item(quantity=0)],[item(),item()],
                    [{'product_id':'p','name':'label','quantity':1,'extra':'wrong'}]]:
            with self.assertRaises(ocado.OcadoError):
                self.save(items=bad)
        self.save(items=[item('shared',47)])
        with self.assertRaisesRegex(ocado.OcadoError,'integers from 1 to 50'):
            recipes.shopping_plan(['Meal one'])
        path=recipes.recipe_path('meal-one')
        path.write_text('not json')
        with self.assertRaisesRegex(ocado.OcadoError,'could not be read'):
            recipes.shopping_plan(['Meal one'])
        # A plain staples-only plan remains available despite unrelated broken recipe data.
        self.assertEqual(len(recipes.shopping_plan()['items']),2)

    def test_manual_edit_is_loaded_without_restart(self):
        self.save()
        path=recipes.recipe_path('meal-one')
        value=json.loads(path.read_text())
        value['items'][0]['quantity']=3
        path.write_text(json.dumps(value))
        self.assertEqual(recipes.shopping_plan(['Meal one'])['items'][0]['quantity'],7)
        value['id']='wrong-id'
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ocado.OcadoError,'match its JSON filename'):
            recipes.shopping_plan(['Meal one'])
