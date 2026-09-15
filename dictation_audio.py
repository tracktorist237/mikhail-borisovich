"""Diagnostic only: PipeWire routing and PCM levels, no audio files or device changes."""
from array import array
from contextlib import contextmanager
import re
import json
import math
import subprocess
import threading
import time


def graph():
    return json.loads(subprocess.check_output(['pw-dump'], timeout=5))


def input_routes(nodes, pid):
    by_id = {n['id']: n for n in nodes}
    inputs = {n['id'] for n in nodes
              if n.get('info', {}).get('props', {}).get('media.class') == 'Stream/Input/Audio'
              and str(n.get('info', {}).get('props', {}).get('application.process.id')) == str(pid)}
    routes = []
    for node in nodes:
        info = node.get('info', {})
        if node['type'] != 'PipeWire:Interface:Link' or info.get('input-node-id') not in inputs:
            continue
        source = by_id.get(info.get('output-node-id'), {}).get('info', {}).get('props', {})
        port = by_id.get(info.get('output-port-id'), {}).get('info', {}).get('props', {})
        routes.append(dict(source=source.get('node.name'), serial=source.get('object.serial'),
                           device=source.get('node.description'), media_class=source.get('media.class'),
                           port=port.get('port.name'), physical=port.get('port.physical', False),
                           monitor=port.get('port.monitor', False), state=info.get('state')))
    return routes


def levels(pcm):
    samples = array('h', pcm)
    result = []
    for channel in (samples[::2], samples[1::2]):
        result.append((round(math.sqrt(sum(x*x for x in channel)/len(channel)), 1),
                       max(abs(x) for x in channel)))
    return result


class InputMeter:
    def __init__(self, serial):
        self.process = subprocess.Popen([
            'pw-record', '--raw', '--rate=8000', '--channels=2', '--format=s16',
            '--latency=100ms', '--target', str(serial),
            '-P', '{ node.name=mb-dictation-input-meter }', '-'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.stopped = False
        self.thread = threading.Thread(target=self.read, daemon=True)
        self.thread.start()

    def read(self):
        started = last = time.monotonic()
        maximum = [[0, 0], [0, 0]]
        try:
            while not self.stopped:
                pcm = self.process.stdout.read(3200)
                if len(pcm) != 3200:
                    break
                for i, (rms, peak) in enumerate(levels(pcm)):
                    maximum[i][0] = max(maximum[i][0], rms)
                    maximum[i][1] = max(maximum[i][1], peak)
                now = time.monotonic()
                if now-last >= 1:
                    print(f'[MIC SOURCE +{now-started:.1f}s] '
                          f'FL rms/peak={maximum[0]} FR rms/peak={maximum[1]}', flush=True)
                    maximum = [[0, 0], [0, 0]]
                    last = now
        finally:
            print('[MIC SOURCE] meter stopped; no further level samples', flush=True)

    def close(self):
        self.stopped = True
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.thread.join(timeout=3)
        self.process.stdout.close()


@contextmanager
def temporary_mic_boost(value):
    """Opt-in diagnostic setting for this laptop's ALSA card 0; restore on exit."""
    if value is None:
        yield
        return
    control = "name=Internal Mic Boost Volume"
    result = subprocess.check_output(['amixer','-c','0','cget',control], text=True)
    match = re.search(r': values=([0-9,]+)', result)
    if not match:
        raise RuntimeError('Cannot read original Internal Mic Boost; no change made')
    original = match.group(1)
    try:
        subprocess.run(['amixer','-q','-c','0','cset',control,str(value)], check=True)
        print(f'[MIC BOOST] temporary={value}, original={original}', flush=True)
        yield
    finally:
        subprocess.run(['amixer','-q','-c','0','cset',control,original], check=True)
        print(f'[MIC BOOST] restored={original}', flush=True)
