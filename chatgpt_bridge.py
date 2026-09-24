"""Asynchronous UI jobs with independent, bounded browser process shutdown."""
from diagnostics import cleanup_log
from concurrent.futures import Future
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from browser_worker import Channel
from browser_timing import STARTUP_ACTIONS, job_timeout
from speaker_activity import OutputGuard

LOCAL_MODE = 'LOCAL_MODE'
LARISA_MODE = 'LARISA_MODE'
ANTON_MODE = 'ANTON_MODE'
CHATGPT_MODE = LARISA_MODE  # compatibility name for v0.2 callers


class Bridge:
    def __init__(self, cfg=None, *, ui_factory='chatgpt_ui:ChatGPTUI'):
        self.cfg = dict(cfg or {})
        job_timeout(self.cfg, 'open')  # Validate before starting a controller.
        self.budget = self.cfg.get('browser_close_timeout', 12)
        if not .5 <= self.budget <= 60:
            raise ValueError('browser_close_timeout must be between .5 and 60 seconds')
        self.factory = ui_factory
        self.lock = threading.Lock()
        self.thread = None
        self.process = None
        self.pending = None
        self.close_future = None
        self.close_deadline = None
        self.confirmed = True
        self.generation = 0
        self.sequence = 0

    @staticmethod
    def _finished(result):
        future = Future()
        future.set_result(result)
        return future

    def _close_locked(self):
        if self.close_future is None:
            if self.pending is not None and self.pending[1] == 'send':
                cleanup_log('[GPT ERROR] Send outcome is unknown; no automatic retry.', flush=True)
            self.close_future = Future()
            self.close_future.set_running_or_notify_cancel()
            self.close_deadline = time.monotonic() + self.budget
            if self.confirmed:
                self.close_future.set_result({'ok': True, 'detail': 'Browser session already closed.'})
        return self.close_future

    def submit(self, action):
        with self.lock:
            if action == 'close':
                return self._close_locked()
            if self.close_future is not None and not self.confirmed:
                return self._finished({'ok': False, 'detail': 'Previous browser cleanup is not confirmed.'})
            if self.pending is not None:
                return self._finished({'ok': False, 'detail': 'Browser operation already pending; not repeated.'})
            if self.thread is None or not self.thread.is_alive():
                if not self.confirmed:
                    return self._finished({'ok': False, 'detail': 'Browser ownership unresolved; launch blocked.'})
                self.close_future = self.close_deadline = None
                self.confirmed = False
                self.generation += 1
                self.thread = threading.Thread(target=self._manage, args=(self.generation,),
                                               name='browser-controller', daemon=True)
                self.thread.start()
            elif self.close_future is not None:
                return self._finished({'ok': False, 'detail': 'Browser controller is still exiting.'})
            self.sequence += 1
            future = Future()
            future.set_running_or_notify_cancel()
            self.pending = (self.sequence, action, future, time.monotonic())
            return future

    def _deliver(self, generation, message):
        with self.lock:
            # Late results can neither complete a new job nor trigger Send.
            if generation != self.generation or self.close_future is not None:
                return
            if self.pending is not None and message['id'] == self.pending[0]:
                future = self.pending[2]
                self.pending = None
                future.set_result(message['result'])

    def _settle_closed(self, generation, result):
        with self.lock:
            if generation != self.generation:
                return
            self.confirmed = result['ok']
            if self.confirmed and self.pending is not None:
                _, action, future, _ = self.pending
                self.pending = None
                future.set_result({'ok': False, 'interrupted': True,
                                   'outcome_unknown': action == 'send',
                                   'detail': ('Send outcome is unknown; no automatic retry.' if action == 'send'
                                              else 'Browser operation interrupted; owned worker has exited.')})
            if self.close_future is None:
                self.close_future = Future()
                self.close_future.set_running_or_notify_cancel()
            if self.close_deadline is None:
                self.close_deadline = time.monotonic() + self.budget
            if not self.close_future.done():
                self.close_future.set_result(result)

    def _manage(self, generation):
        parent, child = socket.socketpair()
        channel = Channel(parent)
        process = None
        sent = None
        close_sent = False
        try:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name('browser_worker.py')), str(child.fileno())],
                pass_fds=(child.fileno(),), start_new_session=True)
            self.process = process
            child.close()
            channel.send({'cfg': self.cfg, 'factory': self.factory})
            while True:
                with self.lock:
                    pending = self.pending
                    if (pending is not None and self.close_future is None and
                            time.monotonic() - pending[3] >= job_timeout(self.cfg, pending[1])):
                        cleanup_log('[GPT ERROR] UI operation timed out; independent shutdown requested.', flush=True)
                        self._close_locked()
                    closing, deadline = self.close_future, self.close_deadline
                if closing is not None and not close_sent:
                    channel.send({'type': 'close', 'deadline': deadline})
                    close_sent = True
                elif closing is None and pending is not None and pending[0] != sent:
                    request = {'type': 'job', 'id': pending[0], 'action': pending[1]}
                    if pending[1] in STARTUP_ACTIONS:
                        request['startup_deadline'] = pending[3] + job_timeout(self.cfg, pending[1])
                    channel.send(request)
                    sent = pending[0]
                for message in channel.pump(.02):
                    if message['type'] == 'result':
                        self._deliver(generation, message)
                    elif message['type'] == 'closing':
                        with self.lock:
                            if self.close_future is None:
                                self.close_future = Future()
                                self.close_future.set_running_or_notify_cancel()
                                self.close_deadline = message['deadline']
                        close_sent = True
                    elif message['type'] == 'closed':
                        result = {key: message[key] for key in ('ok', 'detail', 'forced') if key in message}
                        if result['ok']:
                            remaining = max(0, self.close_deadline - time.monotonic())
                            try:
                                status = process.wait(timeout=remaining)
                                if status:
                                    result = {'ok': False, 'detail': f'Browser supervisor exit status {status}.'}
                            except subprocess.TimeoutExpired:
                                result = {'ok': False, 'detail': 'Browser supervisor exit not confirmed within budget.'}
                        self._settle_closed(generation, result)
                        if result['ok']:
                            return
                if channel.eof or process.poll() is not None:
                    self._settle_closed(generation, {'ok': False,
                        'detail': f'Browser supervisor PID {process.pid} exited without confirming descendant cleanup.'})
                    return
                with self.lock:
                    deadline = self.close_deadline
                    closing = self.close_future
                if deadline is not None and time.monotonic() >= deadline and not closing.done():
                    self._settle_closed(generation, {'ok': False,
                        'detail': 'Browser cleanup deadline exceeded; ownership remains unresolved.'})
                # On an uninterruptible kernel task, keep the reaper and leave
                # the job Future pending; failed close never masquerades as exit.
        except Exception as exc:
            self._settle_closed(generation, {'ok': False,
                'detail': f'Browser controller failure: {type(exc).__name__}; cleanup unconfirmed.'})
        finally:
            child.close()
            parent.close()
            if process is not None and process.poll() is not None:
                process.wait()

    def close(self):
        """Final cleanup only; GPTModes uses submit('close') without blocking."""
        future = self.submit('close')
        deadline = self.close_deadline
        try:
            result = future.result(timeout=max(0, deadline - time.monotonic()))
        except TimeoutError:
            result = {'ok': False, 'detail': 'Browser shutdown exceeded its total budget.'}
        if result['ok'] and self.thread is not None:
            self.thread.join(timeout=max(0, deadline - time.monotonic()))
            if self.thread.is_alive():
                result = {'ok': False, 'detail': 'Browser controller thread exit not confirmed.'}
        if not result['ok']:
            raise RuntimeError(result['detail'])
        return result
