"""Low-cost PCM monitor of the default PipeWire sink; never records to disk."""
from array import array
import math
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
            print(f'[SPEAKER {label}] '
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

    def start(self):
        if self.process or self.closed or self.failed:
            return
        try:
            # WirePlumber selects and follows the default sink. Capture stereo:
            # opposite-phase channels must not cancel into apparent silence.
            self.process = subprocess.Popen(
                ['pw-record', '--raw', '--rate=8000', '--channels=2', '--format=s16',
                 '--latency=100ms', '-P',
                 '{ stream.capture.sink=true stream.monitor=true node.name=mb-speaker-monitor }', '-'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.thread = threading.Thread(target=self.monitor, daemon=True)
            self.thread.start()
        except OSError as exc:
            self.failed = True
            print(f'[SPEAKER ERROR] {exc}', flush=True)

    def monitor(self):
        try:
            while not self.closed:
                pcm = self.process.stdout.read(3200)
                if len(pcm) != 3200:
                    break
                with self.lock:
                    self.activity.update(pcm, time.monotonic())
        finally:
            with self.lock:
                self.activity.updated = None

    def allows(self, now):
        self.start()
        with self.lock:
            return bool(self.process and self.process.poll() is None and self.activity.allows(now))

    def close(self):
        self.closed = True
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            if self.thread:
                self.thread.join(timeout=2)
            self.process.stdout.close()
