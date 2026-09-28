"""Deterministic external environment for the real listen loop; never production defaults."""
from concurrent.futures import Future
import heapq
import itertools
import json
import math
import queue
from types import SimpleNamespace

import main


class ReplayError(AssertionError):
    """A scenario/environment failure, not a recoverable assistant command error."""


class ScenarioComplete(Exception):
    """A declared observation boundary ends replay, with listen's finally intact."""


class Scheduler:
    def __init__(self, max_time=120, max_events=20000):
        self.now = 0.
        self.max_time, self.max_events = max_time, max_events
        self.events, self.sequence, self.executed = [], itertools.count(), 0

    def schedule(self, when, callback):
        if not math.isfinite(when) or when < self.now - 1e-9 or when > self.max_time:
            raise ReplayError('Event outside virtual time limits')
        if len(self.events) + self.executed >= self.max_events:
            raise ReplayError('Maximum event count exceeded')
        heapq.heappush(self.events, (max(self.now, when), next(self.sequence), callback))

    def advance_to(self, when):
        if not math.isfinite(when) or when < self.now or when > self.max_time:
            raise ReplayError('Virtual time limit or backwards advance')
        while self.events and self.events[0][0] <= when + 1e-9:
            at, _, callback = heapq.heappop(self.events)
            self.now = max(self.now, at)
            self.executed += 1
            if self.executed > self.max_events:
                raise ReplayError('Maximum event count exceeded')
            callback()
        self.now = max(self.now, when)

    def advance_by(self, seconds):
        self.advance_to(self.now + seconds)


class EventSink:
    def __init__(self, clock):
        self.clock, self.events = clock, []

    def emit(self, event, **fields):
        # Scalar metadata only. PCM, WebElements, process objects cannot enter.
        if any(not isinstance(v, (str, int, float, bool, type(None))) for v in fields.values()):
            raise ReplayError('Observer accepts scalar metadata only')
        if any(k in fields for k in ('pcm', 'audio', 'answer', 'cookies', 'tokens')):
            raise ReplayError('Forbidden observer field')
        self.events.append({'event': event, 'time': round(self.clock(), 6), **fields})


class FakeSpeech:
    def __init__(self, runtime):
        self.rt, self.generation = runtime, 0
        self.phase = None
        self.playback_end = None
        self.last = 'Тестовый ответ.'

    def say(self, text, remember=True, *, completion_clock=None):
        self.stop()
        generation = self.generation
        role = ('wake_ack' if completion_clock else 'control_ack') if text == 'Слушаю' else 'response'
        if remember:
            self.last = text
        self.phase, self.playback_end = 'synthesis', None
        start = self.rt.scheduler.now + .1
        duration = self.rt.scenario.get('ack_duration_ms', 6000)/1000 if role.endswith('_ack') else .3
        end = start + duration
        self.rt.observe('speech_started', role=role)
        # Announce a known future boundary early enough to replay pre-onset silence.
        self.rt.anchor(role + '.playback_end', end)
        def playback():
            if self.generation == generation:
                self.phase = 'playback'
                self.rt.observe('speech_playback_started', role=role)
        def complete():
            if self.generation == generation:
                self.phase = None
                self.playback_end = completion_clock() if completion_clock else None
                self.rt.observe('speech_playback_end', role=role)
        self.rt.scheduler.schedule(start, playback)
        self.rt.scheduler.schedule(end, complete)
        self.ends = end

    def busy(self):
        return self.phase is not None

    def wait_for_completion(self, timeout):
        self.rt.advance_main(min(timeout, max(0, self.ends-self.rt.scheduler.now)))

    def stop(self):
        self.generation += 1
        self.phase = None

    def close(self):
        self.stop()


ACTIONS = frozenset(('open', 'open_anton', 'close', 'pause', 'abort_dictation',
                     'resume', 'dictate', 'transcribe', 'send', 'poll'))
SYSTEM_ACTIONS = frozenset(('TIME', 'DATE', 'BATTERY', 'INTERNET', 'VOLUME_UP',
                           'VOLUME_DOWN', 'VOLUME_SET', 'OPEN_BROWSER', 'CLOSE_BROWSER', 'STOP', 'REPEAT'))


class FakeBridge:
    def __init__(self, runtime):
        self.rt = runtime
        self.history = []
        self.closed = True
        self.closing = None
        self.generation = 0
        self.pending = []

    def submit(self, action):
        policy = self.rt.scenario.get('bridge', {})
        if action not in ACTIONS or action not in policy:
            raise ReplayError('Unexpected fake Bridge action: ' + action)
        if action == 'close' and self.closing is not None:
            return self.closing
        if self.closing is not None and not self.closed:
            raise ReplayError('New action before confirmed cleanup')
        if action in ('open', 'open_anton'):
            if not self.closed:
                raise ReplayError('Second open before cleanup')
            self.generation += 1
            self.closed, self.closing = False, None
        generation = self.generation
        self.history.append(action)
        self.rt.observe('bridge_action', action=action, generation=generation)
        self.rt.anchor('bridge.' + action, self.rt.scheduler.now)
        future = Future()
        future.set_running_or_notify_cancel()
        rule = policy[action]
        if action == 'close':
            self.closing = future
        else:
            self.pending.append((future, action, rule))
        def complete():
            late = generation != self.generation or (self.closed and action != 'close')
            result = {'ok': rule.get('ok', True), 'detail': 'replay fixture',
                      'end_voice': True, **rule.get('result', {})}
            if action == 'close' and result['ok']:
                self.closed = True
                for old, name, old_rule in self.pending:
                    if not old.done() and not old_rule.get('late_result', False):
                        old.set_result({'ok': False, 'interrupted': True, 'outcome_unknown': name == 'send',
                                        'detail': 'replay closed'})
            self.rt.observe('bridge_result', action=action, ok=result['ok'], late=late)
            if not future.done():
                future.set_result(result)
        delay = rule.get('delay_ms', 0)
        if delay is not None:
            self.rt.scheduler.schedule(self.rt.scheduler.now + delay/1000, complete)
        return future

    def close(self):
        # Resource finalization is separate from a control request; no real owner.
        self.closed = True
        self.rt.observe('bridge_finalized')


class FakeGuard:
    def __init__(self, runtime): self.rt = runtime
    @property
    def failed(self): return self.rt.guard_state == 'failed'
    def allows(self, now): return self.rt.guard_state == 'silent'
    def available(self, now): return self.rt.guard_state in ('silent', 'active')
    def quiet(self, now=None): return self.rt.guard_state == 'silent'
    def close(self): return True


class RecognizerProbe:
    """Transparent accounting; no invented text or altered endpoint decisions."""
    def __init__(self, recognizer, runtime, role):
        self.rec, self.rt, self.role = recognizer, runtime, role
        self.generation = 0
        self.rec_id = runtime.recognizer_count
        runtime.recognizer_count += 1
        self.routed = []
        if runtime.trace:
            runtime.trace.emit('asr_created',recognizer=self.rec_id,generation=0,role=role)
    def Reset(self):
        self.rec.Reset()
        self.generation += 1
        self.routed = []
        if self.rt.trace:
            self.rt.trace.emit('asr_reset', recognizer=self.rec_id, generation=self.generation, role=self.role)
        self.rt.observe('recognizer_reset', role=self.role)
    def AcceptWaveform(self, pcm):
        if self.rt.trace:
            self.routed = self.rt.trace.route(pcm, self.rec_id, self.generation, self.role)
        self.rt.observe('recognizer_input', role=self.role, frames=len(pcm)//2,
                        capture_start=getattr(self.rt, 'last_capture_start', None),
                        capture_end=getattr(self.rt, 'last_capture_end', None))
        return self.rec.AcceptWaveform(pcm)
    def result(self, final):
        value = self.rec.Result() if final else self.rec.PartialResult()
        if self.rt.trace:
            text = json.loads(value).get('text' if final else 'partial', '')
            self.rt.trace.emit('asr_result', recognizer=self.rec_id, generation=self.generation,
                               role=self.role, final=final, text=text, blocks=self.routed)
        return value
    def Result(self): return self.result(True)
    def PartialResult(self): return self.result(False)


class FakeStream:
    def __init__(self, runtime, callback):
        self.rt, self.callback = runtime, callback
        self.open = False
        self.frame = 0
        if runtime.trace: runtime.trace.install_queue(callback)
    @property
    def time(self): return self.rt.audio_epoch + self.rt.scheduler.now
    def __enter__(self):
        self.open = True
        self.rt.scheduler.schedule(.1, self.capture)
        return self
    def __exit__(self, *args): self.open = False
    @property
    def active(self):
        self.rt.advance_main(.01)
        return self.open
    def capture(self):
        if not self.open: return
        frame, frames = self.frame, 1600
        if self.rt.trace: self.rt.trace.capture(frame//frames)
        self.frame += frames
        raw = bytearray(frames*2)
        for start, pcm in self.rt.segments:
            left, right = max(start, frame), min(start+len(pcm)//2, frame+frames)
            if left < right:
                raw[(left-frame)*2:(right-frame)*2] = pcm[(left-start)*2:(right-start)*2]
        start, end = frame/16000, (frame+frames)/16000
        delay = self.rt.callback_delay
        payload = bytes(raw)
        gap = self.rt.gap_next
        self.rt.gap_next = False
        self.rt.observe('capture_block', start=start+self.rt.audio_epoch, end=end+self.rt.audio_epoch)
        def deliver():
            if not self.open: return
            self.rt.check_delivery(frame//frames)
            if self.rt.trace: self.rt.trace.emit('callback_delivery', block=frame//frames)
            self.callback(payload, frames,
                SimpleNamespace(inputBufferAdcTime=self.rt.audio_epoch+start,
                                currentTime=self.time), 'replay gap' if gap else None)
            self.rt.observe('callback_delivered', capture_start=self.rt.audio_epoch+start,
                            capture_end=self.rt.audio_epoch+end)
        self.rt.scheduler.schedule(end+delay, deliver)
        if end+.1 <= self.rt.scheduler.max_time:
            self.rt.scheduler.schedule((self.frame+frames)/16000, self.capture)


class ReplayRuntime(main.ListenRuntime):
    def __init__(self, scenario, clips, model, recognizer, *, require_vosk=True, debug_trace=False):
        if require_vosk and recognizer.__module__ != 'vosk':
            raise ReplayError('Integration replay requires real Vosk')
        self.scenario, self.clips, self.model, self.Recognizer = scenario, clips, model, recognizer
        self.scheduler = Scheduler(scenario.get('duration', 45)+35)
        self.sink = EventSink(self.monotonic)
        self.audio_epoch = 10000.
        self.segments, self.triggered, self.occurrences = [], set(), {}
        self.source_spans = []
        self.delivered_blocks = set()
        self.delivered_source_ranges = {}
        self.recognizer_count = 0
        self.trace = None
        if debug_trace:
            from replay_trace import ReplayTrace
            self.trace = ReplayTrace(self)
        self.guard_state, self.gap_next = 'silent', False
        self.callback_delay = scenario.get('timing', {}).get('callback_delay_ms', 0)/1000
        self.blocked_until = 0.
        self.finished = False
        self.br = FakeBridge(self)
        self.tts = FakeSpeech(self)
        self.local_intents = []

    def monotonic(self): return self.scheduler.now
    def running(self): return not self.finished
    def speech(self, cfg): return self.tts
    def bridge(self, cfg): return self.br
    def guard(self, cfg): return FakeGuard(self)

    def recognition(self, cfg):
        def recognizer(model, rate, grammar):
            words = json.loads(grammar)
            role = 'wake' if 'михаил' in words else 'control' if words == main.CONTROL_GRAMMAR else 'command'
            return RecognizerProbe(self.Recognizer(model, rate, grammar), self, role)
        return SimpleNamespace(check_input_settings=lambda **kw: None,
                               RawInputStream=lambda **kw: FakeStream(self, kw['callback'])), self.model, recognizer

    def execute(self, text, cfg, speech, intent, value):
        if intent not in SYSTEM_ACTIONS or intent not in self.scenario.get('system_actions', []):
            raise ReplayError('Unexpected fake system action: ' + str(intent))
        self.local_intents.append(intent)
        self.observe('intent_matched', intent=intent)
        if intent == 'STOP': speech.stop(); return None
        return 'Тестовый ответ.'

    def advance_main(self, seconds):
        self.scheduler.advance_by(seconds)
        if self.blocked_until > self.scheduler.now:
            self.scheduler.advance_to(self.blocked_until)

    def read_audio(self, audio, timeout):
        if type(audio) is not main.FreshAudioQueue:
            raise ReplayError('Replay must use the production FreshAudioQueue')
        until = self.scheduler.now + timeout
        while True:
            try:
                item = audio.get_nowait()
                capture = item[1]
                self.last_capture_start, self.last_capture_end = capture.start, capture.end
                self.observe('audio_consumed', capture_start=capture.start, capture_end=capture.end,
                             arrival=item[0], gap=item[2], depth=len(audio.items))
                return item
            except queue.Empty:
                if self.scheduler.now >= until: raise
                at = self.scheduler.events[0][0] if self.scheduler.events else until
                self.advance_main(max(0, min(at, until)-self.scheduler.now))

    def source_fragments(self, block):
        frame, end = block*1600, (block+1)*1600
        result = []
        for step, clip, start, frames in self.source_spans:
            left, right = max(frame,start), min(end,start+frames)
            while left < right:
                index = (left-start)//1600
                stop = min(right,start+(index+1)*1600)
                result.append({'step':step,'clip':clip,'block_index':index,
                               'source_start_frame':left-start,'source_end_frame':stop-start})
                left = stop
        return result

    def check_delivery(self, block):
        if block in self.delivered_blocks:
            raise ReplayError('Duplicate capture block delivery')
        self.delivered_blocks.add(block)
        # An unaligned WAV block spans two callbacks; its disjoint fragments
        # are not duplicates. A second declared step is an explicit repeat.
        for part in self.source_fragments(block):
            key = (part['step'], part['clip'], part['block_index'])
            left, right = part['source_start_frame'], part['source_end_frame']
            ranges = self.delivered_source_ranges.setdefault(key, [])
            if any(left < b and a < right for a,b in ranges):
                raise ReplayError('Duplicate source PCM range delivery')
            ranges.append((left,right))

    def observe(self, event, **fields):
        if self.trace: self.trace.observe(event, fields)
        self.sink.emit(event, **fields)
        if event == 'local_state' and fields['state'] == 'waiting':
            self.anchor('waiting', self.scheduler.now)
            if self.occurrences.get('initial_waiting', 0) == 0:
                self.anchor('initial_waiting', self.scheduler.now)
        if event == 'mode_changed':
            self.anchor('phase.' + fields['phase'], self.scheduler.now)
        if event == 'mode_entered':
            self.anchor('mode.' + fields['mode'], self.scheduler.now)

    def anchor(self, name, when):
        count = self.occurrences[name] = self.occurrences.get(name, 0)+1
        if self.scenario.get('stop_at') == name:
            self.finished = True
            raise ScenarioComplete()
        for index, step in enumerate(self.scenario['steps']):
            if index in self.triggered or step['at'] != name or step.get('occurrence', 1) != count:
                continue
            self.triggered.add(index)
            start = when + step.get('offset_ms', 0)/1000
            if 'clip' in step:
                pcm = self.clips[step['clip']]
                if step.get('align') == 'speech_onset':
                    start -= step['speech_onset_frame']/16000
                frame = round(start*16000)
                if start < self.scheduler.now-1e-9:
                    raise ReplayError('Clip prefix precedes known anchor; increase fake cue duration')
                if any(frame < a+len(b)//2 and a < frame+len(pcm)//2 for a,b in self.segments):
                    raise ReplayError('Overlapping WAVs: no implicit mix or trimming')
                self.segments.append((frame, pcm))
                self.source_spans.append((index, step['clip'], frame, len(pcm)//2))
                self.observe('clip_scheduled', clip=step['clip'], capture_start=self.audio_epoch+frame/16000)
                end = (frame+len(pcm)//2)/16000
                self.anchor('clip.'+step['clip']+'.end', end)
            else:
                def apply(step=step):
                    kind = step['kind']
                    if kind == 'guard': self.guard_state = step['state']
                    elif kind == 'main_delay': self.blocked_until = max(self.blocked_until, self.scheduler.now+step['duration_ms']/1000)
                    elif kind == 'gap': self.gap_next = True
                    elif kind == 'callback_delay': self.callback_delay = step['duration_ms']/1000
                    self.observe('environment_event', kind=kind)
                self.scheduler.schedule(start, apply)
        if name == 'wake_ack.playback_end':
            delay = self.scenario.get('timing', {}).get('main_loop_delay_after_playback_ms', 0)/1000
            if delay:
                self.scheduler.schedule(when, lambda: setattr(self, 'blocked_until', max(self.blocked_until, when+delay)))
