"""
circuit_patch.py — Novation Circuit (original) synth patch sysex handling.

Sources:
  * Circuit Programmer's Reference Guide 1.3 (Novation) — message formats,
    patch byte layout, mod-matrix source/destination tables.
  * Macro destination table (0–70) is not printed in Novation's guide; it was
    recovered from the GPL-3 "Circuit Web Synth Editor"
    (framagit.org/pumpkin, mirrored at github.com/ewe2/circuit-web-synth-editor).

No third-party imports: this module only turns bytes into dicts.
"""

# ---- Sysex constants ---------------------------------------------------------

NOVATION_ID = (0x00, 0x20, 0x29)
CIRCUIT_HEADER = NOVATION_ID + (0x01, 0x60)     # product type 01, product 60h
CMD_REPLACE_CURRENT = 0x00
CMD_DUMP_REQUEST = 0x40

PATCH_DATA_LEN = 340
PATCH_DATA_OFFSET = 8          # header bytes before patch data (F0 excluded)
DUMP_DATA_LEN = PATCH_DATA_OFFSET + PATCH_DATA_LEN   # 348; +F0/F7 = 350

MODMATRIX_BASE = 124           # ModMatrix1_Source1, 4 bytes/slot, 20 slots
MACRO_BASE = 204               # MacroKnob1_Position
MACRO_STRIDE = 17              # position + 4 slots x (dest, start, end, depth)

MACRO_CC_FIRST = 80            # CC 80–87 = macro knobs 1–8 on ch 1 / ch 2
SESSION_CHANNEL = 15           # 0-based; "channel 16" in the guide

# ---- Lookup tables ----------------------------------------------------------

MACRO_DEST_NAMES = {
    0: "Off",
    1: "Portamento rate",
    2: "Post-FX level",
    3: "Osc1 wave interp", 4: "Osc1 PW/index", 5: "Osc1 vsync depth",
    6: "Osc1 density", 7: "Osc1 density detune", 8: "Osc1 semitones", 9: "Osc1 cents",
    10: "Osc2 wave interp", 11: "Osc2 PW/index", 12: "Osc2 vsync depth",
    13: "Osc2 density", 14: "Osc2 density detune", 15: "Osc2 semitones", 16: "Osc2 cents",
    17: "Osc1 level", 18: "Osc2 level", 19: "Ring mod level", 20: "Noise level",
    21: "Filter freq", 22: "Filter res", 23: "Filter drive", 24: "Filter tracking",
    25: "Filter env → freq",
    26: "Amp attack", 27: "Amp decay", 28: "Amp sustain", 29: "Amp release",
    30: "Filter attack", 31: "Filter decay", 32: "Filter sustain", 33: "Filter release",
    34: "Env3 delay", 35: "Env3 attack", 36: "Env3 decay", 37: "Env3 sustain", 38: "Env3 release",
    39: "LFO1 rate", 40: "LFO1 rate sync", 41: "LFO1 slew",
    42: "LFO2 rate", 43: "LFO2 rate sync", 44: "LFO2 slew",
    45: "Distortion level", 46: "Chorus level", 47: "Chorus rate",
    48: "Chorus feedback", 49: "Chorus mod depth", 50: "Chorus delay",
}
for _n in range(1, 21):
    MACRO_DEST_NAMES[50 + _n] = f"Mod matrix {_n} depth"

MOD_SOURCES = {0: "direct", 4: "velocity", 5: "keyboard", 6: "LFO1+", 7: "LFO1±",
               8: "LFO2+", 9: "LFO2±", 10: "amp env", 11: "filter env", 12: "env3"}
MOD_DESTS = {0: "osc1&2 pitch", 1: "osc1 pitch", 2: "osc2 pitch", 3: "osc1 vsync",
             4: "osc2 vsync", 5: "osc1 PW/idx", 6: "osc2 PW/idx", 7: "osc1 level",
             8: "osc2 level", 9: "noise level", 10: "ring mod level", 11: "filter drive",
             12: "filter freq", 13: "filter res", 14: "LFO1 rate", 15: "LFO2 rate",
             16: "amp decay", 17: "filter decay"}

PATCH_CATEGORIES = {0: "None", 1: "Arp", 2: "Bass", 3: "Bell", 4: "Classic", 5: "Drum",
                    6: "Keyboard", 7: "Lead", 8: "Movement", 9: "Pad", 10: "Poly",
                    11: "SFX", 12: "String", 13: "User", 14: "Vocal"}


# ---- Messages ----------------------------------------------------------------

def dump_request_bytes(location):
    """Sysex data (without F0/F7) asking for the current patch of synth 0 or 1."""
    return CIRCUIT_HEADER + (CMD_DUMP_REQUEST, location)


def replace_current_patch_bytes(location, patch_data):
    """Sysex data (without F0/F7) writing `patch_data` into a synth's live sound."""
    return CIRCUIT_HEADER + (CMD_REPLACE_CURRENT, location, 0) + tuple(patch_data)


def is_patch_dump(data):
    """True if `data` (bytes between F0 and F7) is a Replace Current Patch dump."""
    return (len(data) == DUMP_DATA_LEN
            and tuple(data[:5]) == CIRCUIT_HEADER
            and data[5] == CMD_REPLACE_CURRENT)


# ---- Decoding ----------------------------------------------------------------

def patch_bytes(data):
    """The 340 payload bytes of a dump, for comparison or re-sending."""
    return tuple(data[PATCH_DATA_OFFSET:])


def decode_patch(data):
    """Decode a patch dump into a dict. `data` excludes F0/F7."""
    location = data[6]
    p = data[PATCH_DATA_OFFSET:]
    name = bytes(p[0:16]).decode("ascii", errors="replace").rstrip()

    modmatrix = []
    for n in range(20):
        o = MODMATRIX_BASE + n * 4
        modmatrix.append(dict(src1=p[o], src2=p[o + 1], depth=p[o + 2] - 64, dest=p[o + 3]))

    macros = []
    for k in range(8):
        base = MACRO_BASE + k * MACRO_STRIDE
        slots = []
        for s in range(4):
            o = base + 1 + s * 4
            dest, start, end, depth = p[o], p[o + 1], p[o + 2], p[o + 3]
            if dest != 0:
                slots.append(dict(dest=dest, start=start, end=end, depth=depth - 64))
        macros.append(dict(position=p[base], slots=slots))

    return dict(location=location, name=name, category=p[16], genre=p[17],
                macros=macros, modmatrix=modmatrix)


def dest_label(dest, patch):
    """
    (headline, suffix) for a macro destination.
    Plain parameter  -> ("Filter freq", "")
    Mod matrix depth -> ("LFO1+ → filter freq", "MM1")
    """
    if 51 <= dest <= 70:
        mm = patch["modmatrix"][dest - 51]
        srcs = [MOD_SOURCES.get(mm["src1"], f"src{mm['src1']}")]
        if mm["src2"] != 0:
            srcs.append(MOD_SOURCES.get(mm["src2"], f"src{mm['src2']}"))
        target = MOD_DESTS.get(mm["dest"], f"dst{mm['dest']}")
        return f"{'×'.join(srcs)} → {target}", f"MM{dest - 50}"
    return MACRO_DEST_NAMES.get(dest, f"dest#{dest}"), ""


def category_name(code):
    return PATCH_CATEGORIES.get(code, f"cat {code}")
