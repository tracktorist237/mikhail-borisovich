#!/usr/bin/env python3
"""Opt-in FreshWake timing. Offline by default; --live explicitly runs main.main.

No PCM/transcripts retained. Live wraps only FreshWake's factory in this process;
production files/defaults are untouched. Timing includes native constructor work,
not model loading; first_accept measures the first call, not end-to-end speech UX.
"""
import argparse
from collections import deque
from contextlib import contextmanager, redirect_stdout
import io
import json
import math
from pathlib import Path
import statistics
import sys
import time
from unittest.mock import patch

import audio_replay as asr
import main
from diagnostics import cleanup_log


class WakeMetrics:
    def __init__(self, clock=time.perf_counter, emit=None):
        self.clock, self.emit = clock, emit
        self.count = self.first_count = 0
        self.total = self.maximum = 0.
        self.samples = deque(maxlen=1000)

    def factory(self, create):
        def timed_create():
            start = self.clock()
            backend = create()
            elapsed = (self.clock()-start)*1000
            self.count += 1
            self.total += elapsed
            self.maximum = max(self.maximum, elapsed)
            row = {'generation': self.count, 'create_ms': elapsed, 'first_accept_ms': None}
            self.samples.append(row)
            return TimedBackend(backend, self, row)
        return timed_create

    def summary(self):
        values = sorted(row['create_ms'] for row in self.samples)
        return {'creations': self.count, 'first_accepts': self.first_count,
                'mean_ms': self.total/self.count if self.count else None,
                'max_ms': self.maximum if self.count else None,
                'sample_count': len(values), 'sample_min_ms': min(values) if values else None,
                'sample_median_ms': statistics.median(values) if values else None,
                'sample_p95_ms': values[math.ceil(.95*len(values))-1] if values else None,
                'samples': list(self.samples)}


class TimedBackend:
    def __init__(self, backend, metrics, row):
        self.backend, self.metrics, self.row = backend, metrics, row

    def AcceptWaveform(self, pcm):
        if self.row['first_accept_ms'] is not None:
            return self.backend.AcceptWaveform(pcm)
        start = self.metrics.clock()
        result = self.backend.AcceptWaveform(pcm)
        self.row['first_accept_ms'] = (self.metrics.clock()-start)*1000
        self.metrics.first_count += 1
        if self.metrics.emit:
            self.metrics.emit(dict(self.row))
        return result

    def Result(self): return self.backend.Result()
    def PartialResult(self): return self.backend.PartialResult()


@contextmanager
def instrument_fresh_wake(metrics):
    original = main.FreshWakeRecognizer
    class MeasuredWake(original):
        def __init__(self, factory):
            super().__init__(metrics.factory(factory))
    with patch.object(main, 'FreshWakeRecognizer', MeasuredWake):
        yield


def offline(count, cfg, Model, Recognizer, metrics):
    with redirect_stdout(io.StringIO()):
        model = main.load_model(cfg, Model)
    grammar = json.dumps(asr.production_wake_grammar(), ensure_ascii=False)
    create = metrics.factory(lambda: Recognizer(model, cfg['sample_rate'], grammar))
    for _ in range(count):
        rec = create()
        rec.AcceptWaveform(bytes(cfg['sample_rate']//10 * 2))
        del rec  # sequential, no accumulation of native recognizers
    return metrics.summary()


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly launch physical main; Ctrl+C exits normally')
    parser.add_argument('--count', type=int, default=20)
    parser.add_argument('--report')
    args = parser.parse_args(argv)
    if not 1 <= args.count <= 1000: parser.error('count must be 1..1000')
    target = asr.report_destination(args.report) if args.report else None
    def emit(row):
        cleanup_log('[WAKE PERF] create=%.3fms first_accept=%.3fms generation=%d' %
                    (row['create_ms'], row['first_accept_ms'], row['generation']), flush=True)
    metrics = WakeMetrics(emit=emit if args.live else None)
    result = 0
    if args.live:
        # Preserve main's signal/error/finally handling; no alternative listen loop.
        with instrument_fresh_wake(metrics), patch.object(sys, 'argv', ['main.py']):
            result = main.main()
    else:
        _, Model, Recognizer = main.dependencies(audio=False)
        offline(args.count, main.config(), Model, Recognizer, metrics)
    report = {'schema_version': 1, 'kind': 'live-instrumentation' if args.live else 'offline-creation-only',
              'git': asr.git_identity(), 'main_sha256': asr.sha256_file(asr.ROOT/'main.py'),
              'model_name': Path(main.config()['model_path']).name, 'timing': metrics.summary()}
    cleanup_log('[WAKE PERF SUMMARY] '+json.dumps({k:v for k,v in report['timing'].items() if k!='samples'}), flush=True)
    if target:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('x') as out: json.dump(report, out, indent=2);out.write('\n')
    return result


if __name__ == '__main__': sys.exit(cli())
