"""Sprint 1: offline error routing. No model execution, no gold in routing signals."""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path

from cqa import checklist as ck, common, conversation as cv, groundtruth
from cqa.common import load_json, save_json, stable_hash

HERE = Path(__file__).resolve().parent
ROOT = common.ROOT
DEFAULT_CONFIG = HERE / 'sprint1-config.json'
SIGNALS = ['model_disagreement', 'invalid_quote', 'ambiguities', 'insufficient_data',
           'low_confidence', 'confidence_risk', 's2_call', 's2_length', 's2_gap',
           's2_duplicates', 's2_system_ending', 'combined']


def discover(cfg):
    out = {}
    for pattern in cfg['source_patterns']:
        for folder in sorted(ROOT.glob(pattern)):
            if not folder.is_dir():
                continue
            src = str(folder.relative_to(ROOT))
            manifest = load_json(folder.parent.parent / 'manifest.json')
            rows = []
            for p in sorted(folder.glob('*.json')):
                r = load_json(p)
                if r.get('criteria') and not r.get('pending'):
                    rows.append(r)
            if rows:
                out[src] = {'manifest': manifest, 'rows': rows,
                            'hashes': {r['conversation_id']: stable_hash(r) for r in rows}}
    return out


def judgment(r, cid, mode='judgment'):
    if mode == 'pipeline':
        return (r.get('answers') or {}).get(cid)
    item = (r.get('criteria') or {}).get(cid, {})
    return item.get('answer_normalized', item.get('answer', (r.get('answers') or {}).get(cid)))


def peer_index(catalog, cfg):
    """Same run (thus same template/decomposition), distinct model aliases only."""
    groups = defaultdict(list)
    for source, bundle in catalog.items():
        run = str(Path(source).parent.parent)
        agent = Path(source).name
        model = cfg['primary_model_aliases'].get(agent, agent)
        for r in bundle['rows']:
            groups[(run, r['conversation_id'])].append((model, r))
    return groups


def extract(source, r, peers, conv, cfg):
    criteria = r.get('criteria') or {}
    quote_checks = [x for c in criteria.values() for x in c.get('evidence_checks') or []]
    # Recompute verbatim matching also when the base pipeline stored only fuzzy matches.
    turns = {t['idx']: t['text'] for t in conv['transcript']}
    evidence = [e for c in criteria.values() for e in c.get('evidence') or []]
    invalid = any(not isinstance(e, dict) or not e.get('quote') or
                  not isinstance(e.get('quote'), str) or e['quote'] not in turns.get(e.get('idx'), '')
                  for e in evidence)
    conf = [c.get('confidence') for c in criteria.values()
            if isinstance(c.get('confidence'), (int, float)) and not isinstance(c.get('confidence'), bool)
            and 0 <= c['confidence'] <= 1]
    seen = {cid for _, peer in peers for cid in (peer.get('criteria') or {})}
    disputed = []
    eligible = []
    for cid in sorted(seen):
        votes = defaultdict(set)
        for model, peer in peers:
            answer = judgment(peer, cid)
            if answer is not None:
                votes[model].add(str(answer))
        if len(votes) >= 2:
            eligible.append(cid)
            # Different models must actually disagree; repetition of one model alone is insufficient.
            if any(a != b for ma, aa in votes.items() for mb, bb in votes.items() if ma != mb for a in aa for b in bb):
                disputed.append(cid)
    f = cv.facts(conv, cfg['gap_threshold_s'])
    out = {'model_disagreement': float(len(disputed)), 'invalid_quote': float(invalid),
           'ambiguities': float(sum(bool(c.get('ambiguities')) for c in criteria.values())),
           'insufficient_data': float(sum(c.get('insufficient_data') is True for c in criteria.values())),
           'low_confidence': float(sum(v < cfg['confidence_threshold'] for v in conf)),
           'confidence_risk': 1 - min(conf) if conf else 0.,
           's2_call': float(conv.get('channel') == 'voice call'),
           's2_length': float(len(conv['transcript'])),
           's2_gap': float(max((x['seconds'] for x in f['operator_reply_gaps_s']), default=0)),
           's2_duplicates': float(len(f['duplicate_turn_idxs'])),
           's2_system_ending': float(bool(conv['transcript']) and conv['transcript'][-1]['speaker'] == 'system')}
    out['combined'] = sum(bool(out[k]) for k in ('model_disagreement','invalid_quote','ambiguities','insufficient_data','low_confidence'))
    availability = {'model_disagreement': bool(eligible), 'invalid_quote': bool(evidence),
                    'ambiguities': any('ambiguities' in c for c in criteria.values()),
                    'insufficient_data': any('insufficient_data' in c for c in criteria.values()),
                    'low_confidence': bool(conf), 'confidence_risk': bool(conf)}
    for k in SIGNALS:
        availability.setdefault(k, True)
    return {'signals': out, 'availability': availability, 'disputed_criteria': disputed,
            'confidence_fields': len(conf), 'insufficient_data_fields':sum('insufficient_data' in c for c in criteria.values())}


def curve(rows, signal):
    """Threshold curve: never break equal-score ties using outcome labels or convenient ids."""
    supported = [r for r in rows if r['availability'].get(signal, True)]
    n = len(supported)
    bad = sum(r['error_count'] for r in supported)
    bad_dialogs = sum(r['error_count'] > 0 for r in supported)
    points = [{'reviewed':0,'review_share':0.,'caught_errors':0,'error_recall':0. if bad else None,
               'caught_error_dialogs':0,'error_dialog_recall':0. if bad_dialogs else None}]
    for cutoff in sorted({r['signals'][signal] for r in supported}, reverse=True):
        picked = [r for r in supported if r['signals'][signal] >= cutoff]
        caught = sum(r['error_count'] for r in picked)
        caught_d = sum(r['error_count'] > 0 for r in picked)
        points.append({'threshold':cutoff,'reviewed':len(picked),'review_share':len(picked)/n,
                       'caught_errors':caught,'error_recall':caught/bad if bad else None,
                       'caught_error_dialogs':caught_d,'error_dialog_recall':caught_d/bad_dialogs if bad_dialogs else None})
    auc = sum((b['review_share']-a['review_share'])*((a['error_recall'] or 0)+(b['error_recall'] or 0))/2
              for a,b in zip(points,points[1:])) if bad else None
    return {'n':n,'available_fraction':n/len(rows) if rows else 0,'errors':bad,'error_dialogs':bad_dialogs,
            'auc':auc,'points':points}


def recall_at(c, budget):
    """Linear expectation inside a tied-score block: random selection, no oracle ordering."""
    if c['auc'] is None:
        return None
    for a,b in zip(c['points'],c['points'][1:]):
        if a['review_share'] <= budget <= b['review_share']:
            t=(budget-a['review_share'])/(b['review_share']-a['review_share'])
            return a['error_recall']+t*(b['error_recall']-a['error_recall'])
    return None


def fixed_policy(summary, rows):
    """Freeze a threshold on tune at the configured budget, then evaluate unchanged on test."""
    signal=summary['best_common_cohort_signal_selected_on_tune']
    tune=summary['common_cohort_curves']['tune'][signal]
    budget=summary['config']['routing_budget_fraction']
    eligible=[p for p in tune['points'] if 'threshold' in p and p['review_share']<=budget]
    threshold=max(eligible,key=lambda p:p['review_share'])['threshold'] if eligible else None
    out={'signal':signal,'threshold':threshold,'selected_on':'tune','budget':budget,'splits':{}}
    for split in ('tune','test'):
        cohort=[r for r in rows if r['split']==split and all(r['availability'].get(k,True)
                  for k in SIGNALS if k!='insufficient_data')]
        selected=[r for r in cohort if threshold is not None and r['signals'][signal]>=threshold]
        errors=sum(r['error_count'] for r in cohort)
        caught=sum(r['error_count'] for r in selected)
        out['splits'][split]={'evaluations':len(cohort),'reviewed':len(selected),
            'review_share':len(selected)/len(cohort) if cohort else None,
            'errors':errors,'caught_errors':caught,'error_recall':caught/errors if errors else None}
    return out


def build_rows(catalog, gold, cfg, mode='judgment'):
    peers = peer_index(catalog,cfg)
    out=[]
    for source,bundle in catalog.items():
        for r in bundle['rows']:
            cid=r['conversation_id']
            if cid not in gold['items']:
                continue
            conv=cv.load(bundle['manifest'].get('dataset') or 'dev',cid)
            # Signals computed before gold labels are touched.
            x=extract(source,r,peers[(str(Path(source).parent.parent),cid)],conv,cfg)
            labs=gold['items'][cid]['labels']
            errors=[c for c,lab in labs.items() if judgment(r,c,mode)!=lab['answer']]
            out.append({'source':source,'conversation_id':cid,
                        'split':'tune' if cid in gold['split']['tune'] else 'test',
                        'status':r.get('status'),'error_count':len(errors),'error_criteria':errors,
                        'label_count':len(labs),**x})
    return out


def analyze(out, cfg):
    out=Path(out)
    if out.exists():
        raise ValueError('Report folder already exists; select a new output path')
    gold=groundtruth.load(cfg['gold'])
    catalog=discover(cfg)
    rows=build_rows(catalog,gold,cfg)
    if not rows:
        raise ValueError('No completed results overlapping gold')
    out.mkdir(parents=True)
    curves={split:{signal:curve([r for r in rows if split=='all' or r['split']==split],signal)
                  for signal in SIGNALS} for split in ('tune','test','all')}
    # Pick only on tune. A constant, missing, or tiny sample is not a deployable detector.
    candidates=[s for s in SIGNALS if curves['tune'][s]['n'] and curves['tune'][s]['auc'] is not None
                and curves['tune'][s]['available_fraction']==1]
    best=max(candidates,key=lambda s:(curves['tune'][s]['auc'], -SIGNALS.index(s))) if candidates else None
    comparable=[r for r in rows if all(r['availability'].get(k,True) for k in SIGNALS if k!='insufficient_data')]
    common_curves={split:{signal:curve([r for r in comparable if r['split']==split],signal)
                        for signal in SIGNALS if signal!='insufficient_data'} for split in ('tune','test')}
    common_candidates=[k for k,c in common_curves['tune'].items() if c['auc'] is not None]
    common_best=max(common_candidates,key=lambda k:common_curves['tune'][k]['auc']) if common_candidates else None
    summary={'common_cohort_curves':common_curves,'best_common_cohort_signal_selected_on_tune':common_best,
             'per_source_curves':{src:{signal:curve([r for r in rows if r['source']==src],signal)
                                      for signal in SIGNALS} for src in catalog},'config':cfg,'config_hash':stable_hash(cfg),'gold_meta':gold['meta'],
             'sources':{s:{'n':len(b['rows']),'hashes':b['hashes']} for s,b in catalog.items()},
             'rows':len(rows),'unique_dialogs':len({r['conversation_id'] for r in rows}),
             'split_counts':{s:{'evaluations':sum(r['split']==s for r in rows),
                 'unique_dialogs':len({r['conversation_id'] for r in rows if r['split']==s})} for s in ('tune','test')},
             'best_signal_selected_on_tune':best,'curves':curves,
             'pipeline_error_view':build_rows(catalog,gold,cfg,mode='pipeline')}
    save_json(out/'analysis.json',summary)
    save_json(out/'rows.json',rows)
    save_json(out/'fixed-policy.json',fixed_policy(summary,rows))
    with (out/'curves.csv').open('w',encoding='utf-8',newline='') as f:
        fields=['split','signal','n','errors','threshold','reviewed','review_share','caught_errors','error_recall']
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader()
        for split,group in curves.items():
            for signal,c in group.items():
                for p in c['points']:
                    w.writerow({'split':split,'signal':signal,'n':c['n'],'errors':c['errors'],**p})
    draw_curves(out,curves,best)
    report(out,summary,rows)
    return summary


def draw_curves(out,curves,best):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    selected=list(dict.fromkeys([best,'model_disagreement','invalid_quote','ambiguities','confidence_risk']))
    selected=[s for s in selected if s]
    fig,axes=plt.subplots(1,2,figsize=(11,4.8),layout='constrained')
    for ax,split in zip(axes,('tune','test')):
        ax.plot([0,1],[0,1],'--',color='gray',label='Random selection (expected)')
        for signal in selected:
            c=curves[split][signal]
            if c['auc'] is None:continue
            ax.plot([p['review_share'] for p in c['points']],
                    [p['error_recall'] for p in c['points']],marker='o',markersize=3,
                    label=f"{signal} (n={c['n']}, AUC={c['auc']:.2f})")
        ax.set(xlim=(0,1),ylim=(0,1),xlabel='Fraction of evaluations reviewed',
               ylabel='Fraction of criterion errors captured',title=split)
        ax.grid(alpha=.2);ax.legend(fontsize=7,loc='lower right')
    fig.suptitle('Retrospective diagnostic pilot: 6 unique conversations, repeated sources')
    for ext in ('svg','png','pdf'):
        fig.savefig(out/('routing-curves.'+ext),dpi=160)
    plt.close(fig)


def report(out,s,rows):
    best=s['best_signal_selected_on_tune']
    lines=['# Sprint 1 — маршрутизация к проверяющему','',
        f"Наблюдений source × dialogue: {s['rows']}; уникальных диалогов: {s['unique_dialogs']}. Эталон: {s['config']['gold']}.",
        f"Разбиение: {s['split_counts']}. Лучший доступный на всех наблюдениях сигнал выбран ТОЛЬКО по tune: **{best}**.",'',
        '![Кривые маршрутизации](routing-curves.svg)','',
        '| Сигнал | tune n / errors | tune AUC | test n / errors | test AUC | test recall @50% |',
        '|---|---:|---:|---:|---:|---:|']
    for signal in SIGNALS:
        a=s['curves']['tune'][signal];b=s['curves']['test'][signal]
        fmt=lambda v:'—' if v is None else f'{v:.3f}'
        lines.append(f"| {signal} | {a['n']} / {a['errors']} | {fmt(a['auc'])} | {b['n']} / {b['errors']} | {fmt(b['auc'])} | {fmt(recall_at(b,.5))} |")
    lines += ['', '## Сопоставимое покрытие', '',
        f"На общей подвыборке с доступными полями (без полностью отсутствующего insufficient_data) лучший tune-сигнал: **{s['best_common_cohort_signal_selected_on_tune']}**.",
        '| Сигнал | common tune n | tune AUC | common test n | test AUC |',
        '|---|---:|---:|---:|---:|']
    for signal,a in s['common_cohort_curves']['tune'].items():
        b=s['common_cohort_curves']['test'][signal]
        lines.append(f"| {signal} | {a['n']} | {fmt(a['auc'])} | {b['n']} | {fmt(b['auc'])} |")
    lines += ['','## Что означает кривая','',
        'Единица — завершённая оценка одного диалога одним источником. Проверка диалога предполагает просмотр всех критериев. '
        'Ошибка — несовпадение нормализованного исходного ответа с человеческой меткой, включая пропуск; '
        'это не ошибка доказательств и не обязательно большое отклонение итога. Основной анализ отделяет суждения '
        'от отбрасывания ответов строгим валидатором. Во вторичном pipeline_error_view учитывается именно answers после фильтра.',
        'Кривая показывает долю пойманных ошибочных критериев; рядом JSON содержит долю пойманных диалогов с ошибкой. '
        'AUC=0.5 — ожидаемый случайный выбор. Для одинаковых значений порог включает весь блок; промежуточные значения '
        'линейной интерполяции — ожидание случайного выбора внутри блока, не гарантированный результат. '
        'GT не участвует в вычислении сигналов или разрешении ничьих. AUC здесь — площадь бюджетной кривой, не ROC-AUC.', '',
        '## Ограничения','',
        'Наблюдения повторяют одни и те же шесть диалогов: модели и схемы не являются независимыми примерами. '
        'Сырые числа нельзя выдавать за проверку на 60 новых диалогах. Сплиты группируются по conversation_id; '
        'на test победитель не подбирается. До спринта эти диалоги уже обсуждались/изучались, поэтому это '
        'ретроспективный диагностический split, не новый независимый hold-out. Разная модель/выборка смешана с трудностью.',
        'model_disagreement доступен только при >=2 разных модельных алиасах внутри того же прогона; '
        'sonnet2 считается повтором sonnet, а не отдельной моделью. Он требует дополнительных вызовов, '
        'поэтому не является бесплатным сигналом. У отсутствующих confidence/insufficient_data не предполагается '
        'отрицательный ответ: покрытие показано отдельно; недоступные строки не входят в кривую этого сигнала.',
        'Признаки S2 не являются правилами качества. Длина/звонок могут узнавать конкретный сложный пример, '
        'а не обобщаемый механизм ошибок. Самооценка confidence не откалибрована. '
        'Результаты insufficient_data нельзя считать доказательством бесполезности, если поле отсутствует в V1.', '',
        '## Цена и M.16 — десять контрольных чатов','',
        'V1 или hints: 10 вызовов; V5: 10+k (0<=k<=10), один пакет спорных критериев на диалог; '
        'V2: сейчас 40; панель из двух моделей V1: 20 до дополнительной проверки. '
        'Уже рассчитанные S2/цитаты/ambiguities/confidence не добавляют LLM-вызовов. '
        'При сценарии 2–4 минуты на ручной вызов V1 займёт 20–40 минут, V5 при k=3 — 26–52, '
        'V2 — 80–160. Плюс зарезервированные 15 минут на получение, экспорт, проверку и push. '
        'Это плановые сценарии, не измеренная latency. V5 при k=10 потребует 40–80 минут плюс резерв; '
        'час не гарантирован. Без репетиции максимальный безопасный ручной бюджет неизвестен.', '',
        'Артефакты: analysis.json — сводка и хеши входов; rows.json — сигналы/ошибки по наблюдениям; '
        'curves.csv — все пороговые точки; routing-curves.svg — рисунок. Официальный results/ не изменяется.']
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--config',type=Path,default=DEFAULT_CONFIG)
    a=p.parse_args()
    s=analyze(a.out,load_json(a.config))
    print(json.dumps({k:s[k] for k in ('rows','unique_dialogs','split_counts','best_signal_selected_on_tune')},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
