"""ChatGPT user interface only: Selenium, ordinary persistent Firefox.

No browser storage, private page state, network interception or ChatGPT APIs.
"""
from pathlib import Path
from contextlib import contextmanager
from functools import wraps
from browser_timing import job_timeout
from diagnostics import cleanup_log
import time
import json
import subprocess
from urllib.parse import urlsplit


class UIError(RuntimeError):
    pass


def startup_action(method):
    @wraps(method)
    def bounded(self, *args, **kwargs):
        with self.startup():
            return method(self, *args, **kwargs)
    return bounded


class ChatGPTUI:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.driver = None
        self.service = None
        self.before = 0
        self.last_text = ''
        self.stable_since = 0
        self._startup_deadline = None
        self._startup_transport = None

    @contextmanager
    def startup(self, deadline=None):
        if self._startup_deadline is not None:
            yield  # open() inside start_voice() shares the original deadline.
            return
        budget = job_timeout(self.cfg, 'open')
        hard_deadline = deadline if deadline is not None else time.monotonic() + budget
        self._startup_started = hard_deadline - budget
        # Reserve a small part of the SAME budget for delivering a bounded error.
        # A stuck constructor/transport is still killed by the independent owner.
        self._startup_deadline = hard_deadline - min(2, budget / 10)
        self._startup_stage = 'browser/composer'
        try:
            self._startup_remaining()
            self._bound_startup_transport()
            yield
        except Exception:
            self.startup_stage(self._startup_stage, 'failed')
            raise
        finally:
            if self._startup_transport is not None:
                driver, execute, timeout = self._startup_transport
                driver.execute = execute
                driver.command_executor.client_config.timeout = timeout
            self._startup_transport = None
            self._startup_deadline = None

    def _startup_remaining(self):
        remaining = self._startup_deadline - time.monotonic()
        if remaining <= 0:
            raise UIError(f'Startup budget exhausted; stage={self._startup_stage}')
        return remaining

    def _bound_startup_transport(self):
        if self.driver is None or self._startup_transport is not None:
            return
        driver = self.driver
        original = driver.execute
        timeout = driver.command_executor.client_config.timeout
        self._startup_transport = driver, original, timeout

        def execute(command, params=None):
            driver.command_executor.client_config.timeout = min(50, self._startup_remaining())
            result = original(command, params)
            self._startup_remaining()
            return result
        driver.execute = execute

    def startup_stage(self, stage, status='waiting'):
        self._startup_stage = stage
        cleanup_log(f'[GPT START] stage={stage} status={status} '
                    f'elapsed={time.monotonic() - self._startup_started:.1f}s '
                    f'timeout={self._startup_deadline - self._startup_started:.1f}s', flush=True)

    @startup_action
    def open(self):
        from selenium import webdriver
        from selenium.webdriver.firefox.service import Service
        from selenium.common.exceptions import TimeoutException
        if self.driver:
            return
        self.startup_stage('browser/composer')
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
        self._bound_startup_transport()
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
        self.startup_stage('browser/composer', 'ready')

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

    def wait(self, condition, description, timeout=30, *, deadline=None):
        from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException
        until = time.monotonic() + timeout
        if deadline is not None:
            until = min(until, deadline)
        if self._startup_deadline is not None:
            until = min(until, self._startup_deadline)
        while time.monotonic() < until:
            try:
                result = condition()
                if time.monotonic() >= until:
                    break
                if result:
                    if self._startup_deadline is not None:
                        self._startup_remaining()
                    return result
            except (NoSuchElementException, StaleElementReferenceException):
                pass
            time.sleep(max(0, min(.5, until - time.monotonic())))
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
        timeout = 3 if self._startup_deadline is None else min(3, self._startup_remaining())
        result = subprocess.run(['pw-dump'], capture_output=True, check=True, timeout=timeout)
        nodes = json.loads(result.stdout)
        return any(str(n.get('info', {}).get('props', {}).get('application.process.id')) == str(pid)
                   and n.get('info', {}).get('props', {}).get('media.class') == 'Stream/Input/Audio'
                   and n.get('info', {}).get('state') == 'running' for n in nodes)

    def click_voice_when_ready(self):
        names = ('Start Voice', 'Start voice', 'Начать голосовой режим')
        query = ','.join('[aria-label=' + json.dumps(name) + ']' for name in names)
        counts = {'visible': 0, 'enabled': 0}
        until = time.monotonic() + 30
        if self._startup_deadline is not None:
            until = min(until, self._startup_deadline)

        def attempt():
            # Stale selection/click is retried by wait(); no retry of an unknown
            # click outcome (e.g. transport timeout). Never click hidden controls.
            elements = self.driver.find_elements('css selector', query)
            visible = [e for e in elements if e.is_displayed()]
            enabled = [e for e in visible if e.is_enabled() and e.get_attribute('aria-label') in names]
            counts.update(visible=len(visible), enabled=len(enabled))
            if len(enabled) > 1:
                raise UIError('Ambiguous Start Voice control')
            if not enabled:
                return False
            self.startup_stage('Start Voice', 'ready')
            # find/visibility/attribute commands may finish after this stage's
            # deadline, even while the overall startup budget still has time.
            if time.monotonic() >= until:
                raise UIError('Start Voice local deadline expired before click')
            enabled[0].click()
            return True

        try:
            self.wait(attempt, 'Start Voice visible/enabled/unique', 30, deadline=until)
        except Exception:
            cleanup_log('[GPT START] stage=Start Voice '
                        f'visible={counts["visible"]} enabled={counts["enabled"]}', flush=True)
            raise

    def voice_microphone_state(self):
        # Voice-only rendered controls; no message/alert text or page state.
        return self.driver.execute_script('''
            const visible=e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
            const buttons=[...document.querySelectorAll('main button,main [role="button"]')].filter(visible);
            const enabled=e=>!e.disabled && e.getAttribute('aria-disabled')!=='true';
            const on=buttons.filter(e=>['Turn off microphone','Выключить микрофон'].includes(e.getAttribute('aria-label')));
            const off=buttons.filter(e=>['Turn on microphone','Включить микрофон'].includes(e.getAttribute('aria-label')));
            const onValid=on.filter(enabled), offValid=off.filter(enabled);
            return {on_visible:on.length, on_enabled:onValid.length,
                off_visible:off.length, off_enabled:offValid.length,
                turn_on:offValid.length===1 ? offValid[0] : null,
                dialog:[...document.querySelectorAll('[role="dialog"],dialog')].some(visible),
                alert:[...document.querySelectorAll('[role="alert"]')].some(visible)};
        ''')

    def wait_voice_microphone(self):
        from selenium.common.exceptions import (StaleElementReferenceException, WebDriverException,
                                                ElementClickInterceptedException, ElementNotInteractableException)
        until = time.monotonic() + 30
        if self._startup_deadline is not None:
            until = min(until, self._startup_deadline)
        state = dict(on_visible=0, on_enabled=0, off_visible=0, off_enabled=0, dialog=False, alert=False)
        attempted, click_result, reason = False, 'not_attempted', 'controls_missing'
        last_report = None

        def report(event, stage=None, status=None):
            nonlocal last_report
            stage = stage or ('microphone-active' if attempted else 'microphone-control')
            status = status or ('failed' if event == 'failed' else
                                'ready' if reason == 'on_confirmed' else 'waiting')
            self._startup_stage = stage
            # Log state changes, not unchanged polls. Never serialize the DOM
            # element or arbitrary values from the returned state dictionary.
            key = (event, stage, status, reason, attempted, click_result,
                   *(state[k] for k in ('on_visible', 'on_enabled', 'off_visible', 'off_enabled', 'dialog', 'alert')))
            if key == last_report:
                return
            last_report = key
            cleanup_log(f'[GPT MIC] stage={stage} status={status} event={event} state={reason} '
                f'mic_on={bool(state["on_enabled"])} mic_off={bool(state["off_enabled"])} '
                f'on_visible={state["on_visible"]} on_enabled={state["on_enabled"]} '
                f'off_visible={state["off_visible"]} off_enabled={state["off_enabled"]} '
                f'dialog={state["dialog"]} alert={state["alert"]} '
                f'click_attempted={attempted} click_result={click_result} '
                f'elapsed={time.monotonic()-self._startup_started:.1f}s '
                f'deadline={until-self._startup_started:.1f}s '
                f'remaining={max(0, until-time.monotonic()):.1f}s', flush=True)

        def observe():
            nonlocal state, attempted, click_result, reason
            try:
                state = self.voice_microphone_state()
            except StaleElementReferenceException:
                reason = 'control_stale'
                report('observed')
                return False
            if time.monotonic() >= until:
                reason = 'deadline'
                raise UIError('Voice microphone deadline')
            if state['on_enabled'] + state['off_enabled'] > 1:
                reason = 'ambiguous_controls'
                report('observed', status='ambiguous')
                raise UIError('Voice microphone ambiguous controls')
            if state['on_enabled'] == 1:
                reason = 'on_confirmed'
                if not attempted:
                    report('observed', 'microphone-control', 'mic_on')
                report('observed', 'microphone-active', 'ready')
                return True
            reason = ('off_after_click' if state['off_enabled'] else 'missing_after_click') if attempted else 'controls_missing'
            if not attempted and state['off_enabled'] == 1:
                reason = 'off_ready'
                report('observed', 'microphone-control', 'mic_off')
                report('before_click', 'microphone-click', 'attempted')
                if time.monotonic() >= until:
                    reason = 'deadline'
                    raise UIError('Voice microphone deadline before click')
                # Only one click request, even if its outcome is unknown/stale.
                # A detached read is retried above; after a click only observe.
                attempted = True
                try:
                    state['turn_on'].click()
                    click_result = 'returned'
                except StaleElementReferenceException:
                    click_result = 'stale'
                except WebDriverException as exc:
                    click_result = type(exc).__name__
                    reason = ('click_blocked' if isinstance(exc, (ElementClickInterceptedException,
                              ElementNotInteractableException)) else 'click_outcome_unknown')
                    raise UIError('Voice microphone click not confirmed') from None
                finally:
                    report('click_attempt', 'microphone-click',
                           'accepted' if click_result == 'returned' else 'outcome_unknown')
                    # Counts describe the pre-click observation. Never retry this request.
                return False
            report('observed')
            return False

        report('initial', 'microphone-control', 'waiting')
        try:
            self.wait(observe, 'Voice microphone mic_on', 30, deadline=until)
        except Exception as exc:
            report('failed')
            raise UIError(f'Voice microphone: {reason}; click={click_result}; error={type(exc).__name__}') from None

    @startup_action
    def start_voice(self):
        self.open()
        self.startup_stage('Start Voice')
        self.click_voice_when_ready()
        self.startup_stage('click accepted', 'ready')
        self.startup_stage('End Voice')
        self.wait(lambda: self.snapshot()['end_voice'], 'End Voice', 45)
        self.startup_stage('End Voice', 'ready')
        self.startup_stage('microphone')
        self.wait_voice_microphone()
        self.startup_stage('microphone', 'ready')
        self.startup_stage('PipeWire capture')
        self.wait(self.capture_ready, 'подключение Firefox к аудиовходу', 20)
        self.startup_stage('PipeWire capture', 'ready')
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
            # This is only the normal WebDriver path. The independent browser
            # supervisor owns escalation and verifies/reaps the entire tree,
            # including children created before the driver constructor returns.
            if self.service and self.service.process:
                self.service.stop()
            self.driver = None
            self.service = None
        if error:
            print(f'[GPT] WebDriver quit failed: {type(error).__name__}; supervisor must confirm cleanup.', flush=True)
        return {'ok': error is None, 'detail': 'WebDriver close finished; process ownership is checked by supervisor.'}
