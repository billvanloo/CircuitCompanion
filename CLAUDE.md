# CircuitCompanion — project context

Read this before changing anything. It records what has been verified against
real hardware, which decisions are settled and why, and where the sharp edges
are. Much of it was expensive to establish and is not recoverable from the code
alone.

## What this is

A companion display for a 2014 Novation Circuit (the original, not Tracks) on
firmware **v1.8**. The Circuit has eight macro knobs and no screen, so there is
no way to know what a knob does on the current patch, and no way to see what the
64 patch slots contain. This program watches the Circuit over USB MIDI and shows
both.

Owner: Bill Van Loo, an electronic musician in Ypsilanti. The Circuit sits in a
hardware studio built around Ableton Live; Live is the master clock. Today the
Circuit connects to the studio Mac over USB. In the planned hardware build, Live
reaches the Circuit over DIN MIDI instead and the Pi takes the Circuit's USB port
— see Hardware.

Current state: a working macOS prototype covering the macro display and a
read-only patch library. Several further phases have been discussed and deferred
rather than ruled out — see Roadmap. The 800×480 window size is not
arbitrary — it is the native resolution of the 7" DSI panel this will eventually
run on, attached to a Raspberry Pi 3 or 4. Do not change the window dimensions or the
layout will stop fitting the target hardware.

## Protocol facts

### Verified on the actual device

These were confirmed by running against Bill's Circuit, not read from a
datasheet. Trust them.

- Macro knobs transmit **CC 80–87** on channel 1 (Synth 1) and channel 2 (Synth 2).
- Selecting a patch on the unit transmits a **Program Change** on the same channels.
- `F0 00 20 29 01 60 40 <location> F7` (9 bytes) requests the current patch;
  the Circuit replies with a 350-byte Replace Current Patch message.
  Location is `0` for Synth 1, `1` for Synth 2.
- Replies are **only ever sent to the USB port**, never to MIDI DIN. This is why
  the Pi has to be the Circuit's USB host, and why Live moves to DIN rather than
  the other way round.
- Patch byte layout (offsets into the 340-byte payload): name 0–15 ASCII,
  category 16, genre 17, mod matrix from 124 (20 slots × 4 bytes),
  macros from 204 (8 knobs × 17 bytes = position + 4 × (dest, start, end, depth)).
- Depth bytes are offset by 64; subtract it for the signed value.
- Program Change **into** the Circuit selects a patch, and a dump request
  immediately after returns that patch — i.e. the scan procedure works as the
  guide describes. Confirmed by a full 64-slot scan on the real unit.
- The position byte in a dump is the patch's **stored** knob position, not where
  the physical knob currently sits. Live values come from the CCs. This is why
  the UI shows stored values dimmed until a knob is actually touched.

### From Novation's Programmer's Reference Guide 1.3

Not committed to this repo — it is Novation's copyrighted document. Download it
from <https://downloads.novationmusic.com/novation/circuit/circuit> if needed.

- Session changes arrive as Program Change on **channel 16** (0-based 15).
- Drum sample slot selection is CC 8, 18, 44, 50 on channel 10 (values 0–63).
  Not used yet; relevant to a future drum sample view.
- Consecutive sysex messages need ≥20 ms between them.
- After a Program Change, allow ~10 ms for the patch to load from flash before
  requesting a dump.

### Recovered elsewhere

The **macro destination table (values 0–70) is not in any Novation document.**
It came from the GPL-3 "Circuit Web Synth Editor"
(framagit.org/pumpkin, mirrored at github.com/ewe2/circuit-web-synth-editor)
and lives in `MACRO_DEST_NAMES` in `circuit_patch.py`.

The important consequence: **values 51–70 are not parameters.** They target the
*depth of mod matrix slots 1–20*. A patch whose macro 2 points at destination 51
is modulating mod-matrix slot 1's depth, and slot 1's own source and destination
are elsewhere in the same dump. `dest_label()` resolves this into something
readable like `LFO1+ → filter freq` with a muted `MM1` suffix. Real patches use
this constantly, so a UI that only printed the raw destination name would be
close to useless.

### Reported but not yet verified by us

`.circuitpack` files are said to be plain zip archives containing a `patches/`
folder of `.syx` files and a `samples/` folder of `.wav` files. If true, a pack
import would read all 64 patch names **and** the drum sample names with no
device interaction at all. Bill has deferred this because it depends on
Components, whose device transfers he finds slow — but it remains the better
long-term answer. Verify by renaming a `.circuitpack` copy to `.zip` and
unzipping before building anything on it.

## Architecture

Two modules, deliberately separated so the parser can be tested and reused
without pygame:

- `circuit_patch.py` — pure byte handling. Sysex constants, message builders,
  lookup tables, `decode_patch()`, `dest_label()`. No I/O, no third-party imports.
- `circuit_display.py` — everything else. `Library` (the on-disk name cache),
  `Scanner` (the state machine that reads all 64 slots), `App` (MIDI plumbing
  and state), `Renderer` (all drawing), and the main loop.
- `test_scanner.py` — a fake Circuit that answers dump requests and honours
  Program Changes, used to exercise the scanner without hardware.

Rendering is immediate-mode: `Renderer.draw()` redraws everything at 60 fps from
`App` state. There is no widget tree and no dirty-rect logic; don't add one
without a reason.

The scanner is a **state machine ticked from the main loop**, not a blocking
loop. Keep it that way — the progress bar has to animate and Esc has to cancel
while a scan runs.

## Current scope: read-only, for now

Today the program only reads. It sends patch dump requests, and the library scan
sends Program Changes — nothing else. **This is a scoping choice for v1, not a
principle.** Writing to the Circuit is planned; see Roadmap phase B.

The distinction matters because "the display is passive" is easy to mistake for
a rule that shouldn't be broken. It isn't one. What was actually decided is that
v1 ships without write features so that the read path could be got right first,
and so that a bug could never alter Bill's patches. When bi-directional work
begins, expect to revisit `App.handle()` (which currently ignores everything
during a scan), the `Scanner`'s exclusive use of the output port, and the
library overlay's click handling, which today only hits category chips.

## Settled decisions

These were decided for a reason and shouldn't be changed as a side effect of
other work. Unlike the scope note above, these are about how the thing behaves,
and they are expected to survive into later phases.

1. **Only channels 1 and 2 are watched.** Drum, mixer and FX knob traffic is
   ignored. The same eight physical knobs send on channel 10 or 16 in those
   modes, and reacting to them would make the display flap. (A future drum view
   would read channel 10 deliberately, not by relaxing this.)
2. **Tab switching** follows knob moves with a 500 ms dwell (`TAB_DWELL_S`),
   and follows patch changes immediately. The dwell exists because riding knobs
   on both synths otherwise makes the view bounce.
3. **Dim until touched.** Rings and range bars render dim until a knob sends its
   first CC after a patch load, because until then the displayed value is the
   patch's stored position and may not match the physical knob.
4. **Layout is fixed at 800×480** to match the target panel. The staggered knob
   arrangement mirrors the Circuit's own two-row knob layout; the library grid
   is two blocks of 8×4 mirroring the two pages of pads. Both are about
   muscle memory — screen position should equal finger position.
5. **The mod-matrix route is the headline**, the slot number a muted suffix.
   Bill chose this explicitly. Four-destination macros fall back to one
   truncated line each; three or fewer wrap to two lines.
6. **Category filtering dims rather than reorders**, so a slot never moves.

## Sharp edges

- **Use `pygame-ce`, not `pygame`.** Bill runs Python 3.14; the original pygame
  has no 3.14 wheels, so pip compiles from source and produces a build with no
  font module. The two packages both install as `pygame` and cannot coexist.
- On macOS, CoreMIDI lets this app and Live hold the Circuit's USB port at the
  same time, which is how the prototype is developed. On the Pi that question
  doesn't arise: Live is on DIN, so this program is the only thing using the
  Circuit's USB port.
- **The scan's program changes are real MIDI on the Circuit.** If the Circuit
  echoes received program changes to its DIN out and a Live track is recording
  that input, a scan would record 64 patch changes. Unverified either way;
  don't scan while recording the Circuit until it's checked.
- Sysex timing is real. `SCAN_STEP_S` (35 ms) and `PC_DUMP_DELAY_S` (20 ms) have
  margin over the documented minimums. If dumps come back empty or garbled on
  hardware, raise these first.
- `library.json` sits next to the script and is gitignored. It is a cache, never
  a source of truth: the display re-reads the current patch on every slot change
  and corrects the cached name when it differs.
- **A wedged Circuit needs a power cycle, not a USB replug.** Seen once, on
  2026-09-01, after three scan attempts in close succession (two full 64-slot
  runs, then a third that failed at its capture step): the Circuit stopped
  answering `40h` dump requests altogether. It was the device, not the host —
  the request kept arriving (`/proc/asound/card2/midi0` Tx rising 9 bytes per
  request) with nothing coming back, no process held `/dev/snd/midiC2D0`, and
  `aconnect -l` showed no subscribers on the Circuit's port. Unplugging USB
  re-enumerated it (new devnum, new device nodes) and did **not** help: the
  Circuit is self-powered, so pulling USB never resets the unit. Power-cycling
  the Circuit itself fixed it instantly. The rapid repeated scans are the
  suspect, unconfirmed — reproducing it means deliberately wedging the
  instrument. **If dumps go silent, power-cycle the Circuit before debugging
  the host.**
- Don't commit Novation's PDF or any purchased patch pack.

## Testing

`python3 test_scanner.py` runs the scanner against a simulated Circuit and
covers the clean path, the unsaved-edits path, and a device that drops replies.
It needs no hardware and no display (it forces SDL's dummy drivers).

**Any change to `Scanner` should be tested here before being tested on the
instrument.** The scanner loads patches in sequence, so a bug is audible and
potentially confusing on real hardware. The simulator's timing is synthetic, so
passing here means the logic is right, not that the pacing is — a live-fire run
is still the real check. The current defaults have had one.

For visual work, `--demo` populates plausible patch and library data with no
Circuit attached, and `--screenshot out.png` renders one frame and exits, which
makes it easy to review layouts headlessly.

## Roadmap

Everything below has been discussed with Bill and deferred, not rejected. The
lettered phases are roughly independent; the numbered hardware phases are
sequential. Nothing here is committed to a schedule.

### Immediate

The scanner has now been run repeatedly against the real Circuit — five full
64-slot scans across both synths, every one returning 64/64 with no gaps — so
the default `SCAN_STEP_S` (35 ms) and `PC_DUMP_DELAY_S` (20 ms) are known-good
on v1.8 firmware and shouldn't be lowered without re-testing on hardware. Dump
replies come back in about 5 ms, so there is real headroom over the guide's
20 ms minimum. A scan of one synth takes 6–8 s.

Verified on 2026-09-01:

- **The unsaved-edits restore path works on hardware.** Scanning with unsaved
  tweaks in play found no byte-for-byte match, so the scanner wrote the captured
  bytes back into the live sound and the footer read `sound restored, but the
  slot on the Circuit may not match`. Bill confirmed by ear that the sound came
  back. This had only ever passed in `test_scanner.py` before.
- **No slots left "not scanned"** in any of the five runs. Worth a glance after
  future scans, but 35 ms is no longer suspect.

Still open:

- **Check mod-matrix routes against real patches.** Now seen on real patches and
  resolving correctly — `direct → osc1&2 pitch` with a muted `MM2` suffix, and
  four-destination macros falling back to one line each. The truncation concern
  is real, though: a four-slot macro on `Kick Designer` renders as
  `filter env → nois…` in the 157 px card. Longer route names may need better
  abbreviations rather than a plain ellipsis.

### Phase A — pack import

Read patch and sample names from a `.circuitpack` file instead of scanning.
Reportedly a zip containing `patches/*.syx` and `samples/*.wav` (unverified —
check before building). Advantages: no device interaction at all, no sound
changes, and it yields **drum sample names**, which is the half of Bill's
original complaint that nothing currently addresses.

The catch, and the reason it is not first: it requires a Components session, and
Bill finds Components' device transfers slow — that friction is itself one of
the pain points this project exists to reduce. So the scan stays the primary
path and pack import is an additional source, not a replacement.

Design already agreed: the pack file is a source, `library.json` is the cache,
and the live dump on each slot change remains the tiebreaker when they disagree.

### Phase B — bi-directional

Turning the library from a reference sheet into a browser. Bill has said he
wants to consider this; it was held back only to get v1 out.

- Tapping a library slot sends a Program Change to select that patch.
- Drum sample slots are selectable the same way: CC 8, 18, 44, 50 on channel 10,
  values 0–63 (documented, currently unused).
- A drum view becomes worthwhile once sample names exist (needs phase A).

Things to think about when this starts: the display becoming a controller
changes the failure modes, so a mistimed or duplicated send now has audible
consequences; and `Scanner` currently assumes exclusive use of the output port.

### Phase C — touch and buttons

Cut from v1 deliberately. Bill wants touch **plus** a few physical buttons
(reasonably: touch for browsing, buttons for things done mid-jam). Keyboard
shortcuts are the current stand-in and the GPIO/touch handlers should call the
same actions rather than duplicating logic. An encoder was floated for scrolling
a patch browser if one ever needs scrolling.

### Hardware

Planned in detail, not started. Topology decided 2026-10-13.

**Topology.** Live reaches the Circuit over **DIN MIDI**: Scarlett 6i6 MIDI Out
→ Circuit MIDI In carries clock, notes and program changes; Circuit MIDI Out →
Scarlett MIDI In is optional, for recording knob CCs into Live. The Pi plugs into
the Circuit's **USB** port and is its only host. The Pi has no connection to the
Mac at all. Diagram: `docs/wiring.svg`.

Consequences worth knowing:

- Live no longer depends on the Pi. The Pi can be off, booting or crashed and
  sequencing is unaffected.
- The display program on the Pi works exactly as it does on the Mac: one
  process holding one USB MIDI port. No bridge, no gadget mode, no ALSA routing.
- The Pi takes ordinary power from its own supply.

**Specs.**

| Part | Spec |
|---|---|
| Computer | Raspberry Pi **3B, 3B+, 3A+ or 4B**. Needs only a DSI port, one USB-A port and the 40-pin header. Bill owns a 4B. |
| OS | Raspberry Pi OS **64-bit** (Lite + a minimal display stack). pygame-ce publishes 64-bit ARM wheels; on 32-bit it may build from source, which is the font-module failure again. |
| Display | Hosyond 7″ DSI, 800×480, capacitive, 2.2 W. Video and touch over the DSI ribbon; 5 V from the Pi's header (2-pin lead to pins 4/6, or the board's contacts when the Pi is bolted to its standoffs — one, not both). Mounting holes match Pi 3 and Pi 4, which share a footprint. |
| Power | Official Pi supply into the Pi's normal power input: 5.1 V 2.5 A micro-USB (Pi 3) or 5.1 V 3 A USB-C (Pi 4). The display is fed from that. Nothing is injected into the header. |
| Circuit link | USB-A (Pi) → USB-B (Circuit). The Circuit keeps its own power supply. |
| Live link | Two 5-pin DIN MIDI cables to the Scarlett 6i6 (one if CCs aren't recorded). |
| Buttons (Phase C) | GPIO 6/13/19/26 (pins 31/33/35/37) to GND on pin 39, internal pull-ups. Clear of I²C, SPI and UART. |

**Before building on it, check:**

- That the Scarlett's MIDI ports are free. If they already drive mmMIDI or
  anything else, a second interface or a thru box is needed.
- That the Circuit sends CC and Program Change on USB and DIN at the same time.
  The display depends on the USB copy. Expected, since it's one output mirrored
  to both ports, but unverified.
- That Live isn't echoing the Circuit's DIN input back out to it (MIDI loop).
- **Pi 3 rendering load.** `Renderer.draw()` redraws the whole screen at 60 fps,
  which is trivial on a Mac and comfortable on a Pi 4 but untested on a Pi 3.
  If a Pi 3 runs hot or laggy, lower the frame rate or redraw only on change.

**Phases.**

1. **Parts.** Panel (in hand), Pi, supply, DIN cables, USB-A-to-B cable,
   3–4 tactile buttons for later. No case ecosystem fits this panel; the
   enclosure is bespoke. Published references: a frame STL for this exact panel
   on Printables (model 1316423) and the Elecrow "7inch DSI Display-A" manual,
   which appears to be the same OEM board (module 192.96 × 110.76 × 20.13 mm,
   active area 154.21 × 85.92 mm). Measure the actual panel before committing.
2. **Re-cable Live to DIN** and re-point the Circuit's track in Live from the
   USB port to the Scarlett's MIDI port. Can be done on the Mac first, before any
   Pi work, and verified with the macOS prototype still on USB.
3. **Deploy the display.** `--fullscreen`, pygame-ce, autostart on boot.
4. **Enclosure.** An angled stand or box sitting behind the Circuit. Bill does
   woodworking and will lead this.

### Longer shots

- **Probe for an undocumented read-by-slot command.** Novation documents `40h`
  (current patch dump request) and `01h` (replace patch, which writes to a
  specific flash slot) but nothing that reads a slot without loading it. The
  command space is nearly empty, so something like `41h` taking a slot number is
  plausible. If it exists it removes the need to scan entirely. Cheap to test;
  back up the pack via Components first, since poking undocumented commands at
  a device isn't risk-free.
- **Sniff Components' bulk read.** "Get Pack from Circuit" proves the device can
  send its whole contents over USB; the protocol just isn't published. Prior art
  exists for the Circuit Tracks side. A project in itself, and phase A gets the
  same data for far less effort.

### Considered and set aside

Recorded so nobody re-researches them.

- **iOS companion** (MIDI Designer + MidiFire). A working version of this exists
  on the Audiobus forum. Dead for Bill: MIDI Designer Pro X now requires iOS 18
  and his iPad tops out below that. It also takes the Circuit off Live entirely,
  since the iPad would have to be the USB host.
- **ESP32-S3 port** of the existing open-source ESP8266 Circuit patch editor.
  Originally set aside because it would force the Circuit onto DIN — which is
  now the plan anyway, so that objection is gone. Still set aside because it
  can't reasonably drive an 800×480 DSI panel or run the existing Python code.
- **USB gadget bridge** (the original Pi plan): the Pi as the Circuit's USB host
  and also a MIDI gadget to the Mac over its USB-C port, forwarding Live's
  traffic through. Dropped 2026-10-13. Powering the Pi through the GPIO header
  (needed to keep USB-C free for data) ties the Mac's VBUS to the Pi's 5 V
  rail; fixing that needs a VBUS-blocking cable. A powered or PD pass-through
  hub doesn't help — it either back-feeds the same way or turns the Pi into
  the host on that port, which breaks the gadget link. It also limited the
  build to a Pi 4 and made Live depend on the Pi being booted. DIN avoids all
  of it.
- **Off-the-shelf.** Nothing does this. USB host boxes give routing, not a screen.

## Working with Bill

He is technical, does his own Eurorack prototyping and woodworking, and prefers
being told the reasoning rather than just the answer. He asks for options with
trade-offs before implementation, and dislikes filler and hedging. When
something can't be verified, say so plainly rather than guessing.
