import copy
import json
import tempfile
import unittest
from pathlib import Path
from cqa import checklist as ck, conversation as cv
from cqa.common import load_json, save_json
from variants.codex.pipeline import HERE, prepare, strict_validate, route, ingest, make_routes, report


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_json(HERE / 'config.json')
        self.cfg['agents'] = ['a', 'b', 'c']
        self.doc = ck.load('default@1')
        self.conv = cv.load('dev', 'p1-en-001')

    def answer(self, doc=None):
        # Synthetic test fixture: deliberately uniform, never a DEV evaluation.
        doc = doc or self.doc
        t = self.conv['transcript'][0]
        return {'criteria': [{'criterion_id': c['id'],
            'answer': max(s['value'] for s in c['scale']) if c['type'] == 'scale' else 'yes',
            'explanation': 'synthetic test fixture', 'evidence': [{'idx': t['idx'], 'quote': t['text']}]}
            for c in ck.active_criteria(doc)]}

    def test_strict_scale_and_quote(self):
        data = self.answer()
        scale = next(c for c in self.doc['criteria'] if c['type'] == 'scale')
        item = next(x for x in data['criteria'] if x['criterion_id'] == scale['id'])
        item['answer'] = 12
        item['evidence'][0]['quote'] = 'invented quote'
        v = strict_validate(self.conv, self.doc, data, [c['id'] for c in self.doc['criteria']])
        self.assertNotIn(scale['id'], v['answers'])
        self.assertEqual(v['status'], 'error')

    def test_dynamic_checklist(self):
        doc = copy.deepcopy(self.doc)
        doc['criteria'] = [dict(doc['criteria'][0], id='new-unseen-id')]
        v = strict_validate(self.conv, doc, self.answer(doc), ['new-unseen-id'])
        self.assertEqual(v['answers'], {'new-unseen-id': 'yes'})
        self.assertEqual(route(doc, [v], self.cfg), {})

    def test_missing_and_disagreement_route(self):
        ids = [c['id'] for c in self.doc['criteria']]
        a = strict_validate(self.conv, self.doc, self.answer(), ids)
        b = copy.deepcopy(a)
        b['answers'][ids[0]] = 'no'
        self.assertIn('disagreement', route(self.doc, [a,b], self.cfg)[ids[0]])

    def test_manual_roundtrip_and_immutable_initial(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'run'
            prepare(out, 'review', ['p1-en-001'], self.cfg)
            self.assertEqual(ingest(out), {'pending': 3})
            with self.assertRaises(ValueError):
                make_routes(out)
            for agent in self.cfg['agents']:
                data = self.answer()
                data['criteria'][0]['ambiguities'] = ['synthetic ambiguity']
                save_json(out / 'agents' / agent / 'responses' / 'p1-en-001__all.json', data)
            self.assertEqual(ingest(out), {'ok': 3})
            r = make_routes(out)
            self.assertEqual(r['routed_dialogs'], 1)
            routing = load_json(out / 'routing.json')
            scope = routing['p1-en-001']['part']['criteria_ids']
            for agent in self.cfg['agents']:
                review = self.answer()
                review['criteria'] = [x for x in review['criteria'] if x['criterion_id'] in scope]
                review['criteria'][0].update(answer='no', checked_range=[1, 1])
                save_json(out / 'agents' / agent / 'responses' / 'p1-en-001__review.json', review)
            ingest(out)
            v = load_json(out / 'validated' / 'a' / 'p1-en-001.json')
            self.assertEqual(v['answers'][scope[0]], 'yes')
            self.assertEqual(v['review_proposal']['answers'][scope[0]], 'no')
            self.assertEqual(len(v['usage']), 2)
            self.assertEqual(report(out)['a']['calls_received'], 2)
            with self.assertRaises(ValueError):
                prepare(out, 'review', ['p1-en-001'], self.cfg)
            with self.assertRaises(ValueError):
                make_routes(out)

    def test_sections_merge_and_bad_json(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'run'
            m = prepare(out, 'sections', ['p1-en-001'], self.cfg)
            for agent in self.cfg['agents']:
                for part in m['conversations']['p1-en-001']['parts']:
                    data = self.answer()
                    data['criteria'] = [x for x in data['criteria'] if x['criterion_id'] in part['criteria_ids']]
                    save_json(out / 'agents' / agent / 'responses' / (part['name'] + '.json'), data)
            self.assertEqual(ingest(out), {'ok': 3})
            v = load_json(out / 'validated' / 'a' / 'p1-en-001.json')
            self.assertTrue(v['score']['complete'])
            path = out / 'agents' / 'a' / 'responses' / (m['conversations']['p1-en-001']['parts'][0]['name'] + '.json')
            path.write_text('{bad')
            ingest(out)
            v = load_json(out / 'validated' / 'a' / 'p1-en-001.json')
            self.assertEqual(v['status'], 'error')
            self.assertIsNone(v['score']['total'])


if __name__ == '__main__':
    unittest.main()
