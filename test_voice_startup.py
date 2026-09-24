"""Startup regressions: synthetic DOM and clock, no ChatGPT or audio."""
from concurrent.futures import Future
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from selenium.common.exceptions import StaleElementReferenceException
from chatgpt_ui import ChatGPTUI, UIError
from chatgpt_bridge import Bridge, LOCAL_MODE, LARISA_MODE, ANTON_MODE
import test_chatgpt


class Clock:
    def __init__(self): self.now = 0.
    def monotonic(self): return self.now
    def sleep(self, seconds): self.now += seconds


class VoiceReadinessTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.patch = patch('chatgpt_ui.time', self.clock)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.ui = ChatGPTUI({'gpt_startup_timeout': 240})
        self.ui.open = Mock()
        self.ui.driver = Mock()
        self.ui.driver.command_executor.client_config.timeout = 50
        self.ui.snapshot = Mock(return_value={'end_voice': True, 'mic_off': False, 'mic_on': True})
        self.ui.voice_microphone_state = Mock(return_value=mic_state(on=1))
        self.ui.capture_ready = Mock(return_value=True)
        self.button = Mock()
        self.button.is_displayed.return_value = True
        self.button.is_enabled.return_value = True
        self.button.get_attribute.return_value = 'Start Voice'

    def test_absent_then_visible_voice_continues(self):
        self.ui.driver.find_elements.side_effect = [[], [], [self.button]]
        self.assertTrue(self.ui.start_voice()['ok'])
        self.assertGreaterEqual(self.clock.now, 1)
        self.button.click.assert_called_once()
        self.ui.capture_ready.assert_called_once()

    def test_never_visible_is_bounded_and_does_not_click(self):
        self.ui.driver.find_elements.return_value = []
        with self.assertRaises(UIError): self.ui.start_voice()
        self.assertEqual(self.clock.now, 30)
        self.button.click.assert_not_called()
        self.ui.capture_ready.assert_not_called()

    def test_control_found_after_local_deadline_is_not_clicked(self):
        def slow_find(*args):
            self.clock.now += 31
            return [self.button]
        self.ui.driver.find_elements.side_effect = slow_find
        with self.assertRaises(UIError):
            self.ui.start_voice()
        self.button.click.assert_not_called()
        self.ui.capture_ready.assert_not_called()
        self.assertEqual(self.clock.now, 31)

    def test_wait_does_not_accept_a_late_true_condition(self):
        def condition():
            self.clock.now += 31
            return True
        with self.assertRaises(UIError):
            self.ui.wait(condition, 'fixture', 30)

    def test_stale_selection_and_click_retry(self):
        self.ui.driver.find_elements.side_effect = [StaleElementReferenceException()] * 3 + [[self.button]] * 2
        self.button.click.side_effect = [StaleElementReferenceException(), None]
        self.assertTrue(self.ui.start_voice()['ok'])
        self.assertEqual(self.ui.driver.find_elements.call_count, 5)
        self.assertEqual(self.button.click.call_count, 2)

    def test_multiple_visible_enabled_voice_fails_without_click(self):
        self.ui.driver.find_elements.return_value = [self.button, self.button]
        with self.assertRaises(UIError): self.ui.start_voice()
        self.button.click.assert_not_called()
        self.assertEqual(self.clock.now, 0)

    def test_hidden_and_disabled_controls_are_never_clicked(self):
        hidden, disabled = Mock(), Mock()
        hidden.is_displayed.return_value = False
        disabled.is_displayed.return_value = True
        disabled.is_enabled.return_value = False
        self.ui.driver.find_elements.side_effect = [[hidden, disabled], [hidden, disabled, self.button]]
        self.assertTrue(self.ui.start_voice()['ok'])
        hidden.click.assert_not_called()
        disabled.click.assert_not_called()
        self.button.click.assert_called_once()


def mic_state(*, on=0, off=0, element=None, dialog=False, alert=False):
    return dict(on_visible=on, on_enabled=on, off_visible=off, off_enabled=off,
                turn_on=element, dialog=dialog, alert=alert)


class MicrophoneReadinessTests(unittest.TestCase):
    def setUp(self):
        VoiceReadinessTests.setUp(self)
        self.ui.driver.find_elements.return_value = [self.button]
        self.mic = Mock()
        self.clicked = False
        def click(): self.clicked = True
        self.mic.click.side_effect = click
        self.state = lambda: mic_state(on=1)
        self.ui.voice_microphone_state.side_effect = lambda: self.state()
        def snapshot():
            s = self.state()
            return dict(end_voice=True, mic_on=bool(s['on_enabled']), mic_off=bool(s['off_enabled']))
        self.ui.snapshot.side_effect = snapshot
        log_patch = patch('chatgpt_ui.cleanup_log')
        self.logs = log_patch.start()
        self.addCleanup(log_patch.stop)

    def test_end_voice_before_mic_off_then_one_click_and_confirmation(self):
        self.state = lambda: (mic_state(on=1) if self.clicked else
            mic_state(off=1, element=self.mic) if self.clock.now >= 1 else mic_state())
        self.assertTrue(self.ui.start_voice()['ok'])
        self.mic.click.assert_called_once()
        self.ui.capture_ready.assert_called_once()
        self.assertGreaterEqual(self.clock.now, 1)

    def test_mic_on_appears_without_click(self):
        self.state = lambda: mic_state(on=int(self.clock.now >= 1))
        self.assertTrue(self.ui.start_voice()['ok'])
        self.mic.click.assert_not_called()

    def test_already_on_skips_click(self):
        self.assertTrue(self.ui.start_voice()['ok'])
        self.mic.click.assert_not_called()

    def test_missing_controls_deadline_is_stage_specific(self):
        self.state = lambda: mic_state()
        with self.assertRaisesRegex(UIError, 'microphone.*controls_missing'):
            self.ui.start_voice()
        self.assertEqual(self.clock.now, 30)
        self.ui.capture_ready.assert_not_called()

    def test_off_after_click_fails_without_second_click_or_capture(self):
        self.state = lambda: mic_state(off=1, element=self.mic)
        with self.assertRaisesRegex(UIError, 'microphone.*off_after_click'):
            self.ui.start_voice()
        self.mic.click.assert_called_once()
        self.assertEqual(self.clock.now, 30)
        self.ui.capture_ready.assert_not_called()

    def test_ambiguous_controls_fail_safe(self):
        for on, off in ((2, 0), (0, 2), (1, 1)):
            with self.subTest(on=on, off=off):
                self.state = lambda: mic_state(on=on, off=off, element=self.mic)
                with self.assertRaisesRegex(UIError, 'microphone.*ambiguous'):
                    self.ui.start_voice()
                self.mic.click.assert_not_called()
                self.ui.capture_ready.assert_not_called()

    def test_stale_observation_retries_before_single_click(self):
        self.state = lambda: mic_state(on=1) if self.clicked else mic_state(off=1, element=self.mic)
        attempts = [0]
        def read():
            attempts[0] += 1
            if attempts[0] < 3: raise StaleElementReferenceException()
            return self.state()
        self.ui.voice_microphone_state.side_effect = read
        self.assertTrue(self.ui.start_voice()['ok'])
        self.mic.click.assert_called_once()

    def test_disappeared_control_after_click_is_not_clicked_again(self):
        self.state = lambda: mic_state() if self.clicked else mic_state(off=1, element=self.mic)
        with self.assertRaisesRegex(UIError, 'microphone.*missing_after_click'):
            self.ui.start_voice()
        self.mic.click.assert_called_once()
        self.ui.capture_ready.assert_not_called()

    def test_stale_click_is_not_repeated(self):
        self.state = lambda: mic_state(off=1, element=self.mic)
        self.mic.click.side_effect = StaleElementReferenceException()
        with self.assertRaisesRegex(UIError, 'microphone.*off_after_click; click=stale'):
            self.ui.start_voice()
        self.mic.click.assert_called_once()
        self.assertEqual(self.clock.now, 30)
        self.ui.capture_ready.assert_not_called()

    def test_permanent_stale_observation_is_bounded(self):
        self.ui.voice_microphone_state.side_effect = StaleElementReferenceException()
        with self.assertRaisesRegex(UIError, 'microphone.*control_stale'):
            self.ui.start_voice()
        self.assertEqual(self.clock.now, 30)
        self.mic.click.assert_not_called()

    def test_click_blocked_by_dialog_fails_without_logging_exception_text(self):
        from selenium.common.exceptions import ElementClickInterceptedException
        self.state = lambda: mic_state(off=1, element=self.mic, dialog=True)
        self.mic.click.side_effect = ElementClickInterceptedException('private fixture text')
        with self.assertRaisesRegex(UIError, 'microphone.*click_blocked'):
            self.ui.start_voice()
        self.mic.click.assert_called_once()
        output = '\n'.join(str(c.args[0]) for c in self.logs.call_args_list)
        self.assertIn('dialog=True', output)
        self.assertNotIn('private fixture text', output)
        self.ui.capture_ready.assert_not_called()

    def test_late_find_does_not_click_or_extend_local_deadline(self):
        self.state = lambda: mic_state(off=1, element=self.mic)
        def read():
            self.clock.now += 31
            return self.state()
        self.ui.voice_microphone_state.side_effect = read
        with self.assertRaisesRegex(UIError, 'microphone.*deadline'):
            self.ui.start_voice()
        self.mic.click.assert_not_called()

    def test_parent_deadline_is_not_renewed_for_microphone(self):
        self.ui.open.side_effect = lambda: setattr(self.clock, 'now', 230)
        self.state = lambda: mic_state()
        with self.assertRaisesRegex(UIError, 'microphone'):
            self.ui.start_voice()
        self.assertEqual(self.clock.now, 238)
        self.ui.capture_ready.assert_not_called()
        self.assertIsNone(self.ui._startup_deadline)

    def test_dialog_presence_is_logged_without_page_text(self):
        self.state = lambda: dict(mic_state(dialog=True, alert=True),
                                 ignored_page_text='private fixture text')
        with self.assertRaisesRegex(UIError, 'microphone.*controls_missing'):
            self.ui.start_voice()
        output = '\n'.join(str(c.args[0]) for c in self.logs.call_args_list)
        self.assertIn('dialog=True', output)
        self.assertIn('alert=True', output)
        self.assertNotIn('private fixture text', output)
        self.assertLess(self.logs.call_count, 15)  # No log on every unchanged poll.

    def test_click_attempt_is_logged_before_side_effect_and_stages_are_explicit(self):
        self.state = lambda: mic_state(on=1) if self.clicked else mic_state(off=1, element=self.mic)
        def click():
            output = '\n'.join(str(c.args[0]) for c in self.logs.call_args_list)
            self.assertIn('stage=microphone-control status=mic_off', output)
            self.assertIn('stage=microphone-click status=attempted', output)
            self.clicked = True
        self.mic.click.side_effect = click
        self.assertTrue(self.ui.start_voice()['ok'])
        output = '\n'.join(str(c.args[0]) for c in self.logs.call_args_list)
        for stage in ('microphone-click status=accepted', 'microphone-active status=ready'):
            self.assertIn('stage=' + stage, output)
        self.assertIn('remaining=', output)
        self.mic.click.assert_called_once()

    def test_late_diagnostic_before_click_cannot_extend_deadline(self):
        self.state = lambda: mic_state(off=1, element=self.mic)
        def log(message, **kwargs):
            if 'stage=microphone-click status=attempted' in message:
                self.clock.now = 31
        self.logs.side_effect = log
        with self.assertRaisesRegex(UIError, 'microphone.*deadline'):
            self.ui.start_voice()
        self.mic.click.assert_not_called()

    def test_stale_click_can_be_confirmed_without_repeating(self):
        self.state = lambda: mic_state(on=1) if self.clicked else mic_state(off=1, element=self.mic)
        def click():
            self.clicked = True
            raise StaleElementReferenceException()
        self.mic.click.side_effect = click
        self.assertTrue(self.ui.start_voice()['ok'])
        self.mic.click.assert_called_once()
        output = '\n'.join(str(c.args[0]) for c in self.logs.call_args_list)
        self.assertIn('stage=microphone-click status=outcome_unknown', output)
        self.assertIn('stage=microphone-active status=ready', output)

    def test_microphone_failure_closes_mode_only_after_confirmation(self):
        self.state = lambda: mic_state(off=1, element=self.mic)
        from browser_worker import perform
        with patch('browser_worker.ui_failure'):
            result = perform(self.ui, 'open', 240)
        self.assertFalse(result['ok'])
        helper, g, job = StartupModeTests().make('open')
        closing = Future()
        helper.bridge.submit.side_effect = None
        helper.bridge.submit.return_value = closing
        job.set_result(result)
        g.tick(1)
        self.assertTrue(g.active)
        self.assertFalse(g.closed_confirmed)
        helper.bridge.submit.assert_called_once_with('close')
        closing.set_result(dict(ok=True, detail='owned descendants reaped'))
        for t in (2, 3, 4, 5): g.tick(t)
        self.assertTrue(g.closed_confirmed)
        self.assertFalse(g.active)
        helper.bridge.submit.assert_called_once_with('close')
        self.ui.capture_ready.assert_not_called()


class StartupModeTests(unittest.TestCase):
    def make(self, action):
        helper = test_chatgpt.ModeTests()
        g = helper.make(LARISA_MODE if action == 'open' else ANTON_MODE)
        g.mode = LOCAL_MODE
        g.phase = 'opening'
        g.cfg['gpt_startup_timeout'] = 240
        job = Future()
        g.job, g.action = job, action
        g.tick(0)
        return helper, g, job

    def test_slow_startup_exceeds_generic_90_seconds_for_both_modes(self):
        for action, mode in [('open', LARISA_MODE), ('open_anton', ANTON_MODE)]:
            with self.subTest(action=action):
                helper, g, job = self.make(action)
                g.tick(91)
                self.assertFalse(g.emergency)
                helper.bridge.submit.assert_not_called()
                job.set_result({'ok': True, 'detail': 'ready'})
                g.tick(150)
                self.assertEqual(g.mode, mode)
                self.assertFalse(g.closed_confirmed)

    def test_hung_startup_closes_once_and_waits_for_confirmation(self):
        helper, g, job = self.make('open')
        closed = Future()
        helper.bridge.submit.return_value = closed
        helper.bridge.submit.side_effect = None
        g.tick(240)
        helper.bridge.submit.assert_called_once_with('close')
        self.assertTrue(g.active)
        self.assertFalse(g.closed_confirmed)
        job.set_result({'ok': True, 'detail': 'late result'})
        g.tick(241)
        self.assertEqual(g.phase, 'closing')
        closed.set_result({'ok': True, 'detail': 'owned descendants reaped'})
        g.tick(242); g.tick(243); g.tick(245)
        self.assertTrue(g.closed_confirmed)
        self.assertFalse(g.active)
        self.assertEqual(g.mode, LOCAL_MODE)
        helper.bridge.submit.assert_called_once_with('close')

    def test_failed_startup_closes_without_send(self):
        helper, g, job = self.make('open')
        job.set_result({'ok': False, 'detail': 'Start Voice unavailable'})
        g.tick(1); g.tick(2); g.tick(3); g.tick(4)
        self.assertTrue(g.closed_confirmed)
        self.assertFalse(g.active)
        helper.bridge.submit.assert_called_once_with('close')


class BridgeStartupClockTests(unittest.TestCase):
    def test_controller_watchdog_uses_startup_budget_and_transmits_deadline(self):
        clock = Clock()
        bridge = Bridge({'gpt_startup_timeout': 240})
        bridge.confirmed = False
        future = Future()
        bridge.pending = (1, 'open', future, 0)
        sent = []
        process = Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        channel = Mock(eof=False)
        channel.send.side_effect = sent.append
        calls = 0
        def pump(_):
            nonlocal calls
            calls += 1
            if calls == 1:
                clock.now = 91
            elif calls == 2:
                self.assertIsNone(bridge.close_future, 'generic 90s watchdog killed startup')
                clock.now = 240
            elif calls == 3:
                self.assertIsNotNone(bridge.close_future)
                return [{'type': 'closed', 'ok': True, 'detail': 'fixture reaped'}]
            else:
                self.fail('controller did not finish')
            return []
        channel.pump.side_effect = pump
        with patch('chatgpt_bridge.time', clock), patch('chatgpt_bridge.Channel', return_value=channel), \
                patch('chatgpt_bridge.subprocess.Popen', return_value=process):
            bridge._manage(0)
        self.assertTrue(bridge.confirmed)
        self.assertTrue(future.result()['interrupted'])
        jobs = [m for m in sent if m.get('type') == 'job']
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['startup_deadline'], 240)
        self.assertEqual(sum(m.get('type') == 'close' for m in sent), 1)


class StartupBudgetTests(unittest.TestCase):
    def test_all_ui_stages_share_parent_deadline(self):
        clock = Clock()
        ui = ChatGPTUI({'gpt_startup_timeout': 240})
        def slow_open(): clock.now = 220
        ui.open = slow_open
        ui.driver = Mock()
        ui.driver.find_elements.return_value = []
        ui.snapshot = Mock()
        from browser_worker import perform
        with patch('chatgpt_ui.time', clock), patch('browser_worker.ui_failure'):
            self.assertFalse(perform(ui, 'open', startup_deadline=240)['ok'])
        self.assertEqual(clock.now, 238)  # No fresh 30 seconds after slow open.
        ui.snapshot.assert_not_called()
        self.assertIsNone(ui._startup_deadline)

    def test_startup_transport_capped_and_restored_for_later_actions(self):
        clock = Clock()
        cfg = SimpleNamespace(timeout=50)
        observed = []
        def execute(command, params):
            observed.append(cfg.timeout)
            return {'value': None}
        driver = SimpleNamespace(execute=execute, command_executor=SimpleNamespace(client_config=cfg))
        ui = ChatGPTUI({'gpt_startup_timeout': 240})
        ui.driver = driver
        with patch('chatgpt_ui.time', clock):
            with ui.startup(240):
                with ui.startup():  # Nested open() must not renew budget.
                    clock.now = 230
                    driver.execute('fixture')
                    clock.now = 238
                    with self.assertRaises(UIError): driver.execute('expired')
            self.assertIs(driver.execute, execute)
            self.assertEqual(cfg.timeout, 50)
            self.assertEqual(observed, [8])

    def test_open_anton_worker_uses_same_deadline_without_starting_voice(self):
        from browser_worker import perform
        clock = Clock()
        ui = ChatGPTUI({'gpt_startup_timeout': 240})
        seen = []
        def open_ui(): seen.append(ui._startup_deadline)
        ui.open = open_ui
        ui.start_voice = Mock()
        with patch('chatgpt_ui.time', clock):
            self.assertTrue(perform(ui, 'open_anton', 240)['ok'])
        self.assertEqual(seen, [238])
        ui.start_voice.assert_not_called()
        self.assertIsNone(ui._startup_deadline)

    def test_budget_validation_and_ordinary_job_limits(self):
        from browser_timing import job_timeout
        for bad in (0, -1, float('inf'), float('nan'), 601):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Bridge({'gpt_startup_timeout': bad})
        for action in ('send', 'poll', 'dictate', 'transcribe', 'abort_dictation', 'close'):
            self.assertEqual(job_timeout({'gpt_startup_timeout': 240}, action), 90)
        self.assertEqual(job_timeout({'gpt_job_timeout': 91}, 'open'), 240)
        self.assertEqual(job_timeout({'gpt_job_timeout': 91}, 'open_anton'), 240)


# Real OS processes, with Event/pipe synchronization reused from the shutdown suite.
import test_bridge_shutdown as shutdown_fixtures


class HangingStartupUI(shutdown_fixtures.FakeUI):
    def start_voice(self):
        return self.poll()  # Signals 'entered', then waits forever, ignoring SIGTERM.


class StartupProcessTests(unittest.TestCase):
    def test_hung_open_worker_and_descendants_reaped_at_startup_limit(self):
        helper = shutdown_fixtures.ShutdownTests()
        helper.addCleanup = self.addCleanup
        helper.setUp()
        bridge = helper.bridge('job_hang', gpt_startup_timeout=.7)
        bridge.factory = 'test_voice_startup:HangingStartupUI'
        job = bridge.submit('open')
        reader = helper.connect()
        helper.event(reader, 'ready')
        helper.event(reader, 'entered')
        result = job.result(timeout=5)
        self.assertFalse(result['ok'])
        self.assertTrue(result['interrupted'])
        self.assertTrue(bridge.close()['ok'])
        self.assertEqual(bridge.sequence, 1)  # No delayed Send or other UI jobs.
        helper.gone(bridge)
