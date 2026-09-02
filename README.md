# Circuit macro display — prototype (macOS)

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
    python3 circuit_display.py --demo     # no Circuit; 1–8 and Q–I simulate knobs
    python3 circuit_display.py --fullscreen

Keys: `1`/`2` switch synth · `R` re-read both patches · `F` fullscreen · `Esc` quit.
Click a tab to switch.

## Behaviour

- Only channels 1 and 2 are watched; drum / mixer / FX knob traffic is ignored.
- A knob move on the other synth switches tabs, subject to a 500 ms dwell.
- A patch change switches immediately and re-reads that patch; a session
  change re-reads both.
- Ring and range bars show dim until a knob has sent its first CC.
- NRPN traffic (Components editing) triggers a re-read, at most once per second.
- If the Circuit disappears, the footer says so and the app retries every 2 s.

Tunables are at the top of `circuit_display.py`.

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
