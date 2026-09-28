#!/usr/bin/env python3
"""Offline real-Vosk replay through main.listen; never opens audio/browser devices.

An external loop-scenarios.json is independent of manifest.json. Unknown fields
are rejected. WAVs remain unchanged. Speech-onset alignment requires an explicit
verified annotation; a missing/unverified onset is NOT RUN, never a guessed PASS.
"""
from contextlib import ExitStack, redirect_stdout
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import sys
import wave
from unittest.mock import patch

import audio_replay as asr
import main
from replay_support import ReplayRuntime, ReplayError, ScenarioComplete, ACTIONS, SYSTEM_ACTIONS


class NotRun(ReplayError): pass


def number(value, low, high, label):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise asr.CorpusError('Invalid ' + label)


def strings(value, label):
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise asr.CorpusError(label + ' must be list[str]')


def validate_scenarios(document, corpus):
    asr.fields(document, {'schema_version', 'scenarios', 'annotations'}, {'schema_version','scenarios'}, 'Loop manifest')
    if type(document['schema_version']) is not int or document['schema_version'] != 1:
        raise asr.CorpusError('Loop schema_version must be 1')
    if not isinstance(document['scenarios'], list) or not document['scenarios']:
        raise asr.CorpusError('No loop scenarios')
    clips = {c['id']: c for c in corpus.clips}
    annotations = document.get('annotations', {})
    if not isinstance(annotations, dict) or set(annotations)-set(clips):
        raise asr.CorpusError('Unknown onset annotation clip')
    for annotation in annotations.values():
        asr.fields(annotation, {'speech_onset_frame','verified','method'}, {'speech_onset_frame','verified'}, 'Annotation')
        if type(annotation['speech_onset_frame']) is not int or annotation['speech_onset_frame'] < 0 or type(annotation['verified']) is not bool:
            raise asr.CorpusError('Invalid onset annotation')
        if 'method' in annotation and not isinstance(annotation['method'], str):
            raise asr.CorpusError('Invalid annotation method')
    anchors = {'initial_waiting','waiting','wake_ack.playback_end','control_ack.playback_end','response.playback_end'}
    anchors |= {'bridge.'+a for a in ACTIONS}
    anchors |= {'mode.'+m for m in ('LOCAL_MODE','ANTON_MODE','LARISA_MODE')}
    anchors |= {'phase.'+p for p in ('idle','speaking','cooldown','opening','voice','dictating','starting_dictation','transcribing','reply','control','pausing','closing','resuming','sending')}
    anchors |= {'clip.'+c+'.end' for c in clips}
    ids = set()
    for case in document['scenarios']:
        asr.fields(case, {'id','tags','steps','timing','expected','bridge','system_actions','duration','ack_duration_ms','stop_at','required'}, {'id','steps','expected'}, 'Scenario')
        key = case['id']
        import re
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', key) or key in ids:
            raise asr.CorpusError('Invalid/duplicate scenario ID')
        ids.add(key)
        strings(case.get('tags', []), 'tags')
        strings(case.get('system_actions', []), 'system_actions')
        if set(case.get('system_actions', []))-SYSTEM_ACTIONS:
            raise asr.CorpusError('Unknown system action')
        if type(case.get('required', True)) is not bool:
            raise asr.CorpusError('required must be boolean')
        number(case.get('duration',45), 1, 120, 'duration')
        number(case.get('ack_duration_ms',6000), 100, 10000, 'ack duration')
        if 'stop_at' in case and (not isinstance(case['stop_at'], str) or case['stop_at'] not in anchors or not case['stop_at'].startswith(('mode.','phase.'))):
            raise asr.CorpusError('Unknown stop anchor')
        timing = case.get('timing', {})
        asr.fields(timing, {'callback_delay_ms','main_loop_delay_after_playback_ms'}, set(), 'Timing')
        for val in timing.values(): number(val,0,10000,'timing delay')
        rules = case.get('bridge', {})
        asr.fields(rules, ACTIONS, set(), 'Bridge policy')
        for rule in rules.values():
            asr.fields(rule, {'delay_ms','ok','result','late_result'}, set(), 'Bridge rule')
            if rule.get('delay_ms',0) is not None: number(rule.get('delay_ms',0),0,120000,'bridge delay')
            for flag in ('ok','late_result'):
                if flag in rule and type(rule[flag]) is not bool: raise asr.CorpusError('Invalid bridge flag')
            asr.fields(rule.get('result', {}), {'end_voice','recording','send','reply_ready'}, set(), 'Fake UI result')
            if any(type(v) is not bool for v in rule.get('result',{}).values()): raise asr.CorpusError('Fake UI flags must be bool')
        expected = case['expected']
        if not expected: raise asr.CorpusError('Expected assertions must not be empty')
        asr.fields(expected, {'local_intents','action_counts','forbidden_gpt_actions','modes','forbidden_modes','returns','wake_count','cleanup_confirmed','min_audio_gaps','min_overflow','min_resets','listening_count','min_settle_ms'}, set(), 'Expected')
        for field in ('local_intents','forbidden_gpt_actions','modes','forbidden_modes','returns'):
            if field in expected: strings(expected[field],field)
        if set(expected.get('forbidden_gpt_actions',[]))-ACTIONS: raise asr.CorpusError('Unknown forbidden action')
        asr.fields(expected.get('action_counts',{}), ACTIONS, set(), 'Expected action counts')
        for val in expected.get('action_counts',{}).values():
            if type(val) is not int or val < 0: raise asr.CorpusError('Invalid action count')
        for field in ('wake_count','min_audio_gaps','min_overflow','min_resets','listening_count','min_settle_ms'):
            if field in expected and (type(expected[field]) is not int or expected[field]<0): raise asr.CorpusError('Invalid expected count')
        if 'cleanup_confirmed' in expected and type(expected['cleanup_confirmed']) is not bool: raise asr.CorpusError('Invalid cleanup expectation')
        if not isinstance(case['steps'],list) or not case['steps']: raise asr.CorpusError('No scenario steps')
        for step in case['steps']:
            asr.fields(step, {'clip','at','align','offset_ms','occurrence','kind','duration_ms','state'}, {'at'}, 'Step')
            if not isinstance(step['at'],str) or step['at'] not in anchors: raise asr.CorpusError('Unknown anchor')
            number(step.get('offset_ms',0),-10000,120000,'step offset')
            if type(step.get('occurrence',1)) is not int or not 1 <= step.get('occurrence',1) <= 100: raise asr.CorpusError('Invalid occurrence')
            if 'clip' in step:
                if not isinstance(step['clip'],str) or step['clip'] not in clips or any(k in step for k in ('kind','duration_ms','state')): raise asr.CorpusError('Unknown clip or mixed step')
                if step.get('align','clip_start') not in ('clip_start','speech_onset'): raise asr.CorpusError('Invalid alignment')
            else:
                if step.get('kind') not in ('guard','gap','main_delay','callback_delay'): raise asr.CorpusError('Unknown environment action')
                if step['kind']=='guard' and step.get('state') not in ('silent','active','unavailable','failed'): raise asr.CorpusError('Invalid guard state')
                if step['kind'] in ('main_delay','callback_delay'): number(step.get('duration_ms'),0,10000,'environment duration')
    return document


def read_clips(corpus, case, annotations):
    case = copy.deepcopy(case)
    clips = {c['id']:c for c in corpus.clips}
    data = {}
    for step in case['steps']:
        if 'clip' not in step: continue
        key = step['clip']
        path = asr.corpus_file(corpus.root, clips[key]['wav'])
        if asr.sha256_file(path) != corpus.wav_hashes[key]: raise ReplayError('WAV changed since validation')
        with wave.open(str(path),'rb') as wav:
            data[key] = wav.readframes(wav.getnframes())
        if step.get('align') == 'speech_onset':
            annotation = annotations.get(key, {})
            onset = clips[key].get('speech_onset_frame')
            if onset is None:
                if not annotation.get('verified'): raise NotRun('Verified speech onset missing: '+key)
                onset = annotation['speech_onset_frame']
            if not 0 <= onset < len(data[key])//2: raise ReplayError('Onset outside WAV')
            step['speech_onset_frame'] = onset
    return case, data


def check_outcomes(runtime, case):
    expected, events = case['expected'], runtime.sink.events
    actions = runtime.br.history
    modes = [e['mode'] for e in events if e['event'] in ('mode_changed','mode_entered')]
    returns = [e['kind'] for e in events if e['event']=='return_detected']
    errors = []
    if runtime.local_intents != expected.get('local_intents',[]): errors.append('local_intents')
    for action, count in expected.get('action_counts',{}).items():
        if actions.count(action) != count: errors.append('action_count:'+action)
    if set(actions)&set(expected.get('forbidden_gpt_actions',[])): errors.append('forbidden_gpt_action')
    if set(modes)&set(expected.get('forbidden_modes',[])): errors.append('forbidden_mode')
    if not set(expected.get('modes',[])) <= set(modes): errors.append('missing_mode')
    if not set(expected.get('returns',[])) <= set(returns): errors.append('missing_return')
    if 'wake_count' in expected and sum(e['event']=='wake_detected' for e in events)!=expected['wake_count']: errors.append('wake_count')
    if 'cleanup_confirmed' in expected and any(e['event']=='cleanup_confirmed' for e in events)!=expected['cleanup_confirmed']: errors.append('cleanup_confirmation')
    counts = {'min_audio_gaps':sum(e['event']=='audio_gap' for e in events),
              'min_overflow':sum(e.get('count',0) for e in events if e['event']=='audio_drop' and e.get('reason')=='overflow'),
              'min_resets':sum(e['event']=='recognizer_reset' for e in events)}
    for name,count in counts.items():
        if count < expected.get(name,0): errors.append(name)
    if 'listening_count' in expected and sum(e['event']=='local_state' and e.get('state')=='listening' for e in events)!=expected['listening_count']:
        errors.append('listening_count')
    if 'min_settle_ms' in expected:
        last_input=max((start+frames)/16000 for _,_,start,frames in runtime.source_spans)
        last_action=max((e['time'] for e in events if e['event'] in ('intent_matched','wake_detected','return_detected','cleanup_confirmed') or
                         (e['event']=='bridge_result' and e.get('action')!='poll')), default=0)
        if runtime.scheduler.now-max(last_input,last_action) < expected['min_settle_ms']/1000-1e-9:
            errors.append('settle_window')
    if len(runtime.triggered)!=len(case['steps']): errors.append('untriggered_step')
    return errors


class NullOutput:
    def write(self, value): return len(value)
    def flush(self): pass


def run_case(corpus, case, annotations, model, recognizer, *, require_vosk=True, debug_trace=False):
    runtime = None
    try:
        prepared, clips = read_clips(corpus, case, annotations)
        runtime = ReplayRuntime(prepared, clips, model, recognizer, require_vosk=require_vosk, debug_trace=debug_trace)
        # Tripwires supplement dependency injection. A future accidental escape
        # must fail loudly rather than launch a browser or perform a system action.
        with ExitStack() as safety, redirect_stdout(NullOutput()):
            for name in ('Speech','Bridge','OutputGuard','command','dependencies','volume','internet','battery_report'):
                safety.enter_context(patch.object(main, name, side_effect=ReplayError('Real dependency reached: '+name)))
            safety.enter_context(patch('subprocess.Popen',side_effect=ReplayError('Process launch forbidden')))
            safety.enter_context(patch('subprocess.run',side_effect=ReplayError('Subprocess forbidden')))
            safety.enter_context(patch('socket.create_connection',side_effect=ReplayError('Network forbidden')))
            safety.enter_context(patch('socket.socket.connect',side_effect=ReplayError('Network forbidden')))
            try:
                main.listen(main.config(), duration=case.get('duration',45), runtime=runtime)
            except ScenarioComplete:
                pass
        errors = check_outcomes(runtime, prepared)
        status = 'FAIL' if errors else 'PASS'
    except NotRun as exc:
        status, errors = 'NOT RUN', [str(exc)]
    except Exception as exc:
        status, errors = 'FAIL', [str(exc) if isinstance(exc,ReplayError) else type(exc).__name__]
    if runtime and runtime.trace:
        runtime.trace.emit('scenario_completed', reason='stop_at' if runtime.finished else 'duration',
                           result=status, expected=case['expected'])
    result = {'id':case['id'], 'result':status,'required':case.get('required',True),'errors':errors,
              'virtual_time':round(runtime.scheduler.now,6) if runtime else 0,
              'events':runtime.sink.events if runtime else [],
              'clips_delivered':sorted({key[1] for key in runtime.delivered_source_ranges}) if runtime else [],
              'local_intents':runtime.local_intents if runtime else [],
              'actions':runtime.br.history if runtime else [],
              'asr':'real Vosk' if require_vosk else 'scripted test double'}
    if debug_trace:
        result['debug_trace'] = runtime.trace.events if runtime else []
    # The existing validator supplied hashes; recheck originals after execution.
    for key in {s['clip'] for s in case['steps'] if 'clip' in s}:
        clip = next(c for c in corpus.clips if c['id']==key)
        if asr.sha256_file(asr.corpus_file(corpus.root,clip['wav']))!=corpus.wav_hashes[key]:
            result.update(result='FAIL',errors=['WAV changed during replay'])
    return result


def cli(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus',required=True)
    parser.add_argument('--scenarios')
    parser.add_argument('--matrix',choices=['all'],help='Generate ground-truth coverage templates; may combine with --scenarios')
    parser.add_argument('--scenario',action='append')
    parser.add_argument('--tag',action='append')
    parser.add_argument('--report')
    parser.add_argument('--verbose',action='store_true')
    parser.add_argument('--debug-trace',action='store_true',help='Replay-only block provenance and ASR partial/final in report')
    args=parser.parse_args(argv)
    try:
        corpus=asr.load_corpus(args.corpus)
        from loop_matrix import build_matrix, coverage_report
        if not args.scenarios and not args.matrix: raise asr.CorpusError('Provide --scenarios and/or --matrix all')
        source=asr.outside_worktree(args.scenarios).read_bytes() if args.scenarios else b''
        doc=json.loads(source,object_pairs_hook=asr.unique_object) if source else {'schema_version':1,'scenarios':[]}
        if source: validate_scenarios(doc,corpus)
        if args.matrix: doc['scenarios'].extend(build_matrix(corpus))
        doc=validate_scenarios(doc,corpus)
        effective_source=json.dumps(doc,sort_keys=True,ensure_ascii=False).encode()
        cases=[c for c in doc['scenarios'] if (not args.scenario or c['id'] in args.scenario) and (not args.tag or set(args.tag)&set(c.get('tags',[])))]
        if not cases or (args.scenario and set(args.scenario)-{c['id'] for c in doc['scenarios']}): raise asr.CorpusError('No/unknown scenarios selected')
        target=asr.report_destination(args.report) if args.report else None
        cfg=main.config()
        _,Model,Recognizer=main.dependencies(audio=False)
        with redirect_stdout(NullOutput()): model=main.load_model(cfg,Model)
        results=[]
        for case in cases:
            result=run_case(corpus,case,doc.get('annotations',{}),model,Recognizer,debug_trace=args.debug_trace)
            results.append(result)
            print(f"{result['id']:<40} {result['result']:<8} {result['virtual_time']:.3f}s",flush=True)
            if result['errors']: print('  '+', '.join(result['errors']),flush=True)
            if args.verbose:
                for event in result['events']:
                    if event['event'] not in ('capture_block','callback_delivered','audio_consumed','recognizer_input','recognizer_reset'): print(json.dumps(event,ensure_ascii=False))
        summary={status:sum(r['result']==status for r in results) for status in ('PASS','FAIL','NOT RUN')}
        sources=['main.py','gpt_modes.py','replay_support.py','loop_replay.py','audio_replay.py','browser_timing.py','replay_trace.py','loop_matrix.py']
        model_path=Path(cfg['model_path']).resolve()
        report={'schema_version':1,'git':asr.git_identity(),'sources':{name:asr.sha256_file(asr.ROOT/name) for name in sources},
                'manifest_sha256':corpus.manifest_sha256,'scenario_sha256':hashlib.sha256(effective_source).hexdigest(),
                'scenario_file_sha256':hashlib.sha256(source).hexdigest() if source else None,
                'wav_sha256':{c['wav']:corpus.wav_hashes[c['id']] for c in corpus.clips if any(s.get('clip')==c['id'] for case in cases for s in case['steps'])},
                'config':{'path':'config.json','sha256':asr.sha256_file(asr.ROOT/'config.json')},
                'model':{'path':model_path.relative_to(asr.ROOT).as_posix() if model_path.is_relative_to(asr.ROOT) else '<external>', 'sample_rate':cfg['sample_rate']},
                'summary':summary,'scenarios':results}
        report['coverage']=coverage_report(corpus,cases,results)
        report['dirty_fingerprint']=hashlib.sha256(json.dumps(report['sources'],sort_keys=True).encode()).hexdigest()
        if target:
            target.parent.mkdir(parents=True,exist_ok=True)
            with target.open('x') as out: json.dump(report,out,ensure_ascii=False,indent=2);out.write('\n')
        print(json.dumps(summary),flush=True)
        return int(any(r['result']!='PASS' and r['required'] for r in results))
    except (asr.CorpusError,OSError,ValueError,RuntimeError) as exc:
        print('ERROR: '+(str(exc) if isinstance(exc,asr.CorpusError) else type(exc).__name__),file=sys.stderr)
        return 2


if __name__=='__main__': sys.exit(cli())
