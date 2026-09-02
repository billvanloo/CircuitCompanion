#!/usr/bin/env python3
"""
circuit_display.py — macro knob companion display for the Novation Circuit.

Two views:
  Macros   — what the eight knobs are wired to on the current patch.
  Library  — a full-screen overlay showing all 64 patch slots by name.

v1 only reads: it asks the Circuit for patch dumps, and the library scan sends
program changes to step through slots. Nothing else is written. That is a
scoping choice rather than a rule — selecting patches and drum samples from
here is a planned later phase. See CLAUDE.md.

Usage:
  python3 circuit_display.py                # window, real Circuit over USB
  python3 circuit_display.py --fullscreen   # for the 800x480 panel on the Pi
  python3 circuit_display.py --demo         # no Circuit needed; keys simulate knobs

Keys:  1 / 2   show Synth 1 / 2          L    open / close the library
       R       re-read both patches      S    scan this synth's library
       F       fullscreen                Esc  close library, or quit
  demo only:   1-8 turn Synth 1 knobs, Q W E R T Y U I turn Synth 2 (Shift = down)

Setup (macOS):  pip install -r requirements.txt
"""

import argparse
import json
import math
import sys
import time
from pathlib import Path

import pygame

import circuit_patch as cp

# ---- Behaviour knobs ----------------------------------------------------------

TAB_DWELL_S = 0.50        # min time between automatic tab switches
LIVE_GLOW_S = 0.90        # how long a moved knob stays highlighted
NRPN_REDUMP_S = 1.00      # throttle for re-reading a patch after Components edits
DUMP_SPACING_S = 0.03     # >= 20 ms between sysex sends (per the reference guide)
RECONNECT_S = 2.0
PC_DUMP_DELAY_S = 0.02    # wait for patch load after a program change (guide: 10 ms)

SCAN_STEP_S = 0.035       # per-slot pacing during a library scan
SCAN_TIMEOUT_S = 0.60     # give up on a slot, retry once, then move on
LIBRARY_FILE = Path(__file__).with_name("library.json")

# ---- Look ---------------------------------------------------------------------

W, H = 800, 480
TABS_H, FOOT_H = 58, 26
CARD_W, CARD_H = 157, 188
CARD_POS = {1: (18, 8), 3: (191, 8), 5: (364, 8), 7: (537, 8),
            2: (104, 202), 4: (277, 202), 6: (450, 202), 8: (623, 202)}

BG, PANEL, LINE = (12, 12, 15), (20, 20, 24), (38, 38, 45)
TEXT, MUTED = (236, 236, 240), (125, 125, 137)
FAINT, GHOST = (85, 85, 95), (67, 67, 76)
S1, S1_DIM = (255, 60, 174), (90, 31, 67)
S2, S2_DIM = (53, 227, 163), (26, 90, 68)
AMBER = (224, 162, 74)
ACCENT = {0: S1, 1: S2}
ACCENT_DIM = {0: S1_DIM, 1: S2_DIM}

FONT_FAMILIES = "helveticaneue,helvetica,arial,dejavusans,freesans"

CHIPS_H = 27
GRID_X, GRID_PAD_Y = 18, 6
CELL_W, CELL_H, CELL_GAP = 92, 40, 4
PAGE_GAP = 9


# ==============================================================================
#  Library cache
# ==============================================================================

class Library:
    """Patch names per synth, cached on disk between runs."""

    def __init__(self):
        self.slots = {0: [None] * 64, 1: [None] * 64}   # each: {"name", "category"}
        self.scanned_at = {0: None, 1: None}
        self.load()

    def load(self):
        try:
            raw = json.loads(LIBRARY_FILE.read_text())
        except Exception:
            return
        for loc in (0, 1):
            d = raw.get(str(loc)) or {}
            s = d.get("slots")
            if isinstance(s, list) and len(s) == 64:
                self.slots[loc] = s
                self.scanned_at[loc] = d.get("scanned_at")

    def save(self):
        try:
            LIBRARY_FILE.write_text(json.dumps(
                {str(loc): {"scanned_at": self.scanned_at[loc], "slots": self.slots[loc]}
                 for loc in (0, 1)}, indent=1))
        except Exception as e:
            print(f"Could not write {LIBRARY_FILE.name}: {e}", file=sys.stderr)

    def entry(self, loc, slot):
        e = self.slots[loc][slot]
        return e if isinstance(e, dict) else None

    def set(self, loc, slot, name, category):
        self.slots[loc][slot] = {"name": name, "category": category}

    def categories(self, loc):
        seen = {e["category"] for e in self.slots[loc]
                if isinstance(e, dict) and e.get("name")}
        return sorted(seen, key=cp.category_name)


# ==============================================================================
#  Library scanner
# ==============================================================================

class Scanner:
    """
    Reads all 64 patch names for one synth.

    There is no "send me every patch" message. The only documented way to see a
    stored patch is to load it (program change) and then ask for the current
    patch dump — so this genuinely changes the sound on the synth while it runs,
    which is why it is never automatic.

    Putting things back: the live sound is captured first, and afterwards the
    scan looks for the slot whose contents match it byte for byte and returns
    there. If nothing matches — the sound had unsaved edits — the captured bytes
    are written straight back into the live sound instead, and the user is told
    the slot number shown on the Circuit may not agree.
    """

    def __init__(self, app, loc):
        self.app, self.loc = app, loc
        self.phase = "capture"
        self.slot = 0
        self.before = None
        self.data = [None] * 64
        self.next_at = 0.0
        self.awaiting = False
        self.retried = False
        self.note = ""
        self.started = time.monotonic()

    @property
    def progress(self):
        return self.slot / 64

    @property
    def finished(self):
        return self.phase in ("done", "failed")

    def send_pc(self, slot):
        import mido
        self.app.outport.send(mido.Message("program_change", channel=self.loc, program=slot))

    def send_dump_request(self):
        import mido
        self.app.outport.send(mido.Message("sysex", data=cp.dump_request_bytes(self.loc)))

    # -- driven from the main loop ------------------------------------------------

    def tick(self, now):
        if self.app.outport is None:
            self.fail("Circuit disconnected")
            return
        if now < self.next_at:
            return

        if self.phase == "capture":
            self.send_dump_request()
            self.awaiting, self.phase = True, "capture_wait"
            self.next_at = now + SCAN_TIMEOUT_S

        elif self.phase == "step":
            self.send_pc(self.slot)
            self.phase, self.next_at = "step_request", now + PC_DUMP_DELAY_S

        elif self.phase == "step_request":
            self.send_dump_request()
            self.awaiting, self.phase = True, "step_wait"
            self.next_at = now + SCAN_TIMEOUT_S

        elif self.phase in ("capture_wait", "step_wait"):
            self.timeout(now)

        elif self.phase == "restore":
            self.do_restore(now)

    def timeout(self, now):
        if not self.retried:
            self.retried, self.awaiting = True, False
            self.phase = "capture" if self.phase == "capture_wait" else "step"
            self.next_at = now
            return
        if self.phase == "capture_wait":
            self.fail("no answer from the Circuit")
            return
        self.data[self.slot] = None          # leave this slot unknown
        self.advance(now)

    # -- fed by App.handle when a dump arrives --------------------------------------

    def receive(self, data, now):
        if not self.awaiting:
            return
        self.awaiting, self.retried = False, False
        payload = cp.patch_bytes(data)
        if self.phase == "capture_wait":
            self.before = payload
            self.phase, self.slot, self.next_at = "step", 0, now
        elif self.phase == "step_wait":
            self.data[self.slot] = payload
            patch = cp.decode_patch(data)
            self.app.library.set(self.loc, self.slot, patch["name"], patch["category"])
            self.advance(now)

    def advance(self, now):
        self.slot += 1
        if self.slot >= 64:
            self.phase, self.next_at = "restore", now
        else:
            self.phase, self.next_at = "step", now + SCAN_STEP_S

    # -- finish -----------------------------------------------------------------------

    def do_restore(self, now):
        import mido
        home = next((i for i, d in enumerate(self.data)
                     if d is not None and d == self.before), None)
        exact = home is not None
        if home is None:
            home = self.app.current_slot[self.loc]
        if home is not None:
            self.send_pc(home)
            self.app.current_slot[self.loc] = home
        if not exact and self.before is not None:
            time.sleep(DUMP_SPACING_S)
            self.app.outport.send(mido.Message(
                "sysex", data=cp.replace_current_patch_bytes(self.loc, self.before)))
            self.note = "sound restored, but the slot on the Circuit may not match"
        self.app.library.scanned_at[self.loc] = time.time()
        self.app.library.save()
        self.phase = "done"
        self.app.request_dump(self.loc, PC_DUMP_DELAY_S)
        self.app.last_msg = (f"Scanned Synth {self.loc + 1} in {now - self.started:.0f} s"
                             + (f" · {self.note}" if self.note else ""))

    def fail(self, why):
        self.phase, self.note = "failed", why
        self.app.last_msg = f"Scan stopped: {why}"


# ==============================================================================
#  State
# ==============================================================================

class SynthState:
    def __init__(self):
        self.patch = None
        self.values = [0] * 8
        self.touched = set()
        self.live_until = [0.0] * 8
        self.read_at = None


class App:
    def __init__(self, demo=False):
        self.demo = demo
        self.synths = [SynthState(), SynthState()]
        self.library = Library()
        self.current = 0
        self.current_slot = [None, None]
        self.overlay = False
        self.filter = None
        self.scanner = None
        self.chip_rects = []
        self.last_switch = 0.0
        self.last_msg = "Waiting for first knob move"
        self.inport = self.outport = None
        self.next_reconnect = 0.0
        self.send_queue = []
        self.last_send = 0.0
        self.nrpn_redump_at = [0.0, 0.0]
        self.nrpn_pending = [False, False]
        if demo:
            self._load_demo()

    # ---- MIDI --------------------------------------------------------------------

    def connect(self):
        import mido
        ins = [n for n in mido.get_input_names() if "circuit" in n.lower()]
        outs = [n for n in mido.get_output_names() if "circuit" in n.lower()]
        if not ins or not outs:
            return False
        self.inport = mido.open_input(ins[0])
        self.outport = mido.open_output(outs[0])
        for s in self.synths:
            s.touched.clear()
        self.request_dump(0)
        self.request_dump(1)
        return True

    def disconnect(self):
        for p in (self.inport, self.outport):
            try:
                p.close()
            except Exception:
                pass
        self.inport = self.outport = None

    @property
    def connected(self):
        return self.demo or self.inport is not None

    @property
    def scanning(self):
        return self.scanner is not None and not self.scanner.finished

    def request_dump(self, location, delay=0.0):
        if self.demo:
            return
        self.send_queue.append((time.monotonic() + delay, location))

    def pump_sends(self, now):
        if not self.outport or not self.send_queue:
            return
        if now - self.last_send < DUMP_SPACING_S:
            return
        for i, (t, loc) in enumerate(self.send_queue):
            if t <= now:
                import mido
                self.send_queue.pop(i)
                self.outport.send(mido.Message("sysex", data=cp.dump_request_bytes(loc)))
                self.last_send = now
                return

    def poll_midi(self, now):
        if self.demo:
            return
        if self.inport is None:
            if now >= self.next_reconnect:
                self.next_reconnect = now + RECONNECT_S
                try:
                    self.connect()
                except Exception as e:
                    self.last_msg = f"MIDI error: {e}"
            return
        try:
            for msg in self.inport.iter_pending():
                self.handle(msg, now)
        except Exception as e:
            self.last_msg = f"Lost connection: {e}"
            self.disconnect()
            return

        if self.scanning:
            self.scanner.tick(now)
            return

        self.pump_sends(now)
        for loc in (0, 1):
            if self.nrpn_pending[loc] and now >= self.nrpn_redump_at[loc]:
                self.nrpn_pending[loc] = False
                self.request_dump(loc)

    def handle(self, msg, now):
        t = msg.type

        if t == "sysex":
            if not cp.is_patch_dump(msg.data):
                return
            if self.scanning:
                self.scanner.receive(msg.data, now)
                return
            patch = cp.decode_patch(msg.data)
            loc = patch["location"]
            s = self.synths[loc]
            s.patch, s.read_at = patch, now
            for k in range(8):
                if k not in s.touched:
                    s.values[k] = patch["macros"][k]["position"]
            # keep the cached library honest as slots are visited
            slot = self.current_slot[loc]
            if slot is not None:
                e = self.library.entry(loc, slot)
                if (not e or e.get("name") != patch["name"]
                        or e.get("category") != patch["category"]):
                    self.library.set(loc, slot, patch["name"], patch["category"])
                    self.library.save()
            return

        if self.scanning:
            return                              # our own echo; ignore

        ch = getattr(msg, "channel", None)

        if t == "program_change":
            if ch in (0, 1):
                self.current_slot[ch] = msg.program
                self.synths[ch].touched.clear()
                self.request_dump(ch, PC_DUMP_DELAY_S)
                self.switch_to(ch, now, force=True)
                self.last_msg = f"Synth {ch + 1} · patch {msg.program + 1:02d}"
            elif ch == cp.SESSION_CHANNEL:
                for loc in (0, 1):
                    self.synths[loc].touched.clear()
                    self.current_slot[loc] = None
                    self.request_dump(loc, PC_DUMP_DELAY_S + loc * DUMP_SPACING_S)
                self.last_msg = "Session change"
            return

        if t == "control_change" and ch in (0, 1):
            c = msg.control
            if cp.MACRO_CC_FIRST <= c < cp.MACRO_CC_FIRST + 8:
                self.knob_moved(ch, c - cp.MACRO_CC_FIRST, msg.value, now)
            elif c in (6, 38, 98, 99):
                self.nrpn_pending[ch] = True
                self.nrpn_redump_at[ch] = max(self.nrpn_redump_at[ch], now + NRPN_REDUMP_S)

    # ---- Behaviour -------------------------------------------------------------------

    def knob_moved(self, synth, k, value, now):
        s = self.synths[synth]
        s.values[k] = value
        s.touched.add(k)
        s.live_until[k] = now + LIVE_GLOW_S
        self.switch_to(synth, now)
        self.last_msg = f"Synth {synth + 1} · knob {k + 1} · CC {cp.MACRO_CC_FIRST + k} = {value}"

    def switch_to(self, synth, now, force=False):
        if synth == self.current:
            return
        if force or now - self.last_switch >= TAB_DWELL_S:
            self.current, self.last_switch, self.filter = synth, now, None

    def start_scan(self):
        if self.demo:
            self.last_msg = "Demo mode: nothing to scan"
            return
        if self.outport is None or self.scanning:
            return
        self.send_queue.clear()
        self.scanner = Scanner(self, self.current)
        self.last_msg = f"Scanning Synth {self.current + 1} — the sound will change"

    # ---- Demo data ------------------------------------------------------------------------

    def _load_demo(self):
        def mk(name, cat, slots, positions, mm):
            p = [0] * cp.PATCH_DATA_LEN
            p[0:16] = list(name.ljust(16).encode())
            p[16] = cat
            for n, (s1, s2, d, dst) in mm.items():
                o = cp.MODMATRIX_BASE + (n - 1) * 4
                p[o:o + 4] = [s1, s2, d + 64, dst]
            for k in range(8):
                b = cp.MACRO_BASE + k * cp.MACRO_STRIDE
                p[b] = positions[k]
                for i, (dest, st, en, dp) in enumerate(slots[k]):
                    o = b + 1 + i * 4
                    p[o:o + 4] = [dest, st, en, dp + 64]
            return p

        chill = mk("Chill Waves", 9, [
            [(53, 0, 93, 1), (57, 120, 127, 3)],
            [(51, 0, 127, 51), (30, 47, 127, -37), (18, 0, 127, -60)],
            [(52, 0, 117, 63)],
            [(26, 64, 127, -24), (29, 0, 68, 26)],
            [(54, 0, 127, 29)],
            [(55, 0, 127, 43), (2, 0, 127, -2)],
            [(46, 0, 127, 33)],
            [(56, 0, 127, 15)],
        ], [0, 0, 17, 17, 10, 0, 0, 0],
            {1: (6, 0, 30, 12), 2: (11, 0, 20, 1), 3: (9, 0, 12, 12), 4: (7, 0, 25, 5),
             5: (4, 0, 40, 11), 6: (8, 0, 10, 14), 7: (12, 0, 33, 7)})
        init = mk("Initial Patch", 10, [
            [(6, 0, 50, 36), (7, 60, 127, 43), (53, 0, 127, -18), (21, 20, 127, 22)],
            [(51, 0, 127, 23)], [(33, 0, 127, 34)], [(52, 0, 127, -22)],
            [(54, 0, 127, 1)], [(45, 0, 127, 9)], [(1, 0, 127, 47)], [(46, 0, 127, 54)],
        ], [0, 97, 57, 0, 76, 0, 0, 30],
            {1: (7, 0, 20, 0), 2: (11, 0, 40, 12), 3: (9, 0, 15, 13), 4: (8, 0, 22, 8)})
        for loc, raw in ((0, chill), (1, init)):
            data = cp.CIRCUIT_HEADER + (0, loc, 0) + tuple(raw)
            patch = cp.decode_patch(data)
            s = self.synths[loc]
            s.patch, s.read_at = patch, time.monotonic()
            s.values = [m["position"] for m in patch["macros"]]
        self.current_slot = [34, 43]
        names = [
            ("Deep Sub",2),("Rubber Bass",2),("Acid Line",2),("Fat Saw Bass",2),("Wood Bass",2),
            ("Reese",2),("FM Thump",2),("Sub Drone",2),("Chill Waves",9),("Glass Pad",9),
            ("Slow Bloom",9),("Tape Choir",9),("Grain Wash",9),("Dust Pad",9),("Cold Front",9),
            ("Long Now",9),("Bright Lead",7),("Square Lead",7),("Portamento",7),("Detune Stack",7),
            ("Whistle",7),("Buzz Lead",7),("Thin Lead",7),("Octave Lead",7),("Bell Tine",3),
            ("Dark Bell",3),("Music Box",3),("Metal Strike",3),("Glass Bell",3),("Prayer Bell",3),
            ("Toy Piano",3),("Struck Rod",3),("Rhodes-ish",6),("Clav Stab",6),("Chill Waves",9),
            ("House Organ",6),("Wurli",6),("Dub Chord",6),("Stab Poly",10),("Warm Poly",10),
            ("Wide Poly",10),("Thin Poly",10),("Slow Strings",12),("Initial Patch",10),
            ("Bowed Wire",12),("Section",12),("Solo Violin",12),("Pizz",12),("Riser",11),
            ("Downlift",11),("Static",11),("Radio Ghost",11),("Vinyl Crackle",11),("Siren",11),
            ("Alarm",11),("Feedback",11),("Arp Pluck",1),("Arp Bells",1),("Arp Bass",1),
            ("Sequence 1",1),("Drift",8),("Slow Morph",8),("Pulse Field",8),("",0)]
        for loc in (0, 1):
            for i, (n, c) in enumerate(names):
                self.library.set(loc, i, n, c)
            self.library.scanned_at[loc] = time.time() - 6 * 86400


# ==============================================================================
#  Rendering
# ==============================================================================

class Renderer:
    def __init__(self, surface):
        self.surf = surface
        f = lambda size, bold=False: pygame.font.SysFont(FONT_FAMILIES, size, bold=bold)
        self.f_tab = f(22, True)
        self.f_patch = f(17)
        self.f_slot = f(13)
        self.f_num = f(34, True)
        self.f_ring = f(12, True)
        self.f_dest = f(14)
        self.f_mm = f(11)
        self.f_depth = f(13)
        self.f_foot = f(12)
        self.f_chip = f(11)
        self.f_cellnum = f(9, True)
        self.f_cellname = f(11)
        self.f_big = f(19, True)

    # ---- helpers -----------------------------------------------------------------------

    def text(self, font, s, color, x, y, anchor="topleft"):
        img = font.render(s, True, color)
        r = img.get_rect(**{anchor: (x, y)})
        self.surf.blit(img, r)
        return r

    def ellipsize(self, font, s, max_w):
        if font.size(s)[0] <= max_w:
            return s
        while s and font.size(s + "…")[0] > max_w:
            s = s[:-1]
        return s.rstrip() + "…"

    def wrap(self, font, s, max_w, max_lines=2):
        if font.size(s)[0] <= max_w:
            return [s]
        words, lines, line = s.split(" "), [], ""
        for i, w in enumerate(words):
            trial = (line + " " + w).strip()
            if font.size(trial)[0] <= max_w:
                line = trial
                continue
            if not line:
                return [self.ellipsize(font, s, max_w)]
            lines.append(line)
            line = " ".join(words[i:])
            break
        else:
            lines.append(line)
            line = ""
        if line and len(lines) < max_lines:
            lines.append(self.ellipsize(font, line, max_w))
        return lines[:max_lines]

    def rounded(self, rect, color, radius, width=0):
        pygame.draw.rect(self.surf, color, rect, width, border_radius=radius)

    def arc(self, cx, cy, r, frac, color, thick=5):
        if frac <= 0:
            return
        start, sweep = math.radians(135), math.radians(270) * frac
        steps = max(2, int(sweep / math.radians(2)))
        for i in range(steps + 1):
            a = start + sweep * i / steps
            pygame.draw.circle(self.surf, color,
                               (round(cx + r * math.cos(a)), round(cy + r * math.sin(a))),
                               thick // 2)

    # ---- chrome --------------------------------------------------------------------------

    def tabs(self, app):
        S = self.surf
        for i in (0, 1):
            x0, active = i * (W // 2), (i == app.current)
            s, col = app.synths[i], ACCENT[i]
            if active:
                tint = pygame.Surface((W // 2, TABS_H), pygame.SRCALPHA)
                tint.fill((*col, 14))
                S.blit(tint, (x0, 0))
                pygame.draw.rect(S, col, (x0, TABS_H - 3, W // 2, 3))
            who = col if active else tuple(int(c * 0.55) for c in col)
            r = self.text(self.f_tab, f"Synth {i + 1}", who, x0 + 22, TABS_H // 2 - 1, "midleft")
            name = s.patch["name"] if s.patch else "No patch read"
            right = cp.category_name(s.patch["category"]) if s.patch else ""
            slot = app.current_slot[i]
            if slot is not None:
                right = f"{slot + 1:02d} · {right}"
            rr = self.text(self.f_slot, right, MUTED, x0 + W // 2 - 22, TABS_H // 2 + 1, "midright")
            max_w = rr.left - (r.right + 14) - 12
            self.text(self.f_patch, self.ellipsize(self.f_patch, name, max_w),
                      TEXT if active else MUTED, r.right + 14, TABS_H // 2 + 1, "midleft")
        pygame.draw.line(S, LINE, (0, TABS_H - 1), (W, TABS_H - 1))

    def footer(self, app, right_text):
        fy = H - FOOT_H
        pygame.draw.line(self.surf, LINE, (0, fy), (W, fy))
        ok = app.connected
        pygame.draw.circle(self.surf, S2 if ok else AMBER, (22, fy + FOOT_H // 2), 4)
        status = "Demo mode" if app.demo else ("Circuit connected" if ok else "Looking for Circuit…")
        r = self.text(self.f_foot, status, MUTED, 34, fy + FOOT_H // 2, "midleft")
        self.text(self.f_foot, app.last_msg, MUTED, r.right + 18, fy + FOOT_H // 2, "midleft")
        if right_text:
            self.text(self.f_foot, right_text, MUTED, W - 18, fy + FOOT_H // 2, "midright")

    # ---- macro view -----------------------------------------------------------------------

    def macros(self, app, now):
        S, cur = self.surf, app.current
        acc, acc_dim = ACCENT[cur], ACCENT_DIM[cur]
        s = app.synths[cur]
        for n in range(1, 9):
            k = n - 1
            cx, cy = CARD_POS[n]
            x, y = cx, cy + TABS_H
            rect = pygame.Rect(x, y, CARD_W, CARD_H)
            live, touched = now < s.live_until[k], k in s.touched
            self.rounded(rect, PANEL, 10)
            self.rounded(rect, acc if live else LINE, 10, 2 if live else 1)
            self.text(self.f_num, str(n), acc if live else TEXT, x + 12, y + 10)
            rx, ry = x + 76, y + 30
            self.arc(rx, ry, 17, 1.0, LINE)
            self.arc(rx, ry, 17, s.values[k] / 127, acc if (live or touched) else acc_dim)
            self.text(self.f_ring, str(s.values[k]), TEXT, rx, ry, "center")

            dy, inner_w, bottom = y + 60, CARD_W - 24, y + CARD_H - 10
            if not s.patch:
                self.text(self.f_dest, "—", MUTED, x + 12, dy)
                continue
            slots = s.patch["macros"][k]["slots"]
            if not slots:
                self.text(self.f_dest, "Not assigned", MUTED, x + 12, dy)
                continue
            one_line = len(slots) >= 4
            for sl in slots:
                head, suffix = cp.dest_label(sl["dest"], s.patch)
                dtxt = f"{sl['depth']:+d}"
                dcol = AMBER if sl["depth"] < 0 else MUTED
                name_w = inner_w - self.f_depth.size(dtxt)[0] - 8
                lines = ([self.ellipsize(self.f_dest, head, name_w)] if one_line
                         else self.wrap(self.f_dest, head, name_w, 2))
                line_h = self.f_dest.get_linesize() - 2
                if dy + line_h * len(lines) + 9 > bottom:
                    break
                ly = dy
                for i, ln in enumerate(lines):
                    r = self.text(self.f_dest, ln, TEXT, x + 12, ly)
                    if (i == len(lines) - 1 and suffix
                            and r.right + 4 + self.f_mm.size(suffix)[0] <= x + 12 + name_w):
                        self.text(self.f_mm, suffix, MUTED, r.right + 4, ly + 3)
                    ly += line_h
                self.text(self.f_depth, dtxt, dcol, x + 12 + inner_w, dy + 1, "topright")
                by = ly + 1
                pygame.draw.rect(S, LINE, (x + 12, by, inner_w, 3), border_radius=2)
                bx = x + 12 + inner_w * sl["start"] / 127
                bw = max(2, inner_w * (sl["end"] - sl["start"]) / 127)
                pygame.draw.rect(S, acc if (live or touched) else acc_dim,
                                 (bx, by, bw, 3), border_radius=2)
                dy = by + 9

        self.footer(app, f"Patch read {now - s.read_at:.0f} s ago" if s.read_at else "")

    # ---- library overlay ---------------------------------------------------------------------

    def library(self, app, now):
        cur = app.current
        acc, acc_dim = ACCENT[cur], ACCENT_DIM[cur]
        lib = app.library

        app.chip_rects = []
        cx, cy = GRID_X, TABS_H + 4
        chips = [("All 64", None)] + [(cp.category_name(c), c) for c in lib.categories(cur)]
        for label, val in chips:
            w = self.f_chip.size(label)[0] + 18
            r = pygame.Rect(cx, cy, w, 19)
            on = (app.filter == val)
            self.rounded(r, acc if on else PANEL, 10)
            if not on:
                self.rounded(r, LINE, 10, 1)
            self.text(self.f_chip, label, BG if on else MUTED, r.centerx, r.centery, "center")
            app.chip_rects.append((r, val))
            cx += w + 5
            if cx > W - 70:
                break

        top = TABS_H + CHIPS_H + GRID_PAD_Y
        for idx in range(64):
            page, within = divmod(idx, 32)
            row, col = divmod(within, 8)
            x = GRID_X + col * (CELL_W + CELL_GAP)
            y = top + page * (4 * (CELL_H + CELL_GAP) + PAGE_GAP) + row * (CELL_H + CELL_GAP)
            rect = pygame.Rect(x, y, CELL_W, CELL_H)
            e = lib.entry(cur, idx)
            name = (e or {}).get("name", "")
            here = (app.current_slot[cur] == idx)
            dim = (app.filter is not None
                   and (not e or not name or e.get("category") != app.filter))

            self.rounded(rect, acc_dim if here else PANEL, 5)
            self.rounded(rect, acc if here else ((26, 26, 31) if dim else LINE), 5, 1)
            self.text(self.f_cellnum, f"{idx + 1:02d}",
                      acc if here else (GHOST if dim else FAINT), x + 5, y + 3)
            if e is None:
                self.text(self.f_cellname, "not scanned", GHOST, x + 5, y + 14)
                continue
            if not name:
                self.text(self.f_cellname, "empty", GHOST, x + 5, y + 14)
                continue
            tcol = (255, 255, 255) if here else (GHOST if dim else (201, 201, 210))
            ly = y + 14
            for ln in self.wrap(self.f_cellname, name, CELL_W - 10, 2):
                self.text(self.f_cellname, ln, tcol, x + 5, ly)
                ly += 13

        if app.scanning:
            self.scan_overlay(app)
            self.footer(app, "")
            return

        when = lib.scanned_at[cur]
        if when is None:
            right = "Never scanned — press S"
        else:
            d = max(0.0, time.time() - when)
            right = ("Scanned just now" if d < 90 else
                     f"Scanned {d / 3600:.0f} h ago" if d < 172800 else
                     f"Scanned {d / 86400:.0f} days ago")
        self.footer(app, right)

    def scan_overlay(self, app):
        sc = app.scanner
        veil = pygame.Surface((W, H - TABS_H - FOOT_H), pygame.SRCALPHA)
        veil.fill((12, 12, 15, 234))
        self.surf.blit(veil, (0, TABS_H))
        acc = ACCENT[sc.loc]
        cy = TABS_H + (H - TABS_H - FOOT_H) // 2
        self.text(self.f_big, f"Reading Synth {sc.loc + 1} patches", TEXT, W // 2, cy - 42, "center")
        bar = pygame.Rect(W // 2 - 200, cy, 400, 6)
        pygame.draw.rect(self.surf, LINE, bar, border_radius=3)
        pygame.draw.rect(self.surf, acc, (bar.x, bar.y, int(400 * sc.progress), 6), border_radius=3)
        self.text(self.f_foot, f"slot {min(sc.slot + 1, 64)} of 64", MUTED, W // 2, cy + 22, "center")
        self.text(self.f_foot, "Each patch loads in turn — the synth goes back where it was afterwards.",
                  MUTED, W // 2, cy + 46, "center")
        self.text(self.f_foot, "Esc to stop", GHOST, W // 2, cy + 68, "center")

    # ---- entry point ---------------------------------------------------------------------------

    def draw(self, app, now):
        self.surf.fill(BG)
        self.tabs(app)
        if app.overlay:
            self.library(app, now)
        else:
            self.macros(app, now)


# ==============================================================================
#  Main loop
# ==============================================================================

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fullscreen", action="store_true")
    ap.add_argument("--demo", action="store_true", help="run without a Circuit")
    ap.add_argument("--library", action="store_true", help="start with the library open")
    ap.add_argument("--screenshot", metavar="PNG", help="render one frame and exit")
    args = ap.parse_args()

    pygame.init()
    pygame.display.set_caption("Circuit macros")
    screen = pygame.display.set_mode((W, H), pygame.FULLSCREEN if args.fullscreen else 0)
    if args.fullscreen:
        pygame.mouse.set_visible(False)
    clock = pygame.time.Clock()

    app = App(demo=args.demo)
    app.overlay = args.library
    rend = Renderer(screen)
    if not args.demo:
        try:
            if not app.connect():
                app.last_msg = "Plug in the Circuit; retrying"
        except Exception as e:
            app.last_msg = f"MIDI error: {e}"

    s1keys = [pygame.K_1, pygame.K_2, pygame.K_3, pygame.K_4,
              pygame.K_5, pygame.K_6, pygame.K_7, pygame.K_8]
    s2keys = [pygame.K_q, pygame.K_w, pygame.K_e, pygame.K_r,
              pygame.K_t, pygame.K_y, pygame.K_u, pygame.K_i]

    running = True
    while running:
        now = time.monotonic()
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False

            elif ev.type == pygame.KEYDOWN:
                k, shift = ev.key, ev.mod & pygame.KMOD_SHIFT
                if k == pygame.K_ESCAPE:
                    if app.scanning:
                        app.scanner.fail("cancelled")
                    elif app.overlay:
                        app.overlay = False
                    else:
                        running = False
                elif app.scanning:
                    pass
                elif app.demo and not app.overlay and k in s1keys:
                    i = s1keys.index(k)
                    v = max(0, min(127, app.synths[0].values[i] + (-4 if shift else 4)))
                    app.knob_moved(0, i, v, now)
                elif app.demo and not app.overlay and k in s2keys:
                    i = s2keys.index(k)
                    v = max(0, min(127, app.synths[1].values[i] + (-4 if shift else 4)))
                    app.knob_moved(1, i, v, now)
                elif k == pygame.K_l:
                    app.overlay, app.filter = not app.overlay, None
                elif k == pygame.K_s:
                    app.overlay = True
                    app.start_scan()
                elif k == pygame.K_1:
                    app.switch_to(0, now, force=True)
                elif k == pygame.K_2:
                    app.switch_to(1, now, force=True)
                elif k == pygame.K_r:
                    app.request_dump(0)
                    app.request_dump(1)
                elif k == pygame.K_f:
                    pygame.display.toggle_fullscreen()

            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                mx, my = ev.pos
                if my < TABS_H:
                    app.switch_to(0 if mx < W // 2 else 1, now, force=True)
                elif app.overlay and not app.scanning:
                    for r, val in app.chip_rects:
                        if r.collidepoint(mx, my):
                            app.filter = val
                            break

        app.poll_midi(now)
        rend.draw(app, now)
        pygame.display.flip()

        if args.screenshot:
            pygame.image.save(screen, args.screenshot)
            break
        clock.tick(60)

    if not app.demo:
        app.disconnect()
    pygame.quit()


if __name__ == "__main__":
    main()
