"""Virtual timing/environment tests: no real ASR, devices, processes or sleep."""
import json
import unittest
from array import array
from unittest.mock import Mock, patch
import main
import gpt_modes
from replay_support import Scheduler, ReplayError, EventSink, ReplayRuntime, FakeStream


class EmptyRecognizer:
    def __init__(self, *args): pass
    def Reset(self): pass
    def AcceptWaveform(self, pcm): return False
    def PartialResult(self): return '{"partial":""}'
    def Result(self): return '{"text":""}'


def runtime(**values):
    case = {'steps': [], 'expected': {}, 'duration': 10, **values}
    return ReplayRuntime(case, {}, None, EmptyRecognizer, require_vosk=False)


class SchedulerTests(unittest.TestCase):
    def test_stable_order_including_nested_events(self):
        s, seen = Scheduler(), []
        def first():
            seen.append(1)
            s.schedule(1, lambda: seen.append(3))
        s.schedule(1, first); s.schedule(1, lambda: seen.append(2))
        s.advance_to(1)
        self.assertEqual(seen, [1, 2, 3])
        s.advance_by(.5); self.assertEqual(s.now, 1.5)

    def test_limits_and_backwards(self):
        s = Scheduler(max_time=2, max_events=2)
        for t in (-1, 3, float('nan')):
            with self.assertRaises(ReplayError): s.schedule(t, lambda: None)
        s.schedule(1, lambda: None); s.schedule(1, lambda: None)
        with self.assertRaises(ReplayError): s.schedule(1, lambda: None)
        s.advance_to(1)
        with self.assertRaises(ReplayError): s.advance_to(0)
        with self.assertRaises(ReplayError): s.advance_to(3)

    def test_self_rescheduling_cannot_run_forever(self):
        s = Scheduler(max_events=10)
        def repeat(): s.schedule(s.now, repeat)
        s.schedule(0, repeat)
        with self.assertRaises(ReplayError): s.advance_to(1)

    def test_callback_delivery_does_not_shift_capture_or_pcm(self):
        rt = runtime(timing={'callback_delay_ms': 250})
        raw = array('h', [0]*800+[1234]*800).tobytes()
        rt.segments = [(0, raw)]
        seen=[]
        def callback(data, frames, timing, status):
            seen.append((rt.monotonic(), main.CapturedPCM.from_callback(data,frames,16000,timing)))
        with FakeStream(rt, callback): rt.scheduler.advance_to(.35)
        self.assertEqual(len(seen), 1)
        arrival, capture = seen[0]
        self.assertAlmostEqual(arrival,.35)
        self.assertEqual((capture.start,capture.end),(10000.,10000.1))
        self.assertEqual(capture.pcm,raw)

    def test_shorter_transition_delay_does_not_shorten_other_main_stall(self):
        rt=runtime(timing={'main_loop_delay_after_playback_ms':200},
                   steps=[{'kind':'main_delay','at':'wake_ack.playback_end','duration_ms':2000}])
        rt.anchor('wake_ack.playback_end',1)
        rt.scheduler.advance_to(1)
        self.assertEqual(rt.blocked_until,3)

    def test_main_delay_keeps_callbacks_running_and_real_queue_overflows(self):
        rt=runtime(); q=main.FreshAudioQueue(maxsize=12)
        def cb(data, frames, timing, status):
            q.put_latest((rt.monotonic(),main.CapturedPCM.from_callback(data,frames,16000,timing)))
        with FakeStream(rt,cb):
            rt.blocked_until=1.5; rt.advance_main(.01)
            self.assertEqual(q.take_dropped(),3)
            first=q.get_nowait()
            self.assertTrue(first[2])
            self.assertAlmostEqual(first[1].start,10000.3)
            self.assertEqual(len(q.items),11)


class RuntimeTests(unittest.TestCase):
    def test_default_dependencies_are_late_bound_production(self):
        rt=main.ListenRuntime()
        with patch('main.time.monotonic', return_value=123), patch('main.Speech') as speech, patch('main.command') as command:
            self.assertEqual(rt.monotonic(),123)
            rt.speech({});speech.assert_called_once_with({})
            rt.execute('время',{},None,'TIME',None);command.assert_called_once()

    def test_gpt_default_and_injected_clocks_do_not_mix(self):
        args=(main.config(),Mock(),Mock(),Mock(),Mock())
        with patch('gpt_modes.time.monotonic',return_value=321):
            default=gpt_modes.GPTModes(*args)
            self.assertEqual(default.clock(),321)
        fake=gpt_modes.GPTModes(*args,clock=lambda:7)
        with patch('gpt_modes.time.monotonic',side_effect=AssertionError('real clock')):
            fake.start(gpt_modes.LARISA_MODE)
            fake.tick(7)
            fake.step(b'\0'*3200,7)
            self.assertEqual(fake.clock(),7)

    def test_integration_disallows_fake_recognizer(self):
        with self.assertRaisesRegex(ReplayError,'real Vosk'):
            ReplayRuntime({'steps':[]},{},None,EmptyRecognizer)

    def test_fake_speech_boundary_is_independent_of_main_poll(self):
        rt=runtime(ack_duration_ms=100)
        rt.tts.say('Слушаю',completion_clock=lambda:10000+rt.monotonic())
        rt.scheduler.advance_to(1)
        self.assertFalse(rt.tts.busy())
        self.assertAlmostEqual(rt.tts.playback_end,10000.2)
        rt.tts.say('Слушаю',completion_clock=lambda:10000+rt.monotonic())
        self.assertIsNone(rt.tts.playback_end)
        rt.tts.stop();rt.scheduler.advance_to(2)
        self.assertIsNone(rt.tts.playback_end)

    def test_unknown_system_and_bridge_actions_fail_closed(self):
        rt=runtime()
        for action in ('send','shell','open'):
            with self.assertRaises(ReplayError):rt.br.submit(action)
        with self.assertRaises(ReplayError):rt.execute('время',{},None,'TIME',None)

    def test_close_is_idempotent_and_late_future_is_real(self):
        rt=runtime(bridge={'open':{'delay_ms':5000,'late_result':True},'close':{'delay_ms':1000}})
        opening=rt.br.submit('open');close=rt.br.submit('close')
        self.assertIs(close,rt.br.submit('close'))
        with self.assertRaises(ReplayError):rt.br.submit('open')
        rt.scheduler.advance_to(1)
        self.assertTrue(close.result()['ok']);self.assertFalse(opening.done())
        rt.br.submit('open')
        rt.scheduler.advance_to(5)
        self.assertTrue(opening.result()['ok'])
        self.assertEqual(rt.br.history,['open','close','open'])
        self.assertTrue(any(e.get('late') for e in rt.sink.events))

    def test_observer_refuses_pcm_and_sensitive_fields(self):
        sink=EventSink(lambda:0)
        for values in ({'pcm':b'x'}, {'audio':'x'},{'tokens':'x'}, {'unknown':object()}):
            with self.assertRaises(ReplayError):sink.emit('bad',**values)
        sink.emit('local_asr_final',text='время',intent='TIME')
        self.assertNotIn('pcm',json.dumps(sink.events))


if __name__=='__main__':unittest.main()
