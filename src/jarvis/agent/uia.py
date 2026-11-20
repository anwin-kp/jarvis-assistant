"""One thread for all app automation (pywinauto / UI Automation).

COM gives every thread one "apartment" mode, fixed by whatever touches COM first on it. pywinauto (comtypes)
wants multithreaded mode; in the running app the main thread is already in the other mode, so importing
pywinauto there fails: "Cannot change thread mode after it is set". All UI automation therefore runs on this
dedicated thread, which is set up for pywinauto before anything else touches it."""

import functools
import sys
from concurrent.futures import ThreadPoolExecutor


def _init() -> None:
    sys.coinit_flags = 0  # COINIT_MULTITHREADED, what pywinauto expects
    import pythoncom

    try:
        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
    except pythoncom.com_error:
        pass  # already initialized the same way
    import pywinauto  # noqa: F401  (comtypes initializes COM for this thread at import)


_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="uia", initializer=_init)


def run(fn, *args, **kwargs):
    """Run fn on the automation thread and wait for it (calls from that thread run directly)."""
    import threading

    if threading.current_thread().name.startswith("uia"):
        return fn(*args, **kwargs)
    return _pool.submit(fn, *args, **kwargs).result()


def on_uia_thread(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return run(fn, *args, **kwargs)

    return wrapper
