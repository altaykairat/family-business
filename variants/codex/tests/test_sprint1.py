import copy
import unittest
from variants.codex.sprint1 import curve, recall_at, extract, build_rows, peer_index, fixed_policy
from cqa import conversation as cv
from cqa.common import load_json
from variants.codex.sprint1 import DEFAULT_CONFIG


class RoutingTests(unittest.TestCase):
    def row(self, score, errors, available=True):
        return {'signals':{'s':score},'availability':{'s':available},'error_count':errors}

    def test_ties_not_sorted_by_gold(self):
        c=curve([self.row(1,3),self.row(1,0),self.row(1,1)],'s')
        self.assertEqual(len(c['points']),2)
        self.assertEqual(c['auc'],.5)
        self.assertEqual(recall_at(c,.5),.5)

    def test_no_errors_and_missing_signal(self):
        c=curve([self.row(1,0),self.row(0,9,False)],'s')
        self.assertEqual(c['n'],1)
        self.assertIsNone(c['auc'])
        self.assertIsNone(recall_at(c,.5))

    def test_signal_independent_of_gold(self):
        cfg=load_json(DEFAULT_CONFIG)
        conv=cv.load('dev','p1-ru-012')
        r={'conversation_id':'p1-ru-012','criteria':{'x':{'answer':'yes','confidence':.4,'ambiguities':[]}},'answers':{'x':'yes'}}
        catalog={'runs/demo/validated/model':{'manifest':{'dataset':'dev'},'rows':[r]}}
        g={'split':{'tune':['p1-ru-012']},'items':{'p1-ru-012':{'labels':{'x':{'answer':'yes'}}}}}
        a=build_rows(catalog,g,cfg)[0]
        g['items']['p1-ru-012']['labels']['x']['answer']='no'
        b=build_rows(catalog,g,cfg)[0]
        self.assertEqual(a['signals'],b['signals'])
        self.assertNotEqual(a['error_count'],b['error_count'])
        self.assertFalse(a['availability']['insufficient_data'])

    def test_same_model_repeats_do_not_make_panel(self):
        cfg=load_json(DEFAULT_CONFIG);conv=cv.load('dev','p1-ru-012')
        a={'criteria':{'x':{'answer':'yes'}}};b={'criteria':{'x':{'answer':'no'}}}
        x=extract('s',a,[('m',a),('m',b)],conv,cfg)
        self.assertFalse(x['availability']['model_disagreement'])
        y=extract('s',a,[('m',a),('n',b)],conv,cfg)
        self.assertEqual(y['signals']['model_disagreement'],1)

    def test_fixed_policy_does_not_select_on_test(self):
        summary={'best_common_cohort_signal_selected_on_tune':'s',
                 'config':{'routing_budget_fraction':.5},
                 'common_cohort_curves':{'tune':{'s':curve([self.row(2,1),self.row(1,1)],'s')}}}
        rows=[dict(self.row(2,0),split='test')]
        a=fixed_policy(summary,rows)
        rows[0]['error_count']=8
        b=fixed_policy(summary,rows)
        self.assertEqual(a['threshold'],b['threshold'])


if __name__=='__main__':unittest.main()
