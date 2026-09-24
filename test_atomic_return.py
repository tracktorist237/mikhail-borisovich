"""Control recognition through real GPTModes; no browser or audio hardware."""
from concurrent.futures import Future
import json
import unittest
from unittest.mock import patch

import test_chatgpt as fixtures
from chatgpt_bridge import LOCAL_MODE, LARISA_MODE, ANTON_MODE


class AtomicReturnTests(unittest.TestCase):
    def make(self, mode=ANTON_MODE, phase='opening', action='open_anton'):
        self.fixture = fixtures.ModeTests()
        self.g = self.fixture.make(mode)
        self.g.phase = phase
        self.old, self.close = Future(), Future()
        self.g.bridge.submit.side_effect = lambda action: self.close if action == 'close' else Future()
        if action:
            self.g.submit(action)
            self.old = self.g.job
        self.g.bridge.submit.reset_mock()
        return self.g

    def feed(self, text, final=True):
        self.fixture.pending_text = text
        self.g.rec.AcceptWaveform.return_value = final
        self.g.rec.PartialResult.return_value = json.dumps({'partial': text})
        self.g.step(fixtures.pcm(1000), .1, .1)

    def finish(self):
        self.close.set_result({'ok': True, 'detail': 'all owned descendants reaped'})
        self.g.tick(.2)
        self.assertEqual(self.g.mode, LOCAL_MODE)
        self.g.tick(.3)
        self.g.tick(2)
        self.assertFalse(self.g.active)
        self.g.bridge.submit.assert_called_once_with('close')

    def test_pending_open_both_modes_late_success_cannot_revive(self):
        for mode, action in ((ANTON_MODE, 'open_anton'), (LARISA_MODE, 'open')):
            with self.subTest(mode=mode):
                self.make(mode, action=action)
                self.feed('Михаил, вернись!')
                self.assertEqual(self.g.phase, 'closing')
                self.assertTrue(self.g.active)
                self.assertFalse(self.g.closed_confirmed)
                self.old.set_result({'ok': True, 'detail': 'late open'})
                self.g.tick(.15)
                self.assertEqual(self.g.phase, 'closing')
                self.finish()
                self.g.start(mode)
                self.assertEqual(self.g.phase, 'speaking')
                self.assertFalse(self.g.closed_confirmed)

    def test_completed_open_or_poll_cannot_run_before_atomic_cancel(self):
        for phase, action in (('opening', 'open_anton'), ('transcribing', 'poll'), ('reply', 'poll')):
            with self.subTest(phase=phase):
                self.make(phase=phase, action=action)
                self.old.set_result({'ok': True, 'detail': 'late', 'composer': 'fixture',
                                     'send': True, 'recording': False, 'reply_ready': True, 'answer': 'private'})
                self.feed('михаил борисович вернись')
                self.g.bridge.submit.assert_called_once_with('close')
                self.g.speech.say.assert_not_called()
                self.finish()

    def test_all_active_phases_close_without_send_or_abort(self):
        for phase, action in (('starting_dictation', 'dictate'), ('dictating', None),
                              ('transcribing', None), ('sending', 'send'), ('reply', None),
                              ('voice', None), ('pausing', 'pause'), ('resuming', 'resume'),
                              ('control', None)):
            with self.subTest(phase=phase):
                self.make(phase=phase, action=action)
                with patch('builtins.print') as log:
                    self.feed('михаил вернись')
                self.g.bridge.submit.assert_called_once_with('close')
                if action == 'send':
                    self.assertIn('outcome is unknown', str(log.call_args_list))
                self.feed('михаил вернись')
                self.finish()

    def test_prefix_partial_does_not_swallow_long_atomic_phrase(self):
        self.make(phase='voice', action=None)
        self.feed('михаил борисович', final=False)
        self.g.bridge.submit.assert_not_called()
        self.feed('михаил борисович вернись')
        self.g.bridge.submit.assert_called_once_with('close')

    def test_exact_phrases_only_and_guard_still_blocks(self):
        for text in ('михаил', 'вернись', 'не михаил вернись', 'михаила вернись',
                     'михаил вернись завтра', 'михаил борисович не вернись'):
            with self.subTest(text=text):
                self.make(phase='voice', action=None)
                self.feed(text)
                self.g.bridge.submit.assert_not_called()
        self.make()
        self.g.guard.allows.return_value = False
        self.feed('михаил вернись')
        self.g.rec.AcceptWaveform.assert_not_called()
        self.g.bridge.submit.assert_not_called()

    def test_existing_two_step_still_pauses(self):
        self.make(phase='dictating', action=None)
        self.feed('михаил борисович')
        self.g.bridge.submit.assert_called_once_with('abort_dictation')

    def test_stale_and_tts_frames_never_cancel(self):
        for phase in ('speaking', 'cooldown', 'opening'):
            self.make(phase=phase)
            self.g.after_speech = 'open_anton'
            self.g.speech.busy.return_value = True
            self.fixture.pending_text = 'михаил вернись'
            self.g.step(fixtures.pcm(1000), -1, .1)
            self.g.rec.AcceptWaveform.assert_not_called()
            self.g.bridge.submit.assert_not_called()

    def test_new_start_before_cleanup_is_rejected(self):
        self.make()
        self.feed('михаил вернись')
        with self.assertRaises(RuntimeError):
            self.g.start(LARISA_MODE)
        self.assertEqual(self.g.phase, 'closing')

    def test_slow_recognizer_result_cannot_cancel(self):
        self.make()
        self.fixture.pending_text = 'михаил вернись'
        with patch('gpt_modes.time.monotonic', side_effect=[.1, 1.]):
            self.g.step(fixtures.pcm(1000), .1)
        self.g.bridge.submit.assert_not_called()

    def test_old_open_finishes_only_after_new_session_started(self):
        self.make()
        self.feed('михаил вернись')
        self.finish()
        self.g.start(LARISA_MODE)
        self.old.set_result({'ok': True, 'detail': 'late old open'})
        self.g.tick(3)
        self.g.tick(4)
        self.assertEqual(self.g.action, 'open')
        self.assertEqual(self.g.phase, 'opening')
        self.assertEqual(self.g.mode, LOCAL_MODE)
        self.assertEqual([c.args[0] for c in self.g.bridge.submit.call_args_list], ['close', 'open'])
