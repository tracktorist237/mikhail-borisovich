"""Opt-in Firefox DOM checks, local fixture only: MB_BROWSER_TEST=1 python -m unittest test_browser_dom."""
import ast
import os
from pathlib import Path
import tempfile
import time
import threading
import unittest
from urllib.parse import quote
from chatgpt_ui import ChatGPTUI, UIError
import main
from chatgpt_bridge import Bridge
from browser_worker import descendants
from browser_timing import job_timeout


class FixtureBrowserUI(ChatGPTUI):
    """Real browser lifetime through Bridge, using only a local blank page."""
    def open(self):
        from selenium import webdriver
        from selenium.webdriver.firefox.service import Service
        opts = webdriver.FirefoxOptions()
        opts.binary_location = self.cfg['firefox_binary']
        for arg in ('-profile', self.cfg['firefox_profile'], '-no-remote'):
            opts.add_argument(arg)
        self.service = Service(self.cfg['geckodriver'], popen_kw={'start_new_session': True})
        started = time.monotonic()
        print('[FIXTURE START] browser waiting', flush=True)
        self.driver = webdriver.Firefox(options=opts, service=self.service)
        print(f'[FIXTURE START] browser ready elapsed={time.monotonic()-started:.1f}s', flush=True)
        self.driver.get('about:blank')
        print(f'[FIXTURE START] local page ready elapsed={time.monotonic()-started:.1f}s', flush=True)
        if self.cfg.get('fixture_hang_quit'):
            # Hang the quit call itself with actual Firefox/geckodriver alive.
            # The independent owner must clean them without another UI call.
            self.driver.quit = threading.Event().wait

    def poll(self):
        return {'ok': True, 'detail': 'local fixture',
                'pid': self.driver.capabilities['moz:processID']}


@unittest.skipUnless(os.environ.get('MB_BROWSER_TEST') == '1', 'opt-in real Firefox fixture')
class BrowserLifecycleTests(unittest.TestCase):
    def test_bridge_closes_and_restarts_real_firefox(self):
        cfg = main.config()
        base = Path(cfg['firefox_profile']).expanduser().parent
        with tempfile.TemporaryDirectory(prefix='mb-lifetime-test-', dir=base) as profile:
            cfg['firefox_profile'] = profile
            bridge = Bridge(cfg, ui_factory='test_browser_dom:FixtureBrowserUI')
            try:
                for hang_quit in (False, True):
                    bridge.cfg['fixture_hang_quit'] = hang_quit
                    self.assertTrue(bridge.submit('open_anton').result(
                        timeout=job_timeout(cfg, 'open_anton') + bridge.budget + 1)['ok'])
                    pid = bridge.submit('poll').result(timeout=15)['pid']
                    self.assertTrue(Path(f'/proc/{pid}').exists())
                    self.assertIn(pid, descendants(bridge.process.pid))
                    result = bridge.close()
                    self.assertTrue(result['ok'])
                    if hang_quit:
                        self.assertTrue(result['forced'])
                    self.assertFalse(Path(f'/proc/{pid}').exists())
                    self.assertFalse(bridge.thread.is_alive())
            finally:
                bridge.close()


@unittest.skipUnless(os.environ.get('MB_BROWSER_TEST') == '1', 'opt-in real Firefox fixture')
class BrowserDOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from selenium import webdriver
        from selenium.webdriver.firefox.service import Service
        cfg=main.config()
        base=Path(cfg['firefox_profile']).expanduser().parent
        cls.tmp=tempfile.TemporaryDirectory(prefix='mb-dom-test-',dir=base)
        cls.ui=ChatGPTUI(cfg)
        opts=webdriver.FirefoxOptions()
        opts.enable_bidi=True
        opts.binary_location=cfg['firefox_binary']
        for arg in ('-profile',cls.tmp.name,'-no-remote'):opts.add_argument(arg)
        cls.ui.service=Service(cfg['geckodriver'],popen_kw={'start_new_session':True})
        try:
            cls.ui.driver=webdriver.Firefox(options=opts,service=cls.ui.service)
            cls.ui.driver.command_executor.client_config.timeout=15
            cls.ui.context=cls.ui.driver.current_window_handle
            cls.ui.script=cls.ui.driver.script
        except Exception:
            cls.ui.close();cls.tmp.cleanup();raise

    @classmethod
    def tearDownClass(cls):
        cls.ui.close();cls.tmp.cleanup()

    def test_os_space_uses_prepared_dom_focus(self):
        from firefox_key import space_to_firefox
        html = """<main><button id="start" onclick="this.outerHTML='<button aria-label=&quot;Stop dictation&quot; onclick=&quot;this.textContent=String(123)&quot;>Stop</button>'">Start</button>
            <button id="other" onclick="this.textContent='Wrong'">Other</button></main>"""
        self.ui.driver.get('data:text/html;charset=utf-8,'+quote(html))
        self.ui.driver.execute_script(self.ui.ARM_STOP_KEY)
        self.ui.driver.find_element('id','start').click()
        self.assertEqual(self.ui.driver.switch_to.active_element.get_dom_attribute('aria-label'),'Stop dictation')
        self.ui.cfg['dictation_stop_method']='os-keyboard'
        self.ui.stop_dictation()
        self.assertEqual(self.ui.driver.find_element('css selector','button[aria-label="Stop dictation"]').text,'123')
        self.ui.driver.execute_script(self.ui.ARM_STOP_KEY)
        self.ui.driver.execute_script("document.getElementById('other').focus()")
        self.ui.stop_dictation()
        self.assertEqual(self.ui.driver.find_element('id','other').text,'Other')
        with self.assertRaises(RuntimeError):
            space_to_firefox(0)

    def test_keyboard_stop_and_ambiguity(self):
        self.ui.cfg['dictation_stop_method']='keyboard'
        html = '<main><button aria-label="Stop dictation" onclick="this.textContent=\'Stopped\'">Stop</button></main>'
        self.ui.driver.get('data:text/html;charset=utf-8,'+quote(html))
        self.ui.stop_dictation()
        self.assertEqual(self.ui.driver.find_element('css selector','main button').text,'Stopped')
        self.ui.driver.get('data:text/html;charset=utf-8,'+quote(html.replace('</main>',
            '<button aria-label="Stop dictation">Duplicate</button></main>')))
        with self.assertRaises(UIError):
            self.ui.stop_dictation()

    def test_voice_ready_click_visible_unique_control(self):
        html = '''<main>
            <button aria-label="Start Voice" hidden onclick="this.textContent='wrong'">hidden</button>
            <button aria-label="Start Voice" disabled>disabled</button>
            <button id="voice" aria-label="Start Voice" onclick="this.textContent='started'">Voice</button>
            </main>'''
        self.ui.driver.get('data:text/html;charset=utf-8,' + quote(html))
        with self.ui.startup():
            self.ui.click_voice_when_ready()
        self.assertEqual(self.ui.driver.find_element('id', 'voice').text, 'started')
        html = html.replace('</main>', '<button aria-label="Start Voice">duplicate</button></main>')
        self.ui.driver.get('data:text/html;charset=utf-8,' + quote(html))
        with self.ui.startup(), self.assertRaises(UIError):
            self.ui.click_voice_when_ready()
        self.assertEqual(self.ui.driver.find_element('id', 'voice').text, 'Voice')

    def test_microphone_rendered_controls_one_click_and_ambiguity(self):
        html = """<main><button aria-label="End Voice">End</button>
            <button aria-label="Turn on microphone" hidden>hidden</button>
            <button aria-label="Turn off microphone" aria-disabled="true">disabled</button>
            <button id="mic" aria-label="Turn on microphone" data-clicks="0"
                onclick="this.dataset.clicks=String(+this.dataset.clicks+1); this.setAttribute('aria-label','Turn off microphone')">Mic</button>
            </main>"""
        self.ui.driver.get('data:text/html;charset=utf-8,' + quote(html))
        state = self.ui.voice_microphone_state()
        self.assertEqual((state['on_visible'], state['on_enabled'], state['off_enabled']), (1, 0, 1))
        with self.ui.startup():
            self.ui.wait_voice_microphone()
        mic = self.ui.driver.find_element('id', 'mic')
        self.assertEqual(mic.get_attribute('data-clicks'), '1')
        self.assertEqual(self.ui.voice_microphone_state()['on_enabled'], 1)
        self.ui.driver.execute_script("document.querySelector('main').insertAdjacentHTML('beforeend', '<button aria-label=\"Turn on microphone\">duplicate</button>')")
        with self.ui.startup(), self.assertRaisesRegex(UIError, 'ambiguous'):
            self.ui.wait_voice_microphone()
        self.assertEqual(mic.get_attribute('data-clicks'), '1')

    def test_embedded_javascript_compiles(self):
        tree=ast.parse(Path('chatgpt_ui.py').read_text())
        self.ui.driver.get('about:blank')
        self.ui.driver.execute_script('new Function(arguments[0]);',self.ui.STOP_SCRIPT)
        self.ui.driver.execute_script('new Function(arguments[0]);',self.ui.ARM_STOP_KEY)
        checked=2
        for node in ast.walk(tree):
            if (isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute)
                    and node.func.attr in ('execute_script','execute') and node.args
                    and isinstance(node.args[0],ast.Constant)):
                self.ui.driver.execute_script('new Function(arguments[0]);',node.args[0].value)
                checked+=1
        self.assertGreaterEqual(checked,2)

    def test_async_stop_activates_only_visible_control(self):
        html='''<button aria-label="Stop dictation" style="display:none"
            onclick="document.body.dataset.wrong='yes'">Hidden</button>
            <button aria-label="Stop dictation" onclick="document.body.dataset.stopped='yes'">Stop</button>'''
        for method in ('native','dom','bidi'):
            with self.subTest(method=method):
                self.ui.cfg['dictation_stop_method']=method
                self.ui.driver.get('data:text/html;charset=utf-8,'+quote(html))
                self.assertIsNone(self.ui.driver.execute_script('return document.body.dataset.stopped'))
                self.ui.stop_dictation()
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    if self.ui.driver.execute_script("return document.body.dataset.stopped")=='yes':break
                    time.sleep(.1)
                self.assertEqual(self.ui.driver.execute_script('return document.body.dataset.stopped'),'yes')
                self.assertIsNone(self.ui.driver.execute_script('return document.body.dataset.wrong'))



if __name__=='__main__':unittest.main()
