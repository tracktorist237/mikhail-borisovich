"""Isolated GPT conversation state machine, driven by the existing audio loop."""
from array import array
from collections import deque
import json
import math
import re
import time
from chatgpt_bridge import LOCAL_MODE, LARISA_MODE, ANTON_MODE

CONTROL_GRAMMAR = ['михаил борисович', 'вернись', 'закрой ларису',
                   'закрой антона павловича', 'закончи разговор', 'стоп',
                   'закрой чат ж п т', 'верни михаила', '[unk]']
RETURNS = {'вернись', 'закрой ларису', 'закрой антона павловича',
           'закончи разговор', 'стоп', 'закрой чат ж п т', 'верни михаила'}


class GPTModes:
    def __init__(self, cfg, speech, bridge, guard, recognizer):
        self.cfg, self.speech, self.bridge, self.guard, self.rec = cfg, speech, bridge, guard, recognizer
        self.mode = LOCAL_MODE
        self.phase = 'idle'
        self.job = None
        self.action = None
        self.deadline = 0
        self.poll_after = 0
        self.accept_after = 0
        self.interrupt = False
        self.resume_phase = 'dictating'
        self.chunks = []
        self.heard = False
        self.last_sound = 0
        self.mic_tail = 0
        self.preroll = deque(maxlen=3)
        self.emergency = False
        self.closed_confirmed = False
        self.blocked_since = None
        self.control_tick = None
        self.control_allowed = False
        self.control_wait_started = None
        self.job_started = None
        self.job_warned = False
        self.last_transition = None

    @property
    def active(self):
        return self.phase != 'idle'

    def say(self, text, after, now):
        self.speech.say(text, remember=False)
        self.phase = 'speaking'
        self.after_speech = after
        self.rec.Reset()
        self.preroll.clear()
        self.mic_tail = 0
        self.accept_after = float('inf')

    def start(self, mode):
        self.emergency = self.closed_confirmed = False
        self.blocked_since = self.control_tick = None
        self.target_mode = mode
        self.say('Зову Ларису.' if mode == LARISA_MODE else 'Зову Антона Павловича.',
                 'open' if mode == LARISA_MODE else 'open_anton', time.monotonic())

    def submit(self, action, phase=None):
        assert self.job is None
        self.action = action
        self.job = self.bridge.submit(action)
        self.job_started = None
        self.job_warned = False
        if phase:
            self.phase = phase

    def fail(self, detail, now):
        print(f'[GPT ERROR] {detail}', flush=True)
        self.interrupt = False
        self.submit('close', 'closing')

    def after(self, action, now):
        if action == 'idle':
            self.phase = 'idle'
        elif action == 'control':
            self.phase = 'control'
            self.deadline = now + self.cfg['command_timeout']
            self.control_tick = now
            self.control_allowed = True
        elif action == 'next_chunk':
            if self.chunks:
                self.say(self.chunks.pop(0), 'next_chunk', now)
            else:
                self.submit('dictate', 'starting_dictation')
        else:
            self.submit(action, {'open': 'opening', 'open_anton': 'opening',
                                'dictate': 'starting_dictation', 'close': 'closing',
                                'resume': 'resuming'}.get(action, action))

    def completed(self, result, action, now):
        if not result['ok']:
            if action == 'close':
                print(f'[GPT ERROR] Возврат заблокирован: {result["detail"]}', flush=True)
                self.phase = 'error_wait'
                self.accept_after = now
                return
            self.fail(result['detail'], now)
            return
        if action != 'poll':
            print(f'[GPT] {result["detail"]}', flush=True)
        if action == 'close':
            self.closed_confirmed = True
            self.mode = LOCAL_MODE
            self.interrupt = False
            print(f'[{LOCAL_MODE}]', flush=True)
            self.say('Я снова слушаю.', 'idle', now)
            return
        if self.interrupt:
            if action in ('open', 'open_anton'):
                self.mode = self.target_mode
            self.resume_phase = {'send': 'reply', 'transcribe': 'transcribing',
                                 'dictate': 'dictating', 'open_anton': 'dictating',
                                 'open': 'voice'}.get(action, self.phase)
            self.interrupt = False
            self.submit('pause', 'pausing')
            return
        if action in ('open', 'open_anton'):
            self.mode = self.target_mode
            print(f'[{self.mode}]', flush=True)
            self.rec.Reset()
            self.accept_after = now
            if self.mode == ANTON_MODE:
                self.say('Говорите после начала диктовки. Завершите вопрос паузой.', 'dictate', now)
            else:
                self.phase = 'voice'
        elif action in ('pause', 'abort_dictation'):
            self.say('Слушаю', 'control', now)
        elif action == 'resume':
            self.phase = 'voice'
        elif action == 'dictate':
            self.phase = 'dictating'
            self.heard = False
            self.last_sound = now
            self.deadline = now + self.cfg.get('dictation_max_seconds', 45)
            self.accept_after = now
            self.rec.Reset()
            print('[DICTATING] Говорите; пауза завершает вопрос.', flush=True)
        elif action == 'transcribe':
            self.phase = 'transcribing'
            self.deadline = now + 60
        elif action == 'send':
            self.phase = 'reply'
            self.deadline = now + self.cfg.get('reply_timeout', 180)
        elif action == 'poll':
            if self.phase == 'transcribing' and result.get('composer') and result.get('send') and not result.get('recording'):
                self.submit('send', 'sending')
            elif self.phase == 'reply' and result.get('reply_ready'):
                # RHVoice's existing per-utterance timeout also applies to playback.
                self.chunks = re.findall(r'.{1,220}(?:\s+|$)|\S{1,220}', result['answer'])
                self.chunks = [s.strip() for s in self.chunks if s.strip()]
                if self.chunks:
                    self.say(self.chunks.pop(0), 'next_chunk', now)
            elif self.phase == 'voice' and not result.get('end_voice'):
                self.fail('Интерфейс Voice завершился или отключился.', now)

    def audio_gap(self, now, *, discard_through=True):
        """Forget recognizer context across a dropped callback block."""
        self.rec.Reset()
        self.preroll.clear()
        self.mic_tail = 0
        if discard_through:
            self.accept_after = max(self.accept_after, now)

    def tick(self, now=None):
        """Service jobs and deadlines even when audio is blocked or absent."""
        now = time.monotonic() if now is None else now
        if not self.active:
            return
        transition = (self.mode, self.phase)
        if transition != self.last_transition:
            print(f'[GPT STATE] {self.mode}/{self.phase}', flush=True)
            self.last_transition = transition
        # Keep servicing the guard even without admissible microphone frames.
        silent = False if self.closed_confirmed else self.guard.allows(now)
        if not self.closed_confirmed:
            if silent or self.guard.available(now):
                self.blocked_since = None
            elif self.blocked_since is None:
                self.blocked_since = now
            prolonged = (self.blocked_since is not None and
                         now - self.blocked_since >= self.cfg.get('speaker_block_timeout', 60))
            if getattr(self.guard, 'failed', False) is True or prolonged:
                if not self.emergency:
                    print('[GPT ERROR] Speaker protection unavailable too long; closing GPT.', flush=True)
                self.emergency = True
                self.interrupt = False
                self.chunks.clear()
        waiting_for_control = (self.phase == 'control' or
                               (self.phase in ('speaking', 'cooldown') and
                                self.after_speech == 'control'))
        if waiting_for_control:
            if self.control_wait_started is None:
                self.control_wait_started = now
            if now - self.control_wait_started >= (self.cfg['command_timeout'] +
                                                    self.cfg.get('speaker_block_timeout', 60)):
                if not self.emergency:
                    print('[GPT ERROR] Return-command recovery window expired; closing GPT.', flush=True)
                self.emergency = True
        else:
            self.control_wait_started = None
        if self.job is not None:
            if self.job_started is None:
                self.job_started = now
            if now - self.job_started >= self.cfg.get('gpt_job_timeout', 90):
                self.emergency = True
                if not self.job_warned:
                    print('[GPT ERROR] UI job timed out; independent browser shutdown requested.', flush=True)
                    self.job_warned = True
        if self.phase in ('transcribing', 'reply') and now >= self.deadline:
            # Check the operation deadline before consuming a late poll result.
            # Otherwise a delayed transcription could still trigger Send.
            if not self.emergency:
                print('[GPT ERROR] Transcription/reply deadline expired; closing GPT.', flush=True)
            self.emergency = True
        if self.job is not None and self.job.done():
            job, action = self.job, self.action
            self.job = None
            try:
                result = job.result()
            except Exception as exc:
                result = {'ok': False, 'detail': type(exc).__name__}
            # Drain the Future, but never turn an emergency poll result into Send.
            if action == 'close' or not self.emergency:
                self.completed(result, action, now)
            if self.phase == 'speaking':
                return
        if self.emergency and not self.closed_confirmed:
            if self.job is not None and self.action != 'close':
                if self.action == 'send':
                    print('[GPT ERROR] Send outcome is unknown; no automatic retry.', flush=True)
                # Bridge keeps the interrupted Future pending until the owned
                # worker is dead. Closing uses its independent supervisor.
                self.job = None
            if self.job is None and self.phase != 'error_wait':
                self.submit('close', 'closing')
            return
        if self.phase == 'speaking' and not self.speech.busy():
            self.phase = 'cooldown'
            self.deadline = now + max(.8, self.cfg.get('tts_cooldown_ms', 500) / 1000)
            return
        # Once browser closure is confirmed only our own TTS and its echo remain.
        if self.phase == 'cooldown' and now >= self.deadline and (self.closed_confirmed or silent):
            self.accept_after = now
            self.rec.Reset()
            self.after(self.after_speech, now)
            return
        if self.phase == 'control':
            if self.control_tick is not None and not (silent and self.control_allowed):
                self.deadline += max(0, now - self.control_tick)
            self.control_tick, self.control_allowed = now, silent
        if self.phase == 'control' and now >= self.deadline:
            if self.mode == ANTON_MODE and self.resume_phase in ('reply', 'transcribing'):
                self.phase = self.resume_phase
                self.deadline = now + self.cfg.get('reply_timeout', 180)
            else:
                self.after('resume' if self.mode == LARISA_MODE else 'dictate', now)
        if self.job is None and self.phase == 'dictating' and now >= self.deadline:
            if self.heard:
                self.submit('transcribe', 'stopping_dictation')
            else:
                self.fail('Не обнаружена речь во время диктовки.', now)
        if self.job is None and self.phase in ('transcribing', 'reply', 'voice'):
            if self.phase != 'voice' and now >= self.deadline:
                self.fail('Истекло время ожидания транскрипции или ответа.', now)
            elif now >= self.poll_after:
                self.poll_after = now + 1
                self.submit('poll')

    def step(self, pcm, captured, now=None):
        now = time.monotonic() if now is None else now
        if not self.active:
            return
        self.tick(now)
        if self.emergency or self.phase in ('speaking', 'cooldown', 'idle', 'closing'):
            return
        if captured <= self.accept_after or not 0 <= now - captured <= .4:
            self.audio_gap(captured)
            return
        silent = self.guard.allows(now)
        if not silent:
            self.rec.Reset()
            self.preroll.clear()
            self.mic_tail = 0
            self.accept_after = now
            return
        # Use the same inexpensive silence/preroll strategy as LOCAL_MODE.
        # Silence still advances dictation and reply timers; it need not run Vosk.
        samples = array('h', pcm)
        rms = math.sqrt(sum(x*x for x in samples)/len(samples)) if samples else 0
        self.preroll.append(pcm)
        rec_pcm = pcm
        if rms >= self.cfg['silence_rms']:
            if not self.mic_tail:
                self.rec.Reset()
                rec_pcm = b''.join(self.preroll)
            self.mic_tail = 12
        elif self.mic_tail:
            self.mic_tail -= 1
        final, text = False, ''
        if self.mic_tail:
            final = self.rec.AcceptWaveform(rec_pcm)
            result = json.loads(self.rec.Result() if final else self.rec.PartialResult())
            text = result.get('text' if final else 'partial', '').strip().lower()
        if self.phase == 'control':
            if final and text in RETURNS:
                self.submit('close', 'closing')
                return
        elif text == 'михаил борисович' and self.phase not in ('closing','pausing'):
            self.rec.Reset()
            self.resume_phase = self.phase
            self.chunks = []
            if self.phase == 'error_wait':
                self.say('Слушаю', 'control', now)
            elif self.phase == 'dictating':
                self.submit('abort_dictation', 'pausing')
            elif self.job is not None:
                self.interrupt = True
            else:
                self.submit('pause', 'pausing')
            return
        if self.job is not None:
            return
        if self.phase == 'dictating':
            if rms >= self.cfg.get('dictation_rms', 250):
                self.heard = True
                self.last_sound = now
            if self.heard and (now-self.last_sound >= self.cfg.get('dictation_silence_ms', 1400)/1000 or now >= self.deadline):
                self.submit('transcribe', 'stopping_dictation')
            elif now >= self.deadline:
                self.fail('Не обнаружена речь во время диктовки.', now)
