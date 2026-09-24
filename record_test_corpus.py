#!/usr/bin/env python3
"""Record a private, external manifest-v1 corpus. Never starts the assistant.

  .venv/bin/python record_test_corpus.py --corpus ~/mb-test-audio
  .venv/bin/python record_test_corpus.py --list
  .venv/bin/python record_test_corpus.py --corpus ~/mb-test-audio --resume

12 prompts x 3 takes = 36 WAVs without retries. Every invocation gets its own
session directory. A normal repeated run adds samples with session-qualified IDs;
--resume skips accepted slots across sessions. Retry/skip WAVs remain on disk but
are not added to manifest. The first manifest is published with the first accepted
take (never an empty manifest, which audio_replay v1 rejects).

Capture is fixed at 5 s, PCM16 mono 16000 Hz. Prompts and the spoken ready cue
finish BEFORE opening the capture stream. Leave a short natural silence after
the cue and after the phrase; no audio is trimmed or transformed. Physical room
reverberation cannot be certified by this tool and still needs a live check.

Quality RMS=600 and clipping>0.5% are recorder warnings, not production thresholds.
Bad quality offers spoken retry/keep/skip. Vosk is loaded lazily ONLY for this
choice, never to derive expected transcripts/intents or adapt any grammar.
All WAV/manifest writes are external, atomic, and no-clobber for recordings.
No network calls; no system audio settings are changed.
"""
from array import array
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
from datetime import datetime
import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import wave

import audio_replay
from audio_replay import CorpusError

production = audio_replay.production
RATE = 16000
BLOCK = 1600
SECONDS = 5
SPEECH_RMS = 600
CLIP_LEVEL = 32760
CLIP_FRACTION = .005
READY = 'Начали.'


class RecorderError(RuntimeError):
    pass


@dataclass(frozen=True)
class Prompt:
    id: str
    text: str
    expected: dict
    group: str


PROMPTS = (
    Prompt('wake-mikhail', 'Михаил', {'transcript': 'михаил', 'wake': True}, 'wake'),
    Prompt('wake-full', 'Михаил Борисович', {'transcript': 'михаил борисович', 'wake': True}, 'wake'),
    Prompt('local-time', 'Который час', {'transcript': 'который час', 'intent': 'TIME'}, 'local'),
    Prompt('local-date', 'Какая дата', {'transcript': 'какая дата', 'intent': 'DATE'}, 'local'),
    Prompt('local-battery', 'Какой заряд', {'transcript': 'какой заряд', 'intent': 'BATTERY'}, 'local'),
    Prompt('local-louder', 'Громче', {'transcript': 'громче', 'intent': 'VOLUME_UP'}, 'local'),
    Prompt('local-quieter', 'Тише', {'transcript': 'тише', 'intent': 'VOLUME_DOWN'}, 'local'),
    Prompt('anton-open', 'Позови Антона Павловича',
           {'transcript': 'позови антона павловича', 'intent': 'ANTON_OPEN'}, 'gpt'),
    Prompt('larisa-open', 'Позови Ларису', {'transcript': 'позови ларису', 'intent': 'LARISA_OPEN'}, 'gpt'),
    Prompt('return-wake', 'Михаил Борисович',
           {'transcript': 'михаил борисович', 'control': 'WAKE'}, 'control'),
    Prompt('return-command', 'Вернись', {'transcript': 'вернись', 'control': 'RETURN'}, 'control'),
    Prompt('atomic-return-planned', 'Михаил вернись', {'transcript': 'михаил вернись'}, 'planned'),
)


@contextmanager
def publish_section():
    """Defer terminal SIGINT for a short file commit, never do I/O in a handler."""
    old = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save_wav(target, pcm):
    """Publish after WAV close/fsync. link+unlink is atomic and cannot overwrite.

    Plain POSIX rename could overwrite a concurrently created recording; a hard
    link in the same directory gives atomic no-clobber publication instead.
    Only our temporary file is removed, never a completed WAV.
    """
    if not pcm or len(pcm) % 2:
        raise RecorderError('Вход не вернул полные PCM16 samples; запись не опубликована.')
    fd, name = tempfile.mkstemp(prefix='.record-', suffix='.tmp', dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as raw:
            with wave.open(raw, 'wb') as wav:
                wav.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
                wav.writeframes(pcm)
            raw.flush()
            os.fsync(raw.fileno())
        with publish_section():
            os.link(temporary, target)  # Fails if target exists, including dangling symlinks.
            temporary.unlink()
            fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def quality(pcm):
    samples = array('h')
    samples.frombytes(pcm)
    if sys.byteorder != 'little':
        samples.byteswap()
    active_squares = active_samples = 0
    for index in range(0, len(samples), BLOCK):
        block = samples[index:index+BLOCK]
        squares = sum(n*n for n in block)
        if block and math.sqrt(squares / len(block)) >= SPEECH_RMS:
            active_squares += squares
            active_samples += len(block)
    duration = len(samples) / RATE
    fraction = sum(abs(n) >= CLIP_LEVEL for n in samples) / max(1, len(samples))
    speech_found = active_samples >= BLOCK
    warnings = []
    if not speech_found:
        warnings.append('speech_not_found')
    if duration < 1:
        warnings.append('too_short')
    if fraction > CLIP_FRACTION:
        warnings.append('clipping')
    return {'duration': duration, 'peak': max((abs(n) for n in samples), default=0),
            'active_rms': math.sqrt(active_squares / active_samples) if active_samples else 0,
            'clipping_fraction': fraction, 'speech_found': speech_found, 'warnings': warnings}


class CorpusSession:
    def __init__(self, root, *, now=datetime.now):
        self.root = audio_replay.outside_worktree(root)
        self.now = now
        self.lock_fd = None

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.lock_fd = os.open(self.root / '.record.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RecorderError('Другой recorder уже использует этот corpus.') from exc
            self.manifest_path = self.root / 'manifest.json'
            if self.manifest_path.is_symlink():
                raise CorpusError('Recorder refuses a symlink manifest.')
            if self.manifest_path.exists():
                # Reuse v1 validation; never repair or drop existing entries silently.
                corpus = audio_replay.load_corpus(self.root)
                self.original = self.manifest_path.read_bytes()
                if hashlib.sha256(self.original).hexdigest() != corpus.manifest_sha256:
                    raise RecorderError('Manifest изменился во время проверки; запустите recorder снова.')
                self.clips = list(corpus.clips)
            else:
                self.original = None
                self.clips = []
            wav_dir = self.root / 'wav'
            if not wav_dir.resolve().is_relative_to(self.root):
                raise CorpusError('wav directory symlink escapes corpus root.')
            audio_replay.outside_worktree(wav_dir)
            wav_dir.mkdir(exist_ok=True, mode=0o700)
            base = 'session-' + self.now().strftime('%Y%m%d-%H%M%S')
            for index in range(1, 10000):
                candidate = wav_dir / (base if index == 1 else f'{base}-{index:02d}')
                try:
                    candidate.mkdir(mode=0o700)
                    self.directory = candidate
                    break
                except FileExistsError:
                    continue
            else:
                raise RecorderError('Не удалось создать уникальную session.')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if self.lock_fd is not None:
            os.close(self.lock_fd)  # flock released, persistent empty lock file is harmless.
            self.lock_fd = None

    def has_take(self, prompt, slot):
        entries = [c for c in self.clips if c['id'] == slot or c['id'].startswith(slot + '--')]
        # The explicitly promoted v1 atomic cases retain their original slot
        # and transcript. Resume may skip them, never downgrade their expected
        # control or rewrite the manifest. Other changes still fail closed.
        promoted = ({'transcript': 'михаил вернись', 'control': 'ATOMIC_RETURN'}
                    if prompt.id == 'atomic-return-planned' and
                    prompt.expected == {'transcript': 'михаил вернись'} else None)
        if any(c['expected'] != prompt.expected and c['expected'] != promoted for c in entries):
            raise RecorderError(f'Эталон существующего {slot} отличается; автоматическая замена запрещена.')
        return bool(entries)

    def save(self, slot, pcm):
        for index in range(1, 10000):
            name = slot if index == 1 else f'{slot}-attempt-{index:02d}'
            path = self.directory / (name + '.wav')
            if path.exists() or path.is_symlink():
                continue
            try:
                save_wav(path, pcm)
                return path
            except FileExistsError:
                continue
        raise RecorderError('Не удалось подобрать имя для записи.')

    def accept(self, prompt, slot, path, metrics):
        ids = {c['id'] for c in self.clips}
        key = slot if slot not in ids else f'{slot}--{self.directory.name.removeprefix("session-")}'
        if key in ids:
            raise RecorderError('ID записи уже существует; ничего не заменено.')
        tags = ['user-voice', 'normal', prompt.group, self.directory.name]
        if metrics['warnings']:
            tags.append('quality-warning')
        clip = {'id': key, 'wav': path.relative_to(self.root).as_posix(),
                'expected': dict(prompt.expected), 'tags': tags}
        document = {'schema_version': 1, 'clips': self.clips + [clip]}
        fd, name = tempfile.mkstemp(prefix='.manifest-', suffix='.tmp', dir=self.root)
        temporary = Path(name)
        encoded = (json.dumps(document, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        try:
            with os.fdopen(fd, 'wb') as out:
                out.write(encoded)
                out.flush()
                os.fsync(out.fileno())
            with publish_section():
                current = self.manifest_path.read_bytes() if self.manifest_path.exists() else None
                if self.manifest_path.is_symlink() or current != self.original:
                    raise RecorderError('Manifest изменён другим процессом; готовый WAV сохранён отдельно.')
                if self.original is None:
                    os.link(temporary, self.manifest_path)
                else:
                    os.replace(temporary, self.manifest_path)
                temporary.unlink(missing_ok=True)
                fsync_directory(self.root)
                self.original = encoded
                self.clips.append(clip)
        finally:
            temporary.unlink(missing_ok=True)


class BlockingSpeech:
    """Use existing RHVoice/player; return only after both synthesis and playback."""
    def __init__(self, cfg):
        self.speech = production.Speech(cfg)

    def say(self, text):
        self.speech.say(text, remember=False)
        while self.speech.busy():
            try:
                self.speech.process.wait(timeout=.1)
            except subprocess.TimeoutExpired:
                pass  # busy() also enforces the existing TTS operation timeout.

    def close(self):
        self.speech.close()


class LiveInput:
    def __init__(self, cfg, *, sd=None):
        if sd is None:
            try:
                import sounddevice as sd
            except ImportError as exc:
                raise RecorderError('sounddevice недоступен; запись не запускалась.') from exc
        self.sd = sd
        self.errors = (OSError, RuntimeError, ValueError)
        portaudio_error = getattr(sd, 'PortAudioError', None)
        if isinstance(portaudio_error, type) and issubclass(portaudio_error, Exception):
            self.errors += (portaudio_error,)
        self.settings = dict(device=cfg.get('input_device'), channels=1, dtype='int16', samplerate=RATE)
        try:
            sd.check_input_settings(**self.settings)
        except self.errors as exc:
            raise RecorderError('Вход не поддерживает PCM16 mono 16000 Hz или недоступен. '
                                'Настройки системы не изменены.') from exc

    def capture(self):
        try:
            return self._capture()
        except RecorderError:
            raise
        except self.errors as exc:
            raise RecorderError('Не удалось открыть или прочитать вход PCM16 mono 16000 Hz. '
                                'Предыдущие записи сохранены; настройки не изменены.') from exc

    def _capture(self):
        chunks = []
        with self.sd.RawInputStream(**self.settings, blocksize=BLOCK) as stream:
            for _ in range(RATE * SECONDS // BLOCK):
                data, overflow = stream.read(BLOCK)
                if overflow or len(data) != BLOCK * 2:
                    raise RecorderError('Разрыв входного потока: текущий неполный take не сохранён. '
                                        'Предыдущие записи сохранены.')
                raw = bytes(data)
                if sys.byteorder != 'little':
                    samples = array('h')
                    samples.frombytes(raw)
                    samples.byteswap()
                    raw = samples.tobytes()
                chunks.append(raw)
        return b''.join(chunks)

    def close(self):
        pass  # Each capture owns a context-managed stream, including on Ctrl+C.


class VoiceChoice:
    """Optional quality dialog only. Never derive corpus expectations from ASR."""
    def __init__(self, speech, source, cfg):
        self.speech, self.source, self.cfg = speech, source, cfg
        self.model = None
        self.Recognizer = None

    def __call__(self, metrics):
        if self.model is None:
            self.speech.say('Для выбора действия загружаю локальное распознавание.')
            try:
                _, Model, self.Recognizer = production.dependencies(audio=False)
                self.model = production.load_model(self.cfg, Model)
            except Exception as exc:
                # Vosk.Model itself raises plain Exception on load failure.
                raise RecorderError('Локальная модель выбора недоступна. Завершённый WAV '
                                    'сохранён вне manifest; ничего не загружалось из сети.') from exc
        choices = {'повторить': 'retry', 'оставить': 'keep', 'пропустить': 'skip'}
        for _ in range(3):
            self.speech.say('Скажите одно слово: повторить, оставить или пропустить. '
                            'После сигнала немного помолчите, затем скажите выбор.')
            self.speech.say(READY)
            pcm = self.source.capture()
            rec = self.Recognizer(self.model, RATE, json.dumps([*choices, '[unk]'], ensure_ascii=False))
            finals = []
            for start in range(0, len(pcm), BLOCK * 2):
                if rec.AcceptWaveform(pcm[start:start+BLOCK*2]):
                    finals.append(json.loads(rec.Result()).get('text', '').strip())
            finals.append(json.loads(rec.FinalResult()).get('text', '').strip())
            text = ' '.join(text for text in finals if text)
            if text in choices:
                return choices[text]
            self.speech.say('Не разобрал выбор.')
        raise RecorderError('Выбор не распознан. WAV сохранён вне manifest; запустите --resume позже.')


def record(corpus, *, takes=3, phrase_ids=None, resume=False, speech_factory=None,
           input_factory=None, decide=None, now=datetime.now, emit=print):
    if type(takes) is not int or not 1 <= takes <= 99:
        raise RecorderError('--takes должен быть от 1 до 99.')
    if phrase_ids and set(phrase_ids) - {p.id for p in PROMPTS}:
        raise RecorderError('Неизвестный --phrase-id; список доступен через --list.')
    prompts = [p for p in PROMPTS if not phrase_ids or p.id in phrase_ids]
    counts = {'accepted': 0, 'attempts': 0, 'skipped': 0, 'resumed': 0}
    with CorpusSession(corpus, now=now) as store:
        pending = []
        for prompt in prompts:
            for take in range(1, takes+1):
                slot = f'{prompt.id}-{take:02d}'
                if resume and store.has_take(prompt, slot):
                    counts['resumed'] += 1
                else:
                    pending.append((prompt, slot))
        emit(f'Session: {store.directory.relative_to(store.root).as_posix()}')
        if not pending:
            emit('Все требуемые take уже есть; запись не запускалась.')
            return counts
        cfg = production.config() if speech_factory is None or input_factory is None or decide is None else {}
        with ExitStack() as cleanup:
            source = input_factory() if input_factory else LiveInput(cfg)
            cleanup.callback(source.close)
            speech = speech_factory() if speech_factory else BlockingSpeech(cfg)
            cleanup.callback(speech.close)
            choose = decide if decide is not None else VoiceChoice(speech, source, cfg)
            speech.say('Записываем отдельные фразы. После слова «Начали» немного помолчите, '
                       'затем скажите фразу обычным голосом и снова помолчите. '
                       'Окно записи — пять секунд. Дождитесь слова «Записано».')
            for prompt, slot in pending:
                while True:
                    speech.say(f'Следующая фраза: {prompt.text}.')
                    speech.say(READY)
                    pcm = source.capture()
                    path = store.save(slot, pcm)
                    counts['attempts'] += 1
                    metrics = quality(pcm)
                    emit(f'{slot}: duration={metrics["duration"]:.2f}s peak={metrics["peak"]} '
                         f'active_rms={metrics["active_rms"]:.1f} '
                         f'clipping={metrics["clipping_fraction"]:.2%} '
                         f'speech={metrics["speech_found"]} warnings={",".join(metrics["warnings"]) or "none"}')
                    action = 'keep'
                    if metrics['warnings']:
                        reasons = {'speech_not_found': 'Речь не обнаружена.',
                                   'too_short': 'Запись слишком короткая.',
                                   'clipping': 'Обнаружена перегрузка сигнала.'}
                        speech.say('Записано. ' + ' '.join(reasons[w] for w in metrics['warnings']))
                        action = choose(metrics)
                    if action == 'keep':
                        store.accept(prompt, slot, path, metrics)
                        counts['accepted'] += 1
                        speech.say('Записано.' if not metrics['warnings'] else 'Оставляю запись.')
                        break
                    if action not in ('retry', 'skip'):
                        raise RecorderError('Недопустимый выбор. Завершённый WAV сохранён вне manifest.')
                    emit(f'{path.relative_to(store.root).as_posix()}: {action}; WAV сохранён вне manifest.')
                    speech.say('Повторим этот образец.' if action == 'retry' else 'Пропускаю этот образец.')
                    if action == 'skip':
                        counts['skipped'] += 1
                        break
            speech.say('Запись набора завершена.')
    return counts


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--corpus', help='External corpus, e.g. ~/mb-test-audio')
    parser.add_argument('--takes', type=int, default=3)
    parser.add_argument('--phrase-id', action='append', dest='phrase_ids')
    parser.add_argument('--list', action='store_true', dest='list_phrases')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args(argv)
    if args.list_phrases:
        for prompt in PROMPTS:
            print(f'{prompt.id:<24} {prompt.text} [{prompt.group}]')
        return 0
    if not args.corpus:
        parser.error('--corpus is required unless --list is used')
    try:
        counts = record(args.corpus, takes=args.takes, phrase_ids=args.phrase_ids, resume=args.resume)
        print('Итог: ' + ', '.join(f'{key}={value}' for key, value in counts.items()))
        return 1 if counts['skipped'] else 0
    except KeyboardInterrupt:
        print('\nЗапись остановлена. Завершённые WAV сохранены; принятые take — в manifest.', file=sys.stderr)
        return 130
    except (CorpusError, RecorderError) as exc:
        print(f'Ошибка: {exc}', file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        # Do not dump exception values containing absolute device/home paths.
        print(f'Ошибка записи/вывода ({type(exc).__name__}). Готовые файлы сохранены; '
              'ничего не удалено. Проверьте устройство и доступ к внешнему corpus.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(cli())
