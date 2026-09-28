"""Finite loop templates generated solely from external manifest expectations.

Clip-start alignment is NOT verified speech-onset alignment. All input, including
trailing PCM, continues through EOF. Full duration plus min_settle_ms assertions
observe late actions; no immediate-success cutoff. No production matcher used.
"""
import hashlib
from replay_support import ACTIONS, SYSTEM_ACTIONS
from audio_replay import CorpusError


def supported(clips):
    return [c for c in clips if 'planned' not in c.get('tags',[])]


def build_matrix(corpus):
    clips = supported(corpus.clips)
    def group(field, value):
        return [c for c in clips if c['expected'].get(field) == value]
    wakes=group('wake',True); atomics=group('control','ATOMIC_RETURN')
    full=group('control','WAKE'); returns=group('control','RETURN')
    anton=group('intent','ANTON_OPEN'); larisa=group('intent','LARISA_OPEN')
    if not all((wakes,atomics,full,returns,anton,larisa)):
        raise CorpusError('Matrix requires wake, both GPT open, control wake/return and atomic ground truth')
    for c in clips:
        e=c['expected']
        if not (e.get('wake') is True or e.get('intent') in SYSTEM_ACTIONS|{'ANTON_OPEN','LARISA_OPEN'} or e.get('control') in {'WAKE','RETURN','ATOMIC_RETURN'}):
            raise CorpusError('Unsupported matrix ground truth: '+c['id'])
    def step(c, at, offset=0):return {'clip':c['id'],'at':at,'offset_ms':offset}
    def base(c, kind):
        name='matrix-'+kind+'-'+c['id']
        if len(name)>80:name=name[:62]+'-'+hashlib.sha256(name.encode()).hexdigest()[:16]
        return {'id':name,'tags':['matrix',kind], 'steps':[step(wakes[0],'initial_waiting')],
                'duration':45,'expected':{'wake_count':1,'local_intents':[],
                'action_counts':{a:0 for a in sorted(ACTIONS-{'poll'})},'min_settle_ms':2000}}
    def open_steps(case, clip):
        action='open_anton' if clip['expected']['intent']=='ANTON_OPEN' else 'open'
        case['steps'].append(step(clip,'wake_ack.playback_end'))
        case['expected']['action_counts'][action]=1
        case['bridge']={action:{},'close':{'delay_ms':1000},'poll':{}}
        return action
    cases=[]
    for c in wakes:
        case=base(c,'wake');case['steps']=[step(c,'initial_waiting')]
        case['duration']=25;case['expected']['listening_count']=1
        cases.append(case)
    for c in clips:
        intent=c['expected'].get('intent')
        if intent not in SYSTEM_ACTIONS:continue
        case=base(c,'local');case['duration']=25
        case['steps'].append(step(c,'wake_ack.playback_end'))
        case['system_actions']=[intent];case['expected']['local_intents']=[intent]
        case['expected']['listening_count']=1
        cases.append(case)
    for c in anton+larisa:
        case=base(c,'open');action=open_steps(case,c)
        mode='ANTON_MODE' if action=='open_anton' else 'LARISA_MODE'
        case['steps'].append(step(atomics[0],'mode.'+mode,3000))
        case['expected'].update(modes=[mode],returns=['ATOMIC_RETURN'],cleanup_confirmed=True)
        case['expected']['action_counts']['close']=1
        if action=='open_anton':
            case['bridge']['dictate']={'delay_ms':None}
            case['expected']['action_counts']['dictate']=1
        cases.append(case)
    for i in range(max(len(full),len(returns))):
        case=base(full[i%len(full)],'two-step-'+str(i))
        open_steps(case,larisa[0])
        case['steps'] += [step(full[i%len(full)],'mode.LARISA_MODE',3000),
                          step(returns[i%len(returns)],'control_ack.playback_end',1200)]
        case['bridge']['pause']={}
        case['expected']['action_counts'].update(pause=1,close=1)
        case['expected'].update(returns=['WAKE','RETURN'],cleanup_confirmed=True)
        cases.append(case)
    for i,c in enumerate(atomics):
        case=base(c,'atomic');action=open_steps(case,(anton if i%2==0 else larisa)[0])
        case['bridge'][action]={'delay_ms':20000,'late_result':True}
        case['steps'].append(step(c,'bridge.'+action,3000))
        case['expected']['action_counts']['close']=1
        case['expected'].update(returns=['ATOMIC_RETURN'],cleanup_confirmed=True,
                                forbidden_modes=['ANTON_MODE','LARISA_MODE'])
        cases.append(case)
    return cases


def coverage_report(corpus, cases, results):
    executed={r['id'] for r in results if r['result']!='NOT RUN'}
    used={key for r in results if r['id'] in executed for key in r.get('clips_delivered',[])}
    eligible={c['id'] for c in supported(corpus.clips)}
    selected={c['id']:c for c in cases}
    audits=[]
    for result in results:
        if result['result']=='NOT RUN':continue
        c=selected[result['id']];events=result['events']
        wake_count=sum(e['event']=='wake_detected' for e in events)
        expected=c['expected']
        audits.append({'id':c['id'],'wake_count':wake_count,
                       'duplicate_wake':wake_count>expected['wake_count'] if 'wake_count' in expected else None,
                       'duplicate_action':any(result['actions'].count(a)>n for a,n in expected.get('action_counts',{}).items()) or
                           any(result['local_intents'].count(a)>expected.get('local_intents',[]).count(a) for a in result['local_intents']),
                       'settle_checked':'min_settle_ms' in expected,
                       'settle_pass': 'settle_window' not in result['errors'] if 'min_settle_ms' in expected else None})
    matrix=[r for r in results if 'matrix' in selected[r['id']].get('tags',[])]
    return {'unique_wav_total':len(corpus.clips),'unique_wav_used':len(used),
            'supported_total':len(eligible),'supported_covered':len(eligible&used),
            'supported_coverage_percent':100*len(eligible&used)/len(eligible) if eligible else 0,
            'uncovered':sorted(eligible-used),'used_clip_ids':sorted(used),
            'matrix_summary':{s:sum(r['result']==s for r in matrix) for s in ('PASS','FAIL','NOT RUN')},
            'stability_checks':audits}
