"""Serialized browser UI jobs; the local microphone loop never waits on Firefox."""
from concurrent.futures import ThreadPoolExecutor
from chatgpt_ui import ChatGPTUI
from speaker_activity import OutputGuard

LOCAL_MODE = 'LOCAL_MODE'
LARISA_MODE = 'LARISA_MODE'
ANTON_MODE = 'ANTON_MODE'
CHATGPT_MODE = LARISA_MODE  # compatibility name for v0.2 callers


class Bridge:
    def __init__(self, cfg=None):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='firefox-ui')
        self.ui = ChatGPTUI(cfg)

    def submit(self, action):
        return self.pool.submit(self.perform, action)

    def perform(self, action):
        try:
            if action == 'open':
                return self.ui.start_voice()
            if action == 'open_anton':
                self.ui.open()
                return {'ok': True, 'detail': 'Текстовый ChatGPT готов.'}
            functions = {'close': self.ui.close, 'pause': self.ui.pause,
                         'abort_dictation': self.ui.abort_dictation,
                         'resume': self.ui.resume_voice, 'dictate': self.ui.start_dictation,
                         'transcribe': self.ui.stop_dictation, 'send': self.ui.send,
                         'poll': self.ui.poll}
            return functions[action]()
        except Exception as exc:
            return {'ok': False, 'detail': f'{type(exc).__name__}: {exc}'}

    def close(self):
        # Enqueue shutdown after any in-flight bounded UI operation. Do not kill
        # the user's other Firefox. Wait so the process cannot outlive main.py.
        try:
            self.pool.submit(self.ui.close).result()
        finally:
            self.pool.shutdown(wait=True, cancel_futures=True)
