"""Local wake readiness with a fake clock/microphone; no devices or browser."""
import json
import unittest
import threading
from unittest.mock import Mock, patch
from array import array
import main


class LocalReadinessTests(unittest.TestCase):
    def run_local(self, *, during_playback=False, lag=0, cycles=1, retry=False,
                  pressure=False, drain_stall=0, decode_stall=0, late_followup=False,
                  first_final='не знаю'):
        # Playback, ADC and callback delivery have distinct scheduled events.
        # The PortAudio epoch deliberately differs from Python monotonic.
        clock = [0.0]
        epoch = 10000.0
        events = []
        callbacks = []
        labels = {}
        delivered = []
        observed = []
        captured_users = []
        utterances = []
        wake_count = [0]
        delayed_cycle = [0]
        ends = [0.0]
        tracked = [False]
        queue_stats = {'peak': 0, 'overflow': 0}
        stalled = [False]
        speech = Mock(last='ответ', playback_end=None)

        def later(when, action):
            events.append((when, action))
            events.sort(key=lambda event: event[0])

        def advance(seconds):
            target = clock[0] + max(0, seconds)
            while events and events[0][0] <= target:
                when, action = events.pop(0)
                clock[0] = when
                action()
            clock[0] = target

        def audio(label, start, delivery=None):
            end = start + .1
            delivery = max(end, delivery if delivery is not None else end)
            pcm = array('h', [1000 + len(labels)] * 1600).tobytes()
            labels[pcm] = label
            def deliver():
                callbacks[0](pcm, 1600, Mock(inputBufferAdcTime=epoch+start,
                                           currentTime=epoch+clock[0]), None)
            later(delivery, deliver)
            return pcm

        def user_question(end, suffix, delay=.06):
            if not during_playback:
                captured_users.append((end+delay, end+delay+.1))
                audio('first-'+suffix, end+delay)
            audio('second-'+suffix, end+delay+.11)

        def say(text, remember=True, *, completion_clock=None):
            utterances.append(text)
            tracked[0] = text == 'Слушаю'
            end = ends[0] = clock[0]+(1.45 if pressure and tracked[0] else .25)
            speech.playback_end = None
            if tracked[0]:
                wake_count[0] += 1
                suffix = str(wake_count[0])
                if pressure:
                    for i in range(14):
                        audio('tts', clock[0]+i*.1)
                # The recorder's end event happens without a main-loop poll.
                later(end, lambda: setattr(speech, 'playback_end', epoch+end))
                audio('first-'+suffix if during_playback else 'late-tts', end-.1, end+.12)
                audio('straddle', end-.04, end+.14)
                user_question(end, suffix)
                if late_followup:
                    audio('first-stale', end+.6, end+1.3)
                    audio('second-stale', end+.71, end+1.41)
            elif text == main.UNKNOWN:
                audio('retry-echo', end+.1)
                user_question(end, 'retry', .62)
            else:
                audio('ordinary-echo', end+.1)
                if text.startswith('Михаил Борисович готов') or wake_count[0] < cycles:
                    audio('wake', end+.65)
        speech.say.side_effect = say
        def busy():
            result = clock[0] < ends[0]
            if tracked[0] and not result:
                observed.append(clock[0])
            return result
        speech.busy.side_effect = busy
        # This simulates a main-loop scheduling delay after the exit event,
        # while ADC capture/callbacks keep running on their own timeline.
        speech.wait_for_completion.side_effect = lambda timeout: advance(min(timeout, max(0, ends[0]-clock[0])))

        class Rec:
            def __init__(self, model, rate, grammar):
                self.wake = 'михаил' in json.loads(grammar)
                self.pending = None
                self.text = ''
            def Reset(self):
                self.pending = None
                self.text = ''  # Never inject input from Reset.
            def AcceptWaveform(self, data):
                label = labels.get(data[-3200:], '')
                delivered.append(label)
                self.text = ''
                if self.wake:
                    self.text = 'михаил' if label == 'wake' else ''
                elif label.startswith('first-'):
                    self.pending = label.removeprefix('first-')
                    if drain_stall and not stalled[0]:
                        stalled[0] = True
                        advance(drain_stall)
                    return False
                elif label.startswith('second-') and self.pending == label.removeprefix('second-'):
                    if decode_stall:
                        advance(decode_stall)
                    self.text = first_final if retry and self.pending == '1' else 'время'
                return True
            def PartialResult(self): return json.dumps({'partial': ''})
            def Result(self): return json.dumps({'text': self.text})

        class Stream:
            def __init__(self, **kwargs): callbacks.append(kwargs['callback'])
            def __enter__(self): return self
            def __exit__(self, *args):
                # Completion reads Stream.time: stop/join before destroying it.
                assert speech.stop.called, 'playback waiter outlived input stream'
            @property
            def active(self):
                if lag and tracked[0] and delayed_cycle[0] != wake_count[0]:
                    delayed_cycle[0] = wake_count[0]
                    advance(ends[0]+lag-clock[0])
                else:
                    advance(.02)
                return True
            @property
            def time(self): return epoch+clock[0]

        class Audio(main.FreshAudioQueue):
            def put_latest(self, *args, **kwargs):
                result = super().put_latest(*args, **kwargs)
                queue_stats['peak'] = max(queue_stats['peak'], len(self.items))
                queue_stats['overflow'] += bool(result)
                return result
            def get(self, timeout=None):
                if timeout and not self.items:
                    advance(.05)
                return super().get(0)

        with patch('main.dependencies', return_value=(Mock(RawInputStream=Stream), Mock(), Rec)), \
                patch('main.load_model'), patch('main.Speech', return_value=speech), \
                patch('main.GPTModes', return_value=Mock(active=False)), patch('main.Bridge'), \
                patch('main.OutputGuard'), patch('main.FreshAudioQueue', Audio), \
                patch('main.time.monotonic', side_effect=lambda: clock[0]), patch('main.log') as log, \
                patch('main.command', return_value='Ответ.') as command:
            main.listen(main.config(), duration=cycles*(3.6 if pressure else 2.2)+1)
        self.queue_stats = queue_stats
        self.ready_logs = [c.args[1] for c in log.call_args_list if c.args[0] == 'LOCAL READY']
        self.asr_logs = [c.args[1] for c in log.call_args_list if c.args[0] == 'LOCAL ASR']
        return delivered, command.call_count, utterances, observed, captured_users

    def test_unknown_immediate_final_is_logged_and_retry_accepts_next_command(self):
        for final in ('не знаю', 'павловича', 'Павловича?!'):
            with self.subTest(final=final):
                _, count, said, observed, users = self.run_local(retry=True, first_final=final)
                self.assertLess(observed[0], users[0][0])  # Speech begins after LISTENING.
                self.assertIn(main.UNKNOWN, said)
                self.assertEqual(count, 1)
                self.assertIn(f'final={json.dumps(main.normalize(final), ensure_ascii=False)} intent=NONE', self.asr_logs)
                self.assertIn('final="время" intent=TIME', self.asr_logs)

    def test_local_diagnostic_does_not_include_gpt_control_recognition(self):
        import test_chatgpt
        with patch('main.log') as log:
            test_chatgpt.AudioLoopIntegrationTests().run_mode(True)
        rows = [c.args[1] for c in log.call_args_list if c.args[0] == 'LOCAL ASR']
        self.assertTrue(rows)
        self.assertTrue(all('final="позови chatgpt" intent=CHATGPT_OPEN' == row for row in rows))

    def test_empty_final_is_logged_without_changing_retry_behavior(self):
        _, count, said, _, _ = self.run_local(retry=True, first_final='')
        self.assertIn('final="" intent=NONE', self.asr_logs)
        self.assertNotIn(main.UNKNOWN, said)
        self.assertEqual(count, 0)

    def test_pressure_preserves_first_post_playback_block_before_poll(self):
        delivered, count, _, observed, users = self.run_local(pressure=True, lag=.30)
        self.assertEqual(self.queue_stats['peak'], 12)
        self.assertGreater(self.queue_stats['overflow'], 0)
        self.assertLess(users[0][1], observed[0])
        self.assertIn('first-1', delivered)
        self.assertEqual(count, 1)
        for forbidden in ('tts', 'late-tts', 'straddle'):
            self.assertNotIn(forbidden, delivered)

    def test_waiting_for_long_playback_does_not_fill_queue(self):
        delivered, count, _, _, _ = self.run_local(pressure=True)
        self.assertEqual(self.queue_stats['overflow'], 0)
        self.assertLess(self.queue_stats['peak'], 12)
        self.assertEqual(count, 1)

    def test_long_delay_does_not_replay_old_command(self):
        delivered, count, _, _, _ = self.run_local(pressure=True, lag=2)
        self.assertNotIn('first-1', delivered)
        self.assertNotIn('second-1', delivered)
        self.assertEqual(count, 0)

    def test_delayed_drain_rechecks_staleness_of_preserved_blocks(self):
        delivered, count, _, _, _ = self.run_local(pressure=True, lag=.30, drain_stall=.6)
        self.assertIn('first-1', delivered)
        self.assertNotIn('second-1', delivered)
        self.assertEqual(count, 0)
        self.assertTrue(any('stale_after=1' in line for line in self.ready_logs))

    def test_repeated_pressure_cycles_do_not_reuse_transition(self):
        delivered, count, _, _, _ = self.run_local(pressure=True, lag=.30, cycles=2)
        self.assertEqual(count, 2)
        self.assertIn('first-2', delivered)
        self.assertEqual(len(self.ready_logs), 2)

    def test_recognition_returning_after_long_delay_cannot_execute_command(self):
        delivered, count, _, _, _ = self.run_local(decode_stall=2)
        self.assertIn('second-1', delivered)
        self.assertEqual(count, 0)

    def test_old_capture_outside_transition_still_rejected(self):
        delivered, count, _, _, _ = self.run_local(during_playback=True, late_followup=True)
        self.assertNotIn('first-stale', delivered)
        self.assertNotIn('second-stale', delivered)
        self.assertEqual(count, 0)

    def test_first_fresh_command_survives_transition_and_reset(self):
        delivered, count, _, _, _ = self.run_local()
        self.assertIn('first-1', delivered)
        self.assertEqual(count, 1)

    def test_playback_audio_cannot_supply_first_command_word(self):
        delivered, count, _, _, _ = self.run_local(during_playback=True)
        self.assertNotIn('first-1', delivered)
        self.assertEqual(count, 0)

    def test_tts_block_delivered_after_playback_is_rejected(self):
        delivered, count, _, _, _ = self.run_local()
        self.assertNotIn('late-tts', delivered)
        self.assertEqual(count, 1)

    def test_user_capture_before_main_observes_exit_is_preserved(self):
        delivered, count, _, observed, users = self.run_local(lag=.22)
        self.assertLess(users[0][1], observed[0])
        self.assertIn('first-1', delivered)
        self.assertEqual(count, 1)

    def test_straddling_block_rejected_and_next_complete_block_accepted(self):
        delivered, count, _, _, _ = self.run_local()
        self.assertNotIn('straddle', delivered)
        self.assertIn('first-1', delivered)
        self.assertIn('second-1', delivered)
        self.assertEqual(count, 1)

    def test_ordinary_tts_cooldown_remains(self):
        delivered, count, _, _, _ = self.run_local()
        self.assertNotIn('ordinary-echo', delivered)
        self.assertEqual(count, 1)

    def test_retry_cooldown_remains(self):
        delivered, count, said, _, _ = self.run_local(retry=True)
        self.assertIn(main.UNKNOWN, said)
        self.assertNotIn('retry-echo', delivered)
        self.assertEqual(count, 1)

    def test_consecutive_wakes_use_their_own_boundary(self):
        delivered, count, said, _, _ = self.run_local(cycles=2)
        self.assertEqual(said.count('Слушаю'), 2)
        self.assertEqual(count, 2)
        self.assertIn('first-2', delivered)
        self.assertNotIn('straddle', delivered)
        self.assertNotIn('late-tts', delivered)


class WakeQueueTests(unittest.TestCase):
    def test_pressure_prefix_trim_retains_original_timestamps_and_gap(self):
        q = main.FreshAudioQueue(12)
        for i in range(12):
            q.put_latest((i*.1+.1, main.CapturedPCM(b'tts', 10+i*.1, 10+i*.1+.1)))
        first = main.CapturedPCM(b'user-first', 11.22, 11.32)
        second = main.CapturedPCM(b'user-second', 11.33, 11.43)
        q.put_latest((1.32, first))
        q.put_latest((1.43, second), discontinuity=True)
        self.assertEqual(q.take_dropped(), 2)
        info = q.trim_wake(1.52, 11.52, 11.2)
        self.assertEqual(info['depth'], 12)
        self.assertEqual(info['dropped'], 10)
        self.assertEqual(info['pending'], {id(first), id(second)})
        self.assertAlmostEqual(info['first_age'], 1.32)
        self.assertAlmostEqual(info['last_age'], .19)
        self.assertEqual(q.get_nowait(), (1.32, first, True))
        self.assertEqual(q.get_nowait(), (1.43, second, True))
        self.assertEqual(q.maxsize, 12)

    def test_wait_drain_does_not_discard_fresh_audio_before_exit_observed(self):
        q = main.FreshAudioQueue()
        first = main.CapturedPCM(b'user', 100.1, 100.2)
        q.put_latest((1.2, first))
        self.assertEqual(q.trim_wake(1.3, 100.3)['dropped'], 0)
        info = q.trim_wake(1.35, 100.35, 100.05)
        self.assertEqual(info['pending'], {id(first)})
        self.assertEqual(q.get_nowait(), (1.2, first, False))

    def test_late_delivery_does_not_refresh_expired_post_playback_pcm(self):
        q = main.FreshAudioQueue()
        old = main.CapturedPCM(b'old-command', 100.1, 100.2)
        q.put_latest((1.7, old))  # Just delivered, capture already 700 ms old.
        info = q.trim_wake(1.8, 100.8, 100.05)
        self.assertEqual(info['post_stale'], 1)
        self.assertFalse(info['pending'])
        with self.assertRaises(main.queue.Empty): q.get_nowait()
        fresh = main.CapturedPCM(b'fresh', 100.7, 100.8)
        q.put_latest((1.8, fresh))
        self.assertEqual(q.get_nowait(), (1.8, fresh, True))


class SpeechCompletionTests(unittest.TestCase):
    def test_stop_joins_waiter_without_publishing_successful_boundary(self):
        entered, exited = threading.Event(), threading.Event()
        process = Mock()
        def wait(**kwargs):
            entered.set()
            if not exited.wait(2):
                raise RuntimeError('fixture exit not released')
            return -15
        process.wait.side_effect = wait
        process.poll.side_effect = lambda: -15 if exited.is_set() else None
        process.terminate.side_effect = exited.set
        record = main.PlaybackEnd(lambda: 10025)
        self.addCleanup(record.join)
        self.addCleanup(exited.set)
        record.start(process)
        self.assertTrue(entered.wait(1))
        speech = main.Speech.__new__(main.Speech)
        speech.process, speech.phase, speech.completion = process, 'playback', record
        speech.errors = Mock()
        speech.stop()
        self.assertTrue(record.done.is_set())
        self.assertFalse(record.thread.is_alive())
        self.assertIsNone(record.boundary)
        process.terminate.assert_called_once()

    def test_exit_event_records_end_before_later_main_poll(self):
        entered, exited = threading.Event(), threading.Event()
        stream_clock = [10025.0]
        process = Mock()
        def wait():
            entered.set()
            if not exited.wait(2):
                raise RuntimeError('fixture exit not released')
            return 0
        process.wait.side_effect = wait
        process.poll.return_value = 0
        record = main.PlaybackEnd(lambda: stream_clock[0])
        self.addCleanup(record.join)
        self.addCleanup(exited.set)
        record.start(process)
        self.assertTrue(entered.wait(1))
        self.assertIsNone(record.boundary)
        exited.set()
        self.assertTrue(record.done.wait(1))
        stream_clock[0] += .3  # Main loop is scheduled only now.
        speech = main.Speech.__new__(main.Speech)
        speech.process, speech.phase, speech.completion = process, 'playback', record
        speech.started, speech.errors = 0, Mock()
        with patch('main.time.monotonic', return_value=1):
            self.assertFalse(speech.busy())
        self.assertEqual(speech.playback_end, 10025.0)
        self.assertFalse(record.thread.is_alive())
        process.wait.assert_called_once()

    def test_invalid_exit_clock_fails_closed_and_joins(self):
        record = main.PlaybackEnd(lambda: float('nan'))
        record.start(Mock(wait=Mock(return_value=0)))
        self.addCleanup(record.join)
        self.assertTrue(record.done.wait(1))
        self.assertIsNone(record.boundary)
        self.assertEqual(record.error, 'ValueError')
        speech = main.Speech.__new__(main.Speech)
        speech.process = Mock(poll=Mock(return_value=0))
        speech.phase, speech.completion = 'playback', record
        speech.started, speech.errors = 0, Mock()
        with patch('main.time.monotonic', return_value=1), self.assertRaises(RuntimeError):
            speech.busy()
        self.assertFalse(record.thread.is_alive())

    def test_new_utterance_cannot_use_old_exit_record(self):
        speech = main.Speech.__new__(main.Speech)
        speech.process, speech.phase, speech.completion = None, None, None
        speech.voice, speech.path = 'fixture', 'unused-fixture.wav'
        with patch('main.subprocess.Popen') as popen, patch('main.log'):
            popen.return_value.poll.return_value = 0
            speech.say('Слушаю', completion_clock=lambda: 10000)
            old = speech.completion
            old.boundary = 10000
            speech.say('Слушаю', completion_clock=lambda: 10001)
            self.addCleanup(speech.stop)
            old.boundary = 10002  # Even a late old publication stays isolated.
            self.assertIsNot(speech.completion, old)
            self.assertIsNone(speech.playback_end)

    def test_synthesis_completion_is_not_playback_completion(self):
        speech = main.Speech.__new__(main.Speech)
        speech.started = 0
        speech.phase = 'synthesis'
        speech.errors = Mock()
        speech.process = Mock()
        speech.process.poll.return_value = 0
        player = Mock()
        player.poll.return_value = None
        speech.player, speech.path = 'fixture-player', 'fixture.wav'
        with patch('main.time.monotonic', return_value=1), \
                patch('main.subprocess.Popen', return_value=player):
            self.assertTrue(speech.busy())
            self.assertEqual(speech.phase, 'playback')
            self.assertTrue(speech.busy())
            player.poll.return_value = 0
            self.assertFalse(speech.busy())
            self.assertIsNone(speech.process)


class CaptureIntervalTests(unittest.TestCase):
    def test_late_callback_does_not_move_adc_interval(self):
        first = main.CapturedPCM.from_callback(b'pcm', 1600, 16000,
            Mock(inputBufferAdcTime=10000, currentTime=10000.1))
        late = main.CapturedPCM.from_callback(b'pcm', 1600, 16000,
            Mock(inputBufferAdcTime=10000, currentTime=10020))
        self.assertEqual(first, late)
        self.assertEqual((late.start, late.end), (10000, 10000.1))

    def test_invalid_or_missing_timestamps_are_not_trusted(self):
        for timing in (None, Mock(inputBufferAdcTime=float('nan'), currentTime=10),
                       Mock(inputBufferAdcTime=20, currentTime=10)):
            with self.subTest(timing=timing):
                block = main.CapturedPCM.from_callback(b'pcm', 1600, 16000, timing)
                self.assertIsNone(block.start)
                self.assertIsNone(block.end)


class TimeFormsTests(unittest.TestCase):
    def test_hours(self):
        for number, word in ((1,'час'),(2,'часа'),(5,'часов'),(11,'часов'),(21,'час'),(22,'часа'),(25,'часов')):
            with self.subTest(number=number):
                self.assertEqual(main.ru_form(number, 'час', 'часа', 'часов'), word)

    def test_minutes(self):
        for number, word in ((1,'минута'),(2,'минуты'),(5,'минут'),(11,'минут'),(21,'минута'),(22,'минуты'),(25,'минут')):
            with self.subTest(number=number):
                self.assertEqual(main.ru_form(number, 'минута', 'минуты', 'минут'), word)

    def test_time_reply_uses_forms(self):
        with patch('main.datetime') as date, patch('main.log'):
            date.now.return_value = Mock(hour=22, minute=1)
            self.assertEqual(main.command('час', main.config(), Mock()), 'Сейчас 22 часа 1 минута.')


if __name__ == '__main__':
    unittest.main()
