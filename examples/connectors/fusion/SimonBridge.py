"""Fusion add-in: timer posts events; all API work happens on Fusion's UI thread."""

import os
import threading

import adsk.core
from creative_host import pump

EVENT_ID = "Simon.NativeBridge.Tick"
app = adsk.core.Application.get()
stop_event = threading.Event()
handler = None
thread = None


class Handler(adsk.core.CustomEventHandler):
    def notify(self, args):
        if stop_event.is_set():
            return
        try:
            pump(os.environ["SIMON_CREATIVE_MAILBOX"], "fusion")
        except Exception:
            stop_event.set()


def run(context):
    global handler, thread
    stop_event.clear()
    handler = Handler()
    app.registerCustomEvent(EVENT_ID).add(handler)

    def schedule():
        while not stop_event.wait(0.5):
            app.fireCustomEvent(EVENT_ID)

    thread = threading.Thread(target=schedule, daemon=True)
    thread.start()


def stop(context):
    stop_event.set()
    if thread is not None:
        thread.join(timeout=2)
    app.unregisterCustomEvent(EVENT_ID)
