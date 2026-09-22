"""Real terminal SIGINT/output loss; run with external timeout, no audio/browser account."""
import ctypes
import fcntl
import json
import io
import os
from pathlib import Path
import pty
import select
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import termios
import time
import traceback
import threading
import unittest
from unittest.mock import Mock, patch

from browser_worker import process_identity, perform
from chatgpt_bridge import Bridge
import main


def fixture_application(cfg, control_fd, status_fd):
    """Run main.main/listen with only hardware/UI replaced; real Bridge/reaper."""
    signal.signal(signal.SIGINT, signal.default_int_handler)
    events = []
    holder = []

    class TestBridge(Bridge):
        def __init__(self, config):
            super().__init__({**config, **cfg}, ui_factory='test_bridge_shutdown:FakeUI')
            holder.append(self)
            self.submit('send')  # Hung, unknown send must never be retried.

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            events.append('stream exit')

        @property
        def active(self):
            os.write(status_fd, b'R')
            command = os.read(control_fd, 1)
            if command == b'E':
                os._exit(17)  # IPC EOF without calling any cleanup method.
            if command == b'B':
                from diagnostics import cleanup_log
                thread = threading.Thread(target=lambda: cleanup_log('fixture diagnostic', file=sys.stderr, flush=True))
                thread.start(); thread.join(timeout=2)
                assert not thread.is_alive()
                return True
            if command == b'P':
                main.log('TEST', 'output channel probe')
                raise RuntimeError('Lost output did not stop application')
            raise RuntimeError('Unexpected control EOF')

    sd = Mock()
    sd.RawInputStream.return_value = Stream()
    speech, guard = Mock(), Mock()
    speech.close.side_effect = lambda: events.append('speech close')
    guard.close.side_effect = lambda: events.append('guard close')
    with patch('main.dependencies', return_value=(sd, Mock(), Mock())), \
            patch('main.load_model'), patch('main.Speech', return_value=speech), \
            patch('main.OutputGuard', return_value=guard), patch('main.Bridge', TestBridge), \
            patch.object(sys, 'argv', ['main.py']):
        try:
            result = main.main()
        except BaseException as exc:
            # Out-of-band fixture evidence survives a closed stdout/stderr.
            events.append({'error': type(exc).__name__, 'frames':
                           [(f.filename, f.lineno, f.name) for f in traceback.extract_tb(exc.__traceback__)]})
            result = 99
    bridge = holder[0]
    os.write(status_fd, (json.dumps({'exit': result, 'events': events,
                'app_pid': os.getpid(), 'confirmed': bridge.confirmed, 'thread_alive': bridge.thread.is_alive(),
                'supervisor_exit': bridge.process.poll(), 'sequence': bridge.sequence})+'\n').encode())
    return result


class TerminalShutdownTests(unittest.TestCase):
    def run_case(self, mode):
        # Adopt failed-fixture orphans so even the BEFORE-fix failures are reaped.
        libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(previous), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
        owned = {}
        app = control = None
        fds = []
        with tempfile.TemporaryDirectory(prefix='mb-sigint-') as tmp:
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(tmp+'/events'); listener.listen(1); listener.settimeout(8)
            reader = conn = None
            output = None
            try:
                cr, cw = os.pipe(); sr, sw = os.pipe()
                master, slave = pty.openpty()
                fds += [cr,cw,sr,sw,master,slave]
                cfg = {'browser_close_timeout': 3, 'test_mode': 'send_hang', 'test_events':tmp+'/events'}
                cmd = shlex.join([sys.executable, '-u', str(Path(__file__).resolve()),
                                  '--fixture', json.dumps(cfg), str(cr), str(sw)])
                if mode == 'file': cmd += ' > '+shlex.quote(tmp+'/output')+' 2>&1'
                if mode in ('tee','tee-i'):
                    cmd += ' 2>&1 | tee '+('-i ' if mode == 'tee-i' else '')+shlex.quote(tmp+'/output')
                stdout = slave
                if mode in ('lost-output', 'parent-eof', 'background-output'):
                    out_r,out_w = os.pipe(); fds += [out_r,out_w]; stdout=out_w
                def terminal():
                    os.setsid()
                    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
                    os.tcsetpgrp(0, os.getpgrp())
                control = subprocess.Popen([sys.executable,'-c','import signal;signal.pause()'],start_new_session=True)
                app = subprocess.Popen(['bash','-c',cmd],stdin=slave,stdout=stdout,stderr=stdout,
                                       pass_fds=(cr,sw),preexec_fn=terminal)
                os.close(cr); fds.remove(cr); os.close(sw); fds.remove(sw)
                conn,_=listener.accept();conn.settimeout(8);reader=conn.makefile('rb')
                for expected in ('ready','send'):
                    record=json.loads(reader.readline());self.assertEqual(record['event'],expected)
                    for pid in [record['supervisor'],record['worker'],*record['children']]:
                        owned[pid]=process_identity(pid)
                self.assertTrue(select.select([sr],[],[],8)[0], 'main did not reach audio loop')
                self.assertEqual(os.read(sr,1),b'R')
                self.assertEqual(os.tcgetpgrp(master), app.pid, 'not foreground terminal group')
                self.assertEqual(os.getsid(record['supervisor']), record['supervisor'])
                self.assertNotEqual(os.getpgid(record['worker']), app.pid)
                stopped = time.monotonic()
                if mode in ('lost-output', 'parent-eof', 'background-output'):
                    os.close(out_r);fds.remove(out_r)
                    os.close(out_w);fds.remove(out_w)
                    os.write(cw, {'parent-eof': b'E', 'background-output': b'B'}.get(mode, b'P'))
                else:
                    os.write(master,b'\x03')  # Terminal driver sends SIGINT to foreground group.
                app.wait(timeout=8)
                if mode == 'parent-eof':
                    supervisor = record['supervisor']
                    fd = os.pidfd_open(supervisor)
                    try:
                        self.assertTrue(select.select([fd],[],[],6)[0], 'EOF cleanup timed out')
                    finally:
                        os.close(fd)
                    _, status = os.waitpid(supervisor, 0)
                    self.assertEqual(os.waitstatus_to_exitcode(status), 0)
                    self.assertEqual(app.returncode, 17)
                    for pid in owned:
                        self.assertIsNone(process_identity(pid), f'EOF left PID {pid}')
                    self.assertIsNone(control.poll())
                    return
                self.assertTrue(select.select([sr],[],[],2)[0], 'no application exit result')
                report=os.read(sr,65536)
                # Capture locally useful terminal output even on assertions below.
                chunks=[]
                while select.select([master],[],[],0)[0]:
                    try:chunks.append(os.read(master,65536))
                    except OSError:break
                if Path(tmp+'/output').exists(): chunks.append(Path(tmp+'/output').read_bytes())
                print(f'CASE {mode}: returncode={app.returncode}; status={report!r}; output='+b''.join(chunks).decode(errors='replace'),flush=True)
                print('OWNED AFTER APPLICATION EXIT: '+str({p:process_identity(p) for p in owned}),flush=True)
                self.assertTrue(report, 'application exited before cleanup report')
                result=json.loads(report)
                self.assertIsNone(process_identity(result['app_pid']), 'application remains alive')
                self.assertTrue(result['confirmed'])
                self.assertFalse(result['thread_alive'])
                self.assertEqual(result['supervisor_exit'],0)
                self.assertEqual(result['events'],['stream exit','guard close','speech close'])
                self.assertEqual(result['sequence'],1, 'Send was repeated')
                self.assertEqual(result['exit'],1 if mode in ('lost-output', 'background-output') else 0)
                self.assertLess(time.monotonic()-stopped, cfg['browser_close_timeout']+1.5)
                for pid,identity in owned.items():
                    current=process_identity(pid)
                    self.assertTrue(current is None or current[1]!=identity[1],f'Owned process remains: {pid} {current}')
                self.assertIsNone(control.poll(), 'foreign control process killed')
            finally:
                # Exact identities only; never kill a Firefox/process by name.
                for pid,identity in reversed(list(owned.items())):
                    current=process_identity(pid)
                    if current and identity and current[1]==identity[1]:
                        try:os.kill(pid,signal.SIGKILL)
                        except ProcessLookupError:pass
                if app:
                    if app.poll() is None:os.killpg(app.pid,signal.SIGKILL)
                    app.wait(timeout=3)
                end=time.monotonic()+3
                while owned and time.monotonic()<end:
                    for pid in list(owned):
                        try:done,_=os.waitpid(pid,os.WNOHANG)
                        except ChildProcessError:done=not process_identity(pid)
                        if done:owned.pop(pid)
                    if owned:select.select([],[],[],.02)
                if control:control.kill();control.wait(timeout=3)
                if reader:reader.close()
                if conn:conn.close()
                listener.close()
                for fd in fds:os.close(fd)
                libc.prctl(36,previous.value,0,0,0)
                self.assertFalse(owned, f'Fixture cleanup left processes: {owned}')

    def test_terminal_sigint(self):
        for mode in ('direct','file','tee','tee-i'):
            with self.subTest(mode=mode): self.run_case(mode)

    def test_parent_ipc_eof_with_lost_stdout(self):
        self.run_case('parent-eof')

    def test_background_stderr_loss_stops_main_loop(self):
        self.run_case('background-output')

    def test_lost_stdout(self):
        self.run_case('lost-output')


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        from diagnostics import output_lost
        self.addCleanup(output_lost.clear)

    def test_ui_errors_have_action_frames_and_no_values(self):
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as tmp:
            for exc in (TypeError('PRIVATE QUESTION secret-token'), RuntimeError('PRIVATE QUESTION secret-token')):
                ui = Mock()
                ui.pause.side_effect = exc
                output = io.StringIO()
                with patch('diagnostics.__file__', tmp+'/diagnostics.py'), redirect_stdout(output):
                    result = perform(ui, 'pause')
                saved = Path(tmp+'/logs/browser-errors.log').read_text()
                self.assertFalse(result['ok'])
                self.assertIn('action=pause', result['detail'])
                self.assertIn('Traceback (frames only)', saved)
                self.assertIn('browser_worker.py', saved)
                self.assertIn('in perform', saved)
                self.assertIn('line ', saved)
                self.assertIn(type(exc).__name__, saved)
                for text in (saved, output.getvalue(), result['detail']):
                    self.assertNotIn('PRIVATE QUESTION', text)
                    self.assertNotIn('secret-token', text)


    def test_logging_cannot_prevent_close_request(self):
        from concurrent.futures import Future
        bridge = Bridge()
        bridge.confirmed = False
        bridge.pending = (1, 'send', Future(), 0)
        with patch('builtins.print', side_effect=BrokenPipeError), patch('diagnostics.silence_broken_stream'):
            result = bridge.submit('close')
        self.assertIs(result, bridge.close_future)
        self.assertIsNotNone(bridge.close_deadline)

    def test_monitor_retire_survives_closed_output(self):
        from speaker_activity import OutputGuard
        guard = OutputGuard()
        process = Mock()
        process.poll.side_effect = [None, 0]
        guard.process = process
        with patch('builtins.print', side_effect=BrokenPipeError), patch('diagnostics.silence_broken_stream'):
            self.assertTrue(guard._retire('closed', 0))
        process.terminate.assert_called_once()
        process.wait.assert_called_once()
        self.assertIsNone(guard.process)

if __name__ == '__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--fixture':
        raise SystemExit(fixture_application(json.loads(sys.argv[2]),int(sys.argv[3]),int(sys.argv[4])))
    unittest.main()
