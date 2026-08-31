#!/usr/bin/env python3
"""Fake ignition for BENCH testing (never in a car): publish pandaStates with ignitionLine=true so
hardwared flips the device onroad — cameras record, loggerd writes real segments, the Drives live UI can
be developed without driving. Stop the script -> ignition drops -> offroad. With no panda connected the
real pandad never publishes, so this is the only pandaStates publisher.

Usage (on the device):
  tmux new-session -d -s fakeign \
    'cd /data/openpilot && PYTHONPATH=/data/openpilot /usr/local/venv/bin/python3 sunnyconf/tools/fake_ignition.py'
  tmux kill-session -t fakeign     # stop -> back offroad

Watch the disk: bench recording writes ~230 MB/min (3x HD cameras); the deleter starts deleting the OLDEST
recordings below 10% free / 5 GB free.
"""
import time

import cereal.messaging as messaging
from cereal import log


def main():
  pm = messaging.PubMaster(['pandaStates'])
  print("publishing fake ignition (Ctrl+C to stop)...")
  while True:
    msg = messaging.new_message('pandaStates', 1)
    ps = msg.pandaStates[0]
    ps.pandaType = log.PandaState.PandaType.tres
    ps.ignitionLine = True
    ps.harnessStatus = log.PandaState.HarnessStatus.normal
    pm.send('pandaStates', msg)
    time.sleep(0.1)


if __name__ == "__main__":
  main()
