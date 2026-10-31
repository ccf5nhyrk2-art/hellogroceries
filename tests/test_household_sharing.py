"""Run with: python -m unittest discover -s tests -p 'test_household_sharing.py'"""
import os
import sys
import unittest
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..','app'))
from household_sharing import merge_state

class HouseholdMergeTests(unittest.TestCase):
    def test_preserves_distinct_shopping_items(self):
        merged=merge_state({'list':[{'name':'Milk','quantity':1}],'revision':3},{'list':[{'name':'Milk','quantity':2},{'name':'Eggs','quantity':12}],'revision':2})
        self.assertEqual(len(merged['list']),3)
        self.assertEqual(merged['revision'],4)
    def test_keeps_both_pantry_items(self):
        merged=merge_state({'pantry_inventory':{'milk':2}},{'pantry_inventory':{'eggs':6}})
        self.assertEqual(merged['pantry_inventory'],{'milk':2,'eggs':6})
    def test_conflict_blocks_merge(self):
        with self.assertRaises(ValueError):
            merge_state({'pantry_inventory':{'milk':2}},{'pantry_inventory':{'milk':8}})
    def test_preserves_unknown_keys(self):
        merged=merge_state({'selected':{'1':2},'revision':1},{'selected':{'2':4},'custom':{'notes':'keep'}})
        self.assertEqual(merged['custom']['notes'],'keep')
        self.assertEqual(merged['selected'],{'1':2,'2':4})
if __name__=='__main__':unittest.main()
