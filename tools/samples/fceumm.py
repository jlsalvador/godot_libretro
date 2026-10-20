#!/usr/bin/env python3
"""FCEUmm sample ROM (NROM-128 with battery-backed SRAM).

Behaviour:

  * the backdrop colour cycles red -> green -> blue -> red ... every 60
    frames (~1 s on NTSC)
  * the noise channel plays continuously at a low volume
  * the battery-backed SRAM holds a 16-bit little-endian counter at
    $6000/$6001. On a fresh save it is initialised to 1 at boot; afterwards it
    is incremented once per second. A "SV" signature at $6002/$6003 tells a
    fresh SRAM from a valid save (set PERSIST_SAVE = False to restart at 1 on
    every boot instead)
  * while ANY button of pad 1 or pad 2 is held, pulse 1 plays a ~440 Hz tone;
    on every initial press (rising edge, tracked per button) the colour jumps
    to the next one right away. The 1-second timer (colour step + save
    counter) is independent of the presses and never restarts

Timing is driven by a real NMI handler (it only bumps a frame counter), so the
main loop never misses a VBlank the way polling $2002 can.

The code is written with a tiny assembler (labels + branch fix-ups) instead of
hand-patched byte offsets.
"""

import sys

ORG = 0x8000                  # PRG bank is mapped at $8000-$BFFF (mirrored at $C000)
PERSIST_SAVE = True
FRAMES_PER_STEP = 60

# Hardware registers
PPUCTRL, PPUMASK, PPUSTATUS = 0x2000, 0x2001, 0x2002
PPUADDR, PPUDATA = 0x2006, 0x2007
PULSE1_VOL, PULSE1_SWEEP, PULSE1_LO, PULSE1_HI = 0x4000, 0x4001, 0x4002, 0x4003
NOISE_VOL, NOISE_PERIOD, NOISE_LEN = 0x400C, 0x400E, 0x400F
DMC_FREQ, APU_STATUS, JOY1, JOY2 = 0x4010, 0x4015, 0x4016, 0x4017  # $4017 write = APU frame counter

# Zero page variables
ZP_VBL = 0x00                 # incremented by the NMI handler
ZP_FRAMES = 0x01              # frames since the last colour change
ZP_COLOR = 0x02               # 0 = red, 1 = green, 2 = blue
ZP_PAD = 0x03                 # bitmask of the buttons held (pad 1 | pad 2)
ZP_PREV = 0x04                # ZP_PAD of the previous frame
ZP_EDGE = 0x05                # bitmask of the buttons whose press starts this frame
ZP_TMP = 0x06

# Battery-backed SRAM layout
SAVE_LO, SAVE_HI = 0x6000, 0x6001
SAVE_MAGIC0, SAVE_MAGIC1 = 0x6002, 0x6003
MAGIC0, MAGIC1 = ord("S"), ord("V")

# FCEUmm palette indices
PAL_RED = 0x16
PAL_GREEN = 0x2A
PAL_BLUE = 0x12

SYMBOLS = {k: v for k, v in globals().items() if k.isupper() and isinstance(v, int)}

_OPS = {
    "ADC": {"imm": 0x69},
    "AND": {"imm": 0x29, "zp": 0x25},
    "BIT": {"abs": 0x2C},
    "CLC": {"imp": 0x18},
    "CLD": {"imp": 0xD8},
    "CMP": {"imm": 0xC9, "zp": 0xC5},
    "DEX": {"imp": 0xCA},
    "DEY": {"imp": 0x88},
    "EOR": {"imm": 0x49},
    "INC": {"zp": 0xE6, "abs": 0xEE},
    "INX": {"imp": 0xE8},
    "JMP": {"abs": 0x4C},
    "JSR": {"abs": 0x20},
    "LDA": {"imm": 0xA9, "zp": 0xA5, "abs": 0xAD, "absx": 0xBD},
    "LDX": {"imm": 0xA2, "zp": 0xA6},
    "LDY": {"imm": 0xA0},
    "LSR": {"imp": 0x4A},
    "ORA": {"imm": 0x09, "zp": 0x05, "abs": 0x0D},
    "ROL": {"zp": 0x26},
    "RTI": {"imp": 0x40},
    "RTS": {"imp": 0x60},
    "SEI": {"imp": 0x78},
    "STA": {"zp": 0x85, "abs": 0x8D},
    "STX": {"zp": 0x86, "abs": 0x8E},
    "TAX": {"imp": 0xAA},
    "TXS": {"imp": 0x9A},
}
_BRANCHES = {"BPL": 0x10, "BMI": 0x30, "BCC": 0x90, "BCS": 0xB0, "BNE": 0xD0, "BEQ": 0xF0}


class Asm:
    """Minimal 6502 assembler: a("LDA #$3F"), a.label("loop"), a("BNE loop")."""

    def __init__(self, origin, symbols):
        self.origin = origin
        self.syms = symbols
        self.code = bytearray()
        self.labels = {}
        self.fixups = []  # (position in code, label name, "abs" | "rel")

    def pc(self):
        return self.origin + len(self.code)

    def label(self, name):
        assert name not in self.labels, f"duplicate label {name}"
        self.labels[name] = self.pc()

    def data(self, *values):
        self.code.extend(values)

    def _try_const(self, tok):
        tok = tok.strip()
        if tok.startswith("$"):
            return int(tok[1:], 16)
        if tok.startswith("%"):
            return int(tok[1:], 2)
        if tok.isdigit():
            return int(tok)
        return self.syms.get(tok)

    def _abs16(self, tok):
        value = self._try_const(tok)
        if value is None:                       # label, resolved in finish()
            self.fixups.append((len(self.code), tok.strip(), "abs"))
            value = 0
        self.code.extend((value & 0xFF, value >> 8))

    def __call__(self, text):
        mnemonic, _, arg = text.partition(" ")
        mnemonic, arg = mnemonic.upper(), arg.strip()

        if mnemonic in _BRANCHES:
            self.code.extend((_BRANCHES[mnemonic], 0))
            self.fixups.append((len(self.code) - 1, arg, "rel"))
            return

        modes = _OPS[mnemonic]
        if not arg:
            self.code.append(modes["imp"])
        elif arg.startswith("#"):
            value = self._try_const(arg[1:])
            assert value is not None and 0 <= value <= 0xFF, f"bad immediate: {text}"
            self.code.extend((modes["imm"], value))
        elif arg.upper().endswith(",X"):
            self.code.append(modes["absx"])
            self._abs16(arg[:-2])
        else:
            value = self._try_const(arg)
            if value is not None and value < 0x100 and "zp" in modes:
                self.code.extend((modes["zp"], value))
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
                # 6502 branches are relative to the address of the NEXT instruction
                delta = target - (self.origin + pos + 1)
                assert -128 <= delta <= 127, f"branch to {name} out of range ({delta})"
                self.code[pos] = delta & 0xFF
        return bytes(self.code)


def build() -> bytes:
    rom_header = bytearray(16)
    rom_header[0:4] = b"NES\x1a"
    rom_header[4] = 1             # 1 x 16 KiB PRG
    rom_header[5] = 0             # no CHR-ROM -> 8 KiB CHR-RAM
    rom_header[6] = 0x02          # battery-backed save RAM

    a = Asm(ORG, SYMBOLS)

    # ---- reset: hardware init -------------------------------------------
    a.label("reset")
    a("SEI")
    a("CLD")
    a("LDX #$40")
    a("STX JOY2")                 # $4017 = $40: APU frame IRQ off
    a("LDX #$FF")
    a("TXS")
    a("INX")                      # X = 0
    a("STX PPUCTRL")              # NMI off
    a("STX PPUMASK")              # rendering off
    a("STX DMC_FREQ")             # DMC IRQ off
    a("STX APU_STATUS")           # all sound channels off

    a("BIT PPUSTATUS")
    a.label("vwait1")             # first VBlank
    a("BIT PPUSTATUS")
    a("BPL vwait1")

    a("LDA #0")                   # zero page state (RAM is undefined at power-on)
    for var in ("ZP_VBL", "ZP_FRAMES", "ZP_COLOR", "ZP_PAD", "ZP_PREV", "ZP_EDGE", "ZP_TMP"):
        a(f"STA {var}")

    a.label("vwait2")             # second VBlank: the PPU is now warmed up
    a("BIT PPUSTATUS")
    a("BPL vwait2")

    # ---- save RAM: first boot -> counter = 1 -----------------------------
    if PERSIST_SAVE:
        a("LDA SAVE_MAGIC0")
        a("CMP #MAGIC0")
        a("BNE save_init")
        a("LDA SAVE_MAGIC1")
        a("CMP #MAGIC1")
        a("BEQ save_ok")
    a.label("save_init")
    a("LDA #1")
    a("STA SAVE_LO")
    a("LDA #0")
    a("STA SAVE_HI")
    a("LDA #MAGIC0")
    a("STA SAVE_MAGIC0")
    a("LDA #MAGIC1")
    a("STA SAVE_MAGIC1")
    a.label("save_ok")

    # ---- video: blank nametable 0 and CHR tile 0 (rendering is off) ------
    a("BIT PPUSTATUS")            # reset the $2006 write latch
    a("LDA #$20")
    a("STA PPUADDR")
    a("LDA #0")
    a("STA PPUADDR")              # PPU address = $2000
    a("LDX #4")                   # 4 pages x 256 = 1 KiB (tiles + attributes)
    a("LDY #0")
    a.label("clr_nt")
    a("STA PPUDATA")
    a("DEY")
    a("BNE clr_nt")
    a("DEX")
    a("BNE clr_nt")
    a("STA PPUADDR")
    a("STA PPUADDR")              # PPU address = $0000 (A is still 0)
    a("LDX #16")                  # tile 0 = 16 zero bytes -> every pixel is the backdrop
    a.label("clr_chr")
    a("STA PPUDATA")
    a("DEX")
    a("BNE clr_chr")

    a("JSR set_backdrop")         # colour 0 = red

    # ---- audio ------------------------------------------------------------
    a("LDA #$BF")                 # duty 50%, length counter halted, constant volume 15
    a("STA PULSE1_VOL")
    a("LDA #$00")
    a("STA PULSE1_SWEEP")         # sweep off
    a("LDA #$FD")
    a("STA PULSE1_LO")            # period 253 -> 1789773 / (16 * 254) = ~440 Hz
    a("LDA #$34")                 # noise: halted, constant volume 4
    a("STA NOISE_VOL")
    a("LDA #$07")
    a("STA NOISE_PERIOD")         # mode 0 (long sequence), period index 7
    a("LDA #%00001000")
    a("STA APU_STATUS")           # enable noise only (pulse 1 is enabled while a button is held)
    a("STA NOISE_LEN")            # load a length counter (it never expires: halt is set)

    # ---- turn the picture and NMI on --------------------------------------
    a("LDA #$0A")                 # bit 3 = show background, bit 1 = show it in the leftmost 8 px
    a("STA PPUMASK")
    a("LDA #$80")
    a("STA PPUCTRL")              # NMI on, background pattern table $0000

    # ---- main loop: one iteration per frame -------------------------------
    a.label("main")
    a("LDA ZP_VBL")
    a.label("wait_nmi")
    a("CMP ZP_VBL")
    a("BEQ wait_nmi")             # sleep until the NMI handler bumps ZP_VBL

    a("JSR read_pad")
    a("STA ZP_PAD")
    a("LDA ZP_PREV")
    a("EOR #$FF")
    a("AND ZP_PAD")               # edge = pressed now AND not pressed last frame (per button)
    a("STA ZP_EDGE")
    a("LDA ZP_PAD")
    a("STA ZP_PREV")

    a("LDA #%00001000")           # noise always on...
    a("LDX ZP_PAD")
    a("BEQ audio_set")
    a("LDA #%00001001")           # ...plus pulse 1 while a button is held
    a.label("audio_set")
    a("STA APU_STATUS")

    a("LDA ZP_EDGE")
    a("BEQ tick")
    a("LDA #$08")                 # (re)load the pulse length counter and restart the phase
    a("STA PULSE1_HI")
    a("JSR next_color")           # jump to the next colour right away

    a.label("tick")
    a("INC ZP_FRAMES")
    a("LDA ZP_FRAMES")
    a("CMP #FRAMES_PER_STEP")
    a("BNE main")
    a("LDA #0")
    a("STA ZP_FRAMES")
    a("JSR next_color")
    a("JSR bump_save")
    a("JMP main")

    # ---- read_pad: A = bitmask of the buttons held on pad 1 | pad 2 ------
    # (bit 7 = A ... bit 0 = Right). ZP_TMP starts as a sentinel bit that
    # falls into carry after exactly 8 reads.
    a.label("read_pad")
    a("LDA #1")
    a("STA JOY1")
    a("STA ZP_TMP")
    a("LSR")                      # A = 0
    a("STA JOY1")                 # latch both pads
    a.label("rp_loop")
    a("LDA JOY1")
    a("ORA JOY2")
    a("LSR")                      # button bit -> carry
    a("ROL ZP_TMP")
    a("BCC rp_loop")
    a("LDA ZP_TMP")
    a("RTS")

    # ---- next_color: advance ZP_COLOR modulo 3 and show it ----------------
    a.label("next_color")
    a("LDA ZP_COLOR")
    a("CLC")
    a("ADC #1")
    a("CMP #3")
    a("BCC nc_store")
    a("LDA #0")
    a.label("nc_store")
    a("STA ZP_COLOR")
    a("JMP set_backdrop")         # tail call; its RTS returns to our caller

    # ---- set_backdrop: $3F00 = palette[ZP_COLOR] (call during VBlank) -----
    a.label("set_backdrop")
    a("LDX ZP_COLOR")
    a("BIT PPUSTATUS")
    a("LDA #$3F")
    a("STA PPUADDR")
    a("LDA #0")
    a("STA PPUADDR")
    a("LDA palette,X")
    a("STA PPUDATA")
    a("LDA #0")
    a("STA PPUADDR")              # leave the PPU address / scroll at $0000, otherwise
    a("STA PPUADDR")              # the picture is drawn from the palette address
    a("RTS")

    # ---- bump_save: 16-bit increment of the SRAM counter ------------------
    a.label("bump_save")
    a("INC SAVE_LO")
    a("BNE bs_done")
    a("INC SAVE_HI")
    a.label("bs_done")
    a("RTS")

    # ---- interrupt handlers ------------------------------------------------
    a.label("nmi")
    a("INC ZP_VBL")               # does not touch A/X/Y
    a("RTI")
    a.label("irq")
    a("RTI")

    a.label("palette")
    a.data(PAL_RED, PAL_GREEN, PAL_BLUE)

    code = a.finish()
    assert len(code) <= 0x3FFA, "code collides with vectors"

    # ---- pack into a 16 KiB PRG bank ---------------------------------------
    prg = bytearray([0xEA] * 0x4000)
    prg[:len(code)] = code
    for offset, name in ((0x3FFA, "nmi"), (0x3FFC, "reset"), (0x3FFE, "irq")):
        addr = a.labels[name]
        prg[offset:offset + 2] = bytes([addr & 0xFF, addr >> 8])

    return bytes(rom_header) + bytes(prg)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "fceumm_sample.nes"
    with open(out, "wb") as f:
        f.write(build())
    print(f"wrote {out}")
