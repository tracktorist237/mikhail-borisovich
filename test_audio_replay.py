"""Corpus/streaming tests use generated PCM and scripted ASR, never a microphone."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import wave

import main as production
import audio_replay as replay


class CorpusFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mb-corpus-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.wav()
        self.clips = [{'id': 'time-01', 'wav': 'time.wav',
                       'expected': {'transcript': 'который час', 'intent': 'TIME'},
                       'tags': ['normal']}]
        self.manifest()

    def wav(self, name='time.wav', channels=1, width=2, rate=16000, frames=4800):
        with wave.open(str(self.root / name), 'wb') as out:
            out.setnchannels(channels)
            out.setsampwidth(width)
            out.setframerate(rate)
            out.writeframes(b'\0' * frames * width * channels)

    def manifest(self):
        (self.root / 'manifest.json').write_text(json.dumps(
            {'schema_version': 1, 'clips': self.clips}), encoding='utf-8')

    def corpus(self):
        return replay.load_corpus(self.root)


class CorpusTests(CorpusFixture):
    def test_valid_pcm16_mono_16k(self):
        corpus = self.corpus()
        self.assertEqual(corpus.clips[0]['id'], 'time-01')
        self.assertEqual(len(corpus.wav_hashes['time-01']), 64)

    def test_wrong_formats_rejected(self):
        for kwargs, reason in [({'channels': 2}, 'mono'), ({'rate': 8000}, '16000'),
                               ({'width': 1}, '16-bit'), ({'width': 3}, '16-bit')]:
            with self.subTest(kwargs=kwargs):
                self.wav(**kwargs)
                with self.assertRaisesRegex(replay.CorpusError, reason):
                    self.corpus()

    def test_malformed_wav(self):
        (self.root / 'time.wav').write_bytes(b'not a WAV')
        with self.assertRaisesRegex(replay.CorpusError, 'WAV'):
            self.corpus()

    def test_truncated_wav(self):
        path = self.root / 'time.wav'
        path.write_bytes(path.read_bytes()[:-10])
        with self.assertRaisesRegex(replay.CorpusError, 'truncated'):
            self.corpus()

    def test_compressed_wav(self):
        path = self.root / 'time.wav'
        data = bytearray(path.read_bytes())
        data[20:22] = (3).to_bytes(2, 'little')  # IEEE float is not integer PCM.
        path.write_bytes(data)
        with self.assertRaisesRegex(replay.CorpusError, 'WAV'):
            self.corpus()

    def test_empty_wav(self):
        self.wav(frames=0)
        with self.assertRaisesRegex(replay.CorpusError, 'empty'):
            self.corpus()

    def test_relative_path_escape_rejected(self):
        for name in ('../time.wav', 'wav/../time.wav', '/tmp/time.wav', 'C:\\time.wav'):
            with self.subTest(name=name):
                self.clips[0]['wav'] = name
                self.manifest()
                with self.assertRaises(replay.CorpusError):
                    self.corpus()

    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            (self.root / 'escape.wav').symlink_to(Path(outside) / 'voice.wav')
            self.clips[0]['wav'] = 'escape.wav'
            self.manifest()
            with self.assertRaisesRegex(replay.CorpusError, 'escapes'):
                self.corpus()

    def test_manifest_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            (self.root / 'manifest.json').unlink()
            (self.root / 'manifest.json').symlink_to(Path(outside) / 'manifest.json')
            with self.assertRaisesRegex(replay.CorpusError, 'escapes'):
                self.corpus()

    def test_duplicate_ids_rejected(self):
        self.clips.append(dict(self.clips[0]))
        self.manifest()
        with self.assertRaisesRegex(replay.CorpusError, 'Duplicate'):
            self.corpus()

    def test_inside_project_worktree_rejected_without_writes(self):
        with self.assertRaisesRegex(replay.CorpusError, 'worktree'):
            replay.load_corpus(replay.ROOT)

    def test_symlink_into_worktree_rejected(self):
        link = self.root / 'repo'
        link.symlink_to(replay.ROOT, target_is_directory=True)
        with self.assertRaisesRegex(replay.CorpusError, 'worktree'):
            replay.load_corpus(link)

    def test_other_git_worktree_rejected(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        with self.assertRaisesRegex(replay.CorpusError, 'worktree'):
            self.corpus()

    def test_unknown_fields_and_bad_types_rejected(self):
        for change in ({'code': 'print(1)'}, {'tags': 'normal'},
                       {'expected': {'wake': 1}}, {'expected': {'intent': 5}},
                       {'expected': {'transcript': None}}, {'expected': {'shell': 'true'}},
                       {'expected': {}}, {'expected': {'wake': True, 'intent': 'TIME'}}):
            with self.subTest(change=change):
                self.clips = [{'id': 'time-01', 'wav': 'time.wav',
                               'expected': {'intent': 'TIME'}, **change}]
                self.manifest()
                with self.assertRaises(replay.CorpusError):
                    self.corpus()

    def test_unknown_top_level_and_duplicate_json_fields(self):
        for document in ('{"schema_version":1,"clips":[],"exec":"true"}',
                         '{"schema_version":1,"schema_version":1,"clips":[]}'):
            (self.root / 'manifest.json').write_text(document)
            with self.assertRaises(replay.CorpusError):
                self.corpus()

    def test_missing_corpus_cli_nonzero(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), patch.object(production, 'dependencies') as deps:
            code = replay.cli(['--corpus', str(self.root / 'missing'), '--mode', 'asr'])
        self.assertEqual(code, 2)
        self.assertIn('does not exist', err.getvalue())
        deps.assert_not_called()


class ASRTests(CorpusFixture):
    def run_replay(self, events=None, eof='который час', cases=None, tags=None):
        events = events if events is not None else [(False, ''), (True, 'который час'), (False, '')]
        recognizers = []

        class Rec:
            def __init__(self, model, rate, grammar):
                self.grammar = json.loads(grammar)
                self.events = iter(events)
                self.blocks = []
                self.current = ''
                recognizers.append(self)
            def AcceptWaveform(self, pcm):
                self.blocks.append(len(pcm))
                final, self.current = next(self.events)
                return final
            def Result(self): return json.dumps({'text': self.current})
            def PartialResult(self): return json.dumps({'partial': self.current})
            def FinalResult(self): return json.dumps({'text': eof})

        model_factory = Mock(return_value=object())
        with patch.object(production, 'dependencies', return_value=(None, model_factory, Rec)) as deps:
            report = replay.run_asr(self.corpus(), case_ids=cases, tags=tags)
        deps.assert_called_once_with(audio=False)
        model_factory.assert_called_once()
        return report, recognizers

    def test_streaming_intent_and_blocks(self):
        report, recs = self.run_replay()
        case = report['cases'][0]
        self.assertEqual(case['result'], 'PASS')
        self.assertTrue(case['streaming']['outcome_before_eof'])
        self.assertEqual(case['streaming']['intent'], 'TIME')
        self.assertEqual(recs[0].grammar, production.GRAMMAR)
        self.assertEqual(recs[0].blocks, [3200, 3200, 3200])

    def test_short_last_block_is_not_padded_or_trimmed(self):
        self.wav(frames=1700)
        report, recs = self.run_replay(events=[(False, ''), (True, 'который час')])
        self.assertEqual(recs[0].blocks, [3200, 200])
        self.assertEqual(report['cases'][0]['result'], 'PASS')

    def test_production_normalization(self):
        report, _ = self.run_replay(events=[(True, 'Который час?')] * 3)
        self.assertEqual(report['cases'][0]['result'], 'PASS')

    def test_changed_wav_after_validation_is_rejected(self):
        corpus = self.corpus()
        self.wav(frames=1600)
        recognizer = Mock()
        with self.assertRaisesRegex(replay.CorpusError, 'changed'):
            replay.recognize_clip(corpus, corpus.clips[0], object(), recognizer, production.GRAMMAR)
        recognizer.assert_not_called()

    def test_asr_does_not_execute_assistant_or_external_actions(self):
        with patch.object(production, 'listen') as listen, \
                patch.object(production, 'command') as command, \
                patch.object(production, 'Speech') as speech, \
                patch.object(production, 'Bridge') as bridge, \
                patch.object(production, 'OutputGuard') as guard:
            self.run_replay()
        for external in (listen, command, speech, bridge, guard):
            external.assert_not_called()

    def test_eof_only_cannot_pass(self):
        report, _ = self.run_replay(events=[(False, 'который час')] * 3)
        case = report['cases'][0]
        self.assertEqual(case['result'], 'FAIL')
        self.assertFalse(case['streaming']['outcome_before_eof'])
        self.assertEqual(case['eof_final'], 'который час')

    def test_expected_intent_is_not_computed_by_matcher(self):
        self.clips[0]['expected'] = {'intent': 'ANTON_OPEN'}
        self.manifest()
        with patch.object(production, 'match_intent', return_value=('TIME', None)) as match:
            report, _ = self.run_replay()
        self.assertEqual(report['cases'][0]['expected']['intent'], 'ANTON_OPEN')
        self.assertEqual(report['cases'][0]['result'], 'FAIL')
        self.assertTrue(all(c.args == ('который час',) for c in match.call_args_list))

    def test_first_nonempty_final_cannot_be_hidden_by_later_correct_command(self):
        report, _ = self.run_replay(events=[(True, 'не знаю'), (True, 'который час'), (False, '')])
        self.assertEqual(report['cases'][0]['streaming']['transcript'], 'не знаю')
        self.assertEqual(report['cases'][0]['result'], 'FAIL')

    def test_wake_partial_passes_and_eof_does_not_create_wake(self):
        self.clips[0]['expected'] = {'wake': True, 'transcript': 'михаил'}
        self.manifest()
        report, recs = self.run_replay(events=[(False, 'михаил'), (False, ''), (False, '')], eof='')
        self.assertEqual(report['cases'][0]['result'], 'PASS')
        self.assertEqual(recs[0].grammar, replay.production_wake_grammar())
        report, _ = self.run_replay(events=[(False, '')] * 3, eof='михаил')
        self.assertEqual(report['cases'][0]['result'], 'FAIL')

    def test_negative_wake(self):
        self.clips[0]['expected'] = {'wake': False}
        self.manifest()
        report, _ = self.run_replay(events=[(False, '')] * 3, eof='')
        self.assertEqual(report['cases'][0]['result'], 'PASS')
        report, _ = self.run_replay(events=[(False, 'михаил')] * 3, eof='')
        self.assertEqual(report['cases'][0]['result'], 'FAIL')

    def test_control_return_in_control_context(self):
        self.clips[0]['expected'] = {'control': 'RETURN', 'transcript': 'вернись'}
        self.manifest()
        report, recs = self.run_replay(events=[(True, 'вернись')] * 3, eof='')
        self.assertEqual(recs[0].grammar, replay.CONTROL_GRAMMAR)
        self.assertEqual(report['cases'][0]['result'], 'PASS')
        self.assertEqual(report['cases'][0]['streaming']['control'], 'RETURN')

    def test_control_wake_and_return_context(self):
        for expected, phase, text, wanted in (
                ('WAKE', 'voice', 'михаил борисович', 'PASS'),
                ('RETURN', 'voice', 'вернись', 'FAIL'),
                ('RETURN', 'control', 'стоп', 'PASS'),
                ('RETURN', 'control', 'михаил', 'FAIL')):
            with self.subTest(expected=expected, phase=phase, text=text):
                self.clips[0]['expected'] = {'control': expected}
                self.clips[0]['control_phase'] = phase
                self.manifest()
                report, _ = self.run_replay(events=[(True, text)] * 3)
                self.assertEqual(report['cases'][0]['result'], wanted)

    def test_atomic_expectation_uses_production_classifier_and_streaming_final(self):
        self.clips[0]['expected'] = {'control': 'ATOMIC_RETURN', 'transcript': 'михаил вернись'}
        self.clips[0]['control_phase'] = 'opening'
        self.manifest()
        report, recs = self.run_replay(events=[(False, 'михаил'), (False, 'михаил вернись'),
                                               (True, 'михаил вернись')])
        self.assertEqual(report['cases'][0]['result'], 'PASS')
        self.assertEqual(report['cases'][0]['streaming']['control'], 'ATOMIC_RETURN')
        self.assertIn('михаил вернись', recs[0].grammar)
        report, _ = self.run_replay(events=[(False, 'михаил вернись')] * 3, eof='михаил вернись')
        self.assertEqual(report['cases'][0]['result'], 'FAIL')
        self.assertIsNone(report['cases'][0]['streaming']['control'])

    def test_planned_does_not_fail_supported_summary(self):
        self.clips.append({'id': 'planned-01', 'wav': 'time.wav',
                           'expected': {'transcript': 'михаил вернись'}, 'tags': ['planned']})
        self.manifest()
        report, _ = self.run_replay()
        self.assertEqual(report['summary'], {'total': 2, 'pass': 1, 'fail': 0, 'planned': 1})
        self.assertEqual(report['cases'][1]['result'], 'PLANNED')
        self.assertEqual(report['cases'][1]['streaming']['transcript'], 'который час')

    def test_model_once_new_recognizer_per_clip_and_filters(self):
        self.clips.append({**self.clips[0], 'id': 'time-02', 'tags': ['quiet']})
        self.manifest()
        report, recs = self.run_replay()
        self.assertEqual(len(recs), 2)
        self.assertIsNot(recs[0], recs[1])
        report, recs = self.run_replay(cases=['time-02'], tags=['quiet'])
        self.assertEqual([c['id'] for c in report['cases']], ['time-02'])
        with self.assertRaises(replay.CorpusError):
            self.run_replay(cases=['missing'])
        with self.assertRaises(replay.CorpusError):
            self.run_replay(tags=['missing'])

    def test_report_private_paths_and_bytes_absent(self):
        report, _ = self.run_replay()
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(str(Path.home()), serialized)
        self.assertNotIn('pcm', serialized.lower())
        self.assertNotIn('firefox_profile', serialized)
        self.assertEqual(len(report['git']['head']), 40)
        self.assertIsInstance(report['git']['dirty'], bool)
        self.assertEqual(report['manifest_sha256'], replay.sha256_file(self.root / 'manifest.json'))
        self.assertEqual(report['cases'][0]['wav_sha256'], replay.sha256_file(self.root / 'time.wav'))
        self.assertEqual(report['summary'], {'total': 1, 'pass': 1, 'fail': 0, 'planned': 0})

    def test_cli_report_is_opt_in_and_fail_exit(self):
        report, _ = self.run_replay()
        original = sorted(p.name for p in self.root.iterdir())
        with patch.object(replay, 'run_asr', return_value=report), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(replay.cli(['--corpus', str(self.root)]), 0)
            self.assertEqual(sorted(p.name for p in self.root.iterdir()), original)
            target = self.root / 'reports/result.json'
            self.assertEqual(replay.cli(['--corpus', str(self.root), '--report', str(target)]), 0)
            self.assertEqual(json.loads(target.read_text()), report)
            report['summary']['fail'] = 1
            self.assertEqual(replay.cli(['--corpus', str(self.root)]), 1)

    def test_report_cannot_overwrite_corpus_or_enter_worktree(self):
        for target in (self.root / 'manifest.json', self.root / 'time.wav', replay.ROOT / 'report.json'):
            with self.subTest(target=target), self.assertRaises(replay.CorpusError):
                replay.report_destination(target)

    def test_wake_extraction_fails_closed_on_changed_production(self):
        with patch.object(replay.inspect, 'getsource', return_value='def listen():\n    pass\n'):
            with self.assertRaisesRegex(replay.CorpusError, 'wake grammar'):
                replay.production_wake_grammar()


@unittest.skipUnless(os.environ.get('MB_ASR_TEST') == '1', 'opt-in real Vosk, artificial silence WAV')
class RealVoskTests(unittest.TestCase):
    def test_real_model_silence_has_no_wake(self):
        with tempfile.TemporaryDirectory(prefix='mb-real-asr-') as tmp:
            root = Path(tmp)
            with wave.open(str(root / 'silence.wav'), 'wb') as wav:
                wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                wav.writeframes(b'\0' * 32000 * 2)
            (root / 'manifest.json').write_text(json.dumps({'schema_version': 1, 'clips': [
                {'id': 'silence', 'wav': 'silence.wav', 'expected': {'wake': False}}]}))
            report = replay.run_asr(replay.load_corpus(root))
            self.assertEqual(report['summary'], {'total': 1, 'pass': 1, 'fail': 0, 'planned': 0})
            self.assertEqual(report['cases'][0]['eof_final'], '')


if __name__ == '__main__':
    unittest.main()
