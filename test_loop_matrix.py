import copy
import json
import unittest
from unittest.mock import patch
import audio_replay as asr
import loop_matrix
import loop_replay
import main
import suggest_onsets
import test_loop_replay as fixtures


class MatrixTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.LoopTests('test_normal_open_uses_production_modes_no_early_send')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        outcomes={'wake':{'wake':True},'time':{'intent':'TIME'},'anton':{'intent':'ANTON_OPEN'},
                  'larisa':{'intent':'LARISA_OPEN'},'atomic':{'control':'ATOMIC_RETURN'},
                  'fullwake':{'control':'WAKE'},'return':{'control':'RETURN'}}
        for c in self.f.clips:
            if c['id'] in outcomes:c['expected'].update(outcomes[c['id']])
            else:c['tags']=['planned']
        self.f.write_manifest()

    def test_matrix_uses_ground_truth_and_covers_every_supported_clip(self):
        with patch.object(main,'match_intent',side_effect=AssertionError):
            cases=loop_matrix.build_matrix(self.f.corpus)
        loop_replay.validate_scenarios({'schema_version':1,'scenarios':cases},self.f.corpus)
        used={s['clip'] for c in cases for s in c['steps']}
        self.assertEqual(used,{'wake','time','anton','larisa','atomic','fullwake','return'})
        for c in cases:
            self.assertNotIn('stop_at',c);self.assertEqual(c['expected']['wake_count'],1)
            self.assertEqual(c['expected']['min_settle_ms'],2000)
        time=next(c for c in cases if 'local' in c['tags'])
        self.assertEqual(time['expected']['local_intents'],['TIME'])
        # Changing expected affects expected, never derived from PCM or matcher.
        next(c for c in self.f.corpus.clips if c['id']=='time')['expected']['intent']='DATE'
        changed=loop_matrix.build_matrix(self.f.corpus)
        self.assertEqual(next(c for c in changed if 'local' in c['tags'])['expected']['local_intents'],['DATE'])

    def test_generated_templates_execute_real_loop(self):
        for case in loop_matrix.build_matrix(self.f.corpus):
            # Short fixture PCM can endpoint before Anton's cue ends; use full
            # recording-like silence prefix rather than weaken action assertions.
            if 'open' in case['tags'] and case['bridge'].get('open_anton') is not None:
                case['steps'][-1]['offset_ms']=5000
            with self.subTest(case=case['id']):
                result=loop_replay.run_case(self.f.corpus,case,{},None,fixtures.MarkerRecognizer,require_vosk=False)
                self.assertEqual(result['result'],'PASS',result['errors'])

    def test_late_duplicate_wake_or_intent_remains_failure(self):
        c=next(c for c in loop_matrix.build_matrix(self.f.corpus) if 'local' in c['tags'])
        c['steps'] += [{'clip':'wake','at':'clip.time.end','offset_ms':5000},
                       {'clip':'time','at':'wake_ack.playback_end','occurrence':2}]
        c['duration']=45
        r=loop_replay.run_case(self.f.corpus,c,{},None,fixtures.MarkerRecognizer,require_vosk=False)
        self.assertEqual(r['result'],'FAIL');self.assertIn('wake_count',r['errors'])
        audit=loop_matrix.coverage_report(self.f.corpus,[c],[r])['stability_checks'][0]
        self.assertTrue(audit['duplicate_wake']);self.assertTrue(audit['duplicate_action'])

    def test_missing_settle_window_fails_even_if_action_success(self):
        c=copy.deepcopy(self.f.case);c['expected']['min_settle_ms']=10000
        r=loop_replay.run_case(self.f.corpus,c,{},None,fixtures.MarkerRecognizer,require_vosk=False)
        self.assertEqual(r['local_intents'],['TIME']);self.assertIn('settle_window',r['errors'])

    def test_unknown_outcome_rejected(self):
        self.f.corpus.clips[0]['expected']={'intent':'RUN_SHELL'}
        with self.assertRaises(asr.CorpusError):loop_matrix.build_matrix(self.f.corpus)

    def test_report_excludes_not_run_from_coverage(self):
        c=self.f.case
        r={'id':c['id'],'result':'NOT RUN','events':[],'errors':[],'actions':[],'local_intents':[]}
        result=loop_matrix.coverage_report(self.f.corpus,[c],[r])
        self.assertEqual(result['unique_wav_used'],0)
        self.assertNotIn(str(self.f.root),json.dumps(result))


class OnsetTests(unittest.TestCase):
    def fixture(self):
        f=fixtures.LoopTests('test_normal_open_uses_production_modes_no_early_send')
        f.setUp();self.addCleanup(f.doCleanups);return f

    def test_candidates_are_unverified_and_do_not_mutate_any_file(self):
        f=self.fixture();before={p.name:p.read_bytes() for p in f.root.iterdir()}
        with patch.object(asr,'validate_wav',wraps=asr.validate_wav) as valid:
            result=suggest_onsets.report(f.corpus)
        self.assertEqual(valid.call_count,len(f.clips))
        self.assertTrue(all(row['verified'] is False for row in result['suggestions']))
        self.assertEqual(before,{p.name:p.read_bytes() for p in f.root.iterdir()})
        text=json.dumps(result);self.assertNotIn(str(f.root),text);self.assertNotIn('pcm',text.lower())
        silence=next(r for r in result['suggestions'] if r['id']=='silence')
        self.assertIsNone(silence['candidate_start_frame'])

    def test_unverified_candidate_does_not_unlock_onset_scenario(self):
        f=self.fixture();c=copy.deepcopy(f.case);c['steps'][1]['align']='speech_onset'
        annotation={'time':{'speech_onset_frame':0,'verified':False}}
        r=loop_replay.run_case(f.corpus,c,annotation,None,fixtures.MarkerRecognizer,require_vosk=False)
        self.assertEqual(r['result'],'NOT RUN')


if __name__=='__main__':unittest.main()
