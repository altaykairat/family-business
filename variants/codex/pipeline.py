"""File-only experiments. No model calls and no semantic evaluation in this module."""
from __future__ import annotations

import argparse
import copy
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from cqa import checklist as ck, conversation as cv, prompts, responses, scoring, tokens
from cqa.common import load_json, save_json, stable_hash, text_hash

HERE = Path(__file__).resolve().parent


def write_new(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as f:
        f.write(text)


def event(out, kind, payload):
    with (Path(out) / 'events.jsonl').open('a', encoding='utf-8') as f:
        f.write(json.dumps({'at': datetime.now(timezone.utc).isoformat(),
                            'type': kind, **payload}, ensure_ascii=False) + '\n')


def features(conv, cfg):
    out = cv.facts(conv, cfg['gap_threshold_s'])
    out['lexical_hints'] = {
        name: [{'idx': t['idx'], 'quote': t['text']} for t in conv['transcript']
               if t['speaker'] == spec['speaker'] and
               any(re.search(p, t['text'], re.I) for p in spec['patterns'])]
        for name, spec in cfg['lexicons'].items()}
    out['warning'] = 'Совпадение словаря — только подсказка, отсутствие совпадения не доказывает отсутствие действия.'
    return out


def strict_validate(conv, doc, data, ids):
    """Keep base format, but never silently round a scale answer or accept invented quotes."""
    if not isinstance(data, dict) or not isinstance(data.get('criteria'), list):
        raise ValueError('Ответ должен быть объектом с массивом criteria')
    if any(not isinstance(x, dict) for x in data['criteria']):
        raise ValueError('criteria должен содержать объекты')
    if any(not isinstance(x.get('evidence', []), list) or
           any(not isinstance(e, dict) or not isinstance(e.get('idx'), int) or
               isinstance(e.get('idx'), bool) or not isinstance(e.get('quote'), str)
               for e in x.get('evidence', [])) for x in data['criteria']):
        raise ValueError('Неверный формат evidence')
    result = responses.validate(conv, doc, data, expected_ids=ids)
    turns = {t['idx']: t['text'] for t in conv['transcript']}
    crit = {c['id']: c for c in ck.active_criteria(doc)}
    seen = set()
    for item in data['criteria']:
        cid = item.get('criterion_id')
        if cid not in ids:
            result['issues'].append({'level': 'error', 'criterion_id': cid, 'msg': 'unexpected criterion'})
            continue
        problems = []
        if cid in seen:
            problems.append('duplicate criterion')
        seen.add(cid)
        ans = item.get('answer')
        allowed = [s['value'] for s in crit[cid]['scale']] if crit[cid]['type'] == 'scale' else ['yes', 'no']
        if crit[cid].get('na_allowed'):
            allowed += ['na']
        if isinstance(ans, bool) or ans not in allowed:
            problems.append('answer must be an exact allowed value')
        for ev in item.get('evidence', []):
            if not ev.get('quote') or ev['quote'] not in turns.get(ev.get('idx'), ''):
                problems.append('quote must be a verbatim substring of an existing turn')
        if not item.get('explanation') or not item.get('evidence'):
            problems.append('explanation and evidence required, including N/A')
        if ans == 'no' or (isinstance(ans, (int, float)) and crit[cid]['type'] == 'scale'
                           and ans < max(s['value'] for s in crit[cid]['scale'])):
            span = item.get('checked_range')
            if not (isinstance(span, list) and len(span) == 2 and
                    all(isinstance(i, int) and not isinstance(i, bool) and i in turns for i in span)
                    and span[0] <= span[1]):
                problems.append('missing or invalid checked_range')
        for msg in problems:
            result['issues'].append({'level': 'error', 'criterion_id': cid, 'msg': msg})
        if problems:
            result['answers'].pop(cid, None)
    result['status'] = 'error' if any(i['level'] == 'error' for i in result['issues']) else ('warn' if result['issues'] else 'ok')
    return result


def route(doc, validations, cfg):
    """Criteria selected across agents; identical scope for every reviewer model."""
    reasons = {}
    for c in ck.active_criteria(doc):
        cid = c['id']
        why = set()
        vals = []
        for v in validations:
            if cid not in v['answers']:
                why.add('invalid_or_missing')
            else:
                vals.append(v['answers'][cid])
            item = v['criteria'].get(cid, {})
            if cfg['review_na'] and v['answers'].get(cid) == 'na':
                why.add('na')
            if cfg['review_ambiguities'] and item.get('ambiguities'):
                why.add('ambiguity')
            confidence = item.get('confidence')
            if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and confidence < cfg['min_confidence']:
                why.add('self_confidence_uncalibrated')
            if any(i.get('criterion_id') == cid for i in v['issues']):
                why.add('validation_issue')
        if cfg['review_on_disagreement'] and len(set(vals)) > 1:
            why.add('disagreement')
        if why:
            reasons[cid] = sorted(why)
    return reasons


def prepare(out, variant, ids, cfg, checklist='default@1', dataset='dev'):
    out = Path(out)
    if out.exists():
        raise ValueError('Папка прогона уже существует; выберите новую')
    doc = ck.load(checklist)
    selected = ids or cv.list_ids(dataset)
    if not selected or set(selected) - set(cv.list_ids(dataset)):
        raise ValueError('Пустая выборка или неизвестные ID')
    m = {'variant': variant, 'dataset': dataset, 'agents': cfg['agents'], 'config': cfg,
         'checklist': ck.ref_of(doc), 'checklist_hash': stable_hash(doc),
         'token_method': tokens.METHOD, 'conversations': {}, 'projection': {},
         'template_hash': text_hash((prompts.TEMPLATES / 'v1.md').read_text(encoding='utf-8')),
         'source_hashes': {p: text_hash((HERE.parents[1] / p).read_text(encoding='utf-8'))
             for p in ['cqa/scoring.py', 'cqa/responses.py', 'cqa/conversation.py',
                       'cqa/prompts.py', 'cqa/tokens.py', 'variants/codex/pipeline.py']}}
    save_json(out / 'checklist.json', doc)
    for cid in selected:
        conv = cv.load(dataset, cid)
        f = features(conv, cfg)
        save_json(out / 'conversations' / f'{cid}.json', conv)
        save_json(out / 'features' / f'{cid}.json', f)
        baseline = prompts.build('v1', doc, conv, f, 'v1')
        built = prompts.build('v1', doc, conv, f, 'v2' if variant == 'sections' else 'v1')
        if variant == 'hints':
            for b in built:
                b['text'] += '\n\n## Лексические подсказки (не решения)\n' + json.dumps(f['lexical_hints'], ensure_ascii=False) + '\n' + f['warning']
        entries = []
        for b in built:
            name = cid + '__' + b['part']
            entry = {'name': name, 'criteria_ids': b['criteria_ids'], 'tokens_in': tokens.count(b['text']),
                     'prompt_hash': text_hash(b['text']), 'stage': 'initial'}
            entries.append(entry)
            for agent in cfg['agents']:
                write_new(out / 'agents' / agent / 'prompts' / f'{name}.md', b['text'])
                (out / 'agents' / agent / 'responses').mkdir(parents=True, exist_ok=True)
        m['conversations'][cid] = {'parts': entries, 'baseline_tokens_in': sum(tokens.count(b['text']) for b in baseline)}
    ncriteria = len(ck.active_criteria(doc))
    base_in = sum(x['baseline_tokens_in'] for x in m['conversations'].values())
    initial_in = sum(p['tokens_in'] for x in m['conversations'].values() for p in x['parts'])
    m['projection'] = {'calls_per_agent': sum(len(x['parts']) for x in m['conversations'].values()),
                       'tokens_in_per_agent': initial_in, 'v1_tokens_in_per_agent': base_in,
                       'input_ratio_to_v1': initial_in / base_in,
                       'output_tokens_per_agent_assumed': len(selected) * ncriteria * cfg['output_tokens_per_criterion'],
                       'review_scenarios': []}
    if variant == 'review':
        for rate in cfg['review_rates']:
            # One batched review per routed dialog, not one call per criterion.
            m['projection']['review_scenarios'].append({'routed_dialog_fraction': rate,
                'calls_per_agent': len(selected) * (1 + rate),
                'tokens_in_assumed': round(initial_in * (1 + rate)),
                'note': 'planning approximation; actual review scope and prompt size measured at route'})
    for agent in cfg['agents']:
        price = cfg['prices'].get(agent, {})
        m['projection'].setdefault('cost_usd_assumed', {})[agent] = tokens.cost(
            initial_in, m['projection']['output_tokens_per_agent_assumed'], price)
    save_json(out / 'manifest.json', m)
    write_new(out / 'README.md', '# Ручной прогон\n\nКаждый prompts/*.md вставьте в новый чат соответствующей модели. '
              'Ответ сохраните в responses/<имя>.json или .md. Не выполняйте промпты силами разработчика.\n'
              'Опционально responses/<имя>.usage.json: {"input_tokens": 123, "output_tokens": 456, "model": "..."}.\n'
              'Далее: python3 -m variants.codex ingest --run <эта папка>. Для review затем route и снова ingest.\n')
    event(out, 'prepared', {'manifest_hash': stable_hash(m)})
    return m


def read_part(out, agent, part, conv, doc):
    box = out / 'agents' / agent / 'responses'
    for ext in ('.json', '.md', '.txt'):
        p = box / (part['name'] + ext)
        if p.exists() and p.read_text().strip():
            raw = p.read_text(encoding='utf-8')
            data, err = responses.parse(raw)
            try:
                if err:
                    raise ValueError(err)
                if data.get('meta', {}).get('conversation_id') not in (None, conv['conversation_id']):
                    raise ValueError('conversation_id mismatch')
                v = strict_validate(conv, doc, data, part['criteria_ids'])
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                v = {'answers': {}, 'criteria': {}, 'issues': [{'level': 'error', 'criterion_id': cid, 'msg': str(e)} for cid in part['criteria_ids']], 'status': 'error'}
            usage_path = box / (part['name'] + '.usage.json')
            usage = load_json(usage_path) if usage_path.exists() else {}
            u = {'part': part['name'], 'tokens_in': usage.get('input_tokens', part['tokens_in']),
                 'tokens_out': usage.get('output_tokens', tokens.count(raw)), 'exact': bool(usage),
                 'model': usage.get('model'), 'response_hash': text_hash(raw)}
            return v, u
    return None, None


def verify_sources(m):
    for name, digest in m.get('source_hashes', {}).items():
        if text_hash((HERE.parents[1] / name).read_text(encoding='utf-8')) != digest:
            raise ValueError('Код изменился после prepare: ' + name + '; создайте новый прогон')


def ingest(out):
    out = Path(out)
    m = load_json(out / 'manifest.json')
    verify_sources(m)
    doc = load_json(out / 'checklist.json')
    stats = Counter()
    for cid, info in m['conversations'].items():
        conv = load_json(out / 'conversations' / f'{cid}.json')
        for agent in m['agents']:
            merged = {'answers': {}, 'criteria': {}, 'issues': []}
            usage, pending = [], []
            parts = info['parts']
            routefile = out / 'routing.json'
            routing = load_json(routefile) if routefile.exists() else {}
            review = routing.get(cid, {}).get('part')
            if review:
                parts = parts + [review]
            for part in parts:
                v, u = read_part(out, agent, part, conv, doc)
                if v is None:
                    pending.append(part['name'])
                    continue
                usage.append(u)
                # Review remains a proposal. Original is always retained below.
                if part['stage'] == 'review':
                    merged['review_proposal'] = v
                else:
                    merged['answers'].update(v['answers'])
                    merged['criteria'].update(v['criteria'])
                    merged['issues'].extend(v['issues'])
            status = 'pending' if pending else ('error' if any(i['level'] == 'error' for i in merged['issues']) else 'ok')
            result = {'run_id': out.name, 'agent': agent, 'conversation_id': cid, 'variant': m['variant'],
                      'checklist': m['checklist'], 'status': status, 'pending': pending,
                      'usage': usage, 'score': scoring.score(doc, merged['answers']), **merged}
            # Incomplete answers must not masquerade as a complete renormalized score.
            if not result['score']['complete']:
                result['score']['total'] = None
            digest = stable_hash(result)
            event(out, 'ingested', {'agent': agent, 'conversation_id': cid, 'snapshot': digest})
            save_json(out / 'snapshots' / digest / agent / f'{cid}.json', result)
            save_json(out / 'validated' / agent / f'{cid}.json', result)
            stats[status] += 1
    save_json(out / 'ingest-summary.json', dict(stats))
    return dict(stats)


def make_routes(out):
    out = Path(out)
    m = load_json(out / 'manifest.json')
    verify_sources(m)
    if m['variant'] != 'review':
        raise ValueError('route применим только к варианту review')
    if (out / 'routing.json').exists():
        raise ValueError('Маршрут уже закреплён; новый эксперимент — новый прогон')
    doc = load_json(out / 'checklist.json')
    if text_hash((prompts.TEMPLATES / 'v1.md').read_text(encoding='utf-8')) != m['template_hash']:
        raise ValueError('Шаблон изменился после prepare; создайте новый прогон')
    routes = {}
    for cid in m['conversations']:
        vals = [load_json(out / 'validated' / a / f'{cid}.json') for a in m['agents']]
        if any(v['pending'] for v in vals):
            raise ValueError('Сначала нужны исходные ответы всех моделей и ingest')
        reasons = route(doc, vals, m['config'])
        routes[cid] = {'reasons': reasons, 'part': None}
        if not reasons:
            continue
        conv = load_json(out / 'conversations' / f'{cid}.json')
        f = load_json(out / 'features' / f'{cid}.json')
        subset = copy.deepcopy(doc)
        subset['criteria'] = [c for c in ck.active_criteria(doc) if c['id'] in reasons]
        b = prompts.build('v1', subset, conv, f, 'v1')[0]
        # Blind review: no initial answers or confidence, to reduce anchoring.
        text = ('Независимая повторная проверка. Оцени только перечисленные критерии. '
                'Предыдущие ответы скрыты. Это черновик для человека, а не решение по апелляции.\n\n' + b['text'])
        name = cid + '__review'
        part = {'name': name, 'criteria_ids': list(reasons), 'tokens_in': tokens.count(text),
                'prompt_hash': text_hash(text), 'stage': 'review'}
        routes[cid]['part'] = part
        for agent in m['agents']:
            write_new(out / 'agents' / agent / 'prompts' / f'{name}.md', text)
    save_json(out / 'routing.json', routes)
    event(out, 'routed', {'routing_hash': stable_hash(routes)})
    return {'routed_dialogs': sum(bool(r['part']) for r in routes.values()),
            'review_tokens_in_per_agent': sum(r['part']['tokens_in'] for r in routes.values() if r['part'])}


def report(out):
    out = Path(out)
    m = load_json(out / 'manifest.json')
    rows = {}
    jury = {}
    for cid in m['conversations']:
        vals = [load_json(out / 'validated' / a / f'{cid}.json') for a in m['agents']
                if (out / 'validated' / a / f'{cid}.json').exists()]
        jury[cid] = {}
        for c in ck.active_criteria(load_json(out / 'checklist.json')):
            votes = [(a, v['answers'][c['id']]) for a, v in zip(
                [a for a in m['agents'] if (out / 'validated' / a / f'{cid}.json').exists()], vals)
                if c['id'] in v['answers']]
            counts = Counter(v for _, v in votes)
            winner, count = counts.most_common(1)[0] if counts else (None, 0)
            jury[cid][c['id']] = {'votes': dict(votes),
                'proposal': winner if count > len(m['agents']) / 2 else None,
                'agreement': count / len(votes) if votes else None, 'n': len(votes),
                'calibrated_probability': None}
    save_json(out / 'jury-proposals.json', jury)
    for agent in m['agents']:
        files = list((out / 'validated' / agent).glob('*.json'))
        vals = [load_json(p) for p in files]
        ins = sum(u['tokens_in'] for v in vals for u in v['usage'])
        outs = sum(u['tokens_out'] for v in vals for u in v['usage'])
        rows[agent] = {'dialogs': len(vals), 'calls_received': sum(len(v['usage']) for v in vals),
                       'tokens_in': ins, 'tokens_out': outs,
                       'cost_usd': tokens.cost(ins, outs, m['config']['prices'].get(agent, {})),
                       'statuses': dict(Counter(v['status'] for v in vals))}
    save_json(out / 'report.json', {'projection': m['projection'], 'observed': rows,
                                  'accuracy': None, 'note': 'Нет человеческого эталона; согласие не равно точности.'})
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['prepare', 'ingest', 'route', 'report'])
    p.add_argument('--run', type=Path, required=True)
    p.add_argument('--variant', choices=['sections', 'review', 'hints'], default='review')
    p.add_argument('--config', type=Path, default=HERE / 'config.json')
    p.add_argument('--checklist', default='default@1')
    p.add_argument('--dataset', default='dev')
    p.add_argument('--ids', nargs='+')
    a = p.parse_args()
    if a.command == 'prepare':
        result = prepare(a.run, a.variant, a.ids, load_json(a.config), a.checklist, a.dataset)['projection']
    else:
        result = {'ingest': ingest, 'route': make_routes, 'report': report}[a.command](a.run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
