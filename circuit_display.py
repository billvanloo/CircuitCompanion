#!/usr/bin/env python3
"""
circuit_display.py — macro knob companion display for the Novation Circuit.

Passive: it listens to the Circuit and only ever sends patch dump requests.

Usage:
  python3 circuit_display.py                # window, real Circuit over USB
  python3 circuit_display.py --fullscreen   # for the 800x480 panel on the Pi
  python3 circuit_display.py --demo         # no Circuit needed; keys simulate knobs

Keys:  1 / 2  show Synth 1 / 2      R  re-request both patches
       F      toggle fullscreen     Esc / Q  quit
  demo only:  1–8 turn Synth 1 knobs, Q W E R T Y U I turn Synth 2 (Shift = down)

Setup (macOS):  pip install pygame mido python-rtmidi
"""

import argparse
import math
import sys
import time

import pygame

import circuit_patch as cp

# ---- Behaviour knobs ----------------------------------------------------------

TAB_DWELL_S = 0.50        # min time between automatic tab switches
LIVE_GLOW_S = 0.90        # how long a moved knob stays highlighted
NRPN_REDUMP_S = 1.00      # throttle for re-reading a patch after Components edits
DUMP_SPACING_S = 0.03     # >= 20 ms between sysex sends (per the reference guide)
RECONNECT_S = 2.0
PC_DUMP_DELAY_S = 0.02    # wait for patch load after a program change (guide: 10 ms)

# ---- Look (mirrors the HTML prototype) ----------------------------------------

W, H = 800, 480
TABS_H, FOOT_H = 58, 26
CARD_W, CARD_H = 157, 188
CARD_POS = {1: (18, 8), 3: (191, 8), 5: (364, 8), 7: (537, 8),
            2: (104, 202), 4: (277, 202), 6: (450, 202), 8: (623, 202)}

BG, PANEL, LINE = (12, 12, 15), (20, 20, 24), (38, 38, 45)
TEXT, MUTED = (236, 236, 240), (125, 125, 137)
S1, S1_DIM = (255, 60, 174), (90, 31, 67)
S2, S2_DIM = (53, 227, 163), (26, 90, 68)
AMBER = (224, 162, 74)
ACCENT = {0: S1, 1: S2}
ACCENT_DIM = {0: S1_DIM, 1: S2_DIM}

FONT_FAMILIES = "helveticaneue,helvetica,arial,dejavusans,freesans"


# ==============================================================================
#  State
# ==============================================================================

class SynthState:
    def __init__(self):
        self.patch = None            # decoded dict, or None
        self.values = [0] * 8        # live macro values
        self.touched = set()         # knobs that have sent a CC since last dump
        self.live_until = [0.0] * 8  # glow timers
        self.read_at = None          # time of last dump


class App:
    def __init__(self, demo=False):
        self.demo = demo
        self.synths = [SynthState(), SynthState()]
        self.current = 0
        self.last_switch = 0.0
        self.last_msg = "Waiting for first knob move"
        self.inport = self.outport = None
        self.port_name = None
        self.next_reconnect = 0.0
        self.send_queue = []          # (not_before_time, location)
        self.last_send = 0.0
        self.nrpn_redump_at = [0.0, 0.0]
        self.nrpn_pending = [False, False]
        if demo:
            self._load_demo()

    # ---- MIDI plumbing ----------------------------------------------------------

    def connect(self):
        import mido
        ins = [n for n in mido.get_input_names() if "circuit" in n.lower()]
        outs = [n for n in mido.get_output_names() if "circuit" in n.lower()]
        if not ins or not outs:
            return False
        self.inport = mido.open_input(ins[0])
        self.outport = mido.open_output(outs[0])
        self.port_name = ins[0]
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
        self.pump_sends(now)
        for loc in (0, 1):
            if self.nrpn_pending[loc] and now >= self.nrpn_redump_at[loc]:
                self.nrpn_pending[loc] = False
                self.request_dump(loc)

    def handle(self, msg, now):
        t = msg.type
        if t == "sysex":
            if cp.is_patch_dump(msg.data):
                patch = cp.decode_patch(msg.data)
                s = self.synths[patch["location"]]
                s.patch = patch
                s.read_at = now
                for k in range(8):
                    if k not in s.touched:
                        s.values[k] = patch["macros"][k]["position"]
            return

        ch = getattr(msg, "channel", None)

        if t == "program_change":
            if ch in (0, 1):
                self.synths[ch].touched.clear()
                self.request_dump(ch, PC_DUMP_DELAY_S)
                self.switch_to(ch, now, force=True)
                self.last_msg = f"Synth {ch + 1} · patch {msg.program + 1:02d}"
            elif ch == cp.SESSION_CHANNEL:
                for loc in (0, 1):
                    self.synths[loc].touched.clear()
                    self.request_dump(loc, PC_DUMP_DELAY_S + loc * DUMP_SPACING_S)
                self.last_msg = "Session change"
            return

        if t == "control_change" and ch in (0, 1):
            c = msg.control
            if cp.MACRO_CC_FIRST <= c < cp.MACRO_CC_FIRST + 8:
                self.knob_moved(ch, c - cp.MACRO_CC_FIRST, msg.value, now)
            elif c in (6, 38, 98, 99):      # NRPN traffic = editor is changing the patch
                self.nrpn_pending[ch] = True
                self.nrpn_redump_at[ch] = max(self.nrpn_redump_at[ch], now + NRPN_REDUMP_S)

    # ---- Behaviour -------------------------------------------------------------

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
            self.current = synth
            self.last_switch = now

    # ---- Demo data ----------------------------------------------------------------

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
            [(51, 0, 127, 23)],
            [(33, 0, 127, 34)],
            [(52, 0, 127, -22)],
            [(54, 0, 127, 1)],
            [(45, 0, 127, 9)],
            [(1, 0, 127, 47)],
            [(46, 0, 127, 54)],
        ], [0, 97, 57, 0, 76, 0, 0, 30],
            {1: (7, 0, 20, 0), 2: (11, 0, 40, 12), 3: (9, 0, 15, 13), 4: (8, 0, 22, 8)})
        for loc, raw in ((0, chill), (1, init)):
            data = cp.CIRCUIT_HEADER + (0, loc, 0) + tuple(raw)
            patch = cp.decode_patch(data)
            s = self.synths[loc]
            s.patch = patch
            s.read_at = time.monotonic()
            s.values = [m["position"] for m in patch["macros"]]


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

    # ---- helpers ------------------------------------------------------------------

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

    def wrap2(self, font, s, max_w):
        """Wrap to at most two lines, ellipsizing the second."""
        if font.size(s)[0] <= max_w:
            return [s]
        words = s.split(" ")
        line, i = "", 0
        while i < len(words) and font.size((line + " " + words[i]).strip())[0] <= max_w:
            line = (line + " " + words[i]).strip()
            i += 1
        if not line:                       # single very long word
            return [self.ellipsize(font, s, max_w)]
        rest = " ".join(words[i:])
        return [line, self.ellipsize(font, rest, max_w)] if rest else [line]

    def rounded(self, rect, color, radius, width=0):
        pygame.draw.rect(self.surf, color, rect, width, border_radius=radius)

    def arc(self, cx, cy, r, frac, color, thick=5):
        """270° pot-style sweep, gap at the bottom, drawn as a dotted stroke."""
        start = math.radians(135)
        sweep = math.radians(270) * frac
        if frac <= 0:
            return
        steps = max(2, int(sweep / math.radians(2)))
        for i in range(steps + 1):
            a = start + sweep * i / steps
            x = cx + r * math.cos(a)
            y = cy + r * math.sin(a)
            pygame.draw.circle(self.surf, color, (round(x), round(y)), thick // 2)

    # ---- screen -------------------------------------------------------------------

    def draw(self, app, now):
        S = self.surf
        S.fill(BG)
        cur = app.current
        acc, acc_dim = ACCENT[cur], ACCENT_DIM[cur]

        # ---- tabs
        for i in (0, 1):
            x0 = i * (W // 2)
            active = (i == cur)
            s = app.synths[i]
            col = ACCENT[i]
            if active:
                tint = pygame.Surface((W // 2, TABS_H), pygame.SRCALPHA)
                tint.fill((*col, 14))
                S.blit(tint, (x0, 0))
                pygame.draw.rect(S, col, (x0, TABS_H - 3, W // 2, 3))
            who_col = col if active else tuple(int(c * 0.55) for c in col)
            r = self.text(self.f_tab, f"Synth {i + 1}", who_col, x0 + 22, TABS_H // 2 - 1, "midleft")
            name = s.patch["name"] if s.patch else "No patch read"
            name_col = TEXT if active else MUTED
            slot = ""
            if s.patch:
                slot = cp.category_name(s.patch["category"])
            slot_r = self.text(self.f_slot, slot, MUTED, x0 + W // 2 - 22, TABS_H // 2 + 1, "midright")
            max_w = slot_r.left - (r.right + 14) - 12
            self.text(self.f_patch, self.ellipsize(self.f_patch, name, max_w), name_col,
                      r.right + 14, TABS_H // 2 + 1, "midleft")
        pygame.draw.line(S, LINE, (0, TABS_H - 1), (W, TABS_H - 1))

        # ---- cards
        s = app.synths[cur]
        for n in range(1, 9):
            k = n - 1
            cx, cy = CARD_POS[n]
            x, y = cx, cy + TABS_H
            rect = pygame.Rect(x, y, CARD_W, CARD_H)
            live = now < s.live_until[k]
            touched = k in s.touched
            self.rounded(rect, PANEL, 10)
            self.rounded(rect, acc if live else LINE, 10, 2 if live else 1)

            self.text(self.f_num, str(n), acc if live else TEXT, x + 12, y + 10)
            # ring
            rx, ry = x + 12 + 30 + 12 + 22, y + 10 + 20
            self.arc(rx, ry, 17, 1.0, LINE)
            self.arc(rx, ry, 17, s.values[k] / 127, acc if (live or touched) else acc_dim)
            self.text(self.f_ring, str(s.values[k]), TEXT, rx, ry, "center")

            # destinations
            dy = y + 10 + 44 + 6
            inner_w = CARD_W - 24
            bottom = y + CARD_H - 10
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
                depth = sl["depth"]
                dtxt = f"{depth:+d}"
                dcol = AMBER if depth < 0 else MUTED
                d_w = self.f_depth.size(dtxt)[0]
                name_w = inner_w - d_w - 8
                lines = [self.ellipsize(self.f_dest, head, name_w)] if one_line \
                    else self.wrap2(self.f_dest, head, name_w)
                line_h = self.f_dest.get_linesize() - 2
                needed = line_h * len(lines) + 3 + 6
                if dy + needed > bottom:
                    break
                ly = dy
                for i, ln in enumerate(lines):
                    r = self.text(self.f_dest, ln, TEXT, x + 12, ly)
                    if i == len(lines) - 1 and suffix:
                        sw = self.f_mm.size(suffix)[0]
                        if r.right + 4 + sw <= x + 12 + name_w:
                            self.text(self.f_mm, suffix, MUTED, r.right + 4, ly + 3)
                    ly += line_h
                self.text(self.f_depth, dtxt, dcol, x + 12 + inner_w, dy + 1, "topright")
                # range bar
                by = ly + 1
                pygame.draw.rect(S, LINE, (x + 12, by, inner_w, 3), border_radius=2)
                bx = x + 12 + inner_w * sl["start"] / 127
                bw = max(2, inner_w * (sl["end"] - sl["start"]) / 127)
                bcol = acc if (live or touched) else acc_dim
                pygame.draw.rect(S, bcol, (bx, by, bw, 3), border_radius=2)
                dy = by + 3 + 6

        # ---- footer
        fy = H - FOOT_H
        pygame.draw.line(S, LINE, (0, fy), (W, fy))
        dot = S2 if app.connected else AMBER
        pygame.draw.circle(S, dot, (18 + 4, fy + FOOT_H // 2), 4)
        status = "Demo mode" if app.demo else ("Circuit connected" if app.connected else "Looking for Circuit…")
        r = self.text(self.f_foot, status, MUTED, 18 + 16, fy + FOOT_H // 2, "midleft")
        self.text(self.f_foot, app.last_msg, MUTED, r.right + 18, fy + FOOT_H // 2, "midleft")
        if s.read_at is not None:
            age = now - s.read_at
            txt = f"Patch read {age:.0f} s ago" if age < 90 else f"Patch read {age / 60:.0f} min ago"
            self.text(self.f_foot, txt, MUTED, W - 18, fy + FOOT_H // 2, "midright")


# ==============================================================================
#  Main loop
# ==============================================================================

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fullscreen", action="store_true")
    ap.add_argument("--demo", action="store_true", help="run without a Circuit, keys simulate knobs")
    ap.add_argument("--screenshot", metavar="PNG", help="render one frame to a file and exit")
    args = ap.parse_args()

    pygame.init()
    pygame.display.set_caption("Circuit macros")
    flags = pygame.FULLSCREEN if args.fullscreen else 0
    screen = pygame.display.set_mode((W, H), flags)
    if args.fullscreen:
        pygame.mouse.set_visible(False)
    clock = pygame.time.Clock()

    app = App(demo=args.demo)
    rend = Renderer(screen)
    if not args.demo:
        try:
            if not app.connect():
                app.last_msg = "Plug in the Circuit; retrying"
        except Exception as e:
            app.last_msg = f"MIDI error: {e}"

    s1keys = [pygame.K_1, pygame.K_2, pygame.K_3, pygame.K_4, pygame.K_5, pygame.K_6, pygame.K_7, pygame.K_8]
    s2keys = [pygame.K_q, pygame.K_w, pygame.K_e, pygame.K_r, pygame.K_t, pygame.K_y, pygame.K_u, pygame.K_i]

    running = True
    while running:
        now = time.monotonic()
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN:
                k, mods = ev.key, ev.mod
                step = -4 if mods & pygame.KMOD_SHIFT else 4
                if k == pygame.K_ESCAPE:
                    running = False
                elif app.demo and k in s1keys:
                    i = s1keys.index(k)
                    app.knob_moved(0, i, max(0, min(127, app.synths[0].values[i] + step)), now)
                elif app.demo and k in s2keys:
                    i = s2keys.index(k)
                    app.knob_moved(1, i, max(0, min(127, app.synths[1].values[i] + step)), now)
                elif k == pygame.K_q:
                    running = False
                elif k == pygame.K_1:
                    app.switch_to(0, now, force=True)
                elif k == pygame.K_2:
                    app.switch_to(1, now, force=True)
                elif k == pygame.K_r:
                    app.request_dump(0); app.request_dump(1)
                elif k == pygame.K_f:
                    pygame.display.toggle_fullscreen()
            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                mx, my = ev.pos
                if my < TABS_H:
                    app.switch_to(0 if mx < W // 2 else 1, now, force=True)

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
