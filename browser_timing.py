"""One startup budget shared by the UI and both independent watchdogs."""
import math

STARTUP_ACTIONS = frozenset(('open', 'open_anton'))


def job_timeout(cfg, action):
    if action not in STARTUP_ACTIONS:
        return cfg.get('gpt_job_timeout', 90)
    seconds = float(cfg.get('gpt_startup_timeout', 240))
    if not math.isfinite(seconds) or not 0 < seconds <= 600:
        raise ValueError('gpt_startup_timeout must be finite, positive and at most 600 seconds')
    return seconds
