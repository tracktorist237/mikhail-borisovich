"""Opt-in Firefox DOM checks, local fixture only: MB_BROWSER_TEST=1 python -m unittest test_browser_dom."""
import ast
import os
from pathlib import Path
import tempfile
import time
import unittest
from urllib.parse import quote
from chatgpt_ui import ChatGPTUI, UIError
import main


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
