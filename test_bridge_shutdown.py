"""Real subprocess shutdown tests. Run under timeout -k 5s 120s."""
from concurrent.futures import Future
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from browser_worker import descendants, process_identity
from chatgpt_bridge import Bridge, ANTON_MODE, LOCAL_MODE
import test_chatgpt as fixtures
import main


CHILD = '''import os,signal,json
signal.signal(signal.SIGTERM,signal.SIG_IGN)
r,w=os.pipe()
p=os.fork()
if p==0:
 os.close(r);os.setsid();os.write(w,b'R');os.close(w)
 while True:signal.pause()
os.close(w);assert os.read(r,1)==b'R';os.close(r)
print(json.dumps([os.getpid(),p]),flush=True)
while True:signal.pause()
'''


class FakeUI:
    """Runs in the real UI child; reports synchronization through a Unix pipe."""
    def __init__(self, cfg):
        self.mode = cfg['test_mode']
        self.events = socket.socket(socket.AF_UNIX)
        self.events.connect(cfg['test_events'])
        self.children = []
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if self.mode in ('constructor_hang', 'startup_crash', 'job_hang', 'quit_hang', 'send_hang'):
            self.child = subprocess.Popen([sys.executable, '-c', CHILD], stdout=subprocess.PIPE,
                                          start_new_session=True, text=True)
            self.children = json.loads(self.child.stdout.readline())
        self.notify('ready')
        if self.mode == 'constructor_hang':
            while True:
                signal.pause()
        if self.mode == 'startup_crash':
            os._exit(23)

    def notify(self, event):
        self.events.sendall(json.dumps({'event': event, 'worker': os.getpid(),
                                       'supervisor': os.getppid(), 'children': self.children}).encode() + b'\n')

    def poll(self):
        self.notify('entered')
        if self.mode == 'job_hang':
            while True:
                signal.pause()
        return {'ok': True, 'detail': 'fixture', 'answer': 'fixture reply'}

    def send(self):
        self.notify('send')
        while True:
            signal.pause()

    def close(self):
        self.notify('quit')
        if self.mode == 'quit_hang':
            while True:
                signal.pause()
        return {'ok': True, 'detail': 'fixture closed'}


class ShutdownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mb-shutdown-')
        self.addCleanup(self.tmp.cleanup)
        self.listener = socket.socket(socket.AF_UNIX)
        self.path = str(Path(self.tmp.name) / 'events')
        self.listener.bind(self.path)
        self.listener.listen(4)
        self.listener.settimeout(5)
        self.addCleanup(self.listener.close)
        self.owned = {}
        self.addCleanup(self.clean_owned)

    def clean_owned(self):
        for pid, identity in reversed(list(self.owned.items())):
            current = process_identity(pid)
            if current and current[1] == identity[1]:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def bridge(self, mode='normal', **cfg):
        bridge = Bridge({'browser_close_timeout': 3, 'test_mode': mode,
                         'test_events': self.path, **cfg}, ui_factory='test_bridge_shutdown:FakeUI')
        def cleanup():
            try:
                bridge.close()
            finally:
                if bridge.thread is not None and bridge.thread.is_alive():
                    self.clean_owned()
                    bridge.process.wait(timeout=3)
                    bridge.thread.join(3)
        self.addCleanup(cleanup)
        return bridge

    def connect(self):
        sock, _ = self.listener.accept()
        sock.settimeout(5)
        self.addCleanup(sock.close)
        reader = sock.makefile('rb')
        self.addCleanup(reader.close)
        return reader

    def event(self, reader, expected):
        record = json.loads(reader.readline())
        self.assertEqual(record['event'], expected)
        for pid in [record['supervisor'], record['worker'], *record['children']]:
            identity = process_identity(pid)
            if identity:
                self.owned[pid] = identity
        return record

    def gone(self, bridge):
        self.assertTrue(bridge.confirmed)
        self.assertFalse(bridge.thread.is_alive())
        self.assertIsNotNone(bridge.process.returncode)
        for pid, identity in self.owned.items():
            current = process_identity(pid)
            self.assertTrue(current is None or current[1] != identity[1], f'Owned PID remains: {pid}')

    def test_normal_close_and_repeat(self):
        bridge = self.bridge()
        job = bridge.submit('poll')
        reader = self.connect()
        self.event(reader, 'ready')
        self.event(reader, 'entered')
        self.assertTrue(job.result(timeout=5)['ok'])
        result = bridge.close()
        self.assertTrue(result['ok'])
        self.assertFalse(result['forced'])
        self.assertEqual(bridge.close(), result)
        self.gone(bridge)

    def test_hung_job_kills_and_reaps_owned_tree_only(self):
        control = subprocess.Popen([sys.executable, '-c', 'import signal;signal.pause()'])
        self.addCleanup(lambda: (control.kill(), control.wait(timeout=3)))
        bridge = self.bridge('job_hang')
        job = bridge.submit('poll')
        reader = self.connect()
        self.event(reader, 'ready')
        self.event(reader, 'entered')
        started = time.monotonic()
        # A Future must not pretend the real, still-running task was cancelled.
        self.assertTrue(job.running())
        self.assertFalse(job.cancel())
        closing = bridge.submit('close')
        self.assertFalse(closing.cancel())
        self.assertFalse(job.done())
        self.assertFalse(bridge.submit('open_anton').result()['ok'])
        self.assertTrue(bridge.close()['forced'])
        self.assertLess(time.monotonic() - started, bridge.budget + .25)
        self.assertTrue(closing.result()['ok'])
        self.assertTrue(job.result()['interrupted'])
        self.assertIsNone(control.poll())
        self.gone(bridge)

    def test_hung_quit_is_independently_terminated(self):
        bridge = self.bridge('quit_hang')
        job = bridge.submit('poll')
        reader = self.connect()
        self.event(reader, 'ready')
        self.event(reader, 'entered')
        self.assertTrue(job.result(timeout=5)['ok'])
        closing = bridge.submit('close')
        self.event(reader, 'quit')
        self.assertTrue(bridge.close()['forced'])
        self.assertTrue(closing.result()['ok'])
        self.gone(bridge)

    def test_constructor_hang_is_owned_before_driver_exists(self):
        bridge = self.bridge('constructor_hang')
        job = bridge.submit('poll')
        self.event(self.connect(), 'ready')
        self.assertTrue(bridge.close()['ok'])
        self.assertTrue(job.result()['interrupted'])
        self.gone(bridge)

    def test_startup_crash_reaps_orphaned_descendants(self):
        bridge = self.bridge('startup_crash')
        job = bridge.submit('poll')
        self.event(self.connect(), 'ready')
        self.assertFalse(job.result(timeout=5)['ok'])
        self.assertTrue(bridge.close()['ok'])
        self.gone(bridge)

    def test_send_outcome_unknown_and_never_repeated(self):
        bridge = self.bridge('send_hang')
        job = bridge.submit('send')
        reader = self.connect()
        self.event(reader, 'ready')
        self.event(reader, 'send')
        bridge.close()
        result = job.result()
        self.assertTrue(result['outcome_unknown'])
        self.assertIn('no automatic retry', result['detail'])
        self.assertEqual(bridge.sequence, 1)
        self.gone(bridge)

    def test_operation_timeout_does_not_depend_on_future(self):
        bridge = self.bridge('job_hang', gpt_job_timeout=.7)
        job = bridge.submit('poll')
        reader = self.connect()
        self.event(reader, 'ready')
        self.event(reader, 'entered')
        self.assertFalse(job.result(timeout=5)['ok'])
        bridge.close()
        self.gone(bridge)

    def test_new_session_and_late_old_result(self):
        bridge = self.bridge()
        first = bridge.submit('poll')
        reader = self.connect()
        self.event(reader, 'ready')
        self.event(reader, 'entered')
        self.assertTrue(first.result(timeout=5)['ok'])
        bridge.close()
        self.gone(bridge)
        old = bridge.generation
        bridge.cfg['test_mode'] = 'job_hang'
        second = bridge.submit('poll')
        reader = self.connect()
        self.event(reader, 'ready')
        self.event(reader, 'entered')
        bridge._deliver(old, {'id': bridge.sequence, 'result': {'ok': True, 'detail': 'late old reply'}})
        self.assertFalse(second.done())
        bridge.close()
        self.assertTrue(second.result()['interrupted'])
        self.gone(bridge)

    def test_application_exits_with_hung_worker(self):
        code = '''import json,sys
from chatgpt_bridge import Bridge
b=Bridge(json.loads(sys.argv[1]),ui_factory='test_bridge_shutdown:FakeUI')
job=b.submit('poll')
assert sys.stdin.readline().strip()=='close'
try:
 assert b.close()['ok']
 assert job.result()['interrupted']
 assert not b.thread.is_alive()
 print('APPLICATION EXIT CONFIRMED',flush=True)
finally:b.close()
'''
        cfg = {'browser_close_timeout': 3, 'test_mode': 'job_hang', 'test_events': self.path}
        app = subprocess.Popen([sys.executable, '-c', code, json.dumps(cfg)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, start_new_session=True)
        try:
            reader = self.connect()
            self.event(reader, 'ready')
            self.event(reader, 'entered')
            output, _ = app.communicate('close\n', timeout=8)
            self.assertEqual(app.returncode, 0, output)
            self.assertIn('APPLICATION EXIT CONFIRMED', output)
            for pid, identity in self.owned.items():
                current = process_identity(pid)
                self.assertTrue(current is None or current[1] != identity[1])
        finally:
            if app.poll() is None:
                self.clean_owned()
                app.kill()
            app.communicate(timeout=3)


class ModeShutdownTests(unittest.TestCase):
    def test_failed_cleanup_never_completes_live_job_or_allows_new_session(self):
        bridge = Bridge()
        pending = Future()
        bridge.pending = (1, 'send', pending, 0)
        bridge.confirmed = False
        bridge.generation = 1
        bridge._settle_closed(1, {'ok': False, 'detail': 'PID 123 remains in D state'})
        self.assertFalse(pending.done())
        self.assertFalse(bridge.submit('open_anton').result()['ok'])
        self.assertFalse(bridge.submit('close').result()['ok'])
        self.assertIsNone(bridge.thread)
        with self.assertRaisesRegex(RuntimeError, 'PID 123 remains'):
            bridge.close()

    def test_unconfirmed_close_keeps_gpt_out_of_local_mode(self):
        g = fixtures.ModeTests().make(ANTON_MODE)
        pending, closing = Future(), Future()
        g.phase, g.action, g.job = 'sending', 'send', pending
        g.bridge.submit.side_effect = lambda action: closing
        g.tick(0)
        g.tick(91)
        closing.set_result({'ok': False, 'detail': 'Owned PID 123 has not exited'})
        for now in (92, 100, 200):
            g.tick(now)
        self.assertEqual(g.phase, 'error_wait')
        self.assertNotEqual(g.mode, LOCAL_MODE)
        self.assertFalse(pending.done())
        g.bridge.submit.assert_called_once_with('close')

    def test_main_finally_cleans_audio_when_browser_close_raises(self):
        speech, guard, bridge = Mock(), Mock(), Mock()
        bridge.close.side_effect = RuntimeError('browser cleanup not confirmed')
        sd = Mock()
        sd.check_input_settings.side_effect = RuntimeError('test input unavailable')
        with patch('main.dependencies', return_value=(sd, Mock(), Mock())), \
                patch('main.load_model'), patch('main.Speech', return_value=speech), \
                patch('main.OutputGuard', return_value=guard), patch('main.Bridge', return_value=bridge):
            with self.assertRaisesRegex(RuntimeError, 'browser cleanup not confirmed'):
                main.listen(main.config())
        guard.close.assert_called_once()
        speech.close.assert_called_once()

    def test_timeout_closes_pending_send_without_dispatching_late_result(self):
        g = fixtures.ModeTests().make(ANTON_MODE)
        pending, closing = Future(), Future()
        g.phase, g.action, g.job = 'sending', 'send', pending
        g.bridge.submit.side_effect = lambda action: closing
        g.tick(0)
        g.tick(91)
        g.bridge.submit.assert_called_once_with('close')
        self.assertFalse(pending.done())
        self.assertIs(g.job, closing)
        self.assertNotEqual(g.mode, LOCAL_MODE)
        pending.set_result({'ok': True, 'detail': 'late sent'})
        g.tick(92)
        self.assertNotEqual(g.mode, LOCAL_MODE)
        closing.set_result({'ok': True, 'detail': 'confirmed'})
        for now in (93, 94, 95): g.tick(now)
        self.assertEqual((g.mode, g.phase), (LOCAL_MODE, 'idle'))
        g.bridge.submit.assert_called_once_with('close')


if __name__ == '__main__':
    unittest.main()
