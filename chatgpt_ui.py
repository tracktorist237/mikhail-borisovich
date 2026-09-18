"""ChatGPT user interface only: Selenium, ordinary persistent Firefox.

No browser storage, private page state, network interception or ChatGPT APIs.
"""
from pathlib import Path
import time
import os
import signal
import json
import subprocess
from urllib.parse import urlsplit


class UIError(RuntimeError):
    pass


class ChatGPTUI:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.driver = None
        self.service = None
        self.before = 0
        self.last_text = ''
        self.stable_since = 0

    def open(self):
        from selenium import webdriver
        from selenium.webdriver.firefox.service import Service
        from selenium.common.exceptions import TimeoutException
        if self.driver:
            return
        profile = Path(self.cfg.get('firefox_profile',
            '~/snap/firefox/common/mb-profile')).expanduser().resolve()
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        options = webdriver.FirefoxOptions()
        options.binary_location = self.cfg.get('firefox_binary', '/snap/firefox/current/usr/lib/firefox/firefox')
        for arg in ('-profile', str(profile), '-no-remote'):
            options.add_argument(arg)
        options.enable_bidi = self.cfg.get('dictation_stop_method') == 'bidi'
        options.page_load_strategy = 'eager'
        options.accept_insecure_certs = False
        self.service = Service(self.cfg.get('geckodriver', '/snap/bin/geckodriver'),
                               popen_kw={'start_new_session': True})
        self.driver = webdriver.Firefox(options=options, service=self.service)
        self.driver.command_executor.client_config.timeout = 50
        if options.enable_bidi:
            self.driver.command_executor.client_config.websocket_timeout = 15
            self.context = self.driver.current_window_handle
            self.script = self.driver.script
        self.driver.set_page_load_timeout(45)
        self.driver.set_script_timeout(10)
        try:
            self.driver.get('https://chatgpt.com/')
        except TimeoutException:
            pass  # A bounded wait below checks the hydrated, visible composer.
        self.wait(lambda: self.snapshot()['dictate'], 'интерактивная кнопка Start dictation', 120)

    def snapshot(self):
        if not self.driver:
            raise UIError('Firefox не запущен')
        url = urlsplit(self.driver.current_url)
        if url.scheme != 'https' or url.hostname != 'chatgpt.com':
            raise UIError('Ожидается обычная страница https://chatgpt.com/')
        # Read rendered UI only. One WebDriver command per poll keeps load low.
        return self.driver.execute_script('''
            const visible=e=>e && e.getClientRects().length &&
                getComputedStyle(e).visibility!=='hidden';
            const buttons=[...document.querySelectorAll('main button,main [role="button"]')].filter(visible);
            const has=(...names)=>buttons.some(e=>!e.disabled && names.includes(e.getAttribute('aria-label')));
            const editor=[...document.querySelectorAll('#prompt-textarea[contenteditable="true"]')].find(visible);
            const messages=[...document.querySelectorAll('[data-message-author-role="assistant"]')].filter(visible);
            const last=messages.at(-1);
            return {
                dictate:has('Start dictation','Начать диктовку'),
                voice:has('Start Voice','Start voice','Начать голосовой режим'),
                end_voice:has('End Voice','Завершить голосовой режим'),
                mic_on:has('Turn off microphone','Выключить микрофон'),
                mic_off:has('Turn on microphone','Включить микрофон'),
                recording:has('Stop dictation','Остановить диктовку'),
                cancel:has('Cancel dictation','Отменить диктовку'),
                send:buttons.some(e=>!e.disabled && e.getAttribute('data-testid')==='send-button'),
                generating:buttons.some(e=>e.getAttribute('data-testid')==='stop-button' ||
                    ['Stop generating','Остановить генерацию'].includes(e.getAttribute('aria-label'))),
                composer:editor ? editor.innerText.trim() : null,
                answers:messages.length, answer:last ? last.innerText.trim() : '',
                answer_done:!!last && !![...((last.closest('[data-testid^="conversation-turn-"]')||last.closest('article')||last.parentElement)
                    .querySelectorAll('button'))].find(e=>visible(e) &&
                    (e.getAttribute('data-testid')==='copy-turn-action-button' ||
                     ['Copy response','Copy','Копировать'].includes(e.getAttribute('aria-label')))),
                alerts:[...document.querySelectorAll('main [role="alert"],[role="dialog"]')]
                    .filter(visible).map(e=>e.innerText).join(' ').slice(0,500)
            };
        ''')

    def wait(self, condition, description, timeout=30):
        from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            try:
                result = condition()
                if result:
                    return result
            except (NoSuchElementException, StaleElementReferenceException):
                pass
            time.sleep(.5)
        raise UIError(f'Не подтверждено: {description} (таймаут {timeout} с). Проверьте видимый UI Firefox.')

    def click(self, *names, selector=None):
        from selenium.common.exceptions import StaleElementReferenceException
        for _ in range(3):
            try:
                query = selector or ','.join('[aria-label=' + json.dumps(name) + ']' for name in names)
                elements = self.driver.find_elements('css selector', query)
                matches = [e for e in elements if e.is_displayed() and e.is_enabled()
                           and (selector or e.get_attribute('aria-label') in names or e.accessible_name in names)]
                if len(matches) != 1:
                    raise UIError(f'Ожидался один доступный элемент {names or selector}; найдено {len(matches)}')
                matches[0].click()
                return
            except StaleElementReferenceException:
                continue
        raise UIError('Элемент изменился во время нажатия')

    def capture_ready(self):
        # OS audio graph only, no browser storage or page-internal state.
        # The UI can show Stop dictation before getUserMedia is connected.
        pid = self.driver.capabilities.get('moz:processID')
        result = subprocess.run(['pw-dump'], capture_output=True, check=True, timeout=3)
        nodes = json.loads(result.stdout)
        return any(str(n.get('info', {}).get('props', {}).get('application.process.id')) == str(pid)
                   and n.get('info', {}).get('props', {}).get('media.class') == 'Stream/Input/Audio'
                   and n.get('info', {}).get('state') == 'running' for n in nodes)

    def start_voice(self):
        self.open()
        self.click('Start Voice', 'Start voice', 'Начать голосовой режим')
        self.wait(lambda: self.snapshot()['end_voice'], 'End Voice', 45)
        snap = self.snapshot()
        if snap['mic_off']:
            self.click('Turn on microphone', 'Включить микрофон')
        self.wait(lambda: self.snapshot()['mic_on'],
                  'активный микрофон Voice; при запросе разрешите его вручную', 30)
        self.wait(self.capture_ready, 'подключение Firefox к аудиовходу', 20)
        # UI + OS capture confirmation, not proof of a spoken server response.
        return {'ok': True, 'detail': 'Voice: End Voice и Turn off microphone доступны.'}

    def pause(self):
        s = self.snapshot()
        if s['end_voice'] and s['mic_on']:
            self.click('Turn off microphone', 'Выключить микрофон')
            self.wait(lambda: self.snapshot()['mic_off'], 'выключение микрофона Voice')
        if s['cancel']:
            self.click('Cancel dictation', 'Отменить диктовку')
            self.wait(lambda: self.snapshot()['dictate'], 'отмена диктовки')
        return {'ok': True, 'detail': 'Ввод GPT приостановлен.'}

    def abort_dictation(self):
        """Stop capture without sending, wait for transcription, then clear it."""
        method = self.cfg.get('dictation_stop_method', 'os-keyboard')
        if method == 'os-keyboard':
            # ARM_STOP_KEY was installed before capture. Do not send any
            # WebDriver command while Firefox is recording: that is the state
            # in which Marionette/BiDi is known to stop responding.
            self.stop_dictation()
            self.wait(lambda: not self.snapshot()['recording'],
                      'завершение диктовки перед возвратом', 30)
            self.wait(lambda: self.snapshot()['composer'] is not None,
                      'восстановление composer после диктовки', 30)
            state = self.snapshot()
        else:
            state = self.snapshot()
            if state['recording']:
                raise UIError('Отмена диктовки требует os-keyboard Stop')
        if state['composer']:
            editor = self.driver.find_element('css selector',
                                               '#prompt-textarea[contenteditable="true"]')
            from selenium.webdriver.common.keys import Keys
            editor.send_keys(Keys.CONTROL, 'a')
            editor.send_keys(Keys.BACKSPACE)
            self.wait(lambda: not self.snapshot()['composer'],
                      'очистка composer', 15)
        return {'ok': True, 'detail': 'Диктовка отменена, текст не отправлен.'}

    def resume_voice(self):
        s = self.snapshot()
        if s['mic_off']:
            self.click('Turn on microphone', 'Включить микрофон')
        self.wait(lambda: self.snapshot()['mic_on'], 'микрофон Voice')
        return {'ok': True, 'detail': 'Voice продолжен.'}

    def start_dictation(self):
        s = self.snapshot()
        if s['end_voice'] or s['composer'] is None or s['composer']:
            raise UIError('Диктовка требует пустой composer и выключенный Voice')
        if self.cfg.get('dictation_stop_method') == 'os-keyboard':
            self.driver.execute_script(self.ARM_STOP_KEY)
        self.click('Start dictation', 'Начать диктовку')
        self.wait(lambda: self.snapshot()['recording'], 'Stop dictation', 15)
        self.wait(self.capture_ready, 'аудиовход диктовки', 20)
        return {'ok': True, 'detail': 'Диктовка запущена.'}

    def stop_dictation(self):
        # Keep failed alternative transports opt-in for reproducible diagnostics.
        # None is currently a verified fix for ChatGPT dictation on this laptop.
        method = self.cfg.get('dictation_stop_method', 'native')
        if method == 'native':
            self.click('Stop dictation', 'Остановить диктовку')
        elif method == 'os-keyboard':
            from firefox_key import space_to_firefox
            space_to_firefox(self.driver.capabilities['moz:processID'])
        elif method == 'keyboard':
            from selenium.webdriver.common.keys import Keys
            elements = self.driver.find_elements('css selector',
                'main button[aria-label="Stop dictation"]:not([disabled]),'
                'main button[aria-label="Остановить диктовку"]:not([disabled])')
            if len(elements) != 1:
                raise UIError(f'Expected one Stop dictation button; found {len(elements)}')
            # Native WebDriver keyboard interaction checks interactability itself.
            # Avoid Selenium's injected is_displayed/get_attribute JavaScript atoms.
            elements[0].send_keys(Keys.SPACE)
        elif method == 'bidi':
            self.script.execute(self.STOP_SCRIPT, context_id=self.context)
        elif method == 'dom':
            self.driver.execute_script('return (' + self.STOP_SCRIPT + ')();')
        else:
            raise UIError('Неизвестный dictation_stop_method')
        return {'ok': True, 'detail': 'Ожидаю транскрипцию.'}

    # Install before capture starts; no WebDriver command is needed to press Stop.
    # The native Space event activates only the preselected, visible DOM button.
    ARM_STOP_KEY = """
        const selector='main button[aria-label="Stop dictation"]:not([disabled]),main button[aria-label="Остановить диктовку"]:not([disabled])';
        const candidates=()=>[...document.querySelectorAll(selector)]
            .filter(e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden');
        const observer=new MutationObserver(()=>{
            const nodes=candidates();
            if(nodes.length===1){nodes[0].focus();observer.disconnect();}
        });
        const cleanup=()=>{observer.disconnect();document.removeEventListener('keydown',guard,true);};
        const guard=e=>{
            if(e.code!=='Space')return;
            const nodes=candidates();
            if(nodes.length!==1 || document.activeElement!==nodes[0]){
                e.preventDefault();e.stopImmediatePropagation();
            }
            cleanup();clearTimeout(timer);
        };
        observer.observe(document.querySelector('main'),{childList:true,subtree:true,
            attributes:true,attributeFilter:['aria-label','disabled']});
        document.addEventListener('keydown',guard,true);
        const timer=setTimeout(cleanup,120000);
    """

    STOP_SCRIPT = '''() => {
        const nodes=[...document.querySelectorAll('button[aria-label="Stop dictation"],button[aria-label="Остановить диктовку"]')]
            .filter(e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden' && !e.disabled);
        if(nodes.length!==1) throw Error('Stop dictation is not uniquely available');
        nodes[0].click();
        return true;
    }'''

    def send(self):
        s = self.snapshot()
        if s['recording'] or not s['composer'] or not s['send'] or s['end_voice']:
            raise UIError('Транскрипция и Send не готовы')
        self.before = s['answers']
        self.last_text = ''
        self.stable_since = time.monotonic()
        self.click(selector='button[data-testid="send-button"]')
        return {'ok': True, 'detail': 'Текст отправлен через Send.'}

    def poll(self):
        s = self.snapshot()
        now = time.monotonic()
        if s['answer'] != self.last_text:
            self.last_text = s['answer']
            self.stable_since = now
        s['reply_ready'] = (s['answers'] > self.before and bool(s['answer'])
                            and s['answer_done'] and not s['generating']
                            and now - self.stable_since >= 1)
        return {'ok': True, 'detail': '', **s}

    def close(self):
        error = None
        try:
            if self.driver:
                self.driver.command_executor.client_config.timeout = 10
                self.driver.quit()
        except Exception as exc:
            error = exc
        finally:
            # geckodriver has its own process group, created by this instance.
            # Clean up hung owned Firefox children even if WebDriver timed out.
            if self.service and self.service.process:
                group = self.service.process.pid
                try:
                    os.killpg(group, signal.SIGTERM)
                    until = time.monotonic() + 3
                    while time.monotonic() < until:
                        try:
                            os.killpg(group, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(.1)
                    else:
                        os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.service.stop()
            self.driver = None
            self.service = None
        if error:
            print(f'[GPT] WebDriver quit: {type(error).__name__}; owned process group cleaned.', flush=True)
        return {'ok': True, 'detail': 'Firefox GPT-сеанса завершён.'}
