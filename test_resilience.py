"""Deterministic regression coverage for audio overload and monitor failures."""
import unittest
import threading
import json
import os
import subprocess
import sys
import main
from unittest.mock import patch
from concurrent.futures import Future
from unittest.mock import Mock

import test_chatgpt as fixtures
from chatgpt_bridge import LOCAL_MODE, ANTON_MODE
from speaker_activity import OutputGuard

pcm = fixtures.pcm


class LocalAudioTests(unittest.TestCase):
    def test_no_audio_tick_finishes_mode_and_checks_monitor_cleanup(self):
        for close_ok in (True, False):
            with self.subTest(close_ok=close_ok):
                clock = [0.0]
                modes = Mock(active=True)
                def tick(now): modes.active = False
                modes.tick.side_effect = tick
                speech = Mock()
                speech.busy.return_value = False
                class Stream:
                    def __init__(self, **kw): pass
                    def __enter__(self): return self
                    def __exit__(self, *args): pass
                    @property
                    def active(self):
                        clock[0] += .1
                        return True
                class EmptyQueue(main.FreshAudioQueue):
                    def get(self, timeout=None): raise main.queue.Empty
                with patch('main.dependencies', return_value=(Mock(RawInputStream=Stream), Mock(), Mock())), \
                        patch('main.load_model'), patch('main.Speech', return_value=speech), \
                        patch('main.Bridge'), patch('main.GPTModes', return_value=modes), \
                        patch('main.OutputGuard') as guards, patch('main.FreshAudioQueue', EmptyQueue), \
                        patch('main.time.monotonic', side_effect=lambda: clock[0]), \
                        patch('main.log') as log:
                    guards.return_value.close.return_value = close_ok
                    if close_ok:
                        main.listen(main.config(), duration=.5)
                        self.assertEqual(guards.call_count, 2)
                        log.assert_any_call('WAITING')
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'shutdown not confirmed'):
                            main.listen(main.config(), duration=.5)
                        self.assertEqual(guards.call_count, 1)
                modes.tick.assert_called_once()

    def test_stale_command_without_queue_overflow_is_not_executed(self):
        clock = [0.0]
        stale = [False]
        recognized = []
        speech = Mock()
        speech.busy.return_value = False
        def say(text, **kwargs):
            if text == 'Слушаю':
                stale[0] = True
                speech.playback_end = 10000 + clock[0]
        speech.say.side_effect = say
        class Audio(main.FreshAudioQueue):
            def get(self, timeout=None):
                captured, data, gap = super().get(timeout)
                if timeout == .2 and stale[0]:
                    captured -= 1
                return captured, data, gap
        class Rec:
            def __init__(self, model, rate, grammar):
                self.kind = 'wake' if 'михаил' in json.loads(grammar) else 'command'
            def Reset(self): pass
            def AcceptWaveform(self, data):
                recognized.append(self.kind)
                return True
            def Result(self):
                return json.dumps({'text': 'михаил' if self.kind == 'wake' else 'время'})
        class Stream:
            def __init__(self, **kw): self.callback = kw['callback']
            def __enter__(self): return self
            def __exit__(self, *args): pass
            @property
            def time(self): return 10000 + clock[0]
            @property
            def active(self):
                clock[0] += .1
                timing = Mock(inputBufferAdcTime=self.time-.1, currentTime=self.time)
                self.callback(pcm(1000), 1600, timing, None)
                return True
        with patch('main.dependencies', return_value=(Mock(RawInputStream=Stream), Mock(), Rec)), \
                patch('main.load_model'), patch('main.Speech', return_value=speech), \
                patch('main.FreshAudioQueue', Audio), patch('main.Bridge'), \
                patch('main.OutputGuard'), patch('main.command') as command, \
                patch('main.time.monotonic', side_effect=lambda: clock[0]):
            main.listen(main.config(), duration=4)
        self.assertIn('wake', recognized)
        command.assert_not_called()
        self.assertNotIn('command', recognized)


class ModeRecoveryTests(unittest.TestCase):
    def make(self):
        return fixtures.ModeTests().make(ANTON_MODE)

    def test_failed_monitor_drains_pending_and_completed_jobs_without_send(self):
        for initially_done in (False, True):
            with self.subTest(initially_done=initially_done):
                g = self.make()
                pending, closing = Future(), Future()
                g.phase, g.action, g.job = 'transcribing', 'poll', pending
                g.guard.failed = True
                g.guard.allows.return_value = False
                g.bridge.submit.side_effect = lambda action: closing
                result = dict(ok=True, detail='', composer='private', send=True, recording=False)
                if initially_done:
                    pending.set_result(result)
                g.tick(1)
                self.assertTrue(g.active)
                self.assertNotEqual(g.mode, LOCAL_MODE)
                if not initially_done:
                    g.bridge.submit.assert_called_once_with('close')
                    self.assertFalse(pending.done())
                    pending.set_result(result)
                    g.tick(2)
                g.bridge.submit.assert_called_once_with('close')
                self.assertIs(g.job, closing)
                g.step(pcm(1000), 3, 3)
                g.rec.AcceptWaveform.assert_not_called()
                closing.set_result(dict(ok=True, detail='closed'))
                g.speech.busy.return_value = True
                g.tick(4)
                self.assertTrue(g.active)
                g.tick(5)
                self.assertTrue(g.active)
                g.speech.busy.return_value = False
                g.tick(6)
                g.tick(7)
                g.tick(8)
                self.assertEqual((g.mode, g.phase), (LOCAL_MODE, 'idle'))
                g.bridge.submit.assert_called_once_with('close')

    def test_failed_monitor_after_close_never_closes_twice(self):
        g = self.make()
        g.guard.failed = True
        g.guard.allows.return_value = False
        for now in range(6):
            g.tick(now)
        self.assertEqual((g.mode, g.phase), (LOCAL_MODE, 'idle'))
        g.bridge.submit.assert_called_once_with('close')

    def test_dictation_absolute_limit_runs_without_allowed_audio(self):
        g = self.make()
        g.guard.allows.return_value = False
        g.heard = True
        g.deadline = 2
        g.tick(3)
        g.bridge.submit.assert_called_once_with('transcribe')

    def test_control_window_pauses_during_blocked_audio(self):
        g = self.make()
        g.phase = 'control'
        g.deadline = 5
        g.guard.allows.return_value = False
        g.tick(1)
        g.tick(6)
        self.assertEqual(g.phase, 'control')
        g.guard.allows.return_value = True
        g.tick(7)
        self.assertEqual(g.phase, 'control')
        self.assertGreater(g.deadline, 7)

    def test_stale_audio_clears_preroll_without_recognition(self):
        g = self.make()
        g.preroll.append(b'old')
        g.mic_tail = 10
        g.step(pcm(1000), 1, 2)
        g.rec.AcceptWaveform.assert_not_called()
        self.assertFalse(g.preroll)
        self.assertEqual(g.mic_tail, 0)

    def test_stale_return_command_cannot_close_mode(self):
        for mode in (ANTON_MODE, fixtures.LARISA_MODE):
            g = self.make()
            g.mode, g.phase, g.deadline = mode, 'control', 100
            g.rec.Result.side_effect = lambda: json.dumps({'text': 'вернись'})
            g.step(pcm(1000), 1, 2)
            g.bridge.submit.assert_not_called()
            g.rec.AcceptWaveform.assert_not_called()

    def test_long_monitor_fault_limits_control_wait(self):
        g = self.make()
        g.phase, g.deadline = 'control', 5
        g.guard.allows.return_value = False
        g.guard.available.return_value = False
        g.tick(0)
        g.tick(59)
        self.assertEqual(g.phase, 'control')
        g.tick(60)
        g.tick(61)
        g.tick(62)
        g.tick(63)
        self.assertEqual((g.mode, g.phase), (LOCAL_MODE, 'idle'))
        g.bridge.submit.assert_called_once_with('close')

    def test_healthy_active_speakers_are_not_a_monitor_fault(self):
        g = self.make()
        g.phase, g.poll_after = 'voice', 1000
        g.guard.allows.return_value = False
        g.guard.available.return_value = True
        g.tick(0)
        g.tick(100)
        g.bridge.submit.assert_not_called()

    def test_control_recovery_has_wall_limit_even_with_intermittent_monitor(self):
        g = self.make()
        g.phase, g.deadline = 'control', 5
        g.guard.allows.return_value = False
        # Samples can arrive often enough to be fresh, without ever producing
        # the continuous silence needed to admit the user's return command.
        g.guard.available.return_value = True
        g.tick(0)
        g.tick(50)
        self.assertEqual(g.phase, 'control')
        g.tick(80)
        g.bridge.submit.assert_called_once_with('close')

    def test_late_poll_after_operation_limit_cannot_send(self):
        g = self.make()
        g.phase, g.deadline, g.action = 'transcribing', 5, 'poll'
        g.job = Future()
        g.job.set_result(dict(ok=True, detail='', composer='private', send=True, recording=False))
        g.tick(6)
        for now in (7, 8, 9):
            g.tick(now)
        g.bridge.submit.assert_called_once_with('close')
        self.assertEqual(g.phase, 'idle')

    def test_unfinished_job_keeps_local_blocked_until_independent_close(self):
        g = self.make()
        pending, closing = Future(), Future()
        g.bridge.submit.side_effect = lambda action: closing
        g.job, g.action = pending, 'poll'
        g.phase, g.deadline = 'reply', 1000
        for now in (0, 91, 200):
            g.tick(now)
            self.assertTrue(g.active)
            self.assertNotEqual(g.mode, LOCAL_MODE)
        g.bridge.submit.assert_called_once_with('close')
        self.assertFalse(pending.done())
        pending.set_result(dict(ok=True, detail='', reply_ready=True, answer='not spoken'))
        g.tick(201)
        g.bridge.submit.assert_called_once_with('close')
        g.speech.say.assert_not_called()
        closing.set_result(dict(ok=True, detail='confirmed'))
        for now in (202, 203, 204):
            g.tick(now)
        self.assertEqual((g.mode, g.phase), (LOCAL_MODE, 'idle'))

    def test_emergency_close_failure_stays_blocked_without_retry_loop(self):
        g = fixtures.ModeTests().make(ANTON_MODE, close_ok=False)
        g.guard.failed = True
        g.guard.allows.return_value = False
        for now in range(10):
            g.step(pcm(1000), now, now)
        self.assertEqual(g.phase, 'error_wait')
        self.assertNotEqual(g.mode, LOCAL_MODE)
        g.bridge.submit.assert_called_once_with('close')
        g.rec.AcceptWaveform.assert_not_called()

    def test_gap_then_stale_audio_still_services_jobs(self):
        g = self.make()
        g.phase, g.action, g.job = 'stopping_dictation', 'transcribe', Future()
        g.job.set_result(dict(ok=True, detail='stopped'))
        g.preroll.append(b'old')
        g.audio_gap(1)
        g.step(pcm(), 1, 2)
        self.assertEqual(g.phase, 'transcribing')
        self.assertFalse(g.preroll)
        g.rec.Reset.assert_called()
        g.rec.AcceptWaveform.assert_not_called()


class QueueConcurrencyTests(unittest.TestCase):
    def test_device_gap_marks_its_frame_not_older_queued_frames(self):
        queue = main.FreshAudioQueue(3)
        queue.put_latest((1, b'before'))
        queue.put_latest((2, b'after'), discontinuity=True)
        self.assertEqual(queue.get_nowait(), (1, b'before', False))
        self.assertEqual(queue.get_nowait(), (2, b'after', True))

    def test_overflow_consumer_interleaving_keeps_latest_and_gap(self):
        queue = main.FreshAudioQueue(2)
        queue.put_latest((1, b'a'))
        queue.put_latest((2, b'b'))
        consumed, produced = threading.Event(), threading.Event()
        results = []
        def consumer():
            results.append(queue.get_nowait())
            consumed.set()
            if produced.wait(2):
                results.append(queue.get_nowait())
                results.append(queue.get_nowait())
        reader = threading.Thread(target=consumer, daemon=True)
        reader.start()
        try:
            self.assertTrue(consumed.wait(2))
            queue.put_latest((3, b'c'))
            queue.put_latest((4, b'd'))
            produced.set()
            reader.join(2)
            self.assertFalse(reader.is_alive())
            self.assertEqual(results, [(1, b'a', False), (3, b'c', True), (4, b'd', False)])
            self.assertEqual(queue.take_dropped(), 1)
        finally:
            produced.set()
            reader.join(2)


class FakeProcess:
    """Actual nonblocking OS pipe, controllable process exit, no live audio."""
    def __init__(self, ignore_term=False, ignore_kill=False):
        read_fd, self.write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, 'rb', buffering=0)
        self.dead = False
        self.ignore_term, self.ignore_kill = ignore_term, ignore_kill
        self.events = []
        self.on_close = lambda: None
        self.terminated = threading.Event()
    def poll(self): return 0 if self.dead else None
    def terminate(self):
        self.events.append('terminate')
        self.terminated.set()
        if not self.ignore_term:
            self.dead = True
    def kill(self):
        self.events.append('kill')
        if not self.ignore_kill:
            self.dead = True
    def wait(self, timeout=None):
        self.events.append('wait')
        if not self.dead:
            raise subprocess.TimeoutExpired('fake', timeout)
        return 0
    def write(self, block): os.write(self.write_fd, block)
    def eof(self):
        os.close(self.write_fd)
        self.write_fd = None
    def cleanup(self):
        self.dead = True
        if self.write_fd is not None:
            self.eof()
        self.stdout.close()


class MonitorReviewTests(unittest.TestCase):
    def make(self, **cfg):
        guard = OutputGuard(cfg)
        # Drive the worker's iterations ourselves using explicit fake times.
        guard.start = Mock()
        self.addCleanup(lambda: guard._retire('test cleanup', 100))
        return guard

    def process(self, **kw):
        process = FakeProcess(**kw)
        self.addCleanup(process.cleanup)
        return process

    def start_process(self, guard, process):
        with patch('speaker_activity.subprocess.Popen', return_value=process):
            guard._service(0)

    def test_retirement_does_not_close_pipe_under_state_mutex(self):
        process = self.process()
        g = self.make()
        self.start_process(g, process)
        close = process.stdout.close
        def checked_close():
            self.assertTrue(g.lock.acquire(blocking=False))
            g.lock.release()
            close()
        with patch.object(process.stdout, 'close', side_effect=checked_close) as closing:
            g._service(10)
            closing.assert_called_once()
        self.assertIsNone(g.process)

    def test_retirement_waits_for_exit_after_terminate(self):
        process = self.process(ignore_term=True)
        g = self.make()
        self.start_process(g, process)
        g._service(10)
        self.assertEqual(process.events, ['terminate', 'wait', 'kill', 'wait'])
        self.assertIsNotNone(process.poll())
        self.assertTrue(process.stdout.closed)
        self.assertIsNone(g.process)

    def test_late_old_reader_cannot_update_new_activity(self):
        g = self.make()
        entered, release = threading.Event(), threading.Event()
        old, new = object(), object()
        def late_read():
            entered.set()
            if not release.wait(2):
                raise RuntimeError('test did not release reader')
            g._accept(old, 1, pcm(), 1)
        g.process = old
        g.generation = 1
        reader = threading.Thread(target=late_read, daemon=True)
        reader.start()
        try:
            self.assertTrue(entered.wait(2))
            with g.lock:
                g.process = new
                g.generation = 2
                g.activity.reset()
            release.set()
            reader.join(2)
            g.process = None
            self.assertFalse(reader.is_alive())
            self.assertIsNone(g.activity.updated)
        finally:
            release.set()
            reader.join(2)

    def test_short_stale_interval_blocks_but_does_not_retire(self):
        process = self.process()
        g = self.make()
        self.start_process(g, process)
        for index in range(10):
            process.write(pcm())
            g._service(index / 10)
        self.assertTrue(g.allows(.9))
        g._service(1.5)
        self.assertFalse(g.allows(1.5))
        self.assertIs(g.process, process)
        self.assertEqual(process.events, [])
        process.write(pcm())
        g._service(1.6)
        self.assertFalse(g.allows(1.6))
        for index in range(17, 26):
            process.write(pcm())
            g._service(index / 10)
        self.assertTrue(g.allows(2.5))

    def test_eof_recovery_requires_new_silence(self):
        first, second = self.process(), self.process()
        g = self.make(speaker_restart_delay=.5)
        with patch('speaker_activity.subprocess.Popen', side_effect=[first, second]) as spawn:
            g._service(0)
            first.eof()
            g._service(.1)
            self.assertTrue(first.dead)
            self.assertTrue(first.stdout.closed)
            g._service(.59)
            self.assertEqual(spawn.call_count, 1)
            g._service(.6)
            self.assertEqual(spawn.call_count, 2)
            self.assertFalse(g.allows(.6))
            for index in range(7, 17):
                second.write(pcm())
                g._service(index / 10)
            self.assertTrue(g.allows(1.6))
            self.assertEqual(g.restarts, 2)

    def test_no_first_data_retries_are_bounded(self):
        processes = [self.process() for _ in range(2)]
        g = self.make(speaker_restart_limit=2, speaker_restart_delay=.5)
        with patch('speaker_activity.subprocess.Popen', side_effect=processes) as spawn:
            for now in (0, .6, 3, 3.5, 6.5, 7, 10, 100):
                g._service(now)
                self.assertFalse(g.allows(now))
            self.assertTrue(g.failed)
            self.assertEqual(spawn.call_count, 2)
            self.assertTrue(all(p.dead and p.stdout.closed for p in processes))

    def test_stalled_read_and_exited_process_recover(self):
        for exited in (False, True):
            with self.subTest(exited=exited):
                first, second = self.process(), self.process()
                g = self.make(speaker_restart_delay=0)
                with patch('speaker_activity.subprocess.Popen', side_effect=[first, second]):
                    g._service(0)
                    first.write(pcm())
                    g._service(.1)
                    first.dead = exited
                    g._service(2.2)
                    self.assertTrue(first.dead)
                    self.assertIsNone(g.process)
                    g._service(2.3)
                    self.assertIs(g.process, second)
                    self.assertFalse(g.allows(2.3))

    def test_unconfirmed_exit_prevents_replacement(self):
        first = self.process(ignore_term=True, ignore_kill=True)
        g = self.make(speaker_restart_delay=0)
        with patch('speaker_activity.subprocess.Popen', return_value=first) as spawn:
            g._service(0)
            g._service(3)
            g._service(10)
            self.assertTrue(g.failed)
            self.assertIs(g.process, first)
            self.assertFalse(g.allows(10))
            self.assertEqual(spawn.call_count, 1)
        first.dead = True

    def test_closed_guard_cancels_scheduled_restart(self):
        first = self.process()
        g = self.make()
        with patch('speaker_activity.subprocess.Popen', return_value=first) as spawn:
            g._service(0)
            first.eof()
            g._service(.1)
            self.assertTrue(g.close())
            self.assertTrue(g.close())
            g._service(100)
            self.assertEqual(spawn.call_count, 1)
            self.assertFalse(g.allows(100))

    def test_real_worker_cleanup_is_nonblocking_for_main_and_joined(self):
        first = self.process(ignore_term=True)
        g = OutputGuard()
        entered, release = threading.Event(), threading.Event()
        original_wait = first.wait
        def wait(timeout=None):
            entered.set()
            if not release.wait(2):
                raise RuntimeError('test cleanup not released')
            self.assertTrue(g.lock.acquire(blocking=False))
            g.lock.release()
            return original_wait(timeout)
        first.wait = wait
        first.eof()
        try:
            with patch('speaker_activity.subprocess.Popen', return_value=first) as spawn:
                g.start()
                self.assertTrue(entered.wait(2))
                # Cleanup is held at a barrier. Main can still consult the gate.
                self.assertFalse(g.allows(100))
                self.assertEqual(spawn.call_count, 1)
                release.set()
                self.assertTrue(g.close())
                self.assertFalse(g.thread.is_alive())
                self.assertTrue(g.close())
                self.assertIsNone(g.process)
                self.assertTrue(first.stdout.closed)
                self.assertEqual(spawn.call_count, 1)
        finally:
            release.set()
            g.close()

    def test_actual_child_ignoring_sigterm_is_killed_and_reaped(self):
        # Run this suite under an external timeout. Cleanup also runs on failure.
        child = subprocess.Popen([sys.executable, '-c',
            'import signal,os,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
            'os.write(1,b"R"); time.sleep(30)'], stdout=subprocess.PIPE, bufsize=0)
        def cleanup():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
            child.stdout.close()
        self.addCleanup(cleanup)
        self.assertEqual(child.stdout.read(1), b'R')
        g = self.make()
        g.process = child
        self.assertTrue(g._retire('test stall', 10))
        self.assertEqual(child.returncode, -9)
        self.assertIsNone(g.process)
        self.assertTrue(child.stdout.closed)


if __name__ == '__main__':
    unittest.main()
