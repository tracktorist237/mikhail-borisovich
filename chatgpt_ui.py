"""Bounded AT-SPI helper; runs with system Python (GI), never reads browser storage."""
import json
import sys
import signal
import time
from urllib.parse import urlsplit

START = {'запустить голосовой режим', 'start voice mode', 'use voice', 'start voice', 'начать голосовой режим', 'использовать голосовой режим', 'голосовой режим'}
END = {'завершить голосовой режим', 'end voice chat', 'end voice mode', 'exit voice mode', 'end conversation', 'завершить разговор', 'завершить голосовой чат', 'выйти из голосового режима'}


def walk(node, depth=0):
    if depth > 24:
        return
    yield node
    for i in range(min(node.get_child_count(), 300)):
        yield from walk(node.get_child_at_index(i), depth + 1)


def buttons(atspi):
    desktop = atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if 'firefox' not in (app.get_name() or '').lower():
            continue
        for node in walk(app):
            if node.get_role() != atspi.Role.DOCUMENT_WEB:
                continue
            uri = node.get_document_iface().get_attribute_value('DocURL') or ''
            if urlsplit(uri).hostname != 'chatgpt.com' or urlsplit(uri).scheme != 'https':
                continue
            for child in walk(node):
                states = child.get_state_set()
                if child.get_role() == atspi.Role.PUSH_BUTTON and all(states.contains(s) for s in (atspi.StateType.SHOWING, atspi.StateType.ENABLED)):
                    yield child


def operate(action):
    import gi
    gi.require_version('Atspi', '2.0')
    from gi.repository import Atspi
    Atspi.set_timeout(1000, 1000)
    clicked = False
    until = time.monotonic() + 20
    while time.monotonic() < until:
        nodes = list(buttons(Atspi))
        ending = [n for n in nodes if (n.get_name() or '').strip().lower() in END]
        if action == 'open' and ending:
            return {'ok': True, 'detail': 'Voice: обнаружена кнопка завершения разговора.'}
        if action == 'close' and not ending and any((n.get_name() or '').strip().lower() in START for n in nodes):
            return {'ok': True, 'detail': 'Voice завершён, доступна кнопка запуска.'}
        targets = ending if action == 'close' else [n for n in nodes if (n.get_name() or '').strip().lower() in START]
        if len(targets) == 1 and not clicked:
            iface = targets[0].get_action_iface()
            for i in range(iface.get_n_actions()):
                if iface.get_action_name(i).lower() in ('click', 'press', 'activate'):
                    clicked = bool(iface.do_action(i))
                    break
        time.sleep(1)
    return {'ok': False, 'detail': 'Не удалось подтвердить Voice через accessibility. Нужна ручная проверка Firefox.'}


if __name__ == '__main__':
    def expired(*args):
        raise TimeoutError('AT-SPI timeout')
    signal.signal(signal.SIGALRM, expired)
    signal.alarm(24)
    try:
        result = operate(sys.argv[1])
    except Exception as exc:
        result = {'ok': False, 'detail': f'Accessibility недоступна: {type(exc).__name__}'}
    print(json.dumps(result, ensure_ascii=False))
