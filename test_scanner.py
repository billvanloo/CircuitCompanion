#!/usr/bin/env python3
"""
test_scanner.py — exercise the library scanner against a simulated Circuit.

The scanner is the one part of this program that changes state on the
instrument, so it should be tested here before being tested on hardware.

    python3 test_scanner.py

Covers:
  * clean scan — all 64 slots read, synth returned to its original slot
  * unsaved edits — no slot matches, so the live sound is written back
  * flaky device — dropped replies are retried, then left marked unknown
"""

import os
import sys
import time

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import mido
import pygame

import circuit_patch as cp
import circuit_display as cd


def make_patch(i):
    """A minimal but structurally valid 340-byte patch payload."""
    p = [0] * cp.PATCH_DATA_LEN
    p[0:16] = list(f"Patch {i + 1}".ljust(16).encode())
    p[16] = i % 15                       # category
    p[cp.MACRO_BASE] = i % 128           # something that differs per slot
    return tuple(p)


LIB = [make_patch(i) for i in range(64)]


class FakeCircuit:
    """
    Stands in for both the input and output MIDI port.

    Answers a Current Patch Dump Request with whatever is currently loaded,
    and loads a new patch when it receives a Program Change — which is exactly
    the behaviour the scanner depends on.
    """

    def __init__(self, start_slot, edited=False, drop_slots=()):
        self.slot = start_slot
        self.live = list(LIB[start_slot])
        if edited:                       # simulate an unsaved tweak
            self.live[cp.MACRO_BASE] = 99
        self.drop_slots = set(drop_slots)
        self.out = []

    def send(self, msg):
        if msg.type == "program_change":
            self.slot = msg.program
            self.live = list(LIB[msg.program])
        elif msg.type == "sysex":
            d = msg.data
            if d[5] == cp.CMD_DUMP_REQUEST:
                if self.slot in self.drop_slots:
                    return               # pretend the message was missed
                self.out.append(mido.Message(
                    "sysex",
                    data=cp.CIRCUIT_HEADER + (cp.CMD_REPLACE_CURRENT, d[6], 0) + tuple(self.live)))
            elif d[5] == cp.CMD_REPLACE_CURRENT:
                self.live = list(cp.patch_bytes(d))

    def iter_pending(self):
        while self.out:
            yield self.out.pop(0)

    def close(self):
        pass


def run_scan(fake, start_slot, max_steps=400_000):
    """Drive App + Scanner on a fake clock until the scan finishes."""
    app = cd.App()
    app.inport = app.outport = fake
    app.current_slot = [start_slot, None]
    app.library.save = lambda: None      # keep the test off disk
    app.start_scan()

    t, steps = time.monotonic(), 0
    while app.scanning and steps < max_steps:
        t += 0.005
        steps += 1
        for m in fake.iter_pending():
            app.handle(m, t)
        if app.scanning:
            app.scanner.tick(t)
    assert steps < max_steps, "scanner did not terminate"
    return app


def test_clean():
    fake = FakeCircuit(21)
    app = run_scan(fake, 21)
    sc = app.scanner
    assert sc.phase == "done", sc.phase
    assert all(d is not None for d in sc.data), "some slots were not read"
    assert fake.slot == 21, f"left the synth on slot {fake.slot}, expected 21"
    assert app.library.entry(0, 0)["name"] == "Patch 1"
    assert app.library.entry(0, 63)["name"] == "Patch 64"
    assert sc.note == "", f"unexpected warning: {sc.note}"
    print("clean scan            OK  (64/64 read, returned to slot 22)")


def test_unsaved_edits():
    fake = FakeCircuit(21, edited=True)
    app = run_scan(fake, 21)
    sc = app.scanner
    assert sc.phase == "done"
    assert fake.slot == 21, f"left the synth on slot {fake.slot}"
    assert fake.live[cp.MACRO_BASE] == 99, "the edited sound was not written back"
    assert sc.note, "should warn that the slot indicator may be wrong"
    print("unsaved edits         OK  (sound restored, mismatch flagged)")


def test_flaky_device():
    fake = FakeCircuit(3, drop_slots=(5, 6))
    app = run_scan(fake, 3)
    sc = app.scanner
    missing = [i for i, d in enumerate(sc.data) if d is None]
    assert sc.phase == "done", sc.phase
    assert missing == [5, 6], f"expected slots 5,6 unknown, got {missing}"
    assert fake.slot == 3, f"left the synth on slot {fake.slot}"
    print("dropped replies       OK  (retried, then left slots 6,7 unknown)")


if __name__ == "__main__":
    pygame.init()
    pygame.display.set_mode((cd.W, cd.H))
    test_clean()
    test_unsaved_edits()
    test_flaky_device()
    pygame.quit()
    print("\nall scanner tests passed")
