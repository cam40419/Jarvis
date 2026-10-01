"""Run in Houdini, Rhino 8, Cinema 4D, or a dedicated SOLIDWORKS Python worker.

Copy src/simon/creative_host.py beside this file. The runner sets
SIMON_CREATIVE_MAILBOX and SIMON_CREATIVE_APPLICATION before starting the host.
Keep the returned callback/dialog alive for the duration of the session.
"""

import os
import time

from creative_host import pump

MAILBOX = os.environ["SIMON_CREATIVE_MAILBOX"]
APPLICATION = os.environ["SIMON_CREATIVE_APPLICATION"]
last_poll = 0.0
stopped = False


def tick(*_args):
    global last_poll, stopped
    if stopped or time.monotonic() - last_poll < 0.5:
        return
    last_poll = time.monotonic()
    try:
        pump(MAILBOX, APPLICATION)
    except Exception:
        stopped = True
        print("Simon host stopped; reconcile the runner session before restarting")


def start():
    if APPLICATION == "houdini":
        import hou

        hou.ui.addEventLoopCallback(tick)
        return tick
    if APPLICATION == "rhino":
        import Rhino

        Rhino.RhinoApp.Idle += tick
        return tick
    if APPLICATION == "cinema4d":
        import c4d

        class BridgeDialog(c4d.gui.GeDialog):
            def CreateLayout(self):
                self.SetTitle("Simon native bridge — close to stop")
                self.SetTimer(500)
                return True

            def Timer(self, message):
                tick()

        dialog = BridgeDialog()
        dialog.Open(c4d.DLG_TYPE_ASYNC, defaultw=320, defaulth=80)
        return dialog
    if APPLICATION == "solidworks":
        import pythoncom

        pythoncom.CoInitialize()
        try:
            while not stopped:
                tick()
                pythoncom.PumpWaitingMessages()
                time.sleep(0.1)
        finally:
            pythoncom.CoUninitialize()
        return None
    raise ValueError("Use the Fusion add-in or Adobe UXP panel for this host")


if __name__ == "__main__":
    bridge = start()
