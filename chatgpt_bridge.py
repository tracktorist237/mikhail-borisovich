"""Firefox UI only. No API, profile access, cookies or browser driver."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import time
import threading

LOCAL_MODE = 'LOCAL_MODE'
CHATGPT_MODE = 'CHATGPT_MODE'


class OutputGuard:
    """Fail closed while any output stream runs; retain a two-second echo tail.

    Deliberately conservative: a browser holding a silent stream also blocks return.
    Subscribe to PipeWire state changes; never read output audio.
    """
    def __init__(self):
        self.process = None
        self.started = False
        self.ready = False
        self.nodes = {}
        self.quiet_since = float('inf')
        self.lock = threading.Lock()

    @staticmethod
    def nodes_quiet(nodes):
        return any(n.get('info', {}).get('props', {}).get('media.class') == 'Audio/Sink' for n in nodes) and not any(
            n.get('info', {}).get('props', {}).get('media.class') == 'Stream/Output/Audio'
            and n.get('info', {}).get('state') == 'running' for n in nodes)

    @staticmethod
    def quiet():
        try:
            result = subprocess.run(['pw-dump'], capture_output=True, timeout=2, check=True)
            return OutputGuard.nodes_quiet(json.loads(result.stdout))
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    def update(self, changes, now):
        with self.lock:
            for node in changes:
                if node.get('info') is None:
                    self.nodes.pop(node['id'], None)
                else:
                    # Monitor updates can omit unchanged properties.
                    old = self.nodes.setdefault(node['id'], {'info': {}})['info']
                    info = node['info']
                    props = {**old.get('props', {}), **info.get('props', {})}
                    old.update(info)
                    old['props'] = props
            quiet = self.nodes_quiet(self.nodes.values())
            self.ready = True
            if not quiet:
                self.quiet_since = float('inf')
            elif self.quiet_since == float('inf'):
                self.quiet_since = now

    def monitor(self):
        buffer = ''
        decoder = json.JSONDecoder()
        try:
            for line in self.process.stdout:
                buffer += line
                if len(buffer) > 4_000_000:
                    break
                if not line.startswith(']'):
                    continue
                try:
                    changes = decoder.decode(buffer)
                except ValueError:
                    continue
                self.update(changes, time.monotonic())
                buffer = ''
        finally:
            with self.lock:
                self.ready = False
                self.quiet_since = float('inf')

    def allows(self, now):
        if not self.started:
            self.started = True
            try:
                self.process = subprocess.Popen(['stdbuf', '-oL', 'pw-dump', '--monitor'], stdout=subprocess.PIPE,
                                                stderr=subprocess.DEVNULL, text=True)
                threading.Thread(target=self.monitor, daemon=True).start()
            except OSError:
                return False
        with self.lock:
            return bool(self.process and self.process.poll() is None and self.ready
                        and now - self.quiet_since >= 2)

    def close(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


class Bridge:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=1)

    def submit(self, action):
        return self.pool.submit(self.perform, action)

    @staticmethod
    def perform(action):
        try:
            if action == 'open':
                child = subprocess.Popen(['firefox', '--new-tab', 'https://chatgpt.com/'],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         start_new_session=True)
                try:
                    if child.wait(timeout=.5):
                        return {'ok': False, 'detail': 'Не удалось открыть Firefox.'}
                except subprocess.TimeoutExpired:
                    pass
            result = subprocess.run(['/usr/bin/python3', str(Path(__file__).with_name('chatgpt_ui.py')), action],
                                    capture_output=True, timeout=25, check=True)
            return json.loads(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return {'ok': False, 'detail': f'Не удалось подтвердить Voice: {type(exc).__name__}.'}

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
