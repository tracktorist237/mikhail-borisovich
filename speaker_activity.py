"""Low-cost PCM monitor of the default PipeWire sink; never records to disk."""
from diagnostics import cleanup_log
from array import array
import math
import os
import subprocess
import threading
import time


class Activity:
    def __init__(self, threshold=100, silence_seconds=.8, peak_threshold=1000):
        if not 0 < threshold <= 32767 or not .1 <= silence_seconds <= 5:
            raise ValueError('Invalid speaker threshold / silence duration')
        self.threshold = threshold
        self.peak_threshold = peak_threshold
        self.silence_seconds = silence_seconds
        self.rms = self.peak = 0
        self.active = True
        self.quiet_since = None
        self.updated = None

    def reset(self):
        self.rms = self.peak = 0
        self.active = True
        self.quiet_since = None
        self.updated = None

    def update(self, pcm, now):
        samples = array('h', pcm)
        if not samples:
            return
        self.rms = math.sqrt(sum(x*x for x in samples) / len(samples))
        self.peak = max(abs(x) for x in samples)
        previous = self.active if self.updated is not None else None
        if self.updated is None or now - self.updated > .5:
            self.quiet_since = None
            self.active = True
        self.updated = now
        if self.rms >= self.threshold or self.peak >= self.peak_threshold:
            self.quiet_since = None
            self.active = True
        else:
            if self.quiet_since is None:
                self.quiet_since = now
            if now - self.quiet_since >= self.silence_seconds:
                self.active = False
        if previous != self.active:
            label = ('ACTIVE' if self.rms >= self.threshold or self.peak >= self.peak_threshold
                     else 'UNAVAILABLE' if self.active else 'SILENT')
            cleanup_log(f'[SPEAKER {label}] '
                  f'rms={self.rms:.1f} peak={self.peak}', flush=True)

    def allows(self, now):
        return self.updated is not None and 0 <= now - self.updated < .5 and not self.active


class OutputGuard:
    def __init__(self, cfg=None):
        cfg = cfg or {}
        self.activity = Activity(cfg.get('speaker_rms', 100),
                                 cfg.get('speaker_silence_ms', 800) / 1000,
                                 cfg.get('speaker_peak', 1000))
        self.process = None
        self.thread = None
        self.lock = threading.Lock()
        self.closed = False
        self.failed = False
        self.restart_limit = cfg.get('speaker_restart_limit', 3)
        self.restart_delay = cfg.get('speaker_restart_delay', .5)
        self.restarts = 0
        self.attempts = 0
        self.next_restart = 0
        self.last_data = None
        self.started_at = None
        self.monitor_done = False
        self.reason = None
        # Sample freshness stays at 500 ms in Activity. Startup and a genuine
        # stalled capture have separate, longer bounds before process recovery.
        self.startup_timeout = cfg.get('speaker_startup_timeout', 3)
        self.stall_timeout = cfg.get('speaker_stall_timeout', 2)
        self.wake = threading.Event()
        self.generation = 0
        self.buffer = bytearray()

    def start(self):
        """Only schedule work here: no process waits on the microphone loop."""
        with self.lock:
            if self.closed or self.failed or self.thread is not None:
                return
            self.thread = threading.Thread(target=self.monitor, daemon=True,
                                           name='speaker-monitor')
            self.thread.start()

    def _unavailable(self, reason):
        with self.lock:
            changed = self.reason != reason
            self.reason = reason
        if changed:
            cleanup_log(f'[SPEAKER UNAVAILABLE] {reason}', flush=True)

    def _retire(self, reason, now):
        """Worker-only cleanup. Never wait/close/join under the state mutex.

        The worker also owns all reads (unbuffered, nonblocking), so no old
        reader can remain behind holding a pipe lock when this returns.
        """
        with self.lock:
            process = self.process
            self.generation += 1
            self.activity.reset()
            self.monitor_done = True
        self._unavailable(reason)
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=.4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=.4)
                if process.poll() is None:
                    raise RuntimeError('monitor exit not confirmed')
            except (OSError, subprocess.TimeoutExpired, RuntimeError):
                with self.lock:
                    self.failed = True
                cleanup_log('[SPEAKER ERROR] Monitor exit not confirmed; replacement blocked.', flush=True)
                return False
            if process.stdout is not None:
                process.stdout.close()
        with self.lock:
            self.process = None
            self.last_data = self.started_at = None
            self.buffer.clear()
            self.next_restart = now + self.restart_delay
        return True

    def _accept(self, process, generation, pcm, now):
        with self.lock:
            if self.closed or process is not self.process or generation != self.generation:
                return
            self.activity.update(pcm, now)
            self.last_data = now
            recovered = self.reason is not None
            self.reason = None
        if recovered:
            cleanup_log('[SPEAKER] Fresh monitor data received; waiting for verified silence.', flush=True)

    def _service(self, now):
        """One worker iteration; separated for fake-clock/pipe tests."""
        if self.closed or self.failed:
            return
        process = self.process
        if process is None:
            if now < self.next_restart:
                return
            if self.attempts >= self.restart_limit:
                self.failed = True
                cleanup_log('[SPEAKER ERROR] Monitor recovery limit reached.', flush=True)
                return
            self.attempts += 1
            cleanup_log(f'[SPEAKER] Monitor start attempt {self.attempts}/{self.restart_limit}.', flush=True)
            try:
                process = subprocess.Popen(
                    ['pw-record', '--raw', '--rate=8000', '--channels=2', '--format=s16',
                     '--latency=100ms', '-P',
                     '{ stream.capture.sink=true stream.monitor=true node.name=mb-speaker-monitor }', '-'],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
                with self.lock:
                    self.process = process
                    self.generation += 1
                    self.activity.reset()
                    self.last_data = None
                    self.started_at = now
                    self.monitor_done = False
                    self.restarts += 1
                os.set_blocking(process.stdout.fileno(), False)
            except OSError:
                self._retire('process start failed', now)
            return
        if process.poll() is not None:
            self._retire('process exited', now)
            return
        try:
            data = os.read(process.stdout.fileno(), 3200)
        except BlockingIOError:
            data = None
        except OSError:
            self._retire('read failed', now)
            return
        if data == b'':
            self._retire('EOF', now)
            return
        if data:
            self.buffer.extend(data)
            if len(self.buffer) >= 3200:
                block = bytes(self.buffer[:3200])
                del self.buffer[:3200]
                self._accept(process, self.generation, block, now)
        age = now - (self.started_at if self.last_data is None else self.last_data)
        limit = self.startup_timeout if self.last_data is None else self.stall_timeout
        if age >= limit:
            self._retire('startup timeout' if self.last_data is None else 'stale', now)
        elif self.last_data is not None and age >= .5:
            self._unavailable('stale')

    def monitor(self):
        try:
            while not self.closed and not self.failed:
                self._service(time.monotonic())
                self.wake.wait(.05)
                self.wake.clear()
        except Exception as exc:
            self.failed = True
            cleanup_log(f'[SPEAKER ERROR] Monitor worker: {type(exc).__name__}', flush=True)
        finally:
            self._retire('closed' if self.closed else 'worker stopped', time.monotonic())

    def allows(self, now):
        self.start()
        with self.lock:
            return self._available(now) and self.activity.allows(now)

    def _available(self, now):
        return bool(not self.closed and not self.failed and self.process is not None
                    and self.process.poll() is None and not self.monitor_done
                    and self.activity.updated is not None
                    and 0 <= now - self.activity.updated < .5)

    def available(self, now):
        """Fresh capture may be healthy while speakers are active."""
        with self.lock:
            return self._available(now)

    def close(self):
        with self.lock:
            self.closed = True
            self.activity.reset()
            thread = self.thread
        self.wake.set()
        if thread is not None:
            thread.join(timeout=2)
            if thread.is_alive():
                self.failed = True
                cleanup_log('[SPEAKER ERROR] Monitor worker did not stop; recognition blocked.', flush=True)
                return False
        return self.process is None
