import json
import unittest
from array import array
from concurrent.futures import Future
from unittest.mock import Mock, patch
import main
from main import FreshAudioQueue
from chatgpt_bridge import Bridge, LOCAL_MODE, LARISA_MODE, ANTON_MODE
from speaker_activity import Activity, OutputGuard
from gpt_modes import GPTModes
from chatgpt_ui import ChatGPTUI, UIError


def pcm(value=0):
    return array('h',[value]*1600).tobytes()


class ChatGPTTests(unittest.TestCase):
    def test_audio_queue_keeps_fresh_frame_and_marks_gap(self):
        audio = FreshAudioQueue(maxsize=2)
        audio.put_latest((1, b'old-1'))
        audio.put_latest((2, b'old-2'))
        audio.put_latest((3, b'fresh'))
        self.assertEqual(audio.take_dropped(), 1)
        self.assertEqual(audio.get(timeout=0), (2, b'old-2', True))
        self.assertEqual(audio.get(timeout=0), (3, b'fresh', False))

    def test_audio_gap_resets_unfinished_recognition(self):
        g = ModeTests().make(ANTON_MODE)
        g.mic_tail = 12
        g.preroll.append(b'partial')
        g.audio_gap(10)
        self.assertEqual(g.mic_tail, 0)
        self.assertFalse(g.preroll)
        g.rec.Reset.assert_called()

    def test_blocked_guard_still_services_poll_timer(self):
        g = ModeTests().make(ANTON_MODE)
        g.phase = 'reply'
        g.poll_after = 0
        g.guard.allows.return_value = False
        g.tick(1)
        self.assertIn('poll', [call.args[0] for call in g.bridge.submit.call_args_list])

    def test_permanent_guard_failure_closes_gpt(self):
        g = ModeTests().make(ANTON_MODE)
        g.guard.failed = True
        g.tick(1)
        self.assertIn('close', [call.args[0] for call in g.bridge.submit.call_args_list])

    def test_intents(self):
        for intent in ('CHATGPT_OPEN','CHATGPT_CLOSE','LARISA_OPEN','ANTON_OPEN'):
            for phrase in main.INTENTS[intent]:
                self.assertEqual(main.match_intent(phrase)[0],intent)
        self.assertEqual(main.match_intent('позови чат ж п т')[0],'CHATGPT_OPEN')
        for phrase in ('не открывай chatgpt','открой chatgpt закрой firefox','не вернись',
                       'расскажи про чат жпт','не позови ларису','лариса время',
                       'антон павлович громче','открой ларису закрой браузер'):
            self.assertIsNone(main.match_intent(phrase)[0],phrase)

    def test_guard(self):
        # Replaces obsolete running-stream assertions: silent PCM must unblock
        # even while Firefox keeps a running output stream.
        activity=Activity()
        for i in range(10):activity.update(pcm(),i*.1)
        self.assertTrue(activity.allows(.9))
        activity.update(pcm(200),1)
        self.assertFalse(activity.allows(1))
        activity.update(pcm(),1.1)
        self.assertFalse(activity.allows(1.2))

    def test_guard_tail_and_disconnect(self):
        activity=Activity()
        activity.update(pcm(500),0)
        for i in range(1,9):activity.update(pcm(),i*.1)
        self.assertFalse(activity.allows(.8))
        activity.update(pcm(),.91)
        self.assertTrue(activity.allows(.91))
        self.assertFalse(activity.allows(1.5))
        activity.update(pcm(),2)
        self.assertFalse(activity.allows(2))
        spike=array('h',[0]*1599+[10000]).tobytes()
        activity.update(spike,2.1)
        self.assertTrue(activity.active)

    def test_no_false_success(self):
        # Opening Firefox alone is not Voice success.
        ui=ChatGPTUI()
        ui.open=Mock()
        ui.click=Mock()
        ui.wait=Mock(side_effect=UIError('no active microphone'))
        with self.assertRaises(UIError):ui.start_voice()
        from browser_worker import perform
        fake_ui=Mock()
        fake_ui.start_voice.side_effect=UIError('unavailable')
        self.assertFalse(perform(fake_ui, 'open')['ok'])

    def test_monitor_failure_is_closed(self):
        with patch('speaker_activity.subprocess.Popen',side_effect=OSError('unavailable')):
            guard=OutputGuard()
            self.assertFalse(guard.allows(0))
            guard.close()

    def test_monitor_recovery_is_bounded(self):
        with patch('speaker_activity.subprocess.Popen', side_effect=OSError('missing')) as popen:
            guard = OutputGuard({'speaker_restart_limit': 2, 'speaker_restart_delay': 0})
            guard.start = Mock()
            for now in (0, 1, 2, 100):
                guard._service(now)
                self.assertFalse(guard.allows(now))
            self.assertTrue(guard.failed)
            self.assertEqual(popen.call_count, 2)
            guard.close()

    def test_monitor_recovers_after_eof(self):
        from test_resilience import FakeProcess
        first, second = FakeProcess(), FakeProcess()
        self.addCleanup(first.cleanup)
        self.addCleanup(second.cleanup)
        first.eof()
        with patch('speaker_activity.subprocess.Popen', side_effect=[first, second]) as popen:
            guard = OutputGuard({'speaker_restart_limit': 3, 'speaker_restart_delay': 0})
            guard.start = Mock()
            self.addCleanup(lambda: guard._retire('test cleanup', 10))
            guard._service(0)
            guard._service(.1)
            guard._service(1)
            self.assertEqual(popen.call_count, 2)
            second.write(pcm())
            guard._service(1.1)
            self.assertIsNotNone(guard.last_data)


class ModeTests(unittest.TestCase):
    def make(self, mode=LARISA_MODE, close_ok=True):
        self.now=0
        self.pending_text=''
        self.speech=Mock()
        self.speech.busy.return_value=False
        self.guard=Mock()
        self.guard.allows.return_value=True
        self.rec=Mock()
        self.rec.AcceptWaveform.return_value=True
        self.rec.Result.side_effect=lambda:json.dumps({'text':self.pending_text})
        self.bridge=Mock()
        self.results={}
        def submit(action):
            result=self.results.get(action,{'ok':close_ok if action=='close' else True,'detail':action})
            f=Future();f.set_result(result);return f
        self.bridge.submit.side_effect=submit
        self.g=GPTModes(main.config(),self.speech,self.bridge,self.guard,self.rec)
        self.g.target_mode=self.g.mode=mode
        self.g.phase='voice' if mode==LARISA_MODE else 'dictating'
        self.g.deadline=100
        self.g.poll_after=100
        self.g.accept_after=-1
        return self.g

    def tick(self,text='',value=0,dt=.1):
        self.now+=dt;self.pending_text=text
        self.g.step(pcm(value or (1000 if text else 0)),self.now,self.now)

    def finish_speech(self):
        self.tick();self.tick(dt=1)

    def return_local(self):
        self.tick('михаил борисович')
        self.tick()  # pause completed, says Слушаю
        self.finish_speech()
        self.tick('вернись')
        self.tick()
        self.finish_speech()

    def run_mode(self,close_ok):
        for mode in (LARISA_MODE,ANTON_MODE):
            self.make(mode,close_ok)
            with patch('main.command') as command:
                for text in ('громче','стоп','вернись','открой браузер'):
                    self.tick(text)
                self.bridge.submit.assert_not_called()
                self.return_local()
                command.assert_not_called()
            self.assertIn('Слушаю',[c.args[0] for c in self.speech.say.call_args_list])
            self.assertEqual(self.g.mode==LOCAL_MODE,close_ok)
            self.assertEqual('Я снова слушаю.' in [c.args[0] for c in self.speech.say.call_args_list],close_ok)
            self.assertEqual(self.g.active,not close_ok)

    def test_mode_isolation_and_return(self):self.run_mode(True)
    def test_failed_close_does_not_restore_local_commands(self):self.run_mode(False)

    def test_all_return_phrases(self):
        for text in ('вернись','закрой ларису','закрой антона павловича','закончи разговор','стоп'):
            self.make();self.g.phase='control';self.g.deadline=100
            self.tick(text)
            self.bridge.submit.assert_called_once_with('close')

    def test_open_only_after_announcement_and_confirmation(self):
        for mode,action in ((LARISA_MODE,'open'),(ANTON_MODE,'open_anton')):
            self.make(mode);self.g.phase='idle';self.g.mode=LOCAL_MODE
            self.g.start(mode)
            self.bridge.submit.assert_not_called()
            self.finish_speech()
            self.bridge.submit.assert_called_once_with(action)
            self.assertEqual(self.g.mode,LOCAL_MODE)
            self.tick()
            self.assertEqual(self.g.mode,mode)

    def test_speaker_and_rhvoice_never_reach_recognizer(self):
        self.make();self.guard.allows.return_value=False
        self.tick('михаил борисович');self.rec.AcceptWaveform.assert_not_called()
        self.guard.allows.return_value=True
        self.g.say('ответ','control',self.now)
        self.tick('михаил борисович');self.rec.AcceptWaveform.assert_not_called()

    def test_wake_during_pending_reply_cancels_before_ack(self):
        self.make(ANTON_MODE)
        f=Future();self.g.job=f;self.g.action='poll';self.g.phase='reply'
        self.tick('михаил борисович')
        f.set_result({'ok':True,'detail':'','reply_ready':True,'answer':'not spoken'})
        self.tick()
        self.bridge.submit.assert_called_once_with('pause')
        self.speech.say.assert_not_called()

    def test_anton_two_turns(self):
        self.make(ANTON_MODE)
        self.g.poll_after=0
        self.results['poll']={'ok':True,'detail':'','composer':'Вопрос', 'send':True,
                              'recording':False,'reply_ready':True,'answer':'Четыре.'}
        for _ in range(2):
            self.tick(value=1000)
            self.tick(dt=1.5)
            self.tick()  # transcribe job
            self.tick(dt=1)  # poll submit
            self.tick()  # poll result -> send
            self.tick()  # send -> reply
            self.tick(dt=1)
            self.tick()
            self.finish_speech()  # response -> next_chunk -> dictate
            self.tick()
        actions=[c.args[0] for c in self.bridge.submit.call_args_list]
        self.assertEqual(actions.count('send'),2,actions)
        self.assertEqual(actions.count('dictate'),2,actions)
        self.assertEqual([c.args[0] for c in self.speech.say.call_args_list],['Четыре.','Четыре.'])


class UITests(unittest.TestCase):
    def test_production_config_arms_os_keyboard_stop(self):
        cfg = main.config()
        self.assertEqual(cfg['dictation_stop_method'], 'os-keyboard')
        ui = ChatGPTUI(cfg)
        ui.driver = Mock()
        ui.snapshot = Mock(return_value={
            'end_voice': False, 'composer': '', 'recording': True})
        ui.wait = Mock()
        ui.capture_ready = Mock(return_value=True)
        ui.click = Mock()
        ui.start_dictation()
        ui.driver.execute_script.assert_called_once_with(ui.ARM_STOP_KEY)
        ui.click.assert_called_once_with('Start dictation', 'Начать диктовку')

    def test_abort_dictation_stops_before_first_snapshot(self):
        cfg = main.config()
        ui = ChatGPTUI(cfg)
        events = []
        ui.stop_dictation = Mock(side_effect=lambda: events.append('stop'))
        ui.snapshot = Mock(side_effect=lambda: (events.append('snapshot') or {
            'recording': False, 'composer': ''}))
        ui.wait = Mock(side_effect=lambda condition, *args: condition())
        result = ui.abort_dictation()
        self.assertTrue(result['ok'])
        self.assertEqual(events[:2], ['stop', 'snapshot'])

    def test_send_requires_visible_transcription(self):
        ui=ChatGPTUI();ui.click=Mock()
        ui.snapshot=Mock(return_value={'recording':False,'composer':'','send':True,'end_voice':False})
        with self.assertRaises(UIError):ui.send()
        ui.click.assert_not_called()

    def test_streamed_or_old_answer_not_spoken(self):
        ui=ChatGPTUI();ui.before=2
        s={'answer':'Ответ','answers':2,'answer_done':True,'generating':False}
        ui.snapshot=Mock(return_value=s)
        with patch('chatgpt_ui.time.monotonic',return_value=10):
            self.assertFalse(ui.poll()['reply_ready'])
        s['answers']=3;s['generating']=True
        with patch('chatgpt_ui.time.monotonic',return_value=12):
            self.assertFalse(ui.poll()['reply_ready'])
        s['generating']=False
        with patch('chatgpt_ui.time.monotonic',return_value=13):
            self.assertTrue(ui.poll()['reply_ready'])


class AudioLoopIntegrationTests(unittest.TestCase):
    def run_mode(self, close_ok):
        clock = [0.0]
        ends = [0.0]
        speech = Mock(last='ответ')
        def say(text, remember=True, *, completion_clock=None):
            ends[0] = clock[0] + .3
            speech.playback_end = 10000 + ends[0] if completion_clock else None
        speech.say.side_effect = say
        speech.busy.side_effect = lambda: clock[0] < ends[0]
        controls = iter(['громче', 'стоп', 'вернись', 'михаил борисович', 'вернись'])
        modes = []
        class ObservedModes(GPTModes):
            def __init__(self, *args):
                super().__init__(*args)
                modes.append(self)
        class Rec:
            def __init__(self, model, rate, grammar):
                words = json.loads(grammar)
                self.kind = 'wake' if 'михаил' in words else ('control' if words == main.CONTROL_GRAMMAR else 'command')
            def Reset(self):
                pass
            def AcceptWaveform(self, data):
                return True
            def Result(self):
                return json.dumps({'text': {'wake': 'михаил', 'command': 'позови чат ж п т'}.get(self.kind, next(controls, 'тишина') if self.kind == 'control' and modes[0].phase in ('voice', 'control') else '')})
        class Stream:
            def __init__(self, **kwargs):
                self.callback = kwargs['callback']
            def __enter__(self): return self
            def __exit__(self, *args): pass
            @property
            def time(self): return 10000 + clock[0]
            @property
            def active(self):
                clock[0] += .1
                timing = Mock(inputBufferAdcTime=self.time-.1, currentTime=self.time)
                self.callback(b'\xff\x7f' * 1600, 1600, timing, None)
                return True
        bridge = Mock()
        def submit(action):
            # Opening must occur after the announcement and its echo cooldown.
            if action == 'open':
                assert clock[0] >= ends[0] + .5
            future = Future(); future.set_result({'ok': close_ok if action == 'close' else True, 'detail': action, 'end_voice': True}); return future
        bridge.submit.side_effect = submit
        with patch('main.dependencies', return_value=(Mock(RawInputStream=Stream), Mock(), Rec)), patch('main.load_model'), patch('main.GPTModes', ObservedModes), patch('main.Speech', return_value=speech), patch('main.Bridge', return_value=bridge), patch('main.OutputGuard') as guard, patch('main.time.monotonic', side_effect=lambda: clock[0]), patch('main.command') as command:
            guard.return_value.allows.return_value = True
            guard.return_value.quiet.return_value = True
            main.listen(main.config(), duration=9)
        command.assert_not_called()
        self.assertEqual([c.args[0] for c in bridge.submit.call_args_list if c.args[0] in ('open','close')][:2], ['open', 'close'])
        self.assertEqual('Я снова слушаю.' in [c.args[0] for c in speech.say.call_args_list], close_ok)

    def test_mode_isolation_and_return(self):
        self.run_mode(True)

    def test_failed_close_does_not_restore_local_commands(self):
        self.run_mode(False)



if __name__=='__main__':unittest.main()
