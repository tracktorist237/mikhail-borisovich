"""Replay-only provenance. Stores routing metadata, never PCM in serialized trace."""
from collections import deque
import sys
import main


class ReplayTrace:
    def __init__(self, runtime):
        self.rt = runtime
        self.events = []
        self.state = 'speaking'
        self.blocks = {}
        self.recent = deque(maxlen=3)
        self.next_recognizer = 0

    def emit(self, event, **values):
        # Read only state names from the suspended real loop, never serialize
        # locals, PCM or GPT content. Needed for cooldown/clear_audio transitions.
        frame = sys._getframe(1)
        while frame:
            if frame.f_code.co_name == 'listen' and frame.f_code.co_filename == main.__file__:
                gpt = frame.f_locals.get('gpt')
                state = (gpt.mode+'/'+gpt.phase if gpt and gpt.active else frame.f_locals.get('state',self.state))
                if state != self.state:
                    self.state = state
                    self.events.append({'event':'state_transition','time':round(self.rt.monotonic(),6),'state':state})
                break
            frame=frame.f_back
        del frame
        self.events.append({'event': event, 'time': round(self.rt.monotonic(), 6),
                            'state': self.state, **values})

    def source(self, block):
        return self.rt.source_fragments(block)

    def capture(self, block):
        self.blocks[block]={'block':block, 'capture_start':self.rt.audio_epoch+block*.1,
                            'capture_end':self.rt.audio_epoch+(block+1)*.1,
                            'sources':self.source(block)}
        self.emit('capture',**self.blocks[block])

    def observe(self, event, fields):
        if event=='local_state': self.state=fields['state']
        elif event=='speech_started': self.state='speaking'
        elif event=='mode_changed': self.state=fields['mode']+'/'+fields['phase']
        self.emit(event,**fields)

    def install_queue(self, callback):
        # Same queue instance and methods; observational wrappers only. The
        # callback's queue is closed over by production listen(), not replaced.
        cells=dict(zip(callback.__code__.co_freevars, callback.__closure__ or ()))
        if 'audio' not in cells: raise AssertionError('Production callback queue seam changed')
        q=cells['audio'].cell_contents
        put,get,trim=q.put_latest,q.get,q.trim_wake
        def block_of(item):return round((item[1].start-self.rt.audio_epoch)*16000)//1600
        def put_latest(item,discontinuity=False):
            old=list(q.items)
            result=put(item,discontinuity)
            if result:self.emit('queue_drop',reason='overflow',block=block_of(old[0]))
            self.emit('queue_insert',block=block_of(item),depth=len(q.items))
            return result
        def get_item(timeout=None):
            item=get(timeout)
            self.recent.append((item[1].pcm,block_of(item)))
            self.emit('queue_remove',block=block_of(item),gap=item[2],depth=len(q.items))
            return item
        def trim_wake(*args,**kwargs):
            old=list(q.items);result=trim(*args,**kwargs)
            retained={id(x[1]) for x in q.items}
            for item in old:
                if id(item[1]) not in retained:self.emit('queue_drop',reason='trim_wake',block=block_of(item))
            return result
        q.put_latest,q.get,q.trim_wake=put_latest,get_item,trim_wake

    def route(self, pcm, rec_id, generation, role):
        count=len(pcm)//3200
        candidates=list(self.recent)[-count:]
        if count<1 or len(pcm)%3200 or b''.join(x[0] for x in candidates)!=pcm:
            raise AssertionError('Cannot prove recognizer input provenance')
        blocks=[x[1] for x in candidates]
        self.emit('asr_input',recognizer=rec_id,generation=generation,role=role,blocks=blocks)
        return blocks
