import json
import unittest
from unittest.mock import patch, Mock
from concurrent.futures import Future
import main
from chatgpt_bridge import Bridge, OutputGuard


class ChatGPTTests(unittest.TestCase):
    def test_intents(self):
        for intent in ('CHATGPT_OPEN', 'CHATGPT_CLOSE'):
            for phrase in main.INTENTS[intent]:
                self.assertEqual(main.match_intent(phrase)[0], intent)
        self.assertEqual(main.match_intent('позови чат ж п т')[0], 'CHATGPT_OPEN')
        for text in ('не открывай chatgpt', 'открой chatgpt закрой firefox', 'не вернись', 'расскажи про чат жпт'):
            self.assertIsNone(main.match_intent(text)[0])

    @patch('chatgpt_bridge.subprocess.run')
    def test_guard(self, run):
        for state, expected in [('running', False), ('idle', True)]:
            run.return_value.stdout = json.dumps([{'info': {'props': {'media.class': 'Audio/Sink'}}}, {'info': {'state': state, 'props': {'media.class': 'Stream/Output/Audio'}}}])
            self.assertEqual(OutputGuard.quiet(), expected)
        run.side_effect = OSError('unavailable')
        self.assertFalse(OutputGuard.quiet())

    def test_guard_tail_and_disconnect(self):
        guard = OutputGuard()
        guard.started = True
        guard.process = Mock()
        guard.process.poll.return_value = None
        guard.update([{'id': 1, 'info': {'props': {'media.class': 'Audio/Sink'}}},
                      {'id': 2, 'info': {'state': 'running', 'props': {'media.class': 'Stream/Output/Audio'}}}], 10)
        self.assertFalse(guard.allows(10))
        guard.update([{'id': 2, 'info': {'state': 'idle'}}], 11)
        self.assertFalse(guard.allows(12))
        self.assertTrue(guard.allows(13.1))
        guard.update([{'id': 2, 'info': {'state': 'running'}}], 14)
        self.assertFalse(guard.allows(14))
        guard.process.poll.return_value = 1
        self.assertFalse(guard.allows(20))

    @patch('chatgpt_bridge.subprocess.run', side_effect=OSError('no bus'))
    @patch('chatgpt_bridge.subprocess.Popen')
    def test_no_false_success(self, popen, run):
        popen.return_value.wait.return_value = 0
        self.assertFalse(Bridge.perform('open')['ok'])
        self.assertEqual(popen.call_args.args[0], ['firefox', '--new-tab', 'https://chatgpt.com/'])
        popen.reset_mock()
        self.assertFalse(Bridge.perform('close')['ok'])
        popen.assert_not_called()



class ModeTests(unittest.TestCase):
    def run_mode(self, close_ok):
        clock = [0.0]
        ends = [0.0]
        speech = Mock(last='ответ')
        def say(text, remember=True):
            ends[0] = clock[0] + .3
        speech.say.side_effect = say
        speech.busy.side_effect = lambda: clock[0] < ends[0]
        controls = iter(['громче', 'стоп', 'вернись', 'михаил борисович', 'вернись'])
        class Rec:
            def __init__(self, model, rate, grammar):
                words = json.loads(grammar)
                self.kind = 'wake' if 'михаил' in words else ('control' if len(words) < 10 else 'command')
            def Reset(self):
                pass
            def AcceptWaveform(self, data):
                return True
            def Result(self):
                return json.dumps({'text': {'wake': 'михаил', 'command': 'позови чат ж п т'}.get(self.kind, next(controls, 'тишина') if self.kind == 'control' else '')})
        class Stream:
            def __init__(self, **kwargs):
                self.callback = kwargs['callback']
            def __enter__(self): return self
            def __exit__(self, *args): pass
            @property
            def active(self):
                clock[0] += .1
                self.callback(b'\xff\x7f' * 1600, 1600, None, None)
                return True
        bridge = Mock()
        def submit(action):
            # Opening must occur after the announcement and its echo cooldown.
            if action == 'open':
                assert clock[0] >= ends[0] + .5
            future = Future(); future.set_result({'ok': close_ok if action == 'close' else True, 'detail': action}); return future
        bridge.submit.side_effect = submit
        with patch('main.dependencies', return_value=(Mock(RawInputStream=Stream), Mock(), Rec)), patch('main.load_model'), patch('main.Speech', return_value=speech), patch('main.Bridge', return_value=bridge), patch('main.OutputGuard') as guard, patch('main.time.monotonic', side_effect=lambda: clock[0]), patch('main.command') as command:
            guard.return_value.allows.return_value = True
            guard.return_value.quiet.return_value = True
            main.listen(main.config(), duration=4.5)
        command.assert_not_called()
        self.assertEqual([c.args[0] for c in bridge.submit.call_args_list][:2], ['open', 'close'])
        self.assertEqual('Я снова слушаю.' in [c.args[0] for c in speech.say.call_args_list], close_ok)

    def test_mode_isolation_and_return(self):
        self.run_mode(True)

    def test_failed_close_does_not_restore_local_commands(self):
        self.run_mode(False)


if __name__ == "__main__":
    unittest.main()
