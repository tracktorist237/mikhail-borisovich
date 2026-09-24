"""Recorder tests: generated PCM and fake input/Speech, no real audio devices."""
from array import array
import contextlib
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import wave

import audio_replay
import record_test_corpus as recorder


def pcm(value=1200, seconds=5):
    # Actual leading/trailing silence; no generated speech or personal data.
    count = int(16000 * seconds)
    return array('h', [0] * (count // 5) + [value] * (count * 3 // 5)
                 + [0] * (count - count // 5 - count * 3 // 5)).tobytes()


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mb-recorder-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'corpus'
        self.events = []
        self.takes = [pcm()]
        owner = self

        class Speech:
            def say(self, text):
                owner.events.append(('speech_start', text))
                owner.events.append(('speech_end', text))
            def close(self): owner.events.append(('speech_close',))

        class Input:
            def capture(self):
                owner.events.append(('capture',))
                value = owner.takes.pop(0)
                if isinstance(value, BaseException):
                    raise value
                return value
            def close(self): owner.events.append(('input_close',))

        self.speech_factory = Mock(side_effect=Speech)
        self.input_factory = Mock(side_effect=Input)

    def run_record(self, **kwargs):
        args = dict(takes=1, phrase_ids=['local-time'],
                    speech_factory=self.speech_factory, input_factory=self.input_factory,
                    decide=Mock(return_value='keep'),
                    now=lambda: datetime(2026, 9, 24, 12, 30, 0), emit=lambda message: None)
        args.update(kwargs)
        return recorder.record(self.root, **args)

    def test_new_external_corpus_and_wav_format(self):
        result = self.run_record()
        corpus = audio_replay.load_corpus(self.root)
        self.assertEqual(result['accepted'], 1)
        clip = corpus.clips[0]
        self.assertEqual(clip['id'], 'local-time-01')
        self.assertEqual(clip['expected'], {'transcript': 'который час', 'intent': 'TIME'})
        with wave.open(str(self.root / clip['wav']), 'rb') as wav:
            self.assertEqual(wav.getparams()[:4], (1, 2, 16000, 80000))
            self.assertEqual(wav.readframes(80000), pcm())

    def test_inside_git_rejected_before_devices(self):
        self.root = audio_replay.ROOT
        with self.assertRaises(audio_replay.CorpusError):
            self.run_record()
        self.speech_factory.assert_not_called()
        self.input_factory.assert_not_called()

    def test_existing_manifest_preserved_new_session_unique_same_clock(self):
        self.run_record()
        first = json.loads((self.root / 'manifest.json').read_text())['clips'][0]
        original = (self.root / first['wav']).read_bytes()
        self.takes = [pcm(1500)]
        self.run_record()
        clips = audio_replay.load_corpus(self.root).clips
        self.assertEqual(clips[0], first)
        self.assertNotEqual(clips[0]['id'], clips[1]['id'])
        self.assertNotEqual(Path(clips[0]['wav']).parent, Path(clips[1]['wav']).parent)
        self.assertEqual((self.root / first['wav']).read_bytes(), original)

    def test_resume_skips_existing_take_without_devices(self):
        self.run_record()
        before = (self.root / 'manifest.json').read_bytes()
        self.input_factory.reset_mock()
        self.speech_factory.reset_mock()
        result = self.run_record(resume=True)
        self.assertEqual(result['resumed'], 1)
        self.assertEqual(before, (self.root / 'manifest.json').read_bytes())
        self.input_factory.assert_not_called()
        self.speech_factory.assert_not_called()

    def test_resume_fills_missing_take_without_overwriting(self):
        self.run_record()
        self.takes = [pcm(), pcm()]
        result = self.run_record(takes=3, resume=True)
        self.assertEqual(result['resumed'], 1)
        self.assertEqual([c['id'] for c in audio_replay.load_corpus(self.root).clips],
                         ['local-time-01', 'local-time-02', 'local-time-03'])

    def test_prompt_and_ready_playback_end_before_capture(self):
        self.run_record()
        capture = self.events.index(('capture',))
        self.assertEqual(self.events[capture-1], ('speech_end', recorder.READY))
        self.assertTrue(any(e[0] == 'speech_end' and e[1].startswith('Следующая фраза:')
                            for e in self.events[:capture]))

    def test_interrupt_during_capture_leaves_no_partial_wav(self):
        self.takes = [KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.run_record()
        self.assertEqual(list(self.root.rglob('*.wav')), [])
        self.assertEqual(list(self.root.rglob('*.tmp')), [])
        self.assertFalse((self.root / 'manifest.json').exists())
        self.assertIn(('input_close',), self.events)
        self.assertIn(('speech_close',), self.events)

    def test_interrupt_keeps_completed_take_and_valid_manifest(self):
        self.takes = [pcm(), KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.run_record(takes=2)
        self.assertEqual(len(audio_replay.load_corpus(self.root).clips), 1)
        self.assertEqual(len(list(self.root.rglob('*.wav'))), 1)

    def test_speech_not_found_retry_preserves_both_wavs(self):
        self.takes = [pcm(0), pcm()]
        decide = Mock(return_value='retry')
        result = self.run_record(decide=decide)
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(len(list(self.root.rglob('*.wav'))), 2)
        self.assertEqual(len(audio_replay.load_corpus(self.root).clips), 1)
        self.assertFalse(decide.call_args.args[0]['speech_found'])

    def test_clipping_retry_keep_skip(self):
        for choice in ('retry', 'keep', 'skip'):
            with self.subTest(choice=choice):
                self.root = Path(self.tmp.name) / choice
                self.takes = [pcm(32767)] + ([pcm()] if choice == 'retry' else [])
                decide = Mock(return_value=choice)
                result = self.run_record(decide=decide)
                self.assertIn('clipping', decide.call_args.args[0]['warnings'])
                self.assertEqual(result['accepted'], int(choice != 'skip'))
                self.assertEqual(len(list(self.root.rglob('*.wav'))), 2 if choice == 'retry' else 1)
                if choice == 'keep':
                    self.assertIn('quality-warning', audio_replay.load_corpus(self.root).clips[0]['tags'])

    def test_short_audio_is_quality_warning_not_trimmed(self):
        self.takes = [pcm(seconds=.5)]
        decide = Mock(return_value='keep')
        self.run_record(decide=decide)
        self.assertIn('too_short', decide.call_args.args[0]['warnings'])
        clip = audio_replay.load_corpus(self.root).clips[0]
        with wave.open(str(self.root / clip['wav'])) as wav:
            self.assertEqual(wav.getnframes(), 8000)

    def test_expected_from_definitions_no_vosk_or_matcher(self):
        with patch.object(audio_replay.production, 'match_intent', side_effect=AssertionError), \
                patch.object(audio_replay.production, 'dependencies', side_effect=AssertionError):
            self.run_record(phrase_ids=['anton-open'])
        self.assertEqual(audio_replay.load_corpus(self.root).clips[0]['expected'],
                         {'transcript': 'позови антона павловича', 'intent': 'ANTON_OPEN'})

    def test_planned_case_has_no_supported_intent(self):
        self.run_record(phrase_ids=['atomic-return-planned'])
        clip = audio_replay.load_corpus(self.root).clips[0]
        self.assertEqual(clip['expected'], {'transcript': 'михаил вернись'})
        self.assertIn('planned', clip['tags'])

    def test_resume_preserves_promoted_atomic_manifest_without_devices(self):
        self.run_record(phrase_ids=['atomic-return-planned'])
        path = self.root / 'manifest.json'
        data = json.loads(path.read_text())
        clip = data['clips'][0]
        clip['expected']['control'] = 'ATOMIC_RETURN'
        clip['control_phase'] = 'opening'
        clip['tags'].remove('planned')
        path.write_text(json.dumps(data))
        before = path.read_bytes()
        self.input_factory.reset_mock()
        self.speech_factory.reset_mock()
        result = self.run_record(phrase_ids=['atomic-return-planned'], resume=True)
        self.assertEqual(result['resumed'], 1)
        self.assertEqual(path.read_bytes(), before)
        self.input_factory.assert_not_called()
        self.speech_factory.assert_not_called()
        # A different control outcome is still a mismatched ground truth.
        clip['expected']['control'] = 'RETURN'
        path.write_text(json.dumps(data))
        with self.assertRaises(recorder.RecorderError):
            self.run_record(phrase_ids=['atomic-return-planned'], resume=True)

    def test_all_prompts_compatible_with_manifest_v1(self):
        self.takes = [pcm()] * len(recorder.PROMPTS)
        self.run_record(phrase_ids=None)
        clips = audio_replay.load_corpus(self.root).clips
        self.assertEqual(len(clips), 12)
        self.assertEqual(3 * len(recorder.PROMPTS), 36)

    def test_atomic_wav_never_overwrites_and_removes_temp(self):
        self.root.mkdir()
        target = self.root / 'take.wav'
        recorder.save_wav(target, pcm())
        before = target.read_bytes()
        with self.assertRaises(FileExistsError):
            recorder.save_wav(target, pcm(32767))
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_wav_is_closed_and_fsynced_before_publication(self):
        self.root.mkdir()
        events = []
        link, fsync = recorder.os.link, recorder.os.fsync
        def sync(fd):
            fsync(fd)
            events.append('fsync')
        def publish(source, target):
            self.assertEqual(events, ['fsync'])
            audio_replay.validate_wav(source)
            events.append('publish')
            link(source, target)
        with patch.object(recorder.os, 'fsync', side_effect=sync), \
                patch.object(recorder.os, 'link', side_effect=publish):
            recorder.save_wav(self.root / 'take.wav', pcm())
        self.assertEqual(events, ['fsync', 'publish', 'fsync'])

    def test_interrupt_before_wav_publish_cleans_temp(self):
        self.root.mkdir()
        target = self.root / 'take.wav'
        with patch.object(recorder.os, 'link', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            recorder.save_wav(target, pcm())
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_interrupted_manifest_publish_preserves_old_manifest(self):
        self.run_record()
        before = (self.root / 'manifest.json').read_bytes()
        self.takes = [pcm()]
        with patch.object(recorder.os, 'replace', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.run_record()
        self.assertEqual((self.root / 'manifest.json').read_bytes(), before)
        self.assertEqual(len(audio_replay.load_corpus(self.root).clips), 1)
        self.assertEqual(list(self.root.rglob('*.tmp')), [])
        self.assertEqual(len(list(self.root.rglob('*.wav'))), 2)  # Completed, unindexed take retained.

    def test_symlink_wav_directory_escape_rejected(self):
        self.root.mkdir()
        (self.root / 'wav').symlink_to(Path(self.tmp.name), target_is_directory=True)
        with self.assertRaises(audio_replay.CorpusError):
            self.run_record()
        self.input_factory.assert_not_called()

    def test_second_writer_rejected(self):
        with recorder.CorpusSession(self.root, now=lambda: datetime(2026, 9, 24)):
            with self.assertRaisesRegex(recorder.RecorderError, 'уже'):
                self.run_record()

    def test_manifest_external_change_is_not_overwritten(self):
        self.run_record()
        prompt = next(p for p in recorder.PROMPTS if p.id == 'local-time')
        with recorder.CorpusSession(self.root) as store:
            path = store.save('local-time-02', pcm())
            altered = store.original + b'\n'
            (self.root / 'manifest.json').write_bytes(altered)
            with self.assertRaisesRegex(recorder.RecorderError, 'другим процессом'):
                store.accept(prompt, 'local-time-02', path, recorder.quality(pcm()))
            self.assertEqual((self.root / 'manifest.json').read_bytes(), altered)
            self.assertTrue(path.exists())

    def test_manifest_change_during_validation_is_rejected(self):
        self.run_record()
        load = audio_replay.load_corpus
        def modified(root):
            corpus = load(root)
            with (root / 'manifest.json').open('ab') as output:
                output.write(b'\n')
            return corpus
        self.input_factory.reset_mock()
        with patch.object(audio_replay, 'load_corpus', side_effect=modified), \
                self.assertRaisesRegex(recorder.RecorderError, 'во время проверки'):
            self.run_record()
        self.input_factory.assert_not_called()

    def test_bad_quality_interruption_retains_completed_wav_outside_manifest(self):
        self.takes = [pcm(0)]
        with self.assertRaises(KeyboardInterrupt):
            self.run_record(decide=Mock(side_effect=KeyboardInterrupt))
        files = list(self.root.rglob('*.wav'))
        self.assertEqual(len(files), 1)
        audio_replay.validate_wav(files[0])
        self.assertEqual(list(self.root.rglob('*.tmp')), [])

    def test_invalid_flags_do_not_create_corpus(self):
        for kwargs in ({'takes': 0}, {'takes': 100}, {'phrase_ids': ['wrong']}):
            with self.subTest(kwargs=kwargs), self.assertRaises(recorder.RecorderError):
                self.run_record(**kwargs)
        self.assertFalse(self.root.exists())

    def test_cli_interrupt_exit_code(self):
        with patch.object(recorder, 'record', side_effect=KeyboardInterrupt), \
                contextlib.redirect_stderr(io.StringIO()) as out:
            self.assertEqual(recorder.cli(['--corpus', str(self.root)]), 130)
        self.assertNotIn(str(self.root), out.getvalue())

    def test_list_does_not_create_corpus_or_open_devices(self):
        with contextlib.redirect_stdout(io.StringIO()) as output, patch.object(recorder, 'record') as record:
            code = recorder.cli(['--list'])
        self.assertEqual(code, 0)
        self.assertIn('atomic-return-planned', output.getvalue())
        self.assertFalse(self.root.exists())
        record.assert_not_called()


class InputAndSpeechTests(unittest.TestCase):
    def test_capture_interrupt_closes_real_adapter_fake_stream(self):
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        stream.read.side_effect = KeyboardInterrupt
        sd = Mock(RawInputStream=Mock(return_value=stream))
        source = recorder.LiveInput({'input_device': None}, sd=sd)
        with self.assertRaises(KeyboardInterrupt):
            source.capture()
        stream.__exit__.assert_called_once()
        sd.check_input_settings.assert_called_once_with(device=None, channels=1, dtype='int16', samplerate=16000)

    def test_exact_capture_frames_and_overflow_abort(self):
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        block = pcm(seconds=.1)
        stream.read.return_value = (block, False)
        sd = Mock(RawInputStream=Mock(return_value=stream))
        source = recorder.LiveInput({'input_device': None}, sd=sd)
        self.assertEqual(source.capture(), block * 50)
        self.assertEqual(stream.read.call_count, 50)
        stream.read.return_value = (block, True)
        with self.assertRaises(recorder.RecorderError):
            source.capture()

    def test_unsupported_16k_rejected(self):
        sd = Mock()
        sd.check_input_settings.side_effect = RuntimeError('unsupported')
        with self.assertRaisesRegex(recorder.RecorderError, '16000'):
            recorder.LiveInput({'input_device': None}, sd=sd)
        sd.RawInputStream.assert_not_called()

    def test_portaudio_open_error_is_clear_and_does_not_change_settings(self):
        class PortAudioError(Exception):
            pass
        sd = Mock(PortAudioError=PortAudioError)
        sd.RawInputStream.side_effect = PortAudioError('private device detail')
        source = recorder.LiveInput({'input_device': None}, sd=sd)
        with self.assertRaisesRegex(recorder.RecorderError, '16000') as caught:
            source.capture()
        self.assertNotIn('private', str(caught.exception))

    def test_blocking_speech_waits_for_playback_not_only_synthesis(self):
        speech = Mock()
        speech.busy.side_effect = [True, True, False]
        with patch.object(audio_replay.production, 'Speech', return_value=speech):
            wrapper = recorder.BlockingSpeech({})
            wrapper.say('Сигнал')
        self.assertEqual(speech.process.wait.call_count, 2)
        self.assertEqual(speech.busy.call_count, 3)

    def test_voice_quality_choice_bounded_and_model_reused(self):
        speech, source = Mock(), Mock()
        source.capture.return_value = pcm()
        responses = iter(['оставить', 'пропустить', 'повторить', '', '', ''])
        class Rec:
            def __init__(self, model, rate, grammar):
                self.text = next(responses)
            def AcceptWaveform(self, data): return False
            def FinalResult(self): return json.dumps({'text': self.text})
        with patch.object(recorder.production, 'dependencies', return_value=(None, Mock(), Rec)), \
                patch.object(recorder.production, 'load_model', return_value=object()) as load:
            choice = recorder.VoiceChoice(speech, source, {})
            self.assertEqual(choice({}), 'keep')
            self.assertEqual(choice({}), 'skip')
            self.assertEqual(choice({}), 'retry')
            with self.assertRaisesRegex(recorder.RecorderError, 'Выбор не распознан'):
                choice({})
        load.assert_called_once()
        self.assertEqual(source.capture.call_count, 6)

    def test_quality_numbers_and_negative_clipping(self):
        data = array('h', [-32768] * 1600).tobytes()
        result = recorder.quality(data)
        self.assertEqual(result['peak'], 32768)
        self.assertEqual(result['active_rms'], 32768)
        self.assertEqual(result['clipping_fraction'], 1)
        self.assertEqual(result['duration'], .1)
        self.assertTrue(result['speech_found'])


if __name__ == '__main__':
    unittest.main()
