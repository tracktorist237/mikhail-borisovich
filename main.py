#!/usr/bin/env python3
"""Михаил Борисович: локальный голосовой ассистент v0.1.1."""
import argparse
from array import array
from collections import deque
from datetime import datetime
import json
import math
import os
from pathlib import Path
import queue
import re
import resource
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent


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
    return cfg


def voices():
    result = []
    for base in [Path('/usr/share/RHVoice/voices'), Path('/usr/local/share/RHVoice/voices'), Path.home() / '.local/share/RHVoice/voices']:
        for path in sorted(base.glob('*/voice.info')):
            info = dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line)
            result.append(info)
    return result


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
        self.last = 'Пока нечего повторять.'
        log('SPEAKING', f'Голос: {self.voice}; вывод: {self.player}')

    def say(self, text, remember=True):
        self.stop()
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
            return True
        self.stop()
        return False

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
GRAMMAR = list(dict.fromkeys(COMMANDS + ['открой', 'закрой', 'файр фокс', 'браузер',
    'пять файр фокс', 'открой час', 'процент замолчи', 'пожалуйста'] +
    [f'громкость {w} {ending}' for w in NUMBERS
     for ending in ['процент', 'процента', 'процентов']] + ['[unk]']))
UNKNOWN = 'Не понял. Повторите команду.'


def normalize(text):
    text = ' '.join(re.sub(r'[^а-яa-z0-9\s]', ' ', text.lower().replace('ё', 'е')).split())
    return re.sub(r'\b(?:файр фокс|фаер фокс|файрфокс|фаерфокс|браузер)\b', 'firefox', text)


def match_intent(text, pending=None):
    text = normalize(text)
    words = set(text.split())
    if words & {'стоп', 'замолчи', 'хватит'}:
        return 'STOP', None
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
             if any(f' {phrase} ' in f' {text} ' for phrase in phrases)}
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


def command(text, cfg, speech, intent=None, value=None):
    text = normalize(text)
    log('COMMAND', text)
    if intent is None:
        intent, value = match_intent(text)
    if intent == 'STOP':
        speech.stop()
        return None
    if intent == 'TIME':
        now = datetime.now()
        return f'Сейчас {now.hour} часов {now.minute} минут.'
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
        batteries = [p for p in Path('/sys/class/power_supply').glob('*') if (p / 'type').read_text().strip() == 'Battery']
        if not batteries:
            return 'Батарея не обнаружена.'
        parts = []
        for p in batteries:
            level = (p / 'capacity').read_text().strip()
            status = (p / 'status').read_text().strip()
            parts.append(f'Заряд {level} процентов. ' + {'Charging': 'Идет зарядка.', 'Discharging': 'Работа от батареи.', 'Full': 'Батарея заряжена.'}.get(status, ''))
        return ' '.join(parts)
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


def listen(cfg, duration=0):
    sd, Model, Recognizer = dependencies()
    model = load_model(cfg, Model)
    speech = Speech(cfg)
    audio = queue.Queue(maxsize=12)
    issues = queue.Queue(maxsize=1)
    rate = cfg['sample_rate']
    block = rate // 10
    def callback(data, frames, timing, status):
        if status and issues.empty():
            issues.put_nowait(str(status))
        try:
            audio.put_nowait((time.monotonic(), bytes(data)))
        except queue.Full:
            if issues.empty():
                issues.put_nowait('Очередь микрофона переполнена; CPU не успевает.')
    def recognizer(words):
        return Recognizer(model, rate, json.dumps(words, ensure_ascii=False))
    wake = recognizer(['михаил', 'михаил борисович', '[unk]'])
    commands = recognizer(GRAMMAR)
    session = CommandSession()
    accept_after = float('inf')

    def clear_audio():
        while True:
            try:
                audio.get_nowait()
            except queue.Empty:
                break
    state = 'speaking'
    next_state = 'waiting'
    deadline = 0
    tail = 0
    preroll = deque(maxlen=3)
    began = time.monotonic()
    last_audio = began
    try:
        try:
            sd.check_input_settings(device=cfg['input_device'], channels=1, dtype='int16', samplerate=rate)
            stream = sd.RawInputStream(device=cfg['input_device'], samplerate=rate, blocksize=block, dtype='int16', channels=1, callback=callback)
        except Exception as exc:
            raise RuntimeError(f'Микрофон недоступен: {exc}. Запустите diagnose.py --devices.') from None
        with stream:
            speech.say('Михаил Борисович готов. Скажите Михаил.', remember=False)
            while not duration or time.monotonic() - began < duration:
                if not stream.active:
                    raise RuntimeError('Микрофон отключился.')
                if not issues.empty():
                    log('ERROR', issues.get_nowait())
                if state == 'speaking' and not speech.busy():
                    # Даем затихнуть динамикам, отбрасываем акустическое эхо.
                    state = 'cooldown'
                    deadline = time.monotonic() + cfg.get('tts_cooldown_ms', 500) / 1000
                    clear_audio()
                if state == 'cooldown' and time.monotonic() >= deadline:
                    state = next_state
                    wake.Reset()
                    commands.Reset()
                    clear_audio()
                    accept_after = time.monotonic()
                    tail = 0
                    preroll.clear()
                    deadline = time.monotonic() + cfg['command_timeout']
                    log('LISTENING' if state == 'listening' else 'WAITING')
                if state == 'listening' and time.monotonic() > deadline:
                    speech.say('Не услышал команду.', remember=False)
                    state, next_state = 'speaking', 'waiting'
                try:
                    captured, data = audio.get(timeout=0.2)
                except queue.Empty:
                    if time.monotonic() - last_audio > 3:
                        raise RuntimeError('Микрофон не передает звук более трех секунд.')
                    continue
                last_audio = time.monotonic()
                if state in ('speaking', 'cooldown') or captured <= accept_after:
                    continue
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
                text = result.get('text' if final else 'partial', '')
                if state == 'waiting' and 'михаил' in text.split():
                    log('WAKE WORD', text)
                    speech.say('Слушаю', remember=False)
                    session = CommandSession()
                    state, next_state = 'speaking', 'listening'
                elif state == 'listening' and final and text:
                    try:
                        answer, next_state = session.handle(text, cfg, speech)
                    except (RuntimeError, OSError, ValueError) as exc:
                        log('ERROR', str(exc))
                        answer = 'Не удалось выполнить команду.'
                        next_state = 'waiting'
                    if answer:
                        speech.say(answer, remember=answer not in (UNKNOWN, 'Не понял. Возвращаюсь в режим ожидания.'))
                    state = 'speaking'
    finally:
        speech.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-seconds', type=float, default=0, help='Ограничить время запуска для проверки')
    args = parser.parse_args()
    try:
        started = time.monotonic()
        listen(config(), args.run_seconds)
        if args.run_seconds:
            usage = resource.getrusage(resource.RUSAGE_SELF)
            cpu = (usage.ru_utime + usage.ru_stime) / (time.monotonic() - started) * 100
            log('WAITING', f'Проверка завершена; средний CPU {cpu:.1f}% одного ядра; пиковая RAM {usage.ru_maxrss / 1024:.0f} МиБ (включая загрузку модели).')
        return 0
    except KeyboardInterrupt:
        log('WAITING', 'Ассистент остановлен.')
        return 0
    except Exception as exc:
        log('ERROR', f'{exc}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
