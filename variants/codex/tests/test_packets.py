import copy
import tempfile
import unittest
from pathlib import Path
from cqa import conversation as cv, checklist as ck, manual, sources
from cqa.common import load_json, save_json
from variants.codex.packets import CONFIG, transform, render, prepare_run, ingest, validate_response


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.cfg=load_json(CONFIG);self.conv=cv.load('dev','p1-ru-012');self.doc=ck.load('default@1')

    def fixture(self,conv,ids):
        t=conv['transcript'][0]
        return {'criteria':[{'criterion_id':c['id'],
            'answer':max(s['value'] for s in c['scale']) if c['type']=='scale' else 'yes',
            'evidence':[{'idx':t['idx'],'quote':t['text']}], 'explanation':'Synthetic test fixture.'}
            for c in self.doc['criteria'] if c['id'] in ids]}

    def test_mutations_preserve_ids_and_roles(self):
        for attack in ['control','injection','neutral_swap','typo']:
            out,prov=transform(self.conv,attack,self.cfg)
            self.assertEqual([t['idx'] for t in out['transcript']],list(range(1,len(out['transcript'])+1)))
            self.assertEqual([t['speaker'] for t in out['transcript']],[t['speaker'] for t in self.conv['transcript']])
            self.assertEqual([t for t in out['transcript'] if t['speaker']=='operator'],
                             [t for t in self.conv['transcript'] if t['speaker']=='operator'])
        self.assertEqual(self.conv,transform(self.conv,'control',self.cfg)[0])

    def test_renderer_dynamic_and_no_gold(self):
        c=dict(next(c for c in self.doc['criteria'] if c['type']=='scale'),id='unseen')
        text=render(self.doc,self.conv,[c],focus=True)
        self.assertNotIn('{{',text)
        self.assertIn('unseen',text)
        self.assertNotIn('dev-gold',text)
        self.assertNotIn('R4',text)

    def test_strict_quotes_and_scales(self):
        c=next(c for c in self.doc['criteria'] if c['type']=='scale')
        d=self.fixture(self.conv,[c['id']]);d['criteria'][0]['answer']=12
        with self.assertRaises(ValueError):validate_response(self.conv,self.doc,d,[c['id']])
        d=self.fixture(self.conv,[c['id']]);d['criteria'][0]['evidence'][0]['quote']='fake'
        with self.assertRaises(ValueError):validate_response(self.conv,self.doc,d,[c['id']])

    def test_preparation_manual_and_full_merge(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'review'
            m=prepare_run(out,self.cfg,'recheck')
            self.assertLessEqual(len(m['conversations']),10)
            self.assertTrue(m['preview_only'])
            self.assertEqual(manual.status(str(out))['agents']['claude']['done'],0)
            for cid,info in m['conversations'].items():
                part=info['parts'][0];conv=load_json(out/'conversations'/(cid+'.json'))
                save_json(out/'agents'/'claude'/'responses'/(Path(part['file']).stem+'.json'),
                          self.fixture(conv,part['criteria_ids']))
            report=ingest(out)
            self.assertEqual(report['errors'],0)
            self.assertEqual(report['completed_valid_dialogs'],len(m['conversations']))
            _,results=sources.load_results(str(out/'validated'/'claude'))
            for r in results.values():
                self.assertEqual(len(r['answers']),8)
                self.assertIn('checklist',r);self.assertIn('usage',r)
                self.assertGreater(len(r['usage']),len(r['incremental_usage']))
            from cqa import metrics
            compared=metrics.compare('dev-gold@v1',[str(out/'validated'/'claude')],save=False)
            self.assertEqual(compared['sources'][0]['coverage']['predicted'],len(results))
            with self.assertRaises(ValueError):prepare_run(out,self.cfg,'recheck')

    def test_robustness_waits_for_same_template_control(self):
        with tempfile.TemporaryDirectory() as td:
            cfg=copy.deepcopy(self.cfg);cfg['robustness_ids']=['p1-ru-012'];cfg['control_run']=str(Path(td)/'control')
            control=prepare_run(Path(cfg['control_run']),cfg,'robustness','control')
            attack_path=Path(td)/'attack';m=prepare_run(attack_path,cfg,'robustness','injection')
            for run,man in [(attack_path,m)]:
                for cid,info in man['conversations'].items():
                    conv=load_json(run/'conversations'/(cid+'.json'));part=info['parts'][0]
                    save_json(run/'agents'/'claude'/'responses'/(Path(part['file']).stem+'.json'),self.fixture(conv,part['criteria_ids']))
            a=ingest(attack_path)
            self.assertEqual(a['pending_control'],1);self.assertIsNone(a['changed_answer_share'])
            run=Path(cfg['control_run'])
            for cid,info in control['conversations'].items():
                conv=load_json(run/'conversations'/(cid+'.json'));part=info['parts'][0]
                save_json(run/'agents'/'claude'/'responses'/(Path(part['file']).stem+'.json'),self.fixture(conv,part['criteria_ids']))
            ingest(run);a=ingest(attack_path)
            self.assertEqual(a['comparable_pairs'],1)
            self.assertEqual(a['changed_answer_share'],0.)


if __name__=='__main__':unittest.main()
