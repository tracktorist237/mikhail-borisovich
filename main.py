#!/usr/bin/env python3
"""Михаил Борисович: локальный голосовой ассистент v0.2.1 (экспериментальные GPT-режимы)."""
import argparse
from array import array
from collections import deque
from contextlib import ExitStack
from datetime import datetime
import json
import math
import os
from pathlib import Path
import queue
import threading
import re
import resource
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import NamedTuple

from chatgpt_bridge import Bridge, OutputGuard, LOCAL_MODE, LARISA_MODE, ANTON_MODE
from gpt_modes import GPTModes, CONTROL_GRAMMAR
from diagnostics import cleanup_log, silence_broken_stream, output_lost

ROOT = Path(__file__).resolve().parent


class FreshAudioQueue:
    """Bounded callback queue which always keeps the newest audio blocks."""
    def __init__(self, maxsize=12):
        if maxsize < 1:
            raise ValueError('maxsize must be positive')
        self.maxsize = maxsize
        self.items = deque()
        self.condition = threading.Condition()
        self.dropped = 0
        self.discontinuities = 0
        self.pending_gap = False

    def put_latest(self, item, discontinuity=False):
        with self.condition:
            gap = False
            if len(self.items) >= self.maxsize:
                self.items.popleft()
                self.dropped += 1
                self.discontinuities += 1
                self.pending_gap = True
                gap = True
            self.items.append((*item, discontinuity))
            self.condition.notify()
            return gap

    def get(self, timeout=None):
        with self.condition:
            if not self.items:
                if not self.condition.wait(timeout):
                    raise queue.Empty
            if not self.items:
                raise queue.Empty
            captured, data, device_gap = self.items.popleft()
            gap = self.pending_gap or device_gap
            self.pending_gap = False
            return captured, data, gap

    def get_nowait(self):
        return self.get(0)

    def take_dropped(self):
        with self.condition:
            count = self.dropped
            self.dropped = 0
            return count

    def trim_wake(self, now, capture_now, boundary=None):
        """Consumer only: discard the unusable prefix in one bounded pass.

        No extra buffer or extended freshness window. While waiting for the
        player, keep fresh PCM irrespective of its origin; only the recorded
        playback boundary can later distinguish TTS from user audio.
        """
        def fresh(item):
            arrived, capture, _ = item
            return (isinstance(capture, CapturedPCM) and capture.start is not None
                    and 0 <= now-arrived <= .4 and 0 <= capture_now-capture.start <= .4)

        def post(item):
            capture = item[1]
            return (boundary is not None and isinstance(capture, CapturedPCM)
                    and capture.start is not None and capture.start >= boundary)

        def age(item):
            capture = item[1]
            return (round(capture_now-capture.start, 3)
                    if isinstance(capture, CapturedPCM) and capture.start is not None else None)

        with self.condition:
            info = dict(depth=len(self.items), dropped=0, post_stale=0,
                        last_arrival=self.items[-1][0] if self.items else None,
                        first_age=age(self.items[0]) if self.items else None,
                        last_age=age(self.items[-1]) if self.items else None)
            while self.items:
                item = self.items[0]
                if fresh(item) and (boundary is None or post(item)):
                    break
                self.items.popleft()
                info['dropped'] += 1
                info['post_stale'] += int(post(item) and not fresh(item))
                # Removing only a prefix preserves the existing gap contract:
                # the next frame resets recognition before it is consumed.
                self.pending_gap = True
            info['pending'] = {id(item[1]) for item in self.items if post(item) and fresh(item)}
            return info


def log(state, message=''):
    print(f'[{state}] {message}', flush=True)


def run(args, **kwargs):
    try:
        return subprocess.run(args, check=True, capture_output=True, timeout=8, **kwargs)
    except FileNotFoundError:
        raise RuntimeError(f'Не найдена программа {args[0]}') from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'{args[0]} не отвечает') from None
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode(errors='replace') if isinstance(exc.stderr, bytes) else exc.stderr
        raise RuntimeError(f'{args[0]}: {(detail or "ошибка выполнения").strip()[:400]}') from None


def config():
    cfg = json.loads((ROOT / 'config.json').read_text())
    cfg['model_path'] = str(ROOT / cfg['model_path'])
    if cfg['sample_rate'] != 16000 or not 0 <= cfg['silence_rms'] <= 32767:
        raise ValueError('sample_rate должен быть 16000; silence_rms — от 0 до 32767')
    if not 1 <= cfg['volume_step'] <= 100 or not 1 <= cfg['command_timeout'] <= 120:
        raise ValueError('Некорректные volume_step или command_timeout')
    if not 0.3 <= cfg.get('tts_cooldown_ms', 500) / 1000 <= 2:
        raise ValueError('tts_cooldown_ms должен быть от 300 до 2000')
    if not 0 < cfg.get('speaker_rms', 100) <= 32767 or not 0 < cfg.get('speaker_peak', 1000) <= 32768:
        raise ValueError('Некорректные speaker_rms / speaker_peak')
    if not 100 <= cfg.get('speaker_silence_ms', 800) <= 5000:
        raise ValueError('speaker_silence_ms должен быть от 100 до 5000')
    if not 0 < cfg.get('dictation_rms', 1000) <= 32767 or not 500 <= cfg.get('dictation_silence_ms', 1400) <= 5000:
        raise ValueError('Некорректные параметры диктовки')
    if cfg.get('dictation_stop_method', 'os-keyboard') not in {
            'os-keyboard', 'native', 'dom', 'bidi', 'keyboard'}:
        raise ValueError('Недопустимый dictation_stop_method')
    if not 5 <= cfg.get('dictation_max_seconds', 45) <= 120 or not 10 <= cfg.get('reply_timeout', 180) <= 600:
        raise ValueError('Некорректные таймауты GPT')
    if not .5 <= cfg.get('browser_close_timeout', 12) <= 60:
        raise ValueError('browser_close_timeout должен быть от 0.5 до 60 секунд')
    return cfg


def voices():
    result = []
    for base in [Path('/usr/share/RHVoice/voices'), Path('/usr/local/share/RHVoice/voices'), Path.home() / '.local/share/RHVoice/voices']:
        for path in sorted(base.glob('*/voice.info')):
            info = dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line)
            result.append(info)
    return result


class CapturedPCM(NamedTuple):
    pcm: bytes
    start: float | None
    end: float | None

    @classmethod
    def from_callback(cls, data, frames, rate, timing):
        # ADC timestamps and stream.time share PortAudio's unspecified epoch.
        # Never interpret them as Python monotonic timestamps or rebase them
        # at callback delivery (which may be delayed by the scheduler).
        try:
            start = float(timing.inputBufferAdcTime)
            end = start + frames / rate
            current = float(timing.currentTime)
            if not all(math.isfinite(x) for x in (start, end, current)) or end > current + 1 / rate:
                start = end = None
        except (AttributeError, TypeError, ValueError):
            start = end = None
        return cls(bytes(data), start, end)


class PlaybackEnd:
    """One blocking wait, one timestamp, one utterance. No polling worker.

    Player exit after drain is the available completion confirmation, not a
    DAC timestamp. Sampling the input stream clock immediately after wait()
    gives a conservative boundary independent of the main-loop polling lag.
    Scheduling lag of this waiter is not subtracted or guessed away.
    """
    def __init__(self, clock):
        self.clock = clock
        self.done = threading.Event()
        self.boundary = None
        self.status = None
        self.error = None
        self.thread = None

    def start(self, process):
        def completed():
            try:
                self.status = process.wait()
                if self.status == 0:
                    boundary = float(self.clock())
                    if not math.isfinite(boundary):
                        raise ValueError('Invalid playback clock')
                    self.boundary = boundary
            except Exception as exc:
                self.error = type(exc).__name__
            finally:
                self.done.set()
        self.thread = threading.Thread(target=completed, name='wake-playback-end', daemon=True)
        self.thread.start()

    def join(self):
        if self.thread is not None:
            self.thread.join(timeout=1)
            if self.thread.is_alive():
                raise RuntimeError('Не подтверждено завершение ожидания playback.')


class Speech:
    def __init__(self, cfg):
        if not shutil.which('RHVoice-test'):
            raise RuntimeError('RHVoice-test не найден. Проверьте установку RHVoice.')
        available = voices()
        self.voice = cfg['voice']
        if self.voice == 'auto':
            male = [v['name'] for v in available if v.get('language', '').lower() == 'russian' and v.get('gender') == 'male']
            if not male:
                raise RuntimeError('Не найден русский мужской голос. Запустите diagnose.py --voices и укажите voice в config.json.')
            self.voice = 'Mikhail' if 'Mikhail' in male else male[0]
        self.player = next((x for x in ['pw-play', 'paplay', 'aplay'] if shutil.which(x)), None)
        if not self.player:
            raise RuntimeError('Не найден аудиоплеер pw-play, paplay или aplay.')
        self.tmp = tempfile.TemporaryDirectory(prefix='mb-')
        self.path = str(Path(self.tmp.name) / 'speech.wav')
        self.process = None
        self.phase = None
        self.completion = None
        self.last = 'Пока нечего повторять.'
        log('SPEAKING', f'Голос: {self.voice}; вывод: {self.player}')

    def say(self, text, remember=True, *, completion_clock=None):
        self.stop()
        self.completion = PlaybackEnd(completion_clock) if completion_clock is not None else None
        if remember:
            self.last = text
        log('SPEAKING', text)
        self.errors = tempfile.TemporaryFile()
        self.process = subprocess.Popen(['RHVoice-test', '-p', self.voice, '-o', self.path], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.errors)
        self.process.stdin.write(text.encode('utf-8'))
        self.process.stdin.close()
        self.phase = 'synthesis'
        self.started = time.monotonic()

    def busy(self):
        if not self.process:
            return False
        if time.monotonic() - self.started > 30:
            self.stop()
            raise RuntimeError('Синтез речи или аудиовыход не отвечает.')
        completion = getattr(self, 'completion', None)
        if self.phase == 'playback' and completion is not None:
            if not completion.done.is_set():
                return True
            if completion.error:
                self.stop()
                raise RuntimeError('Не удалось подтвердить границу окончания playback.')
            status = completion.status
        else:
            status = self.process.poll()
        if status is None:
            return True
        if status:
            self.errors.seek(0)
            detail = self.errors.read(1000).decode(errors='replace')
            self.stop()
            raise RuntimeError(f'Не удалось озвучить ответ: {detail.strip()}')
        if self.phase == 'synthesis':
            self.process = subprocess.Popen([self.player, self.path], stdout=subprocess.DEVNULL, stderr=self.errors)
            self.phase = 'playback'
            if completion is not None:
                completion.start(self.process)
            return True
        self.stop()
        return False

    @property
    def playback_end(self):
        return self.completion.boundary if self.completion is not None else None

    def wait_for_completion(self, timeout):
        if self.completion is not None:
            self.completion.done.wait(timeout)

    def stop(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process = None
            self.errors.close()
        completion = getattr(self, 'completion', None)
        if completion is not None:
            completion.join()
        self.phase = None

    def close(self):
        self.stop()
        self.tmp.cleanup()


def internet():
    # IP адреса: проверка не зависит от DNS и не передает речь.
    for host in ['1.1.1.1', '8.8.8.8']:
        try:
            with socket.create_connection((host, 443), timeout=2):
                return True
        except OSError:
            pass
    return False


ONES = 'ноль один два три четыре пять шесть семь восемь девять десять одиннадцать двенадцать тринадцать четырнадцать пятнадцать шестнадцать семнадцать восемнадцать девятнадцать'.split()
TENS = 'двадцать тридцать сорок пятьдесят шестьдесят семьдесят восемьдесят девяносто'.split()


def number_words(n):
    if n < 20:
        return ONES[n]
    if n == 100:
        return 'сто'
    return TENS[n // 10 - 2] + (' ' + ONES[n % 10] if n % 10 else '')


NUMBERS = {number_words(n): n for n in range(101)}
INTENTS = {
    'LARISA_OPEN': ['позови ларису', 'открой ларису', 'хочу поговорить с ларисой', 'лариса'],
    'ANTON_OPEN': ['позови антона павловича', 'открой антона павловича', 'антон павлович', 'хочу спросить антона павловича'],
    'CHATGPT_OPEN': ['позови chatgpt', 'позови чат жпт', 'открой chatgpt', 'открой чат жпт', 'хочу поговорить с chatgpt', 'чат жпт'],
    'CHATGPT_CLOSE': ['закрой chatgpt', 'закрой чат жпт', 'вернись', 'верни Михаила', 'закончи разговор', 'закрой ларису', 'закрой антона павловича'],
    'TIME': ['который час', 'сколько времени', 'время', 'час'],
    'DATE': ['какая дата', 'какое сегодня число', 'сегодняшняя дата', 'дата'],
    'INTERNET': ['есть интернет', 'интернет', 'проверь интернет', 'есть связь'],
    'BATTERY': ['какой заряд', 'сколько заряда', 'батарея', 'заряд'],
    'OPEN_BROWSER': ['открой firefox', 'firefox открой'],
    'CLOSE_BROWSER': ['закрой firefox', 'firefox закрой'],
    'VOLUME_UP': ['громче', 'сделай громче', 'прибавь звук'],
    'VOLUME_DOWN': ['тише', 'сделай тише', 'убавь звук'],
    'STOP': ['стоп', 'замолчи', 'хватит'],
    'REPEAT': ['повтори'],
}
COMMANDS = [phrase for phrases in INTENTS.values() for phrase in phrases]
# В словаре Vosk Firefox представлен фонетически.
COMMANDS = [phrase.replace('firefox', alias) for phrase in COMMANDS
            for alias in ('файр фокс', 'фаер фокс', 'браузер')]
COMMANDS = [p.replace('chatgpt', 'чат ж п т').replace('жпт', 'ж п т').lower() for p in COMMANDS]
GRAMMAR = list(dict.fromkeys(COMMANDS + ['открой', 'закрой', 'файр фокс', 'браузер',
    'пять файр фокс', 'открой час', 'процент замолчи', 'пожалуйста'] +
    [f'громкость {w} {ending}' for w in NUMBERS
     for ending in ['процент', 'процента', 'процентов']] + ['[unk]']))
UNKNOWN = 'Не понял. Повторите команду.'


def normalize(text):
    text = ' '.join(re.sub(r'[^а-яa-z0-9\s]', ' ', text.lower().replace('ё', 'е')).split())
    text = re.sub(r'\b(?:чат ж\s*п\s*т|чат джи пи ти)\b', 'chatgpt', text)
    return re.sub(r'\b(?:файр фокс|фаер фокс|файрфокс|фаерфокс|браузер)\b', 'firefox', text)


def match_intent(text, pending=None):
    text = normalize(text)
    if text == 'закрой чат ж п':
        text = 'закрой chatgpt'
    words = set(text.split())
    if words & {'стоп', 'замолчи', 'хватит'}:
        return 'STOP', None
    # ChatGPT names are matched before generic browser verbs. Reject mixed actions.
    trimmed = ' '.join(w for w in text.split() if w not in {'ну', 'пожалуйста'})
    for intent in ('CHATGPT_CLOSE', 'CHATGPT_OPEN', 'LARISA_OPEN', 'ANTON_OPEN'):
        if trimmed in {normalize(p) for p in INTENTS[intent]}:
            return intent, None
    if words & {'chatgpt', 'лариса', 'ларису', 'ларисой', 'антон', 'антона', 'павлович', 'павловича'}:
        return None, None
    # Глагол действия блокирует случайное совпадение с «час» и другими запросами.
    actions = words & {'открой', 'закрой'}
    if actions:
        if len(actions) == 1 and 'firefox' in words:
            return ('OPEN_BROWSER' if 'открой' in actions else 'CLOSE_BROWSER'), None
        return None, None
    if pending and text == 'firefox':
        return pending, None
    match = re.search(r'\bгромкость (.+?) процент(?:а|ов)?\b', text)
    if match:
        raw = match[1]
        if raw.isdecimal() or raw in NUMBERS:
            return 'VOLUME_SET', match[0]
    found = {intent for intent, phrases in INTENTS.items()
             if intent not in ('CHATGPT_OPEN', 'CHATGPT_CLOSE', 'LARISA_OPEN', 'ANTON_OPEN') and any(f' {phrase} ' in f' {text} ' for phrase in phrases)}
    if len(found) == 1:
        return found.pop(), None
    return None, None


class CommandSession:
    """Одна команда на обращение, максимум две попытки."""
    def __init__(self):
        self.failures = 0
        self.pending = None

    def handle(self, text, cfg, speech):
        intent, value = match_intent(text, self.pending)
        if intent is None:
            self.failures += 1
            self.pending = {'открой': 'OPEN_BROWSER', 'закрой': 'CLOSE_BROWSER'}.get(normalize(text))
            if self.failures < 2:
                return UNKNOWN, 'listening'
            return 'Не понял. Возвращаюсь в режим ожидания.', 'waiting'
        return command(text, cfg, speech, intent, value), 'waiting'


def volume(text, cfg):
    if text in ('громче', 'тише'):
        delta = cfg['volume_step'] * (1 if text == 'громче' else -1)
        if shutil.which('wpctl'):
            out = run(['wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@']).stdout.decode()
            current = round(float(re.search(r'Volume:\s*([\d.]+)', out)[1]) * 100)
        else:
            out = run(['pactl', 'get-sink-volume', '@DEFAULT_SINK@']).stdout.decode()
            current = int(re.search(r'(\d+)%', out)[1])
        value = max(0, min(100, current + delta))
    else:
        match = re.fullmatch(r'громкость (.+) процент(?:а|ов)?', text)
        if not match:
            return 'Скажите: громкость пятьдесят процентов.'
        raw = match[1]
        value = int(raw) if raw.isdecimal() else NUMBERS.get(raw, -1)
        if not 0 <= value <= 100:
            return 'Громкость должна быть от нуля до ста процентов.'
    if shutil.which('wpctl'):
        run(['wpctl', 'set-volume', '@DEFAULT_AUDIO_SINK@', f'{value}%'])
        if value:
            run(['wpctl', 'set-mute', '@DEFAULT_AUDIO_SINK@', '0'])
    else:
        run(['pactl', 'set-sink-volume', '@DEFAULT_SINK@', f'{value}%'])
        if value:
            run(['pactl', 'set-sink-mute', '@DEFAULT_SINK@', '0'])
    return f'Громкость {value} процентов.'


def ru_form(number, one, few, many):
    """Russian noun form for an integer count."""
    number = abs(number)
    if 11 <= number % 100 <= 14:
        return many
    if number % 10 == 1:
        return one
    if 2 <= number % 10 <= 4:
        return few
    return many


def battery_report(root=Path('/sys/class/power_supply')):
    """Read every available battery; sysfs entries can disappear while queried."""
    try:
        entries = list(Path(root).iterdir())
    except FileNotFoundError:
        return 'Батарея не обнаружена.'
    except OSError:
        return 'Не удалось получить данные о заряде батареи.'
    if not entries:
        return 'Батарея не обнаружена.'

    detected_battery = False
    unreadable_type = False
    parts = []
    for entry in entries:
        try:
            supply_type = (entry / 'type').read_text().strip()
        except OSError:
            unreadable_type = True
            continue
        if supply_type != 'Battery':
            continue
        detected_battery = True
        try:
            level = (entry / 'capacity').read_text().strip()
            status = (entry / 'status').read_text().strip()
        except OSError:
            continue
        status_text = {
            'Charging': 'Идет зарядка.',
            'Discharging': 'Работа от батареи.',
            'Full': 'Батарея заряжена.',
        }.get(status, '')
        parts.append(f'Заряд {level} процентов. ' + status_text)

    if parts:
        return ' '.join(parts)
    if detected_battery or unreadable_type:
        return 'Не удалось получить данные о заряде батареи.'
    return 'Батарея не обнаружена.'


def command(text, cfg, speech, intent=None, value=None):
    text = normalize(text)
    log('COMMAND', text)
    if intent is None:
        intent, value = match_intent(text)
    if intent in ('CHATGPT_OPEN', 'CHATGPT_CLOSE', 'LARISA_OPEN', 'ANTON_OPEN'):
        return 'Команда ChatGPT доступна в голосовом цикле.'
    if intent == 'STOP':
        speech.stop()
        return None
    if intent == 'TIME':
        now = datetime.now()
        hours = ru_form(now.hour, 'час', 'часа', 'часов')
        minutes = ru_form(now.minute, 'минута', 'минуты', 'минут')
        return f'Сейчас {now.hour} {hours} {now.minute} {minutes}.'
    if intent == 'DATE':
        months = 'января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split()
        now = datetime.now()
        return f'Сегодня {now.day} {months[now.month - 1]} {now.year} года.'
    if intent in ('VOLUME_UP', 'VOLUME_DOWN', 'VOLUME_SET'):
        return volume({'VOLUME_UP': 'громче', 'VOLUME_DOWN': 'тише'}.get(intent, value), cfg)
    if intent in ('OPEN_BROWSER', 'CLOSE_BROWSER'):
        if intent == 'OPEN_BROWSER':
            if not shutil.which('firefox'):
                return 'Firefox не установлен.'
            child = subprocess.Popen(['firefox'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            time.sleep(0.3)
            if child.poll() not in (None, 0):
                return 'Не удалось открыть Firefox.'
            return 'Открываю Firefox.'
        # SIGTERM только процессам Firefox текущего пользователя; без SIGKILL.
        result = subprocess.run(['pkill', '-TERM', '-u', str(os.getuid()), '-x', 'firefox|firefox-bin'], capture_output=True, timeout=3)
        if result.returncode not in (0, 1):
            raise RuntimeError('Не удалось закрыть Firefox.')
        return 'Закрываю Firefox.' if result.returncode == 0 else 'Firefox уже закрыт.'
    if intent == 'INTERNET':
        return 'Интернет доступен.' if internet() else 'Не удалось подтвердить доступ к интернету.'
    if intent == 'BATTERY':
        return battery_report()
    if intent == 'REPEAT':
        return speech.last
    return UNKNOWN


def dependencies(audio=True):
    try:
        sd = None
        if audio:
            if sys.platform == "linux" and not Path("/dev/snd").exists():
                raise RuntimeError("Аудиоустройства /dev/snd недоступны. Запустите программу в обычном терминале Lubuntu вне песочницы.")
            import sounddevice as sd
        from vosk import Model, KaldiRecognizer, SetLogLevel
    except Exception as exc:
        raise RuntimeError(f'Не удалось загрузить Vosk или аудиобиблиотеку: {exc}. Установите requirements.txt в .venv.') from None
    SetLogLevel(-1)
    return sd, Model, KaldiRecognizer


def load_model(cfg, Model):
    path = Path(cfg['model_path'])
    if not (path / 'am/final.mdl').is_file():
        raise RuntimeError(f'Нет модели Vosk: {path}. Инструкция загрузки в README.md.')
    log('WAITING', 'Загружаю локальную модель Vosk…')
    return Model(str(path))


def listen(cfg, duration=0, check_mode=None):
    sd, Model, Recognizer = dependencies()
    model = load_model(cfg, Model)
    speech = Speech(cfg)
    audio = FreshAudioQueue(maxsize=12)
    issues = queue.Queue(maxsize=1)
    rate = cfg['sample_rate']
    block = rate // 10
    def callback(data, frames, timing, status):
        if status and issues.empty():
            issues.put_nowait(str(status))
        audio.put_latest((time.monotonic(), CapturedPCM.from_callback(data, frames, rate, timing)),
                         discontinuity=bool(status))
    def recognizer(words):
        return Recognizer(model, rate, json.dumps(words, ensure_ascii=False))
    wake = recognizer(['михаил', 'михаил борисович', '[unk]'])
    commands = recognizer(GRAMMAR)
    session = CommandSession()
    bridge = Bridge(cfg)
    guard = OutputGuard(cfg)
    control = recognizer(CONTROL_GRAMMAR)
    gpt = GPTModes(cfg, speech, bridge, guard, control)
    accept_after = float('inf')
    command_capture_after = None  # PortAudio clock, never compared to monotonic.
    handoff = None
    wake_trimmed = 0
    wake_overflow_start = 0

    def report_handoff():
        nonlocal handoff
        if handoff is not None:
            log('LOCAL READY', f"boundary={handoff['boundary']:.3f} "
                f"poll_lag={handoff['poll_lag']:.3f} "
                f"depth={handoff['depth']} overflow={handoff['overflow']} "
                f"expired_wait={wake_trimmed} dropped={handoff['dropped']} "
                f"first_age={handoff['first_age']} last_age={handoff['last_age']} "
                f"post_kept={handoff['kept']} post_stale={handoff['post_stale']} "
                f"stale_after={handoff['stale_after']} result_stale={handoff['result_stale']} "
                f"unconsumed={len(handoff['pending'])}")
            handoff = None

    def clear_audio():
        while True:
            try:
                audio.get_nowait()
            except queue.Empty:
                break
    state = 'speaking'
    next_state = 'waiting'
    readiness = 'cooldown'
    deadline = 0
    tail = 0
    preroll = deque(maxlen=3)
    began = time.monotonic()
    last_audio = began
    drop_counts = {'overflow': 0, 'stale': 0}
    next_audio_report = began
    try:
        try:
            sd.check_input_settings(device=cfg['input_device'], channels=1, dtype='int16', samplerate=rate)
            stream = sd.RawInputStream(device=cfg['input_device'], samplerate=rate, blocksize=block, dtype='int16', channels=1, callback=callback)
        except Exception as exc:
            raise RuntimeError(f'Микрофон недоступен: {exc}. Запустите diagnose.py --devices.') from None
        with stream, ExitStack() as playback_cleanup:
            # The exit waiter samples this stream's clock. Stop/join it before
            # RawInputStream.__exit__ destroys the PortAudio stream handle.
            playback_cleanup.callback(speech.stop)
            if check_mode:
                gpt.start(check_mode)
            else:
                speech.say('Михаил Борисович готов. Скажите Михаил.', remember=False)
            while not duration or time.monotonic() - began < duration:
                if output_lost.is_set():
                    raise BrokenPipeError('Output channel closed')
                if not stream.active:
                    raise RuntimeError('Микрофон отключился.')
                if handoff is not None and (not handoff['pending'] or state != 'listening' or gpt.active):
                    report_handoff()
                if not issues.empty():
                    log('ERROR', issues.get_nowait())
                drop_counts['overflow'] += audio.take_dropped()
                if time.monotonic() >= next_audio_report and any(drop_counts.values()):
                    log('AUDIO', f"Discarded frames: overflow={drop_counts['overflow']}, stale={drop_counts['stale']}")
                    drop_counts = {'overflow': 0, 'stale': 0}
                    next_audio_report = time.monotonic() + 2
                if gpt.active:
                    try:
                        captured, data, discontinuity = audio.get(timeout=.2)
                    except queue.Empty:
                        gpt.tick(time.monotonic())
                        if time.monotonic() - last_audio > 3:
                            raise RuntimeError('Микрофон не передает звук более трех секунд.')
                    else:
                        last_audio = time.monotonic()
                        if isinstance(data, CapturedPCM):
                            data = data.pcm
                        if discontinuity:
                            gpt.audio_gap(captured, discard_through=False)
                        if not 0 <= last_audio - captured <= .4:
                            drop_counts['stale'] += 1
                        gpt.step(data, captured)
                    if not gpt.active:
                        if guard.close() is False:
                            raise RuntimeError('Speaker monitor shutdown not confirmed; replacement blocked.')
                        guard = OutputGuard(cfg)
                        gpt.guard = guard
                        state = 'waiting'
                        wake.Reset()
                        commands.Reset()
                        clear_audio()
                        accept_after = time.monotonic()
                        command_capture_after = None
                        tail = 0
                        preroll.clear()
                        log('WAITING')
                    continue
                playback_ready = False
                if state == 'speaking' and not speech.busy():
                    if readiness == 'playback_end':
                        # Exit was recorded independently of this later poll.
                        boundary = speech.playback_end
                        if not isinstance(boundary, (int, float)) or not math.isfinite(boundary):
                            raise RuntimeError('Недостоверная граница окончания «Слушаю».')
                        command_capture_after = boundary
                        handoff = audio.trim_wake(time.monotonic(), stream.time, boundary)
                        if handoff['last_arrival'] is not None:
                            last_audio = max(last_audio, handoff['last_arrival'])
                        handoff.update(boundary=boundary, poll_lag=stream.time-boundary,
                                       kept=len(handoff['pending']), stale_after=0, result_stale=0,
                                       overflow=audio.discontinuities-wake_overflow_start)
                        accept_after = float('-inf')
                        playback_ready = True
                    else:
                        state = 'cooldown'
                        deadline = time.monotonic() + cfg.get('tts_cooldown_ms', 500) / 1000
                        clear_audio()
                if playback_ready or (state == 'cooldown' and time.monotonic() >= deadline):
                    state = next_state
                    wake.Reset()
                    commands.Reset()
                    if not playback_ready:
                        clear_audio()
                        accept_after = time.monotonic()
                        command_capture_after = None
                    readiness = 'cooldown'
                    tail = 0
                    preroll.clear()
                    deadline = time.monotonic() + cfg['command_timeout']
                    log('LISTENING' if state == 'listening' else 'WAITING')
                if state == 'speaking' and readiness == 'playback_end':
                    # Drain only already-expired PCM while waiting. Fresh PCM
                    # can belong to the user before this loop observes exit.
                    trimmed = audio.trim_wake(time.monotonic(), stream.time)
                    wake_trimmed += trimmed['dropped']
                    if trimmed['last_arrival'] is not None:
                        last_audio = max(last_audio, trimmed['last_arrival'])
                    speech.wait_for_completion(.2)
                    continue
                if state == 'listening' and time.monotonic() > deadline:
                    speech.say('Не услышал команду.', remember=False)
                    state, next_state = 'speaking', 'waiting'
                try:
                    captured, data, discontinuity = audio.get(timeout=0.2)
                except queue.Empty:
                    if time.monotonic() - last_audio > 3:
                        raise RuntimeError('Микрофон не передает звук более трех секунд.')
                    continue
                last_audio = time.monotonic()
                capture = data if isinstance(data, CapturedPCM) else None
                if capture is not None:
                    data = capture.pcm
                stale = not 0 <= last_audio - captured <= .4
                if command_capture_after is not None and capture is not None and capture.start is not None:
                    age = stream.time - capture.start
                    stale = stale or not 0 <= age <= .4
                if handoff is not None and capture is not None and id(capture) in handoff['pending']:
                    handoff['pending'].remove(id(capture))
                    handoff['stale_after'] += int(stale)
                if discontinuity or stale:
                    wake.Reset()
                    commands.Reset()
                    preroll.clear()
                    tail = 0
                if stale:
                    drop_counts['stale'] += 1
                    continue
                if state in ('speaking', 'cooldown', 'chatgpt_busy') or captured <= accept_after:
                    continue
                if command_capture_after is not None:
                    if capture is None or capture.start is None or capture.start < command_capture_after:
                        commands.Reset()
                        continue  # Includes every block straddling the end.
                if state == 'waiting':
                    samples = array('h', data)[::4]
                    rms = math.sqrt(sum(x*x for x in samples) / len(samples))
                    preroll.append(data)
                    if rms >= cfg['silence_rms']:
                        if tail == 0:
                            wake.Reset()
                            data = b''.join(preroll)
                        tail = 12
                    elif tail:
                        tail -= 1
                    else:
                        continue
                rec = wake if state == 'waiting' else commands
                final = rec.AcceptWaveform(data)
                result = json.loads(rec.Result() if final else rec.PartialResult())
                if state == 'listening' and command_capture_after is not None and (
                        not 0 <= time.monotonic()-captured <= .4 or
                        not 0 <= stream.time-capture.start <= .4):
                    # Recognition itself may stall. Never execute its now-old
                    # command or retain a partial result across that gap.
                    commands.Reset()
                    drop_counts['stale'] += 1
                    if handoff is not None:
                        handoff['result_stale'] += 1
                    continue
                text = result.get('text' if final else 'partial', '')
                if state == 'waiting' and 'михаил' in text.split():
                    log('WAKE WORD', text)
                    report_handoff()
                    wake_trimmed = 0
                    wake_overflow_start = audio.discontinuities
                    speech.say('Слушаю', remember=False, completion_clock=lambda: stream.time)
                    session = CommandSession()
                    state, next_state = 'speaking', 'listening'
                    readiness = 'playback_end'
                elif state == 'listening' and final:
                    try:
                        intent, _ = match_intent(text)
                        diagnostic_intent = intent or match_intent(text, session.pending)[0]
                        diagnostic_text = normalize(text)
                        log('LOCAL ASR', f'final={json.dumps(diagnostic_text[:240], ensure_ascii=False)} '
                            f'intent={diagnostic_intent or "NONE"}' + (' truncated=true' if len(diagnostic_text) > 240 else ''))
                        if not text:
                            continue  # Preserve empty-final behavior; no automatic retry.
                        if intent in ('CHATGPT_OPEN', 'LARISA_OPEN', 'ANTON_OPEN'):
                            gpt.start(ANTON_MODE if intent == 'ANTON_OPEN' else LARISA_MODE)
                            clear_audio()
                            continue
                        elif intent == 'CHATGPT_CLOSE':
                            answer, next_state = 'Я снова слушаю.', 'waiting'
                        else:
                            answer, next_state = session.handle(text, cfg, speech)
                    except (RuntimeError, OSError, ValueError) as exc:
                        log('ERROR', str(exc))
                        answer = 'Не удалось выполнить команду.'
                        next_state = 'waiting'
                    if answer:
                        speech.say(answer, remember=answer not in (UNKNOWN, 'Не понял. Возвращаюсь в режим ожидания.'))
                    state = 'speaking'
    finally:
        try:
            bridge.close()
        finally:
            try:
                guard.close()
            finally:
                speech.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-seconds', type=float, default=0, help='Ограничить время запуска для проверки')
    parser.add_argument('--check-mode', choices=['larisa', 'anton'],
                        help='Диагностический вход в GPT-режим без wake word; не acceptance A/B')
    args = parser.parse_args()
    try:
        started = time.monotonic()
        listen(config(), args.run_seconds,
               {'larisa': LARISA_MODE, 'anton': ANTON_MODE}.get(args.check_mode))
        if args.run_seconds:
            usage = resource.getrusage(resource.RUSAGE_SELF)
            cpu = (usage.ru_utime + usage.ru_stime) / (time.monotonic() - started) * 100
            log('WAITING', f'Проверка завершена; средний CPU {cpu:.1f}% одного ядра; пиковая RAM {usage.ru_maxrss / 1024:.0f} МиБ (включая загрузку модели).')
        return 0
    except KeyboardInterrupt:
        cleanup_log('[WAITING] Ассистент остановлен.', flush=True)
        return 0
    except BrokenPipeError:
        silence_broken_stream(sys.stdout)
        silence_broken_stream(sys.stderr)
        return 1
    except Exception as exc:
        cleanup_log(f'[ERROR] {exc}', flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
