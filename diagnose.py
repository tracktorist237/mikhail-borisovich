#!/usr/bin/env python3
"""Диагностика без изменения настроек звука."""
import argparse
from array import array
import sys
import time
from main import config, dependencies, internet, load_model, log, run, shutil, Speech, voices


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--voices', action='store_true', help='Список голосов RHVoice')
    parser.add_argument('--devices', action='store_true', help='Список устройств записи')
    args = parser.parse_args()
    if args.voices:
        for v in voices():
            print(f"{v.get('name')}: {v.get('language')}, {v.get('gender')}")
        return 0
    cfg = config()
    _, Model, _ = dependencies(audio=False)
    if args.devices:
        sd, _, _ = dependencies()
        print(sd.query_devices())
        return 0
    failures = []
    def check(name, action):
        try:
            action()
            print(f'[OK] {name}', flush=True)
        except Exception as exc:
            failures.append(name)
            log('ERROR', f'{name}: {exc}')
    def microphone():
        sd, _, _ = dependencies()
        with sd.RawInputStream(device=cfg['input_device'], channels=1, samplerate=cfg['sample_rate'], dtype='int16') as stream:
            data, overflow = stream.read(cfg['sample_rate'] * 2)
            peak = max(abs(x) for x in array('h', bytes(data)))
            print(f'Микрофон: пик {peak}/32768; переполнение: {overflow}', flush=True)
            if peak >= 32760:
                print('[INFO] Микрофон достигает предела сигнала: возможна перегрузка. При плохом распознавании уменьшите усиление входа.')
            if peak == 0:
                raise RuntimeError('Получена только цифровая тишина. Проверьте mute и выбор микрофона.')
    def output():
        if shutil.which('wpctl'):
            print(run(['wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@']).stdout.decode().strip())
        elif shutil.which('pactl'):
            run(['pactl', 'info'])
        elif not shutil.which('aplay'):
            raise RuntimeError('Нет доступного аудиоплеера.')
    def speech():
        obj = Speech(cfg)
        try:
            obj.say('Проверка звука. Михаил Борисович готов к работе.', remember=False)
            while obj.busy():
                time.sleep(0.05)
        finally:
            obj.close()
    check('Микрофон: реальная запись двух секунд', microphone)
    check('Аудиовыход', output)
    check('RHVoice: синтез и воспроизведение', speech)
    check('Модель Vosk: загрузка', lambda: load_model(cfg, Model))
    if internet():
        print('[OK] Интернет: TCP соединение установлено')
    else:
        print('[INFO] Интернет недоступен или проверка блокируется; локальная работа возможна.')
    print('Диагностика завершена. Слышимость фразы и качество распознавания проверяются человеком.')
    return 1 if failures else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        log('ERROR', str(exc))
        sys.exit(1)
