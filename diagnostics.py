"""Best-effort lifecycle diagnostics; never a prerequisite for process cleanup."""
import os
from pathlib import Path
import sys
import traceback
import threading


output_lost = threading.Event()


def silence_broken_stream(stream):
    """Prevent another failed flush at interpreter exit; process-local fd only."""
    try:
        fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(fd, stream.fileno())
        finally:
            os.close(fd)
    except (OSError, ValueError, AttributeError):
        pass


def cleanup_log(*args, **kwargs):
    """A closed tee/file/terminal must not abort terminate/wait/reap."""
    try:
        print(*args, **kwargs)
        return True
    except (OSError, ValueError):
        output_lost.set()
        silence_broken_stream(kwargs.get('file') or sys.stdout)
        return False


def ui_failure(action, exc):
    """Frames only: no exception values, source lines, locals or UI contents."""
    descriptions = {TypeError: 'incompatible argument or value type',
                    RuntimeError: 'operation could not complete'}
    description = descriptions.get(type(exc), 'operation raised an exception')
    lines = [f'[GPT UI ERROR] action={action} type={type(exc).__name__}: {description}; '
             'exception values omitted', 'Traceback (frames only):']
    for frame, lineno in traceback.walk_tb(exc.__traceback__):
        lines.append(f'  File "{frame.f_code.co_filename}", line {lineno}, in {frame.f_code.co_name}')
    text = '\n'.join(lines)+'\n'
    # Local log remains useful even after tee exits. Never log exc/args/locals.
    try:
        path = Path(__file__).resolve().parent / 'logs' / 'browser-errors.log'
        path.parent.mkdir(exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'w') as log:
            log.write(text)
    except OSError:
        pass
    cleanup_log(text, end='', flush=True)
