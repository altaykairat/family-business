"""Reproducible analysis of isolated Luna/Terra pilot; never generates model answers."""
from __future__ import annotations
import json
from pathlib import Path
from collections import Counter
from cqa import scoring, tokens
from cqa.common import load_json, save_json
from .pipeline import HERE, ingest, strict_validate, read_part

BASE = HERE / 'runs'
PRICES = {
    'luna': {'model': 'gpt-6-luna', 'input_per_mtok': .10, 'output_per_mtok': .50,
             'source': 'https://developers.openai.com/api/docs/models/gpt-6-luna'},
    'terra': {'model': 'gpt-6.1-terra', 'input_per_mtok': 1., 'output_per_mtok': 5.,
              'source': 'https://developers.openai.com/api/docs/models/gpt-6.1-terra'}}


def analyze():
    output = HERE / 'artifacts' / 'isolated-20261107'
    output.mkdir(parents=True, exist_ok=True)
    summary = {'prices_standard_usd_per_million': PRICES, 'price_checked': '2026-11-07',
       'token_method': tokens.METHOD, 'reasoning_effort': 'medium', 'fork_turns': 'none',
       'accuracy': None, 'human_ground_truth': None,
       'cost_scope': 'prompt+visible JSON only; estimated tokens, no hidden reasoning or harness/tool overhead; not actual Codex bill',
       'rows': [], 'disagreements': []}
    results = {}
    for variant in ('review', 'sections'):
        run = BASE / ('isolated-20261107-' + variant)
        ingest(run)
        m = load_json(run / 'manifest.json')
        for agent in m['agents']:
            vals = [load_json(run / 'validated' / agent / (cid+'.json')) for cid in m['conversations']]
            results[(variant,agent)] = {v['conversation_id']:v for v in vals}
            ins = sum(u['tokens_in'] for v in vals for u in v['usage'])
            outs = sum(u['tokens_out'] for v in vals for u in v['usage'])
            price = PRICES[agent]
            cost = (ins*price['input_per_mtok']+outs*price['output_per_mtok'])/1e6
            n = len(vals)
            summary['rows'].append({'variant': 'monolith' if variant=='review' else variant,
                 'agent':agent,'model':price['model'],'dialogs':n,
                 'calls_received':sum(len(v['usage']) for v in vals),
                 'input_tokens_est':ins,'visible_output_tokens_est':outs,
                 'visible_api_equivalent_usd':cost, 'per_dialog_usd':cost/n,
                 'per_1000_dialogs_usd':cost/n*1000,
                 'reasoning_1000_tokens_per_call_extra_usd':sum(len(v['usage']) for v in vals)*1000*price['output_per_mtok']/1e6,
                 'status':dict(Counter(v['status'] for v in vals)),
                 'totals':{v['conversation_id']:v['score']['total'] for v in vals},
                 'provisional_totals_before_strict_gate': {v['conversation_id']:scoring.score(
                     load_json(run/'checklist.json'), {cid:x.get('answer_normalized')
                     for cid,x in v['criteria'].items()})['total']
                     if len(v['criteria']) == len(load_json(run/'checklist.json')['criteria']) else None for v in vals},
                 'formal_issues': {v['conversation_id']:v['issues'] for v in vals}})
    ids = list(results[('review','luna')])
    for cid in ids:
        doc = load_json(BASE / 'isolated-20261107-review' / 'checklist.json')
        for c in doc['criteria']:
            vals = {variant+'_'+agent: results[(variant,agent)][cid]['criteria'].get(c['id'],{}).get('answer_normalized')
                    for variant in ('review','sections') for agent in PRICES}
            if len(set(vals.values()))>1:
                summary['disagreements'].append({'conversation_id':cid, 'criterion_id':c['id'],'answers':vals})
    save_json(output / 'comparison.json',summary)
    # A neutral adjudication packet: no model answer or majority exposed to human.
    packet = {'instructions':'Человек оценивает независимо, затем сравнивает ответы. Не подменять эталон большинством моделей.',
              'checklist':doc,'conversations':[]}
    for cid in ids:
        conv=load_json(BASE/'isolated-20261107-review'/'conversations'/(cid+'.json'))
        packet['conversations'].append({'conversation':conv,'human_criteria':[
             {'criterion_id':c['id'],'answer':None,'evidence':[],'explanation':''} for c in doc['criteria']]})
    save_json(output/'human-review-blank.json',packet)
    lines=['# Изолированный пилот Luna / Terra', '',
           'Два диалога, один повтор на условие; 20 независимых заданий. Точность неизвестна: человеческого эталона нет.', '',
           '| Модель | Схема | Вызовы | Вход ≈ | Видимый выход ≈ | $ / диалог ≈ | $ / 1000 ≈ |',
           '|---|---|---:|---:|---:|---:|---:|']
    for r in summary['rows']:
        lines.append(f"| {r['model']} | {r['variant']} | {r['calls_received']} | {r['input_tokens_est']} | {r['visible_output_tokens_est']} | {r['per_dialog_usd']:.5f} | {r['per_1000_dialogs_usd']:.2f} |")
    lines += ['', 'Стоимость — эквивалент Standard API только для сгенерированного промпта и видимого JSON, без кеша. '
              'Не включает скрытые reasoning-токены, системный контекст, чтение/запись инструментами. '
              'Фактические usage/latency API недоступны. При дополнительных 1000 reasoning-токенов на вызов добавляется '
              '$0.0005 Luna или $0.005 Terra. Цены проверены 07.11.2026 по официальным страницам моделей.', '',
              '## Итоги по диалогам', '']
    for r in summary['rows']:
        lines.append(f"- {r['model']} / {r['variant']}: {r['totals']}; статусы {r['status']}.")
        lines.append(f"  Предварительные баллы до строгого фильтра: {r['provisional_totals_before_strict_gate']}.")
    lines += ['', 'Строгий валидатор требует checked_range при любом no, включая отсутствие грубых ошибок. '
              'Это спорное требование контракта, а не доказанная ошибка смысловой оценки. '
              'Исходные ответы сохранены без правок. Предварительные баллы не являются проверенным финальным результатом.', '']
    lines += ['', '## Расхождения (не ошибки относительно эталона)', '']
    for d in summary['disagreements']:
        lines.append(f"- {d['conversation_id']} / {d['criterion_id']}: {d['answers']}")
    lines += ['', '## Измеренный компромисс', '']
    for agent in PRICES:
        mono = next(r for r in summary['rows'] if r['agent'] == agent and r['variant'] == 'monolith')
        sect = next(r for r in summary['rows'] if r['agent'] == agent and r['variant'] == 'sections')
        delta = sect['per_dialog_usd'] - mono['per_dialog_usd']
        ratio = sect['per_dialog_usd'] / mono['per_dialog_usd'] if mono['per_dialog_usd'] else 0
        lines.append(f'- {agent}: секции добавили ≈${delta:.5f} на диалог; видимый API-эквивалент ×{ratio:.2f}. '
                     'Число исправленных ошибок неизвестно до проверки человеком.')
    lines += ['', '## Ограничения и следующий эксперимент', '',
        'fork_turns=none убирает историю родителя; каждому новому агенту передан путь ровно одного промпта. '
        'Общий файловый доступ остаётся: это изоляция инструкцией, не OS-песочница. Модель внутри Codex получает '
        'системные инструкции и инструменты, поэтому это суррогат API, а не точное воспроизведение API. '
        'Все агенты запускались с medium; повторный вызов всегда новым агентом. Развёрнутый шаблон сохранён, '
        'но агентам запрошены проверяемые обоснования без раскрытия внутренних цепочек рассуждений.', '',
        'Два намеренно выбранных диалога не позволяют доказать выигрыш 1–2 п.п. Нужны человеческая разметка, '
        'разные языки/звонки, несколько повторов и интервалы по диалогам. Выбирать победителя только по согласию нельзя.', '',
        'Research thesis: при фиксированном бюджете и одинаковых диалогах проверить, снижает ли независимая '
        'декомпозиция/выборочная перепроверка ошибку относительно человека и как меняется стоимость исправленной ошибки. '
        'Сравнить Luna-монолит, Luna-секции, Terra-монолит, Terra-секции, затем Luna + Terra только на спорных критериях.', '',
        'Цены: [Luna](https://developers.openai.com/api/docs/models/gpt-6-luna), '
        '[Terra](https://developers.openai.com/api/docs/models/gpt-6.1-terra).']
    (output/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps(summary['rows'],ensure_ascii=False,indent=2))

if __name__=='__main__':
    analyze()
