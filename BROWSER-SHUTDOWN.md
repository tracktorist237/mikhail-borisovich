Browser shutdown uses `browser_close_timeout` in `config.json` (12 seconds).
This is one wall-clock budget starting when closing is requested, separate
from the existing timeout of a normal UI operation (`gpt_job_timeout`, 90 s).

`Bridge.submit('close')` is asynchronous. `Bridge.close()` is the bounded
final-cleanup interface. Only ChatGPTUI runs in the UI child process;
Vosk, RHVoice and the speaker monitor remain in the main application.

A separate Linux supervisor becomes a child subreaper before starting that
UI child. It does not import Selenium or call WebDriver. It owns and reaps
the UI child, geckodriver, Firefox and orphaned descendants, including those
created before the WebDriver constructor returns or in separate sessions.
Signals target verified descendants via pidfds, never process names.

The first half of the remaining budget permits normal UI close. The next
20% permits SIGTERM; the final 30% permits SIGKILL, reaping and IPC/controller
exit. A close result is successful only after all descendants and the
supervisor have exited. The synchronous close also joins its controller
thread. No profile or lock files are deleted.

Closing blocks new actions and drops late results. Interrupted job Futures
are completed only after the worker has exited. Interrupted Send has an
unknown outcome and is never automatically retried. Generation and request
IDs keep old replies out of later sessions.

Linux cannot guarantee immediate termination of an uninterruptible kernel
task (D state), even with SIGKILL. On a budget overrun, close reports an error
and keeps LOCAL_MODE/new sessions blocked. The supervisor remains as reaper;
the original job stays pending until actual cleanup. Unexpected supervisor
loss also fails closed: descendant cleanup cannot then be certified.

Checks (sequential; local browser fixtures do not access ChatGPT):

```
timeout -k 5s 150s .venv/bin/python -m unittest discover -v
timeout -k 10s 240s env MB_BROWSER_TEST=1 .venv/bin/python -m unittest test_browser_dom -v
```

Process tests synchronize via Unix sockets/pipes, include SIGTERM-resistant
descendants and a separate application which must actually exit. The normal
unit run skips the opt-in browser fixtures. Logs for this change are in
`logs/bridge-before.log`, `logs/unit-browser-shutdown-final.log`,
`logs/browser-dom-shutdown.log` and `logs/browser-lifecycle-forced.log`
(ignored by Git). The last fixture also blocks quit with real Firefox alive;
it checks the independent termination path and the browser's ownership tree.
