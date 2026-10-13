# CircuitCompanion — Novation Circuit macro & patch display

Two files:

- `circuit_patch.py` — decodes Circuit patch dumps, macro destinations, mod matrix.
- `circuit_display.py` — the 800×480 display. Listens to the Circuit, requests
  patch dumps, follows knob moves and patch changes.

## Setup

    python3 -m venv ~/circuit-env
    source ~/circuit-env/bin/activate
    pip install -r requirements.txt

Uses **pygame-ce** (the community fork), not the original `pygame` package —
the original has no Python 3.14 wheels as of this writing and will fail to
build the font module from source. If you ever `pip install pygame` into this
venv by habit, remove it first (`pip uninstall pygame`); the two packages
both install as `pygame` and can't coexist.

## Run

    python3 circuit_display.py            # real Circuit over USB (Live can stay open)
    python3 circuit_display.py --demo     # no Circuit; 1-8 and Q-I simulate knobs
    python3 circuit_display.py --fullscreen
    python3 circuit_display.py --library  # start on the library view

Keys: `1`/`2` switch synth, `L` open/close the library, `S` scan this synth's
library, `R` re-read both patches, `F` fullscreen, `Esc` close the library or quit.
Click a tab to switch synth, or a category chip to filter.

## The library view

A full-screen overlay showing all 64 patch slots in the hardware's own geometry:
two blocks of 8x4, matching the two pages of pads on the Circuit, so a slot sits
in the same place on screen as it does under your finger. The current patch is
highlighted. Category chips filter by dimming rather than reordering, so nothing
ever moves.

### Scanning

There is no "send me every patch name" message. The only documented way to read
a stored patch is to load it and then request the current patch dump, so a scan
**changes the sound on that synth while it runs**. That is why it is never
automatic. Press `S`; it takes a few seconds.

Stop playback before scanning. Patch changes are silent on their own, but very
audible if the sequencer is running.

The scan captures the live sound first, walks all 64 slots, then finds the slot
whose contents match what it captured and returns there. If the sound had
unsaved edits nothing will match, so the captured bytes are written back into
the live sound instead and the footer warns that the slot number shown on the
Circuit may not agree.

Results are cached in `library.json` next to the script, so a scan is only
needed after loading a new pack. Between scans the cache self-corrects: every
time you land on a slot the display reads that patch anyway and updates the
cached name if it changed. Slots that fail to answer are marked "not scanned"
rather than guessed.

## Behaviour

- Only channels 1 and 2 are watched; drum / mixer / FX knob traffic is ignored.
- A knob move on the other synth switches tabs, subject to a 500 ms dwell.
- A patch change switches immediately and re-reads that patch; a session
  change re-reads both.
- Ring and range bars show dim until a knob has sent its first CC.
- NRPN traffic (Components editing) triggers a re-read, at most once per second.
- If the Circuit disappears, the footer says so and the app retries every 2 s.

Tunables are at the top of `circuit_display.py`.

## Hardware

The planned standalone build runs this on a Raspberry Pi 3 or 4 with a 7″
800×480 DSI touchscreen. Live talks to the Circuit over DIN MIDI; the Pi only
hosts the Circuit's USB port for the display. Wiring: `docs/wiring.svg`. Specs and
phases are in `CLAUDE.md`.

## Working on this

`CLAUDE.md` holds the project context: verified protocol facts, the reasoning
behind settled design decisions, known sharp edges, and open work. Read it
before making changes.

`python3 test_scanner.py` exercises the library scanner against a simulated
Circuit — no hardware needed. Run it after touching `Scanner`.

## Reference

`circuit_patch.py` implements the sysex formats and patch layout from
Novation's *Circuit Programmer's Reference Guide 1.3*, plus the macro
destination table (0–70), which isn't printed in Novation's guide and was
recovered from the GPL-3 "Circuit Web Synth Editor"
(framagit.org/pumpkin, mirrored at github.com/ewe2/circuit-web-synth-editor).

Neither document is included in this repo. The Programmer's Reference Guide
is Novation's copyrighted material — download it directly from
[downloads.novationmusic.com/novation/circuit/circuit](https://downloads.novationmusic.com/novation/circuit/circuit)
if you need to check the byte layout yourself.
