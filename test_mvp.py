"""Проверки команд без изменения системы и RHVoice → Vosk без динамиков."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import wave
from array import array
import main


class Commands(unittest.TestCase):
    def setUp(self):
        self.cfg = main.config()
        self.speech = Mock(last='Предыдущий ответ')

    def test_readonly(self):
        for text in ['который час', 'сколько времени', 'какая дата', 'какой заряд']:
            self.assertTrue(main.command(text, self.cfg, self.speech))
        self.assertEqual(main.command('повтори', self.cfg, self.speech), 'Предыдущий ответ')
        self.assertIsNone(main.command('замолчи', self.cfg, self.speech))
        self.speech.stop.assert_called_once()
        self.assertIn('Не понял', main.command('удали файлы', self.cfg, self.speech))

    @patch('main.run')
    @patch('main.shutil.which', return_value='/usr/bin/wpctl')
    def test_volume(self, which, run):
        run.return_value.stdout = b'Volume: 0.95'
        self.assertIn('100', main.volume('громче', self.cfg))
        run.assert_any_call(['wpctl', 'set-volume', '@DEFAULT_AUDIO_SINK@', '100%'])
        for n in range(101):
            self.assertEqual(main.volume(f'громкость {main.number_words(n)} процентов', self.cfg), f'Громкость {n} процентов.')
        run.reset_mock()
        self.assertIn('от нуля до ста', main.volume('громкость 101 процентов', self.cfg))
        run.assert_not_called()

    @patch('main.subprocess.run')
    def test_firefox_close(self, run):
        run.return_value.returncode = 1
        self.assertIn('уже закрыт', main.command('закрой файрфокс', self.cfg, self.speech))
        self.assertIn('-TERM', run.call_args.args[0])

    def test_missing_model(self):
        with self.assertRaisesRegex(RuntimeError, 'Нет модели'):
            main.load_model({'model_path': '/tmp/no-such-mb-model'}, Mock())


class IntentTests(unittest.TestCase):
    def test_variants(self):
        for intent, phrases in main.INTENTS.items():
            for phrase in phrases:
                with self.subTest(phrase=phrase):
                    self.assertEqual(main.match_intent('ну пожалуйста ' + phrase + ' пожалуйста')[0], intent)
        for phrase in ['открой файрфокс', 'открой браузер', 'браузер открой']:
            self.assertEqual(main.match_intent(phrase)[0], 'OPEN_BROWSER')
        for phrase in ['firefox', 'пять firefox', 'открой час', 'часовщик', 'неизвестная команда']:
            self.assertIsNone(main.match_intent(phrase)[0])
        self.assertEqual(main.match_intent('процент замолчи')[0], 'STOP')

    @patch('main.volume')
    @patch('main.subprocess.Popen')
    def test_no_accidental_actions(self, popen, volume):
        for phrase in ['firefox', 'пять firefox', 'открой час', 'процент замолчи']:
            main.command(phrase, main.config(), Mock())
        popen.assert_not_called()
        volume.assert_not_called()

    def test_retry(self):
        session = main.CommandSession()
        self.assertEqual(session.handle('не знаю', main.config(), Mock()), (main.UNKNOWN, 'listening'))
        answer, state = session.handle('час', main.config(), Mock())
        self.assertIn('Сейчас', answer)
        self.assertEqual(state, 'waiting')
        session = main.CommandSession()
        session.handle('не знаю', main.config(), Mock())
        self.assertEqual(session.handle('не знаю', main.config(), Mock()),
                         ('Не понял. Возвращаюсь в режим ожидания.', 'waiting'))

    @patch('main.command', return_value='Открываю Firefox.')
    def test_browser_context(self, command):
        session = main.CommandSession()
        self.assertEqual(session.handle('открой', main.config(), Mock())[1], 'listening')
        self.assertEqual(session.handle('firefox', main.config(), Mock())[1], 'waiting')
        self.assertEqual(command.call_args.args[3], 'OPEN_BROWSER')
        self.assertIsNone(main.match_intent('пять firefox', 'OPEN_BROWSER')[0])

    def test_audio_feedback_and_retry(self):
        # Реальный цикл listen с виртуальными часами и микрофоном.
        clock = [0.0]
        speech = Mock(last='ответ')
        ends = [0.0]
        def say(text, remember=True):
            ends[0] = clock[0] + .3
        speech.say.side_effect = say
        speech.busy.side_effect = lambda: clock[0] < ends[0]
        callbacks = []
        delivered = []
        command_texts = iter(['неизвестно', 'час'])
        class Rec:
            def __init__(self, model, rate, grammar):
                self.wake = 'михаил' in json.loads(grammar)
            def Reset(self):
                pass
            def AcceptWaveform(self, data):
                delivered.append(data)
                # Эхо TTS и хвост из cooldown никогда не должны сюда попасть.
                assert data != b'echo'
                return True
            def Result(self):
                return json.dumps({'text': 'михаил' if self.wake else next(command_texts, 'стоп')})
        class Stream:
            def __init__(self, **kwargs):
                callbacks.append(kwargs['callback'])
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            @property
            def active(self):
                clock[0] += .1
                echo = clock[0] <= ends[0] + .5
                callbacks[0](b'echo' if echo else b'\xff\x7f' * 1600, 1600, None, None)
                return True
        sd = Mock(RawInputStream=Stream)
        with patch('main.dependencies', return_value=(sd, Mock(), Rec)), \
             patch('main.load_model'), patch('main.Speech', return_value=speech), \
             patch('main.time.monotonic', side_effect=lambda: clock[0]), \
             patch('main.command', wraps=main.command) as execute:
            main.listen(main.config(), duration=3.8)
        spoken = [call.args[0] for call in speech.say.call_args_list]
        self.assertIn(main.UNKNOWN, spoken)
        self.assertEqual(sum(text.startswith('Сейчас') for text in spoken), 1)
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[0], 'час')
        self.assertTrue(delivered)


def recognition():
    _, Model, Recognizer = main.dependencies(audio=False)
    cfg = main.config()
    model = main.load_model(cfg, Model)
    phrases = ['михаил', 'михаил борисович', 'который час', 'сколько времени', 'какая дата', 'громче', 'тише', 'громкость пятьдесят процентов', 'открой файрфокс', 'закрой файрфокс', 'есть интернет', 'какой заряд', 'повтори', 'стоп', 'замолчи']
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / 'test.wav')
        for phrase in phrases:
            main.run(['RHVoice-test', '-p', 'Mikhail', '-o', path], input=phrase.encode())
            with wave.open(path, 'rb') as f:
                assert f.getsampwidth() == 2 and f.getnchannels() == 1
                source_rate = f.getframerate()
                samples = array('h', f.readframes(f.getnframes()))
            # Только для тестового WAV: линейное приведение частоты к 16 кГц.
            converted = array('h')
            for i in range(int(len(samples) * 16000 / source_rate)):
                pos = i * source_rate / 16000
                left = int(pos)
                right = min(left + 1, len(samples) - 1)
                converted.append(round(samples[left] + (samples[right] - samples[left]) * (pos - left)))
            grammar = ['михаил', 'михаил борисович', '[unk]'] if phrase.startswith('михаил') else main.GRAMMAR
            rec = Recognizer(model, 16000, json.dumps(grammar, ensure_ascii=False))
            rec.AcceptWaveform(converted.tobytes())
            result = json.loads(rec.FinalResult())['text']
            print(f'[TEST] {phrase} -> {result}', flush=True)
            if main.normalize(result) != main.normalize(phrase):
                failures.append((phrase, result))
    if failures:
        raise AssertionError(f'Ошибки распознавания: {failures}')


if __name__ == '__main__':
    import sys
    if '--recognition' in sys.argv:
        recognition()
    else:
        unittest.main()
