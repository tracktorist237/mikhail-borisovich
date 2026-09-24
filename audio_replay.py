#!/usr/bin/env python3
"""Offline ASR replay v1; no devices, browser, TTS or command execution.

Usage: .venv/bin/python audio_replay.py --corpus ~/mb-test-audio --mode asr
Optional --case/--tag filters intersect (multiple values within a filter are OR).
All selected supported cases are required; planned tags report separately. --report is opt-in and must be a new file outside
Git worktrees. Corpus: manifest.json plus PCM16 mono 16000-Hz WAVs; no transforms.
Manifest v1 allows only schema_version/clips, clip id/wav/expected/tags/control_phase and
expected transcript (str), wake (bool), intent (str), control (str).
At most one of wake/intent/control selects the grammar; transcript-only is LOCAL.
NONE is the explicit expected.intent for a nonempty final with no LOCAL intent.
All corpus files are validated, even when --case/--tag selects a subset.

Wake selects the production wake grammar; control selects CONTROL_GRAMMAR but its
outcome uses the production classifier (final endpoint required) in control_phase.
control_phase defaults to control for RETURN, voice otherwise. Planned cases use
CONTROL_GRAMMAR for observation only, count separately and never become PASS by
removing a transcript mismatch. Otherwise GRAMMAR is
used. LOCAL evaluates the first nonempty streaming final, not a later correction.
Wake detection observes both partials and finals. Transcript checks use normalize.
EOF FinalResult is diagnostic only. This is ASR, not the listen-loop acceptance:
no RMS/preroll gates, playback, retries, queue scheduling or real mode transitions.
"""
import argparse
import ast
from dataclasses import dataclass
import hashlib
import inspect
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import wave

import main as production
from gpt_modes import CONTROL_GRAMMAR, classify_control

ROOT = Path(__file__).resolve().parent
BLOCK_FRAMES = 1600


class CorpusError(ValueError):
    """Invalid corpus, unsupported schema or unsafe destination."""


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(65536), b''):
            digest.update(block)
    return digest.hexdigest()


def outside_worktree(path):
    """Resolve symlinks; reject this project and other Git worktrees."""
    path = Path(path).expanduser().resolve()
    if path.is_relative_to(ROOT):
        raise CorpusError('Corpus/report must be outside the repository worktree.')
    parent = path if path.is_dir() else path.parent
    while not parent.exists():
        parent = parent.parent
    result = subprocess.run(['git', '-C', str(parent), 'rev-parse', '--is-inside-work-tree'],
                            capture_output=True, text=True, timeout=5)
    if result.returncode == 0 and result.stdout.strip() == 'true':
        raise CorpusError('Corpus/report must be outside any Git worktree.')
    return path


def corpus_file(root, name):
    if (not isinstance(name, str) or not name or '\\' in name
            or any(ord(c) < 32 for c in name)):
        raise CorpusError('WAV/manifest path must be a relative POSIX filename.')
    relative = PurePosixPath(name)
    if relative.is_absolute() or '..' in relative.parts:
        raise CorpusError('Relative WAV path required; ../ traversal is forbidden.')
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise CorpusError('WAV/manifest symlink escapes corpus root.')
    if path.is_relative_to(ROOT):
        raise CorpusError('Corpus files must be outside the repository worktree.')
    if not path.is_file():
        raise CorpusError(f'Corpus file does not exist: {name}')
    return path


def fields(value, allowed, required, where):
    if not isinstance(value, dict):
        raise CorpusError(f'{where} must be an object.')
    if set(value) - allowed or required - set(value):
        raise CorpusError(f'{where}: unknown or missing fields; executable fields are forbidden.')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CorpusError('Duplicate JSON field.')
        result[key] = value
    return result


def validate_wav(path):
    try:
        with wave.open(str(path), 'rb') as wav:
            if wav.getcomptype() != 'NONE':
                raise CorpusError('WAV must be uncompressed PCM.')
            if wav.getnchannels() != 1:
                raise CorpusError('WAV must be mono.')
            if wav.getsampwidth() != 2:
                raise CorpusError('WAV must be signed 16-bit PCM.')
            if wav.getframerate() != 16000:
                raise CorpusError('WAV sample rate must be 16000 Hz; resampling is not performed.')
            expected = wav.getnframes() * 2
            if not expected:
                raise CorpusError('WAV is empty.')
            actual = 0
            while block := wav.readframes(BLOCK_FRAMES):
                actual += len(block)
            if actual != expected:
                raise CorpusError('WAV data is truncated or has incomplete frames.')
    except (wave.Error, EOFError) as exc:
        raise CorpusError('Malformed or unsupported PCM WAV.') from exc


@dataclass
class Corpus:
    root: Path
    clips: list
    manifest_sha256: str
    wav_hashes: dict


def load_corpus(path):
    root = outside_worktree(path)
    if not root.is_dir():
        raise CorpusError('Corpus directory does not exist; provide an external --corpus.')
    source = corpus_file(root, 'manifest.json').read_bytes()
    try:
        manifest = json.loads(source, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CorpusError('manifest.json is not valid JSON.') from exc
    fields(manifest, {'schema_version', 'clips'}, {'schema_version', 'clips'}, 'Manifest')
    if type(manifest['schema_version']) is not int or manifest['schema_version'] != 1:
        raise CorpusError('Only manifest schema_version 1 is supported.')
    clips = manifest['clips']
    if not isinstance(clips, list) or not clips:
        raise CorpusError('clips must be a nonempty list.')
    ids, hashes = set(), {}
    for clip in clips:
        fields(clip, {'id', 'wav', 'expected', 'tags', 'control_phase'}, {'id', 'wav', 'expected'}, 'Clip')
        key = clip['id']
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', key):
            raise CorpusError('Clip id must contain 1..80 ASCII letters, digits, dots, _ or -.')
        if key in ids:
            raise CorpusError(f'Duplicate clip id: {key}')
        ids.add(key)
        expected = clip['expected']
        fields(expected, {'transcript', 'wake', 'intent', 'control'}, set(), 'Expected')
        if not expected:
            raise CorpusError('expected must contain at least one assertion.')
        for name, value in expected.items():
            wanted = bool if name == 'wake' else str
            if type(value) is not wanted or (name in ('intent', 'control') and not value):
                raise CorpusError(f'expected.{name} must be {wanted.__name__}.')
        if sum(name in expected for name in ('wake', 'intent', 'control')) > 1:
            raise CorpusError('Use separate clips for wake, intent and control grammar contexts.')
        if clip.get('control_phase', 'control') not in ('voice', 'control', 'opening'):
            raise CorpusError('control_phase must be voice, control or opening.')
        tags = clip.get('tags', [])
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise CorpusError('tags must be list[str].')
        wav = corpus_file(root, clip['wav'])
        try:
            validate_wav(wav)
        except CorpusError as exc:
            raise CorpusError(f'{key}: {exc}') from exc
        hashes[key] = sha256_file(wav)
    return Corpus(root, clips, hashlib.sha256(source).hexdigest(), hashes)


def production_wake_grammar():
    """Read the actual inline literal without copying it or running listen().

    main.py cannot be changed in stage 1. AST extraction is deliberately narrow:
    a future production refactor requires an explicit adapter update, no fallback.
    Only trusted local source is parsed; manifest values are never Python input.
    """
    tree = ast.parse(inspect.getsource(production.listen))
    matches = [node.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == 'wake' for t in node.targets)]
    if len(matches) == 1:
        call = matches[0]
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == 'recognizer' and len(call.args) == 1 and not call.keywords):
            try:
                value = ast.literal_eval(call.args[0])
            except (ValueError, TypeError):
                value = None
            if isinstance(value, list) and value and all(isinstance(x, str) for x in value):
                return value
    raise CorpusError('Cannot read production wake grammar; inline definition changed. No fallback used.')


def recognize_clip(corpus, clip, model, Recognizer, grammar):
    path = corpus_file(corpus.root, clip['wav'])
    if sha256_file(path) != corpus.wav_hashes[clip['id']]:
        raise CorpusError('WAV changed after validation; rerun on a stable corpus.')
    rec = Recognizer(model, 16000, json.dumps(grammar, ensure_ascii=False))
    finals, events = [], []
    wake_seen = False
    latest = ''
    frames = 0
    with wave.open(str(path), 'rb') as wav:
        while pcm := wav.readframes(BLOCK_FRAMES):
            frames += len(pcm) // 2
            final = rec.AcceptWaveform(pcm)
            result = json.loads(rec.Result() if final else rec.PartialResult())
            text = result.get('text' if final else 'partial', '')
            if final and text.strip():
                finals.append(text)
            if text:
                latest = text
                wake_seen = wake_seen or 'михаил' in text.split()
            event = {'kind': 'final' if final else 'partial', 'text': text}
            if not events or any(events[-1][k] != v for k, v in event.items()):
                events.append({**event, 'at_frame': frames})
    eof = json.loads(rec.FinalResult()).get('text', '')  # Never contributes to assertions.
    if sha256_file(path) != corpus.wav_hashes[clip['id']]:
        raise CorpusError('WAV changed during recognition; result discarded.')
    expected = clip['expected']
    is_wake = 'wake' in expected
    transcript = latest if is_wake else (finals[0] if finals else '')
    intent = None
    if transcript and not is_wake and 'control' not in expected:
        intent = production.match_intent(production.normalize(transcript))[0]
    reasons = []
    if is_wake:
        if wake_seen != expected['wake']:
            reasons.append('wake_mismatch')
    elif not finals:
        reasons.append('no_streaming_endpoint')
    if 'intent' in expected and (intent or 'NONE') != expected['intent']:
        reasons.append('intent_mismatch')
    if ('transcript' in expected and
            production.normalize(transcript) != production.normalize(expected['transcript'])):
        reasons.append('transcript_mismatch')
    control = None
    if 'control' in expected:
        phase = clip.get('control_phase', 'control' if expected['control'] == 'RETURN' else 'voice')
        for event in events:
            actual = classify_control(event['text'], final=event['kind'] == 'final', phase=phase)
            if actual:
                control = actual
                break
        if control != expected['control']:
            reasons.append('control_mismatch')
    planned = 'planned' in clip.get('tags', [])
    return {'id': clip['id'], 'wav': clip['wav'], 'wav_sha256': corpus.wav_hashes[clip['id']],
            'expected': expected, 'tags': clip.get('tags', []),
            'result': 'PLANNED' if planned else 'FAIL' if reasons else 'PASS', 'reasons': reasons,
            'streaming': {'transcript': transcript, 'intent': intent, 'control': control,
                          'wake': wake_seen if is_wake else None,
                          'endpoint_seen': bool(finals), 'outcome_before_eof': not reasons,
                          'events': events}, 'eof_final': eof}


def git_identity():
    def git(*args):
        return subprocess.run(['git', '-C', str(ROOT), *args], check=True,
                              capture_output=True, text=True, timeout=5).stdout.strip()
    return {'head': git('rev-parse', 'HEAD'), 'dirty': bool(git('status', '--porcelain'))}


def run_asr(corpus, *, case_ids=None, tags=None):
    selected = [c for c in corpus.clips if (not case_ids or c['id'] in case_ids)
                and (not tags or set(tags).intersection(c.get('tags', [])))]
    if case_ids and set(case_ids) - {c['id'] for c in corpus.clips}:
        raise CorpusError('Unknown --case ID.')
    if not selected:
        raise CorpusError('No cases selected; an empty run is not PASS.')
    cfg = production.config()
    model_path = Path(cfg['model_path']).resolve()
    if not (model_path / 'am/final.mdl').is_file():
        raise CorpusError('Configured local Vosk model is missing; nothing will be downloaded.')
    grammars = {'local': production.GRAMMAR, 'control': CONTROL_GRAMMAR}
    if any('wake' in c['expected'] for c in selected):
        grammars['wake'] = production_wake_grammar()
    _, Model, Recognizer = production.dependencies(audio=False)
    model = Model(str(model_path))  # Existing explicit local path; never model_name/download.
    results = []
    for clip in selected:
        context = ('wake' if 'wake' in clip['expected'] else
                   'control' if 'control' in clip['expected'] or 'planned' in clip.get('tags', []) else 'local')
        result = recognize_clip(corpus, clip, model, Recognizer, grammars[context])
        result['grammar'] = context
        encoded = json.dumps(grammars[context], ensure_ascii=False).encode()
        result['grammar_sha256'] = hashlib.sha256(encoded).hexdigest()
        results.append(result)
    passed = sum(c['result'] == 'PASS' for c in results)
    return {'schema_version': 1, 'mode': 'asr', 'git': git_identity(),
            'config': {'path': 'config.json', 'sha256': sha256_file(ROOT / 'config.json')},
            'model': {'path': (model_path.relative_to(ROOT).as_posix()
                               if model_path.is_relative_to(ROOT) else '<external>'),
                      'name': model_path.name,
                      'sample_rate': cfg['sample_rate']},
            'manifest': 'manifest.json', 'manifest_sha256': corpus.manifest_sha256,
            'cases': results, 'summary': {'total': len(results), 'pass': passed, 'fail': sum(c['result'] == 'FAIL' for c in results),
                                        'planned': sum(c['result'] == 'PLANNED' for c in results)}}


def report_destination(path):
    path = outside_worktree(path)
    if path.exists():
        raise CorpusError('Report must be a new file; existing files are never overwritten.')
    return path


def print_report(report):
    print('ID                   RESULT  TRANSCRIPT                      OUTCOME')
    for case in report['cases']:
        stream = case['streaming']
        outcome = ((stream['control'] or 'NONE') if 'control' in case['expected'] else
                   ('WAKE' if stream['wake'] else 'NO_WAKE') if case['grammar'] == 'wake' else
                   stream['intent'] or 'NONE')
        text = json.dumps(stream['transcript'], ensure_ascii=False)
        print(f'{case["id"]:<20} {case["result"]:<7} {text:<31} {outcome}')
        print(f'  streaming outcome before EOF: {stream["outcome_before_eof"]}; '
              f'endpoint: {stream["endpoint_seen"]}; reasons: {",".join(case["reasons"]) or "-"}')
        print('  EOF diagnostic only: ' + json.dumps(case['eof_final'], ensure_ascii=False))
    print(f'SUPPORTED: PASS {report["summary"]["pass"]} / FAIL {report["summary"]["fail"]}')
    print(f'PLANNED: {report["summary"]["planned"]}')
    for key, value in report['summary'].items():
        print(f'{key.upper()} {value}')


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--corpus', required=True)
    parser.add_argument('--mode', choices=['asr'], default='asr')
    parser.add_argument('--case', action='append', dest='case_ids')
    parser.add_argument('--tag', action='append', dest='tags')
    parser.add_argument('--report', help='New JSON file outside Git; no file by default')
    args = parser.parse_args(argv)
    try:
        corpus = load_corpus(args.corpus)
        target = report_destination(args.report) if args.report else None
        report = run_asr(corpus, case_ids=args.case_ids, tags=args.tags)
        if target:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('x', encoding='utf-8') as out:
                json.dump(report, out, ensure_ascii=False, indent=2)
                out.write('\n')
        print_report(report)
        return 1 if report['summary']['fail'] else 0
    except (CorpusError, OSError, RuntimeError, subprocess.SubprocessError, wave.Error, EOFError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(cli())
