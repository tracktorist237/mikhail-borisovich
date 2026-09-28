"""Debug tracing and delivery invariants; synthetic inputs only."""
import copy
import json
import unittest
import wave
from array import array
from unittest.mock import patch
import main
import loop_replay
from replay_support import FakeStream, ReplayError
from test_replay_support import runtime
import test_loop_replay as fixtures
from test_loop_replay import MarkerRecognizer


class TraceTests(unittest.TestCase):
    def fixture(self):
        fixture=fixtures.LoopTests('test_normal_open_uses_production_modes_no_early_send')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        return fixture

    def test_exact_time_endpoint_not_rerouted_to_wake(self):
        f=self.fixture()
        # A new non-wake input after TIME forces lazy creation of the next
        # wake backend; pure silence would correctly never instantiate it.
        with wave.open(str(f.root/'silence.wav'),'wb') as out:
            out.setnchannels(1);out.setsampwidth(2);out.setframerate(16000)
            out.writeframes(array('h',[900]*16000).tobytes())
        f.write_manifest()
        f.case['steps'].append({'clip':'silence','at':'clip.time.end','offset_ms':2000})
        result=loop_replay.run_case(f.corpus,f.case,{},None,MarkerRecognizer,require_vosk=False,debug_trace=True)
        self.assertEqual(result['result'],'PASS',result['errors'])
        trace=result['debug_trace']
        endpoint=next(e for e in trace if e['event']=='asr_result' and e['role']=='command' and e['text']=='который час')
        block=endpoint['blocks'][-1]
        self.assertFalse(any(e['event']=='asr_input' and e['role']=='wake' and block in e['blocks'] for e in trace))
        delivered=[e['block'] for e in trace if e['event']=='callback_delivery']
        self.assertEqual(len(delivered),len(set(delivered)))
        created=[e['recognizer'] for e in trace if e['event']=='asr_created' and e['role']=='wake']
        self.assertGreaterEqual(len(created),3)
        self.assertEqual(len(created),len(set(created)))
        resets=[e['generation'] for e in trace if e['event']=='asr_reset' and e['role']=='wake']
        self.assertEqual(resets,sorted(set(resets)))
        json_trace=json.dumps(trace)
        self.assertNotIn('pcm',json_trace.lower());self.assertNotIn(str(f.root),json_trace)
        self.assertIn('cooldown',{e['state'] for e in trace if e['event']=='state_transition'})

    def test_debug_is_observational(self):
        f=self.fixture()
        a=loop_replay.run_case(f.corpus,f.case,{},None,MarkerRecognizer,require_vosk=False)
        b=loop_replay.run_case(f.corpus,f.case,{},None,MarkerRecognizer,require_vosk=False,debug_trace=True)
        b.pop('debug_trace')
        self.assertEqual(a,b)

    def test_source_fragments_cover_unaligned_clip_once(self):
        f=self.fixture();case=copy.deepcopy(f.case);case['steps'][1]['offset_ms']=50
        r=loop_replay.run_case(f.corpus,case,{},None,MarkerRecognizer,require_vosk=False,debug_trace=True)
        self.assertEqual(r['result'],'PASS')
        seen=set()
        for event in r['debug_trace']:
            if event['event']!='capture':continue
            for source in event['sources']:
                if source['clip']!='time':continue
                frames=set(range(source['source_start_frame'],source['source_end_frame']))
                self.assertFalse(seen & frames);seen |= frames
        self.assertEqual(seen,set(range(4800)))

    def test_duplicate_delivery_fails(self):
        rt=runtime();rt.check_delivery(1)
        with self.assertRaisesRegex(ReplayError,'Duplicate'):rt.check_delivery(1)

    def test_duplicate_source_range_fails_but_explicit_repeat_allowed(self):
        rt=runtime()
        rt.source_spans=[(0,'clip',0,1600),(0,'clip',1600,1600)]
        rt.check_delivery(0)
        with self.assertRaisesRegex(ReplayError,'source PCM'):rt.check_delivery(1)
        rt=runtime()
        rt.source_spans=[(0,'clip',0,1600),(1,'clip',1600,1600)]
        rt.check_delivery(0);rt.check_delivery(1)
        self.assertEqual(len(rt.delivered_source_ranges),2)

    def test_lazy_wake_reset_recreates_backend_without_reloading_model(self):
        from unittest.mock import Mock
        factory=Mock(side_effect=lambda:Mock())
        wake=main.FreshWakeRecognizer(factory)
        old=wake.rec
        wake.Reset();wake.Reset()
        self.assertEqual(factory.call_count,1)
        wake.AcceptWaveform(b'\0'*3200)
        self.assertEqual(factory.call_count,2)
        self.assertIsNot(wake.rec,old)
        old.AcceptWaveform.assert_not_called()

    def test_closed_stream_does_not_deliver_pending_callback(self):
        rt=runtime(timing={'callback_delay_ms':250});seen=[]
        with FakeStream(rt,lambda *a:seen.append(a)):
            rt.scheduler.advance_to(.1)
        rt.scheduler.advance_to(1)
        self.assertEqual(seen,[])

    def test_fake_speech_cannot_generate_microphone_pcm_or_schedule_clip(self):
        rt=runtime();seen=[]
        with FakeStream(rt,lambda data,*a:seen.append(data)):
            rt.tts.say('Михаил. Слушаю. Ответ времени.')
            rt.scheduler.advance_to(1)
        self.assertEqual(rt.segments,[])
        self.assertTrue(seen);self.assertTrue(all(not any(data) for data in seen))


if __name__=='__main__':unittest.main()
