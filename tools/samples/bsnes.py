#!/usr/bin/env python3
"""bsnes sample ROM (64 KiB LoROM, battery-backed SRAM).

Behaviour:

  * the backdrop colour (CGRAM[0]) cycles red -> green -> blue -> red ...
    once per second (60 VBlanks, NTSC)
  * the S-DSP noise generator plays all the time (quiet hiss, voice 0)
  * the battery-backed save is a 16-bit little-endian counter at $70:0000.
    On a blank SRAM it is initialised to 1 (a "SV" signature at $70:0002 tells
    a blank SRAM from a valid save); afterwards it is incremented once per
    second
  * on every new button press (any button of pad 1, edge detected per
    button) the colour advances immediately, and a ~440 Hz tone (S-DSP voice 1,
    looping BRR sine) plays for as long as at least one button is held. The
    1-second timer is independent of the presses and never restarts

How it works:

  * 65816 resets into emulation mode; the program switches to native mode with
    8-bit A/X/Y, direct page $0000 and data bank $00. NMI/IRQ stay disabled;
    the main loop polls the VBlank flag in $4210 (RDNMI).
  * the pad is read by hand through $4016 (auto-joypad is off), 16 bits per
    frame; B,Y,Select,Start,Up,Down,Left,Right,A,X,L,R end up in bits 15..4.
  * LoROM header lives at file offset $7FC0 ($00:FFC0); SRAM is mapped by the
    cartridge at $70:0000-$7FFF, not at $00:6000.
  * audio: the S-CPU uploads a small SPC700 program, a sample directory and
    one BRR block through the IPL boot protocol on $2140-$2143, then jumps to
    it. The SPC700 program configures the S-DSP and watches port 0: bit 0 set
    = tone on (voice 1 volume up), clear = tone off.
  * timing assumes NTSC: 60 frames ~ 1 s.

Colours are 15-bit BGR: red $001F, green $03E0, blue $7C00.
"""

import math
import sys

ORG = 0x8000                  # bank $00 mirror of file offset 0 (LoROM)

RED, GREEN, BLUE = 0x001F, 0x03E0, 0x7C00
FRAMES_PER_STEP = 60

# Hardware registers
INIDISP, CGADD, CGDATA = 0x2100, 0x2121, 0x2122
APUIO0, APUIO1, APUIO2 = 0x2140, 0x2141, 0x2142
JOYSER0 = 0x4016
NMITIMEN, WRIO, MDMAEN, HDMAEN, RDNMI = 0x4200, 0x4201, 0x420B, 0x420C, 0x4210

# Direct page variables
ZP_FRAMES = 0x00              # frames since the last second tick
ZP_COLOR = 0x01               # 0 = red, 1 = green, 2 = blue
ZP_PAD, ZP_PAD_HI = 0x02, 0x03        # buttons held now (16 bit)
ZP_PREV = 0x04                # buttons held last frame (16 bit)
ZP_EDGE, ZP_EDGE_HI = 0x06, 0x07      # buttons whose press starts this frame
ZP_SRC = 0x10                 # SPC upload: source pointer (16 bit)
ZP_DST = 0x12                 # SPC upload: destination address (16 bit)
ZP_LEN = 0x14                 # SPC upload: block length (1..127)
ZP_HDR = 0x15                 # SPC upload: block header value for port 0

# Battery-backed SRAM ($70:0000, LoROM)
SAVE_CNT = 0x700000
SAVE_MAGIC0, SAVE_MAGIC1 = 0x700002, 0x700003
MAGIC0, MAGIC1 = ord("S"), ord("V")

# SPC700 RAM layout
SPC_DIR = 0x0200              # sample directory (DIR = $02)
SPC_SAMPLE = 0x0300           # BRR block
SPC_CODE = 0x0400             # program entry

SYMBOLS = {k: v for k, v in globals().items() if k.isupper() and isinstance(v, int)}

# --------------------------------------------------------------------------
# 65816 assembler (explicit operand widths: "LDA #$8F" is 8 bit, "LDA.w #$BBAA"
# is 16 bit; the caller picks them with REP/SEP)
# --------------------------------------------------------------------------
_OPS = {
    "AND": {"imm": 0x29, "dp": 0x25},
    "CLC": {"imp": 0x18},
    "CLD": {"imp": 0xD8},
    "CMP": {"imm": 0xC9, "dp": 0xC5, "abs": 0xCD},
    "CPY": {"dp": 0xC4},
    "DEX": {"imp": 0xCA},
    "EOR": {"imm": 0x49},
    "INC": {"imp": 0x1A, "dp": 0xE6},
    "INY": {"imp": 0xC8},
    "JMP": {"abs": 0x4C},
    "JSR": {"abs": 0x20},
    "LDA": {"imm": 0xA9, "dp": 0xA5, "abs": 0xAD, "absx": 0xBD, "long": 0xAF, "indy": 0xB1},
    "LDX": {"imm": 0xA2, "dp": 0xA6},
    "LDY": {"imm": 0xA0},
    "LSR": {"imp": 0x4A},
    "ORA": {"imm": 0x09, "dp": 0x05},
    "PHK": {"imp": 0x4B},
    "PLB": {"imp": 0xAB},
    "REP": {"imm": 0xC2},
    "ROL": {"dp": 0x26},
    "RTI": {"imp": 0x40},
    "RTS": {"imp": 0x60},
    "SEI": {"imp": 0x78},
    "SEP": {"imm": 0xE2},
    "STA": {"dp": 0x85, "abs": 0x8D, "long": 0x8F},
    "STZ": {"dp": 0x64, "dpx": 0x74, "abs": 0x9C, "absx": 0x9E},
    "TCD": {"imp": 0x5B},
    "TCS": {"imp": 0x1B},
    "TYA": {"imp": 0x98},
    "XCE": {"imp": 0xFB},
}
_BRANCHES = {"BPL": 0x10, "BCC": 0x90, "BRA": 0x80, "BNE": 0xD0, "BEQ": 0xF0}


class Asm:
    """a("LDA #$3F"), a.label("loop"), a("BNE loop"), a("JSR routine")."""

    def __init__(self, origin, symbols):
        self.origin = origin
        self.syms = symbols
        self.code = bytearray()
        self.labels = {}
        self.fixups = []          # (position in code, label name, "abs" | "rel")

    def pc(self):
        return self.origin + len(self.code)

    def label(self, name):
        assert name not in self.labels, f"duplicate label {name}"
        self.labels[name] = self.pc()

    def data(self, *values):
        self.code.extend(values)

    def _const(self, tok):
        tok = tok.strip()
        if tok.startswith("$"):
            return int(tok[1:], 16)
        if tok.startswith("%"):
            return int(tok[1:], 2)
        if tok.isdigit():
            return int(tok)
        return self.syms.get(tok)

    def _abs16(self, tok):
        value = self._const(tok)
        if value is None:                       # label, resolved in finish()
            self.fixups.append((len(self.code), tok.strip(), "abs"))
            value = 0
        assert 0 <= value <= 0xFFFF, f"bad address: {tok}"
        self.code.extend((value & 0xFF, value >> 8))

    def __call__(self, text):
        mnemonic, _, arg = text.partition(" ")
        mnemonic, arg = mnemonic.upper(), arg.strip()
        wide = mnemonic.endswith(".W")
        mnemonic = mnemonic.split(".")[0]

        if mnemonic in _BRANCHES:
            self.code.extend((_BRANCHES[mnemonic], 0))
            self.fixups.append((len(self.code) - 1, arg, "rel"))
            return

        modes = _OPS[mnemonic]
        if not arg:
            self.code.append(modes["imp"])
        elif arg.startswith("#"):
            self.code.append(modes["imm"])
            if wide:
                self._abs16(arg[1:])
            else:
                value = self._const(arg[1:])
                assert value is not None and 0 <= value <= 0xFF, f"bad immediate: {text}"
                self.code.append(value)
        elif arg.startswith("(") and arg.upper().endswith("),Y"):
            value = self._const(arg[1:-3])
            assert value is not None and value < 0x100, f"bad pointer: {text}"
            self.code.extend((modes["indy"], value))
        elif arg.upper().endswith(",X"):
            value = self._const(arg[:-2])
            if value is not None and value < 0x100 and "dpx" in modes:
                self.code.extend((modes["dpx"], value))
            else:
                self.code.append(modes["absx"])
                self._abs16(arg[:-2])
        else:
            value = self._const(arg)
            if value is not None and value > 0xFFFF:
                self.code.append(modes["long"])
                self.code.extend((value & 0xFF, (value >> 8) & 0xFF, value >> 16))
            elif value is not None and value < 0x100 and "dp" in modes:
                self.code.extend((modes["dp"], value))
            else:
                self.code.append(modes["abs"])
                self._abs16(arg)

    def finish(self):
        for pos, name, kind in self.fixups:
            target = self.labels[name]
            if kind == "abs":
                self.code[pos] = target & 0xFF
                self.code[pos + 1] = target >> 8
            else:
                # branches are relative to the address of the NEXT instruction
                delta = target - (self.origin + pos + 1)
                assert -128 <= delta <= 127, f"branch to {name} out of range ({delta})"
                self.code[pos] = delta & 0xFF
        return bytes(self.code)


# --------------------------------------------------------------------------
# SPC700 program (assembled by hand: it is small)
# --------------------------------------------------------------------------
# S-DSP register / value pairs written at start-up. Voice 0 = noise, voice 1 =
# BRR sine tone (silent until a button is held).
DSP_INIT = [
    (0x6C, 0x2E),   # FLG: unmuted, echo writes off, noise clock 14
    (0x0C, 0x7F),   # MVOLL
    (0x1C, 0x7F),   # MVOLR
    (0x2C, 0x00),   # EVOLL
    (0x3C, 0x00),   # EVOLR
    (0x0D, 0x00),   # EFB
    (0x2D, 0x00),   # PMON
    (0x4D, 0x00),   # EON
    (0x5C, 0x00),   # KOF
    (0x5D, SPC_DIR >> 8),   # DIR
    (0x3D, 0x01),   # NON: voice 0 outputs noise
    # voice 0: noise, constant quiet volume
    (0x00, 0x28), (0x01, 0x28),             # VOLL / VOLR
    (0x02, 0x00), (0x03, 0x10),             # pitch $1000
    (0x04, 0x00),                           # SRCN 0
    (0x05, 0x00), (0x06, 0x00),             # ADSR off -> GAIN
    (0x07, 0x7F),                           # GAIN: direct, maximum
    # voice 1: tone, volume 0 until a button is pressed
    (0x10, 0x00), (0x11, 0x00),             # VOLL / VOLR
    (0x12, 0x85), (0x13, 0x03),             # pitch $0385 -> ~440 Hz (16-sample sine)
    (0x14, 0x00),                           # SRCN 0
    (0x15, 0x00), (0x16, 0x00),
    (0x17, 0x7F),
    (0x4C, 0x03),   # KON: key on voices 0 and 1
]
TONE_VOLUME = 0x50


def build_spc_program():
    code = bytearray()
    labels = {}
    fixups = []

    def emit(*bs):
        code.extend(bs)

    def label(name):
        labels[name] = SPC_CODE + len(code)

    def abs_ref(opcode, name):
        emit(opcode, 0, 0)
        fixups.append((len(code) - 2, "abs", name))

    def rel_ref(opcode, name):
        emit(opcode, 0)
        fixups.append((len(code) - 1, "rel", name))

    emit(0x8F, 0x30, 0xF1)                  # MOV $F1,#$30  (clear input ports 0-3)
    emit(0xCD, 0xFF)                        # MOV X,#$FF
    emit(0xBD)                              # MOV SP,X
    emit(0x8F, 0x00, 0x10)                  # MOV $10,#0   (last tone state)
    emit(0xCD, 0x00)                        # MOV X,#0
    label("init")
    abs_ref(0xF5, "dsp_table")              # MOV A,!table+X
    emit(0xC4, 0xF2)                        # MOV $F2,A    (DSP register)
    emit(0x3D)                              # INC X
    abs_ref(0xF5, "dsp_table")              # MOV A,!table+X
    emit(0xC4, 0xF3)                        # MOV $F3,A    (DSP value)
    emit(0x3D)                              # INC X
    emit(0xC8, 2 * len(DSP_INIT))           # CMP X,#len
    rel_ref(0xD0, "init")                   # BNE init

    label("main")
    emit(0xE4, 0xF4)                        # MOV A,$F4    (port 0 from the S-CPU)
    emit(0x28, 0x01)                        # AND A,#1
    emit(0x64, 0x10)                        # CMP A,$10
    rel_ref(0xF0, "main")                   # BEQ main     (unchanged)
    emit(0xC4, 0x10)                        # MOV $10,A
    emit(0xE4, 0x10)                        # MOV A,$10    (set flags)
    rel_ref(0xF0, "tone_off")               # BEQ tone_off
    emit(0xE8, TONE_VOLUME)                 # MOV A,#volume
    rel_ref(0x2F, "set_vol")                # BRA set_vol
    label("tone_off")
    emit(0xE8, 0x00)                        # MOV A,#0
    label("set_vol")
    emit(0x8F, 0x10, 0xF2)                  # MOV $F2,#$10  (VOLL voice 1)
    emit(0xC4, 0xF3)                        # MOV $F3,A
    emit(0x8F, 0x11, 0xF2)                  # MOV $F2,#$11  (VOLR voice 1)
    emit(0xC4, 0xF3)                        # MOV $F3,A
    rel_ref(0x2F, "main")                   # BRA main

    label("dsp_table")
    for reg, val in DSP_INIT:
        emit(reg, val)

    for pos, kind, name in fixups:
        target = labels[name]
        if kind == "abs":
            code[pos] = target & 0xFF
            code[pos + 1] = target >> 8
        else:
            delta = target - (SPC_CODE + pos + 1)
            assert -128 <= delta <= 127, f"SPC branch to {name} out of range ({delta})"
            code[pos] = delta & 0xFF
    return bytes(code)


def build_brr_sine():
    """One looping BRR block: 16-sample sine, range 12, filter 0."""
    nibbles = [round(7 * math.sin(2 * math.pi * k / 16)) & 0xF for k in range(16)]
    body = bytes((nibbles[i] << 4) | nibbles[i + 1] for i in range(0, 16, 2))
    return bytes([(12 << 4) | 0b0011]) + body       # range 12, filter 0, loop + end


# --------------------------------------------------------------------------
# ROM
# --------------------------------------------------------------------------
def build() -> bytes:
    spc_dir = bytes([SPC_SAMPLE & 0xFF, SPC_SAMPLE >> 8, SPC_SAMPLE & 0xFF, SPC_SAMPLE >> 8])
    spc_sample = build_brr_sine()
    spc_prog = build_spc_program()
    # IPL protocol: first block header is $CC, later ones (and the final jump)
    # use previous block length + 1 (non-zero, and not the next data index)
    blocks = [(SPC_DIR, "spc_dir", len(spc_dir)),
              (SPC_SAMPLE, "spc_sample", len(spc_sample)),
              (SPC_CODE, "spc_prog", len(spc_prog))]
    assert all(0 < n <= 127 for _, _, n in blocks), "SPC block too long for one transfer"

    a = Asm(ORG, SYMBOLS)

    # ---- reset: CPU + PPU init ---------------------------------------------
    a.label("reset")
    a("SEI")
    a("CLC")
    a("XCE")                      # native mode
    a("CLD")
    a("SEP #$30")                 # 8-bit A, X, Y
    a("REP #$20")
    a("LDA.w #$01FF")
    a("TCS")                      # stack at $01FF
    a("LDA.w #$0000")
    a("TCD")                      # direct page $0000
    a("SEP #$20")
    a("PHK")
    a("PLB")                      # data bank = program bank = $00

    a("LDA #$8F")
    a("STA INIDISP")              # forced blank while we set things up
    a("LDX #$33")
    a.label("clr_ppu")            # $2101-$2133 = 0 (all layers off, backdrop only)
    a("STZ $2100,X")
    a("DEX")
    a("BNE clr_ppu")
    a("STZ NMITIMEN")             # NMI, IRQ and auto-joypad off
    a("STZ MDMAEN")
    a("STZ HDMAEN")
    a("LDA #$FF")
    a("STA WRIO")

    a("LDX #$1F")
    a.label("clr_zp")             # direct page variables = 0
    a("STZ $00,X")
    a("DEX")
    a("BPL clr_zp")

    # ---- save RAM: blank -> counter = 1 -------------------------------------
    a("LDA SAVE_MAGIC0")
    a("CMP #MAGIC0")
    a("BNE save_init")
    a("LDA SAVE_MAGIC1")
    a("CMP #MAGIC1")
    a("BEQ save_ok")
    a.label("save_init")
    a("REP #$20")
    a("LDA.w #1")
    a("STA SAVE_CNT")
    a("SEP #$20")
    a("LDA #MAGIC0")
    a("STA SAVE_MAGIC0")
    a("LDA #MAGIC1")
    a("STA SAVE_MAGIC1")
    a.label("save_ok")

    # ---- audio: upload the SPC700 program and start it ----------------------
    a("REP #$20")
    a("LDA.w #$BBAA")
    a.label("wait_ipl")           # the IPL ROM announces itself with $AA/$BB
    a("CMP APUIO0")
    a("BNE wait_ipl")
    a("SEP #$20")

    header = 0xCC
    for dest, name, length in blocks:
        a("REP #$20")
        a(f"LDA.w #{name}")
        a("STA ZP_SRC")
        a(f"LDA.w #{dest}")
        a("STA ZP_DST")
        a("SEP #$20")
        a(f"LDA #{length}")
        a("STA ZP_LEN")
        a(f"LDA #{header}")
        a("STA ZP_HDR")
        a("JSR spc_block")
        header = length + 1

    a("REP #$20")
    a(f"LDA.w #{SPC_CODE}")
    a("STA APUIO2")               # entry address
    a("SEP #$20")
    a("STZ APUIO1")               # port 1 = 0 -> execute
    a(f"LDA #{header}")
    a("STA APUIO0")
    a.label("wait_exec")
    a("CMP APUIO0")
    a("BNE wait_exec")
    a("STZ APUIO0")               # tone off

    # ---- first colour, then screen on ----------------------------------------
    a("JSR set_backdrop")         # ZP_COLOR = 0 -> red
    a("LDA #$0F")
    a("STA INIDISP")              # screen on, full brightness

    # ---- main loop: once per frame ---------------------------------------------
    a.label("main")
    a.label("wait_vbl")
    a("LDA RDNMI")
    a("BPL wait_vbl")             # bit 7 = VBlank started (read clears it)

    a("JSR read_pad")
    a("REP #$20")
    a("LDA ZP_PREV")
    a("EOR.w #$FFFF")
    a("AND ZP_PAD")               # edge = pressed now AND not pressed last frame
    a("STA ZP_EDGE")
    a("LDA ZP_PAD")
    a("STA ZP_PREV")
    a("SEP #$20")

    a("LDA ZP_PAD")               # tone on while any button is held
    a("ORA ZP_PAD_HI")
    a("BEQ tone_off")
    a("LDA #1")
    a("BRA tone_set")
    a.label("tone_off")
    a("LDA #0")
    a.label("tone_set")
    a("STA APUIO0")

    a("LDA ZP_EDGE")              # any new press -> next colour right away
    a("ORA ZP_EDGE_HI")
    a("BEQ no_press")
    a("JSR next_color")
    a.label("no_press")

    a("INC ZP_FRAMES")
    a("LDA ZP_FRAMES")
    a("CMP #FRAMES_PER_STEP")
    a("BNE frame_end")
    a("STZ ZP_FRAMES")
    a("JSR next_color")
    a("JSR bump_save")
    a.label("frame_end")
    a("JMP main")

    # ---- read_pad: ZP_PAD/ZP_PAD_HI = 16 bits from $4016 -------------------------
    a.label("read_pad")
    a("LDA #1")
    a("STA JOYSER0")
    a("STZ JOYSER0")              # latch
    a("STZ ZP_PAD")
    a("STZ ZP_PAD_HI")
    a("LDX #16")
    a.label("rp_loop")
    a("LDA JOYSER0")
    a("LSR")                      # button bit -> carry
    a("ROL ZP_PAD")
    a("ROL ZP_PAD_HI")
    a("DEX")
    a("BNE rp_loop")
    a("RTS")

    # ---- next_color: advance ZP_COLOR modulo 3 and show it -------------------------
    a.label("next_color")
    a("LDA ZP_COLOR")
    a("INC")
    a("CMP #3")
    a("BCC nc_store")
    a("LDA #0")
    a.label("nc_store")
    a("STA ZP_COLOR")
    a("JMP set_backdrop")         # tail call; its RTS returns to our caller

    # ---- set_backdrop: CGRAM[0] = colour[ZP_COLOR] (during VBlank / forced blank) -----
    a.label("set_backdrop")
    a("LDX ZP_COLOR")
    a("STZ CGADD")
    a("LDA pal_lo,X")
    a("STA CGDATA")
    a("LDA pal_hi,X")
    a("STA CGDATA")
    a("RTS")

    # ---- bump_save: 16-bit increment of the SRAM counter ---------------------------------
    a.label("bump_save")
    a("REP #$20")
    a("LDA SAVE_CNT")
    a("INC")
    a("STA SAVE_CNT")
    a("SEP #$20")
    a("RTS")

    # ---- spc_block: send ZP_LEN bytes from (ZP_SRC) to SPC RAM at ZP_DST ------------------
    a.label("spc_block")
    a("REP #$20")
    a("LDA ZP_DST")
    a("STA APUIO2")               # destination
    a("SEP #$20")
    a("LDA #1")
    a("STA APUIO1")               # non-zero = transfer
    a("LDA ZP_HDR")
    a("STA APUIO0")
    a.label("sb_w1")
    a("CMP APUIO0")
    a("BNE sb_w1")                # wait for the echo of the header
    a("LDY #0")
    a.label("sb_loop")
    a("LDA (ZP_SRC),Y")
    a("STA APUIO1")               # data first...
    a("TYA")
    a("STA APUIO0")               # ...then the byte index
    a.label("sb_w2")
    a("CMP APUIO0")
    a("BNE sb_w2")
    a("INY")
    a("CPY ZP_LEN")
    a("BNE sb_loop")
    a("RTS")

    # ---- interrupt stub -----------------------------------------------------------------------
    a.label("rti")
    a("RTI")

    # ---- data ---------------------------------------------------------------------------------
    a.label("pal_lo")
    a.data(*(c & 0xFF for c in (RED, GREEN, BLUE)))
    a.label("pal_hi")
    a.data(*(c >> 8 for c in (RED, GREEN, BLUE)))
    a.label("spc_dir")
    a.data(*spc_dir)
    a.label("spc_sample")
    a.data(*spc_sample)
    a.label("spc_prog")
    a.data(*spc_prog)

    code = a.finish()
    assert len(code) < 0x7FC0, "code collides with the header"

    # ---- 64 KiB LoROM image ------------------------------------------------------------------
    rom = bytearray(0x10000)
    rom[:len(code)] = code

    h = 0x7FC0                                   # header at $00:FFC0
    rom[h:h + 21] = b"BSNES SAMPLE".ljust(21)
    rom[h + 0x15] = 0x20                         # LoROM, SlowROM
    rom[h + 0x16] = 0x02                         # ROM + RAM + battery
    rom[h + 0x17] = 0x06                         # 64 KiB ROM
    rom[h + 0x18] = 0x03                         # 8 KiB SRAM
    rom[h + 0x19] = 0x01                         # USA (NTSC)
    rom[h + 0x1A] = 0x33                         # extended header not used
    rom[h + 0x1B] = 0x00

    def vec(offset, label):
        addr = a.labels[label]
        rom[offset:offset + 2] = bytes([addr & 0xFF, addr >> 8])

    for offset in (0x7FE4, 0x7FE6, 0x7FE8, 0x7FEA, 0x7FEE,      # native COP/BRK/ABORT/NMI/IRQ
                   0x7FF4, 0x7FF8, 0x7FFA, 0x7FFE):              # emulation COP/ABORT/NMI/IRQ
        vec(offset, "rti")
    vec(0x7FEC, "reset")
    vec(0x7FFC, "reset")

    # checksum: sum of all bytes with complement/checksum fields as $FFFF/$0000
    rom[h + 0x1C:h + 0x20] = bytes([0xFF, 0xFF, 0x00, 0x00])
    checksum = sum(rom) & 0xFFFF
    complement = checksum ^ 0xFFFF
    rom[h + 0x1C:h + 0x20] = bytes([complement & 0xFF, complement >> 8,
                                    checksum & 0xFF, checksum >> 8])
    return bytes(rom)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "bsnes_sample.sfc"
    with open(out, "wb") as f:
        f.write(build())
    print(f"wrote {out}")
