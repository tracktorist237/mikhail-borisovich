"""Linux browser process boundary and independent descendant reaper.

Only the UI child imports Selenium. The supervisor never calls WebDriver.
Its subreaper boundary also owns children orphaned during driver construction,
including descendants which create their own sessions/process groups.
"""
from diagnostics import cleanup_log, ui_failure
from contextlib import nullcontext
import ctypes
import importlib
import json
import os
from pathlib import Path
import select
import signal
import socket
import sys
import time


class Channel:
    """Nonblocking local IPC; no feeder or reader threads."""
    def __init__(self, sock):
        self.sock = sock
        sock.setblocking(False)
        self.incoming = bytearray()
        self.outgoing = bytearray()
        self.eof = False

    def send(self, message):
        self.outgoing.extend(json.dumps(message, ensure_ascii=False).encode() + b'\n')

    def pump(self, timeout=0):
        if self.eof:
            if timeout:
                select.select([], [], [], timeout)
            return []
        ready, writable, _ = select.select([self.sock], [self.sock] if self.outgoing else [], [], timeout)
        try:
            if writable:
                sent = self.sock.send(self.outgoing)
                del self.outgoing[:sent]
            if ready:
                data = self.sock.recv(65536)
                if not data:
                    self.eof = True
                self.incoming.extend(data)
        except (BrokenPipeError, ConnectionResetError):
            self.eof = True
        if len(self.incoming) > 16 * 1024 * 1024:
            raise RuntimeError('Browser IPC message exceeds limit')
        messages = []
        while b'\n' in self.incoming:
            line, _, tail = self.incoming.partition(b'\n')
            self.incoming = bytearray(tail)
            messages.append(json.loads(line))
        return messages


def perform(ui, action, startup_deadline=None):
    try:
        if action in ('open', 'open_anton'):
            startup = getattr(type(ui), 'startup', None)
            with startup(ui, startup_deadline) if startup else nullcontext():
                if action == 'open':
                    return ui.start_voice()
                ui.open()
                return {'ok': True, 'detail': 'Текстовый ChatGPT готов.'}
        method = {'close': 'close', 'pause': 'pause', 'abort_dictation': 'abort_dictation',
                  'resume': 'resume_voice', 'dictate': 'start_dictation',
                  'transcribe': 'stop_dictation', 'send': 'send', 'poll': 'poll'}[action]
        return getattr(ui, method)()
    except Exception as exc:
        ui_failure(action, exc)
        # Do not add conversation contents to lifecycle diagnostics.
        return {'ok': False, 'outcome_unknown': action == 'send',
                'detail': ('Send outcome is unknown; no automatic retry. ' if action == 'send' else '')
                          + f'UI operation failed: {type(exc).__name__}; action={action}; see logs/browser-errors.log'}


def ui_worker(sock, cfg, factory):
    sock.setblocking(True)
    reader = sock.makefile('rb')
    module, name = factory.split(':')
    ui = getattr(importlib.import_module(module), name)(cfg)
    for line in reader:
        request = json.loads(line)
        result = perform(ui, request['action'], request.get('startup_deadline'))
        sock.sendall(json.dumps({'id': request['id'], 'result': result}, ensure_ascii=False).encode() + b'\n')
        if request['action'] == 'close':
            return


def process_identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return int(fields[1]), int(fields[19]), fields[0]  # PPID, start ticks, state
    except (FileNotFoundError, ProcessLookupError):
        return None


def descendants(root):
    """Walk only this supervisor's tree, including children of non-main threads."""
    found = {}
    pending = [root]
    while pending:
        parent = pending.pop()
        for task in Path(f'/proc/{parent}/task').glob('*'):
            try:
                children = (task / 'children').read_text().split()
            except (FileNotFoundError, ProcessLookupError):
                continue
            for raw in children:
                pid = int(raw)
                identity = process_identity(pid)
                if identity and identity[0] == parent and pid not in found:
                    found[pid] = identity
                    pending.append(pid)
    return found


def signal_owned(root, sig):
    for pid, identity in reversed(list(descendants(root).items())):
        try:
            fd = os.pidfd_open(pid)
            try:
                # Pin identity across PID reuse, then signal through the pidfd.
                current = process_identity(pid)
                parent = current[0] if current else 0
                seen = set()
                while parent not in (0, root) and parent not in seen:
                    seen.add(parent)
                    ancestor = process_identity(parent)
                    parent = ancestor[0] if ancestor else 0
                if current and current[1] == identity[1] and parent == root:
                    signal.pidfd_send_signal(fd, sig)
            finally:
                os.close(fd)
        except (ProcessLookupError, PermissionError):
            pass


def reap():
    """ECHILD proves there are no owned descendants left under the subreaper."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return True
        if pid == 0:
            return False


def supervise(parent_socket):
    # PR_SET_CHILD_SUBREAPER: orphaned browser children are adopted here,
    # never mixed with RHVoice, PipeWire or the user's unrelated Firefox.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'Cannot establish browser subreaper')
    parent = Channel(parent_socket)
    initial = []
    while not initial and not parent.eof:
        initial = parent.pump(.05)
    if not initial:
        return
    setup = initial[0]
    cfg = setup['cfg']
    budget = cfg.get('browser_close_timeout', 12)
    supervisor_end, child_end = socket.socketpair()
    pid = os.fork()
    if pid == 0:
        parent_socket.close()
        supervisor_end.close()
        try:
            ui_worker(child_end, cfg, setup['factory'])
        except BaseException as exc:
            ui_failure('worker startup/IPC', exc)
            os._exit(1)
        os._exit(0)  # UI threads cannot keep this isolated process alive.
    child_end.close()
    worker = Channel(supervisor_end)
    deadline = None
    normal_until = term_until = 0
    forced = False
    expired_reported = False
    shutdown_stage = 'normal'
    root = os.getpid()
    queued = initial[1:]

    def begin_close(end):
        nonlocal deadline, normal_until, term_until
        if deadline is not None:
            return
        now = time.monotonic()
        deadline = end
        remaining = max(0, end - now)
        normal_until = now + remaining * .5
        term_until = now + remaining * .7
        worker.send({'id': 0, 'action': 'close'})
        parent.send({'type': 'closing', 'deadline': end})
        cleanup_log(f'[GPT CLOSE] normal cleanup; remaining budget {remaining:.2f}s', flush=True)

    try:
        while True:
            try:
                incoming = parent.pump(.02)
            except (OSError, ValueError, RuntimeError):
                parent.eof = True
                incoming = []
            for message in queued + incoming:
                if message['type'] == 'close':
                    begin_close(message['deadline'])
                elif deadline is None:
                    worker.send(message)
            queued = []
            try:
                replies = worker.pump(0)
            except (OSError, ValueError, RuntimeError):
                worker.eof = True
                replies = []
            for message in replies:
                if deadline is None:
                    parent.send({'type': 'result', **message})
            if (parent.eof or worker.eof) and deadline is None:
                begin_close(time.monotonic() + budget)
            no_children = reap()
            if deadline is not None:
                now = time.monotonic()
                if no_children:
                    cleanup_log('[GPT CLOSE] all owned descendants reaped', flush=True)
                    parent.send({'type': 'closed', 'ok': True, 'forced': forced,
                                 'detail': 'Owned browser processes exited and were reaped.'})
                    # Reserve the last part of the parent's budget for IPC/exit.
                    while parent.outgoing and not parent.eof and time.monotonic() < deadline:
                        parent.pump(.01)
                    return
                if now >= normal_until:
                    forced = True
                    stage = 'kill' if now >= term_until else 'terminate'
                    if stage != shutdown_stage:
                        cleanup_log(f'[GPT CLOSE] {stage} owned descendants', flush=True)
                        shutdown_stage = stage
                    signal_owned(root, signal.SIGKILL if stage == 'kill' else signal.SIGTERM)
                if now >= deadline - .1 and not expired_reported:
                    remaining = descendants(root)
                    parent.send({'type': 'closed', 'ok': False,
                                 'detail': f'Browser cleanup unconfirmed; remaining PID/state: '
                                           f'{[(p, info[2]) for p, info in remaining.items()]}'})
                    expired_reported = True
                    # Stay as reaper if a kernel task is uninterruptible. Never
                    # report success or abandon ownership just to meet a timer.
    finally:
        supervisor_end.close()
        parent_socket.close()


if __name__ == '__main__':
    supervise(socket.socket(fileno=int(sys.argv[1])))
