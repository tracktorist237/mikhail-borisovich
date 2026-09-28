"""Synthetic PCM timing through actual listen/CommandSession/GPTModes.

The recognizer here is explicitly a scripted double; real Vosk runs are CLI-only
and reported separately. No test below claims real-user ASR accuracy.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import wave
from array import array
from unittest.mock import patch
import audio_replay as asr
import loop_replay as loop
import main
from replay_support import ReplayError


class MarkerRecognizer:
    def __init__(self, model, rate, grammar):
        words=json.loads(grammar)
        self.role='wake' if 'михаил' in words else 'control' if words==main.CONTROL_GRAMMAR else 'command'
        self.text='';self.pending=False
    def Reset(self):self.text='';self.pending=False
    def AcceptWaveform(self, pcm):
        samples=set(array('h',pcm));self.text=''
        if self.role=='wake' and 1000 in samples:self.text='михаил'
        if self.role=='command':
            if 2000 in samples:self.pending=True
            if 2100 in samples and self.pending:self.text='который час'
            if 2200 in samples:self.text='позови антона павловича'
            if 2300 in samples:self.text='позови ларису'
            if 2400 in samples:self.text='неизвестно'
        if self.role=='control':
            self.text=next((t for n,t in ((3000,'михаил вернись'),(3100,'михаил борисович'),(3200,'вернись'),(3300,'михаил')) if n in samples),'')
        return bool(self.text)
    def Result(self):return json.dumps({'text':self.text})
    def PartialResult(self):return '{"partial":""}'


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='mb-loop-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        definitions={'wake':[0,1000,0],'time':[2000,2100,0], 'anton':[2200,0], 'larisa':[2300,0],
                     'atomic':[3000,0], 'fullwake':[3100,0], 'return':[3200,0], 'single':[3300,0],
                     'unknown':[2400,0], 'silence':[0]*10}
        self.clips=[]
        for key,blocks in definitions.items():
            with wave.open(str(self.root/(key+'.wav')),'wb') as out:
                out.setnchannels(1);out.setsampwidth(2);out.setframerate(16000)
                out.writeframes(array('h',[n for n in blocks for _ in range(1600)]).tobytes())
            self.clips.append({'id':key,'wav':key+'.wav','expected':{'transcript':'ground truth'}})
        self.write_manifest()
        self.case={'id':'local','duration':6,'ack_duration_ms':1000,'system_actions':['TIME'],
          'steps':[{'clip':'wake','at':'initial_waiting'},{'clip':'time','at':'wake_ack.playback_end','offset_ms':100}],
          'expected':{'local_intents':['TIME'],'wake_count':1}}

    def write_manifest(self):
        (self.root/'manifest.json').write_text(json.dumps({'schema_version':1,'clips':self.clips}))
        self.corpus=asr.load_corpus(self.root)

    def run_loop(self,case=None,annotations=None):
        case=copy.deepcopy(case or self.case)
        loop.validate_scenarios({'schema_version':1,'scenarios':[case]},self.corpus)
        with patch('main.listen',wraps=main.listen) as listen, patch('main.CommandSession',wraps=main.CommandSession) as session, patch('main.GPTModes',wraps=main.GPTModes) as modes:
            result=loop.run_case(self.corpus,case,annotations or {},None,MarkerRecognizer,require_vosk=False)
        if result['result'] != 'NOT RUN':
            listen.assert_called_once();session.assert_called();modes.assert_called_once()
        return result

    def test_real_loop_components_and_first_post_boundary_block(self):
        for offset in (0,50,100,200):
            with self.subTest(offset=offset):
                c=copy.deepcopy(self.case);c['steps'][1]['offset_ms']=offset
                c['timing']={'main_loop_delay_after_playback_ms':200}
                r=self.run_loop(c)
                self.assertEqual(r['result'],'PASS',r['errors'])
                boundary=next(e['boundary'] for e in r['events'] if e['event']=='playback_handoff')
                inputs=[e for e in r['events'] if e['event']=='recognizer_input' and e['role']=='command']
                self.assertTrue(inputs)
                self.assertTrue(all(e['capture_start']>=boundary for e in inputs))

    def test_delayed_callback_keeps_capture_timestamp_and_stale_limit(self):
        for delay in (0,100,250,400,2000):
            c=copy.deepcopy(self.case);c['timing']={'callback_delay_ms':delay}
            if delay>=400:c['expected']['local_intents']=[]
            r=self.run_loop(c)
            self.assertEqual(r['result'],'PASS',r['errors'])
            delivered=next(e for e in r['events'] if e['event']=='callback_delivered')
            self.assertAlmostEqual(delivered['time']-(delivered['capture_end']-10000),delay/1000)

    def test_late_tts_and_straddling_pcm_not_recognized(self):
        for offset in (-200,):
            c=copy.deepcopy(self.case);c['steps'][1]['offset_ms']=offset;c['timing']={'callback_delay_ms':100}
            c['expected']['local_intents']=[]
            r=self.run_loop(c)
            self.assertEqual(r['result'],'PASS',r['errors'])

    def test_overflow_old_context_reset_then_fresh_utterance(self):
        c=copy.deepcopy(self.case);c['duration']=10
        c['steps'] += [{'kind':'main_delay','at':'wake_ack.playback_end','duration_ms':2000},
                       {'clip':'time','at':'clip.time.end','offset_ms':2500}]
        c['expected'].update(min_audio_gaps=1,min_overflow=1,min_resets=3)
        r=self.run_loop(c);self.assertEqual(r['result'],'PASS',r['errors'])

    def test_explicit_gap_resets_partial_and_fresh_command_still_works(self):
        c=copy.deepcopy(self.case)
        c['steps'] += [{'kind':'gap','at':'wake_ack.playback_end','offset_ms':220},
                       {'clip':'time','at':'clip.time.end','offset_ms':500}]
        c['expected']['min_audio_gaps']=1
        r=self.run_loop(c);self.assertEqual(r['result'],'PASS',r['errors'])

    def gpt_case(self, mode='anton', atomic=False):
        action='open_anton' if mode=='anton' else 'open'
        c=copy.deepcopy(self.case);c.pop('system_actions')
        c['steps'][1]['clip']=mode
        c['bridge']={action:{}}
        c['expected']={'action_counts':{action:1,'send':0},'modes':[mode.upper()+'_MODE']}
        c['stop_at']='mode.'+mode.upper()+'_MODE'
        if atomic:
            c.pop('stop_at');c['duration']=12
            c['bridge']={action:{'delay_ms':5000,'late_result':True},'close':{'delay_ms':1000}}
            c['steps'].append({'clip':'atomic','at':'bridge.'+action,'offset_ms':300})
            c['expected']={'action_counts':{action:1,'close':1,'send':0},'returns':['ATOMIC_RETURN'],
                            'forbidden_modes':[mode.upper()+'_MODE'],'cleanup_confirmed':True}
        return c

    def test_anton_larisa_pending_open_and_late_result(self):
        for mode in ('anton','larisa'):
            r=self.run_loop(self.gpt_case(mode,True))
            self.assertEqual(r['result'],'PASS',r['errors'])
            events=r['events'];close=next(e['time'] for e in events if e['event']=='cleanup_confirmed')
            local=[e['time'] for e in events if e['event']=='local_state' and e['state']=='waiting']
            self.assertGreater(local[-1],close)
            self.assertTrue(any(e.get('late') and e['event']=='bridge_result' for e in events))

    def test_normal_open_uses_production_modes_no_early_send(self):
        for mode in ('anton','larisa'):
            r=self.run_loop(self.gpt_case(mode));self.assertEqual(r['result'],'PASS',r['errors'])

    def test_two_step_return_and_negative_control(self):
        c=self.gpt_case('larisa');c.pop('stop_at');c['duration']=12
        c['bridge'].update(poll={},pause={},close={'delay_ms':300})
        c['steps'] += [{'clip':'fullwake','at':'mode.LARISA_MODE','offset_ms':300},
                       {'clip':'return','at':'control_ack.playback_end','offset_ms':1200}]
        c['expected'].update(action_counts={'open':1,'pause':1,'close':1},returns=['WAKE','RETURN'],cleanup_confirmed=True)
        r=self.run_loop(c);self.assertEqual(r['result'],'PASS',r['errors'])
        for clip in ('single','return','silence'):
            negative=self.gpt_case('larisa');negative.pop('stop_at');negative['duration']=8
            negative['bridge'].update(poll={},pause={},close={})
            negative['steps'].append({'clip':clip,'at':'mode.LARISA_MODE','offset_ms':300})
            negative['expected'].update(action_counts={'open':1,'pause':0,'close':0},cleanup_confirmed=False)
            r=self.run_loop(negative);self.assertEqual(r['result'],'PASS',r['errors'])

    def test_guard_blocks_atomic_audio_but_pending_job_is_still_serviced(self):
        for state in ('active','unavailable'):
            c=self.gpt_case('larisa',True)
            c['steps'] += [{'kind':'guard','at':'bridge.open','state':state}]
            c['bridge'].update(poll={})
            c['expected']={'action_counts':{'open':1,'close':0},'modes':['LARISA_MODE'],'cleanup_confirmed':False}
            r=self.run_loop(c);self.assertEqual(r['result'],'PASS',r['errors'])
            self.assertFalse(any(e['event']=='recognizer_input' and e['role']=='control' for e in r['events']))

    def test_retry_and_ordinary_cooldown_without_queue_policy_changes(self):
        c=copy.deepcopy(self.case);c['duration']=8;c['steps'][1]['clip']='unknown'
        # response occurrence 1 is greeting; occurrence 2 is UNKNOWN retry.
        c['steps'].append({'clip':'time','at':'response.playback_end','occurrence':2,'offset_ms':800})
        r=self.run_loop(c);self.assertEqual(r['result'],'PASS',r['errors'])
        finals=[e for e in r['events'] if e['event']=='local_asr_final']
        self.assertIsNone(finals[0]['intent']);self.assertEqual(finals[-1]['intent'],'TIME')
        c['steps'][-1]['offset_ms']=100;c['expected']['local_intents']=[]
        r=self.run_loop(c);self.assertEqual(r['result'],'PASS',r['errors'])

    def test_repeated_wake_cycles_get_new_boundaries(self):
        c=copy.deepcopy(self.case);c['duration']=12
        c['steps'] += [{'clip':'wake','at':'waiting','occurrence':2,'offset_ms':200},
                       {'clip':'time','at':'wake_ack.playback_end','occurrence':2,'offset_ms':100}]
        c['expected'].update(local_intents=['TIME','TIME'],wake_count=2)
        r=self.run_loop(c);self.assertEqual(r['result'],'PASS',r['errors'])
        boundaries=[e['boundary'] for e in r['events'] if e['event']=='playback_handoff']
        self.assertEqual(len(boundaries),2);self.assertLess(boundaries[0],boundaries[1])

    def test_unknown_bridge_and_system_fail_without_real_effect(self):
        c=self.gpt_case();c['bridge']={}
        r=self.run_loop(c);self.assertEqual(r['result'],'FAIL')
        self.assertIn('Unexpected fake Bridge',r['errors'][0])
        c=copy.deepcopy(self.case);c['system_actions']=[]
        r=self.run_loop(c);self.assertEqual(r['result'],'FAIL')
        self.assertIn('Unexpected fake system',r['errors'][0])

    def test_no_pcm_or_absolute_corpus_in_result(self):
        r=self.run_loop();encoded=json.dumps(r)
        self.assertNotIn(str(self.root),encoded);self.assertNotIn('pcm',encoded.lower())
        self.assertEqual(r['asr'],'scripted test double')

    def test_wake_reset_cannot_reuse_backend_feature_context_after_time(self):
        # Models the real Vosk reset experiment: Reset discards the decoder
        # result but retains old acoustic context. A fresh instance does not.
        original = MarkerRecognizer
        class RetainingRecognizer(original):
            def AcceptWaveform(self, pcm):
                if self.role == 'wake':
                    if 1000 in set(array('h',pcm)):
                        self.old_wake = True
                    if getattr(self,'old_wake',False):
                        self.text='михаил'
                        return True
                return super().AcceptWaveform(pcm)
        with wave.open(str(self.root/'time.wav'),'wb') as out:
            out.setnchannels(1);out.setsampwidth(2);out.setframerate(16000)
            out.writeframes(array('h',[n for n in [2000,2100]+[900]*35 for _ in range(1600)]).tobytes())
        self.write_manifest()
        with patch(__name__+'.MarkerRecognizer',RetainingRecognizer):
            r=self.run_loop()
        self.assertEqual(r['local_intents'],['TIME'])
        self.assertEqual(r['result'],'PASS',r['errors'])

    def test_expected_not_derived_from_matcher(self):
        c=copy.deepcopy(self.case);c['expected']['local_intents']=['DATE']
        r=self.run_loop(c);self.assertEqual(r['result'],'FAIL');self.assertEqual(r['local_intents'],['TIME'])

    def test_missing_onset_not_guessed(self):
        c=copy.deepcopy(self.case);c['steps'][1]['align']='speech_onset'
        r=self.run_loop(c);self.assertEqual(r['result'],'NOT RUN')
        r=self.run_loop(c,{'time':{'speech_onset_frame':0,'verified':False}})
        self.assertEqual(r['result'],'NOT RUN')
        r=self.run_loop(c,{'time':{'speech_onset_frame':0,'verified':True}})
        self.assertEqual(r['result'],'PASS',r['errors'])

    def test_validator_reused_and_silence_preserved(self):
        with patch('audio_replay.validate_wav',wraps=asr.validate_wav) as validate:
            corpus=asr.load_corpus(self.root)
        self.assertEqual(validate.call_count,len(self.clips))
        _,data=loop.read_clips(corpus,self.case,{})
        self.assertEqual(len(data['wake']),9600);self.assertEqual(data['wake'][:3200],b'\0'*3200)
        self.clips[0]['speech_onset_frame']=1600;self.write_manifest()
        self.assertEqual(self.corpus.clips[0]['speech_onset_frame'],1600)

    def test_manifest_code_paths_and_unknown_actions_rejected(self):
        mutations=[lambda c:c.update(code='print(1)'),lambda c:c['steps'][0].update(clip='../secret.wav'),
                   lambda c:c.update(bridge={'shell':{}}),lambda c:c.update(system_actions=['EXEC']),
                   lambda c:c['steps'][0].update(at='eval(1)'),lambda c:c.update(timing={'callback_delay_ms':-1})]
        for mutate in mutations:
            c=copy.deepcopy(self.case);mutate(c)
            with self.assertRaises(asr.CorpusError):loop.validate_scenarios({'schema_version':1,'scenarios':[c]},self.corpus)

    def test_file_tampering_after_validation_rejected(self):
        (self.root/'time.wav').write_bytes(b'bad')
        with self.assertRaisesRegex(ReplayError,'WAV changed'):loop.read_clips(self.corpus,self.case,{})


if __name__=='__main__':unittest.main()
