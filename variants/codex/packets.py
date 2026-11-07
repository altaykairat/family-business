"""Manual-only V5 scale recheck and paired robustness packets, with compatible output ingestion."""
from __future__ import annotations
import argparse
import copy
import json
import re
from pathlib import Path
from cqa import checklist as ck, common, conversation as cv, prompts as pr, responses as rs, scoring, sources, tokens
from cqa.common import load_json, save_json, stable_hash, text_hash
from .sprint1 import HERE, DEFAULT_CONFIG, discover, peer_index, judgment

CONFIG=HERE/'sprint1-packets.json'


def put(path,text):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as f:f.write(text)


def transform(conv,kind,cfg):
    original=copy.deepcopy(conv)
    turns=original['transcript']
    mapping=list(range(1,len(turns)+1))
    details={}
    clients=[i for i,t in enumerate(turns) if t['speaker']=='client']
    if not clients:raise ValueError('No client message for perturbation')
    if kind=='control':
        details={'unchanged':True}
    elif kind=='injection':
        i=clients[0];details={'idx':i+1,'before':turns[i]['text']}
        turns[i]['text']+=cfg['injection_text']
        details['after']=turns[i]['text']
    elif kind=='neutral_swap':
        first_op=next((i for i,t in enumerate(turns) if t['speaker']=='operator'),len(turns))
        pair=next((i for i in range(first_op-1) if turns[i]['speaker']==turns[i+1]['speaker']==cfg['neutral_role']),None)
        if pair is None:raise ValueError('No adjacent neutral-role pair before first operator')
        # Swap text only: timestamp and role sequence unchanged, ids are valid original-array positions.
        i=pair
        details={'indices':[i+1,i+2],'texts_before':[turns[i]['text'],turns[i+1]['text']],
                 'assumption':'bot greeting/disclaimer order is neutral to operator QA; verify manually'}
        turns[i]['text'],turns[i+1]['text']=turns[i+1]['text'],turns[i]['text']
        mapping[i],mapping[i+1]=mapping[i+1],mapping[i]
    elif kind=='typo':
        for i in clients:
            match=next((m for m in re.finditer(r'[^\W\d_]+',turns[i]['text'],re.UNICODE)
                        if len(m.group())>=cfg['typo_min_word_length'] and m.group()[1]!=m.group()[2]),None)
            if match:
                word=match.group();new=word[0]+word[2]+word[1]+word[3:]
                details={'idx':i+1,'before':turns[i]['text'],'word_before':word,'word_after':new}
                turns[i]['text']=turns[i]['text'][:match.start()]+new+turns[i]['text'][match.end():]
                details['after']=turns[i]['text'];break
        else:raise ValueError('No suitable word for a controlled transposition')
    else:raise ValueError('Unknown attack')
    for i,t in enumerate(turns,1):t['idx']=i
    return cv.normalize(original),{'kind':kind,'details':details,'mutated_to_original_idx':mapping,
                                  'original_hash':stable_hash(conv),'mutated_hash':stable_hash(original)}


def render(doc,conv,criteria,template='v2',focus=False):
    text=(common.TEMPLATES/(template+'.md')).read_text(encoding='utf-8')
    ids=[c['id'] for c in criteria]
    scope='Оцени только критерии '+', '.join(ids)+'. Другие критерии не включай.'
    if focus:
        scope+='\nНезависимая перепроверка шкал. Исходные ответы скрыты. Проверяй ступени сверху вниз. '
        scope+='Для каждого снижения назови конкретно нарушенное условие и дословную цитату; не снижай за предполагаемый недостаток. '
        scope+='Отдельно поясни границу с соседними ступенями; содержание консультации и форма общения независимы.'
    fields={'CHECKLIST_TITLE':doc.get('title',''),'SCOPE_NOTE':scope,
        'GLOBAL_RULES':'\n'.join(f'{i}. {x}' for i,x in enumerate(doc.get('global_rules',[]),1)),
        'CRITERIA':'\n\n'.join(pr.render_criterion(doc,c) for c in criteria),
        'DIALOG_META':cv.render_meta(conv),'DIALOG':cv.render_dialog(conv),
        'FACTS':cv.render_facts(cv.facts(conv)), 'CRITERIA_IDS':', '.join(ids),
        'CONVERSATION_ID':conv['conversation_id'],'PROMPT_ID':'codex-sprint1|'+stable_hash(doc)+'|'+stable_hash(conv)+'|'+','.join(ids),
        'RULES':'','PRECEDENTS':''}
    for k,v in fields.items():text=text.replace('{{'+k+'}}',v)
    if re.search(r'{{[A-Z_]+}}',text):raise ValueError('Unresolved prompt placeholder')
    return text


def source_context(spec):
    src,results=sources.load_results(spec)
    ref=sources.checklist_ref(src,results)
    if not ref:raise ValueError('Baseline has no pinned checklist')
    doc=ck.load(ref.replace('@v','@'))
    run=src.get('run_dir')
    if run and (run/'checklist.json').exists():doc=load_json(run/'checklist.json')
    # A learned-context run needs its exact rules/precedents, not silently an empty context.
    man=src.get('manifest') or {}
    if any(man.get(k) for k in ('rules','precedents','rule_versions','precedent_versions')):
        raise ValueError('Baseline has learned context; provide a dedicated context-aware renderer before preparing')
    return src,results,doc


def prepare_run(out,cfg,kind,attack=None):
    out=Path(out)
    if out.exists():raise ValueError('Output run exists; preserve it and choose a new name')
    src,results,doc=source_context(cfg['baseline_source'])
    dataset=sources.dataset_of(src)
    selected={}
    routing={}
    if kind=='recheck':
        catalog=discover(load_json(DEFAULT_CONFIG)); peers=peer_index(catalog,load_json(DEFAULT_CONFIG))
        source_key=common.rel(src['dir'])
        for cid,r in results.items():
            scope=[];why={}
            for c in ck.active_criteria(doc):
                if c['id'] not in cfg['target_criteria'] or c['type']!='scale':continue
                item=(r.get('criteria') or {}).get(c['id'],{});reasons=[]
                conf=item.get('confidence')
                if isinstance(conf,(int,float)) and conf<cfg['confidence_threshold']:reasons.append('low_confidence')
                if item.get('ambiguities'):reasons.append('ambiguity')
                if item.get('insufficient_data') is True:reasons.append('insufficient_data')
                if any(i.get('criterion_id')==c['id'] for i in r.get('issues') or []):reasons.append('validation_issue')
                pool=peers.get((str(Path(source_key).parent.parent),cid),[])
                if len({judgment(p,c['id']) for _,p in pool if judgment(p,c['id']) is not None})>1:
                    reasons.append('peer_disagreement')
                if reasons:scope.append(c);why[c['id']]=reasons
            if scope:
                selected[cid]=scope;routing[cid]=why
        # No gold labels are loaded or used. Fixed deterministic order, not hindsight correctness.
        selected={cid:selected[cid] for cid in sorted(selected)[:cfg['recheck_max_dialogs']]}
    else:
        if not attack:raise ValueError('Attack required')
        for cid in cfg['robustness_ids']:
            if cid not in results:raise ValueError('Missing paired baseline: '+cid)
            selected[cid]=ck.active_criteria(doc)
    if not selected:raise ValueError('No disputed scales; no model calls needed')
    m={'run_id':out.name,'mode':'manual','variant':'codex-scale-recheck' if kind=='recheck' else 'codex-robustness',
       'kind':kind,'attack':attack,'dataset':dataset,'checklist':ck.ref_of(doc),'agents':[cfg['agent']],
       'config':cfg,'baseline_source':cfg['baseline_source'],'preview_only':cfg['preview_only'],
       'control_run':cfg.get('control_run'),'template':cfg['template'],'template_hash':text_hash((common.TEMPLATES/(cfg['template']+'.md')).read_text(encoding='utf-8')),
       'conversations':{},'token_method':tokens.METHOD}
    save_json(out/'checklist.json',doc)
    for cid,criteria in selected.items():
        r=results[cid]
        conv=cv.load(dataset,cid)
        provenance=None
        if kind=='robustness':conv,provenance=transform(conv,attack,cfg)
        save_json(out/'conversations'/(cid+'.json'),conv)
        save_json(out/'baseline'/(cid+'.json'),r)
        if provenance:save_json(out/'mutations'/(cid+'.json'),provenance)
        text=render(doc,conv,criteria,cfg['template'],kind=='recheck')
        name=cid+'__'+('scales' if kind=='recheck' else attack)
        part={'file':name+'.md','part':'scales' if kind=='recheck' else attack,
              'criteria_ids':[c['id'] for c in criteria],'tokens_in':tokens.count(text),'prompt_hash':text_hash(text)}
        m['conversations'][cid]={'parts':[part],'baseline_hash':stable_hash(r),'routing':routing.get(cid)}
        put(out/'prompts'/(name+'.md'),text)
        put(out/'agents'/cfg['agent']/'prompts'/(name+'.md'),text)
        (out/'agents'/cfg['agent']/'responses').mkdir(parents=True,exist_ok=True)
    m['projection']={'calls':len(selected),'tokens_in':sum(i['parts'][0]['tokens_in'] for i in m['conversations'].values()),
                     'measured_latency_s':None,'output_tokens':None,'dollars':None}
    save_json(out/'manifest.json',m)
    put(out/'README.md','# Ручной пакет\n\n'+
        ('PREVIEW: это НЕ R-base; исходный источник '+cfg['baseline_source']+'.\n\n' if cfg['preview_only'] else '')+
        'Используйте ту же фактическую модель/версию, что в baseline, и новый чат на каждый промпт. '
        'Агент claude — имя почтового ящика, а не проверенное имя модели. '
        'python3 scripts/paste.py status --run '+str(out)+'\n\n'+
        'После ручного переноса: python3 -m variants.codex.packets ingest --run '+str(out)+'\n'+
        'Сырые ответы и исходная оценка сохраняются. Повторная оценка — экспериментальный кандидат, не решение апелляции.\n')
    return m


def validate_response(conv,doc,data,expected):
    if not isinstance(data,dict) or not isinstance(data.get('criteria'),list):raise ValueError('Expected criteria array')
    items=data['criteria']
    if any(not isinstance(i,dict) for i in items):raise ValueError('Invalid criterion object')
    ids=[i.get('criterion_id') for i in items]
    if len(ids)!=len(set(ids)) or set(ids)!=set(expected):raise ValueError('Missing, duplicate or unexpected criteria')
    by_id={c['id']:c for c in ck.active_criteria(doc)}
    for i in items:
        c=by_id[i['criterion_id']];a=i.get('answer')
        allowed=[s['value'] for s in c['scale']] if c['type']=='scale' else ['yes','no']
        if c.get('na_allowed'):allowed+=['na']
        if isinstance(a,bool) or a not in allowed:raise ValueError('Invalid exact scale/answer')
    v=rs.validate(conv,doc,data,expected_ids=expected)
    turns={t['idx']:t['text'] for t in conv['transcript']}
    for i in items:
        if not i.get('explanation') or not i.get('evidence'):raise ValueError('Evidence and explanation required')
        for e in i['evidence']:
            if not isinstance(e,dict) or not isinstance(e.get('quote'),str) or not e['quote'] or e['quote'] not in turns.get(e.get('idx'),''):
                raise ValueError('Quote is not verbatim or turn is missing')
        span=i.get('checked_range')
        if c['type']=='flag' and i['answer']=='no' and span is None:
            raise ValueError('Negative flag requires checked_range under template v2')
        if span is not None and (not isinstance(span,list) or len(span)!=2 or
            not all(isinstance(x,int) and not isinstance(x,bool) and x in turns for x in span) or span[0]>span[1]):
            raise ValueError('Invalid checked_range')
    if v['status']=='error':raise ValueError(str(v['issues']))
    return v


def ingest(run):
    run=Path(run);m=load_json(run/'manifest.json');doc=load_json(run/'checklist.json')
    rows=[]
    valid_count=0
    errors=0
    pending_control=0
    for agent in m['agents']:
        for cid,info in m['conversations'].items():
            part=info['parts'][0];name=Path(part['file']).stem
            rawpath=next((run/'agents'/agent/'responses'/(name+ext) for ext in ('.json','.md','.txt')
                          if (run/'agents'/agent/'responses'/(name+ext)).exists()),None)
            if rawpath is None:continue
            raw=rawpath.read_text(encoding='utf-8');data,error=rs.parse(raw)
            usagepath=rawpath.with_name(name+'.usage.json');u=load_json(usagepath) if usagepath.exists() else {}
            usage=[{'part':part['part'],'tokens_in':u.get('input_tokens',part['tokens_in']),
                    'tokens_out':u.get('output_tokens',tokens.count(raw)),'exact':bool(u),'model':u.get('model'),
                    'response_file':common.rel(rawpath)}]
            result={'run_id':run.name,'agent':agent,'conversation_id':cid,'checklist':m['checklist'],
                    'variant':m['variant'],'usage':usage,'baseline_source':m['baseline_source']}
            baseline=load_json(run/'baseline'/(cid+'.json'));conv=load_json(run/'conversations'/(cid+'.json'))
            try:
                if error:raise ValueError(error)
                if data.get('meta',{}).get('conversation_id') not in (None,cid):raise ValueError('Conversation mismatch')
                v=validate_response(conv,doc,data,part['criteria_ids'])
                if m['kind']=='recheck':
                    result.update(answers={**baseline['answers'],**v['answers']},
                                  criteria={**baseline['criteria'],**v['criteria']},issues=v['issues'],status=v['status'],
                                  baseline_usage=baseline.get('usage',[]),review_proposal=v,
                                  incremental_usage=usage,usage=baseline.get('usage',[])+usage,
                                  meta=data.get('meta'),experimental_candidate=True)
                else:result.update(v)
                result['score']=scoring.score(doc,result['answers'])
                if not result['score']['complete']:result['score']['total']=None
            except (ValueError,TypeError,KeyError,AttributeError) as e:
                result.update(status='error',answers={},criteria={},score=None,
                              issues=[{'level':'error','criterion_id':None,'msg':str(e)}])
            digest=stable_hash(result)
            save_json(run/'snapshots'/digest/agent/(cid+'.json'),result)
            save_json(run/'validated'/agent/(cid+'.json'),result)
            if result['status']=='error':
                errors+=1
                continue
            valid_count+=1
            if m['kind']=='robustness':
                if m['attack']=='control':continue
                control=Path(m['control_run'])/'validated'/agent/(cid+'.json')
                if not control.exists():
                    pending_control+=1
                    continue
                baseline=load_json(control)
                control_manifest=load_json(Path(m['control_run'])/'manifest.json')
                if control_manifest['template_hash']!=m['template_hash'] or control_manifest['checklist']!=m['checklist']:
                    raise ValueError('Control and attack have different template/checklist')
                if baseline.get('status')=='error':
                    pending_control+=1
                    continue
            if result['status']!='error':
                changes=[c for c,a in result['answers'].items() if a!=judgment(baseline,c)]
                rows.append({'conversation_id':cid,'agent':agent,'changed_criteria':changes,
                             'compared_criteria':len(result['answers']),'changed_count':len(changes)})
    compared=sum(r['compared_criteria'] for r in rows);changed=sum(r['changed_count'] for r in rows)
    report={'kind':m['kind'],'completed_valid_dialogs':valid_count,'comparable_pairs':len(rows),'errors':errors,'pending_control':pending_control,'expected_dialogs':len(m['conversations'])*len(m['agents']),
            'rows':rows,'changed_answers':changed,'compared_answers':compared,
            'changed_answer_share':changed/compared if compared else None,
            'changed_dialog_share':sum(bool(r['changed_count']) for r in rows)/len(rows) if rows else None,
            'note':'No ground truth; change is not proof of error. Robustness compares only to the same-template fresh control run, never to old V1 results.'}
    save_json(run/'pairwise-report.json',report)
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['recheck','robustness','ingest'])
    p.add_argument('--run',type=Path,required=True);p.add_argument('--config',type=Path,default=CONFIG)
    p.add_argument('--base-source');p.add_argument('--preview',action='store_true')
    p.add_argument('--attack',choices=['control','injection','neutral_swap','typo'])
    a=p.parse_args()
    if a.command=='ingest':r=ingest(a.run)
    else:
        cfg=load_json(a.config)
        if a.base_source:cfg['baseline_source']=a.base_source;cfg['preview_only']=a.preview
        r=prepare_run(a.run,cfg,a.command,a.attack)['projection']
    print(json.dumps(r,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
