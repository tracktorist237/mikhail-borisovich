import gc
import json
import unittest
import threading
import weakref
from unittest.mock import Mock, patch
import main
import wake_perf


class FreshWakeTests(unittest.TestCase):
    def test_reset_releases_backend_without_retaining_model_or_old_backend(self):
        class Model: pass
        class Backend:
            def __init__(self, model): self.model = model
            def AcceptWaveform(self, pcm): return False
        model = Model(); model_ref = weakref.ref(model)
        wake = main.FreshWakeRecognizer(lambda: Backend(model))
        old = weakref.ref(wake.rec)
        wake.Reset(); gc.collect()
        self.assertIsNone(old()); self.assertIsNotNone(model_ref())
        wake.Reset();self.assertIsNone(wake.rec)
        wake.AcceptWaveform(b'\0\0'); current = weakref.ref(wake.rec)
        del wake, model; gc.collect()
        self.assertIsNone(current());self.assertIsNone(model_ref())

    def test_actual_listen_creates_only_outside_callback_and_active_playback(self):
        import replay_support
        import loop_replay
        import test_loop_replay as fixtures
        from replay_support import ReplayRuntime
        f=fixtures.LoopTests('test_normal_open_uses_production_modes_no_early_send')
        f.setUp();self.addCleanup(f.doCleanups)
        created=[];runtimes=[];inside_callback=False
        class Runtime(ReplayRuntime):
            def __init__(self,*args,**kwargs):
                super().__init__(*args,**kwargs);runtimes.append(self)
        class Recognizer(fixtures.MarkerRecognizer):
            def __init__(self,*args):
                self.assert_safe()
                super().__init__(*args)
                if self.role=='wake':created.append(threading.get_ident())
            def assert_safe(self):
                if inside_callback:raise AssertionError('constructor inside callback')
                if runtimes and runtimes[-1].tts.busy():raise AssertionError('constructor during playback')
        original_put=main.FreshAudioQueue.put_latest
        def callback_put(q,*args,**kwargs):
            nonlocal inside_callback
            inside_callback=True
            try:return original_put(q,*args,**kwargs)
            finally:inside_callback=False
        with patch.object(loop_replay,'ReplayRuntime',Runtime), patch.object(main.FreshAudioQueue,'put_latest',callback_put):
            result=loop_replay.run_case(f.corpus,f.case,{},None,Recognizer,require_vosk=False)
        self.assertEqual(result['result'],'PASS',result['errors'])
        self.assertEqual(created,[threading.get_ident()]*2) # initial discarded + first admitted utterance

    def test_reset_and_idle_never_create_until_next_admitted_call(self):
        factory = Mock(return_value=Mock())
        wake = main.FreshWakeRecognizer(factory)
        for _ in range(20): wake.Reset()
        self.assertEqual(factory.call_count, 1)
        wake.AcceptWaveform(b'\0\0');wake.PartialResult();wake.AcceptWaveform(b'\0\0')
        self.assertEqual(factory.call_count, 2)


class PerfTests(unittest.TestCase):
    def test_timing_first_call_only_values_and_privacy(self):
        times = iter([1,1.02,2,2.003])
        rows=[];m=wake_perf.WakeMetrics(clock=lambda:next(times),emit=rows.append)
        backend=Mock();backend.AcceptWaveform.return_value=True
        rec=m.factory(lambda:backend)()
        self.assertTrue(rec.AcceptWaveform(b'private PCM'))
        self.assertTrue(rec.AcceptWaveform(b'other PCM'))
        rec.PartialResult();backend.PartialResult.assert_called_once()
        self.assertEqual(m.count,1);self.assertEqual(m.first_count,1);self.assertEqual(len(rows),1)
        self.assertAlmostEqual(rows[0]['create_ms'],20);self.assertAlmostEqual(rows[0]['first_accept_ms'],3)
        self.assertNotIn('PCM',json.dumps(m.summary()))

    def test_instrumentation_is_scoped_and_does_not_retain_backends(self):
        original=main.FreshWakeRecognizer;m=wake_perf.WakeMetrics()
        with wake_perf.instrument_fresh_wake(m):
            w=main.FreshWakeRecognizer(lambda:Mock())
            ref=weakref.ref(w.rec.backend)
            w.Reset();gc.collect();self.assertIsNone(ref())
            self.assertEqual(m.count,1)
            w.AcceptWaveform(b'');self.assertEqual(m.count,2)
        self.assertIs(main.FreshWakeRecognizer,original)

    def test_offline_loads_model_once_and_no_devices(self):
        Model,Rec=Mock(),Mock()
        cfg={'model_path':'unused','sample_rate':16000}
        with patch.object(main,'load_model',return_value=object()) as load, patch.object(main,'dependencies',side_effect=AssertionError):
            result=wake_perf.offline(3,cfg,Model,Rec,wake_perf.WakeMetrics())
        load.assert_called_once();self.assertEqual(Rec.call_count,3)
        self.assertEqual(result['creations'],3);self.assertEqual(result['first_accepts'],3)
        for call in Rec.return_value.AcceptWaveform.call_args_list:
            self.assertEqual(call.args,(bytes(3200),))

    def test_live_launcher_calls_original_main_and_restores_class(self):
        original=main.FreshWakeRecognizer
        def fake_main():
            self.assertIsNot(main.FreshWakeRecognizer,original)
            return 7
        with patch.object(main,'main',side_effect=fake_main) as entry, patch.object(wake_perf,'cleanup_log'), patch.object(wake_perf.asr,'git_identity',return_value={}):
            self.assertEqual(wake_perf.cli(['--live']),7)
        entry.assert_called_once();self.assertIs(main.FreshWakeRecognizer,original)

    def test_summary_window_bounded_and_mean_all_generations(self):
        m=wake_perf.WakeMetrics(clock=lambda:0)
        for _ in range(1005):m.factory(lambda:None)()
        self.assertEqual(len(m.summary()['samples']),1000)
        self.assertEqual(m.summary()['creations'],1005)


if __name__=='__main__':unittest.main()
