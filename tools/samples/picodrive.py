#!/usr/bin/env python3
"""PicoDrive sample ROM (64 KiB cartridge image with battery-backed save RAM).

Behaviour (same as the other sample ROMs):

  * the backdrop colour (palette 0, colour 0) cycles red -> green -> blue ->
    red ... once per second
  * the noise channel of the programmable sound generator plays all the time
    (quiet hiss)
  * the battery-backed save is a 16-bit little-endian counter at the start of
    the save RAM, followed by the signature "SV". A missing signature means a
    blank save: the counter is then initialised to 1; afterwards it is
    incremented once per second
  * on every new button press (any of the 8 buttons of pad 1, edge detected
    per button) the colour advances immediately, and tone channel 0 plays a
    ~440 Hz square wave for as long as at least one button is held. The
    1-second timer is independent of the presses and never restarts

How it works:

  * big-endian image: vector table at $000, header at $100, code at $200. The
    main CPU runs with interrupts masked and everything is polled
  * video: the VDP registers are written from a table, VRAM is cleared and
    the display is enabled with every plane empty, so the screen shows the
    backdrop colour; CRAM entry 0 is rewritten during VBlank. The main loop
    syncs to the start of VBlank (status bit 3, 0 -> 1 edge)
  * 1 second = 60 frames, or 50 when the version register reports the 50 Hz
    standard
  * audio: the sound generator is written directly through its port; no
    second CPU program is needed. Noise is white noise at a medium rate, tone
    channel 0 uses divider 34 (3,579,545 / (240 * 34) ~ 439 Hz)
  * pad: the 3-button protocol with the TH line toggled, active-low bits
    combined into one 8-bit mask (Up, Down, Left, Right, B, C, A, Start)
  * save RAM: 8-bit RAM on odd addresses starting at $200001, declared in the
    header; its enable register is written for cartridges that need it
  * the header checksum is the sum of all 16-bit words from $200 to the end
"""

import struct
import sys

ROM_SIZE = 0x10000
ENTRY = 0x200

RED, GREEN, BLUE = 0x000E, 0x00E0, 0x0E00           # 9-bit colour: ----BBB-GGG-RRR-

# Hardware addresses used by the program
SYMBOLS = {
    "VDP_DATA": 0xC00000, "VDP_CTRL": 0xC00004, "PSG": 0xC00011,
    "PAD_DATA": 0xA10003, "PAD_CTRL": 0xA10009, "VERSION": 0xA10001,
    "BOOT_REG": 0xA14000, "SAVE_ENABLE": 0xA130F1,
    # save RAM: odd addresses, byte 0 = counter low, 1 = counter high, 2 = 'S', 3 = 'V'
    "SAVE_LO": 0x200001, "SAVE_HI": 0x200003, "SAVE_M0": 0x200005, "SAVE_M1": 0x200007,
    # boot-check signature: 4 ASCII bytes the hardware compares at power-up
    "BOOT_SIGNATURE": 0x53454741,
}

# VDP register writes: 0x8000 | register << 8 | value
VDP_REGS = [
    0x8004,   # R0: no horizontal interrupt
    0x8104,   # R1: display OFF for now, 5-mode bit set (display is enabled at the end)
    0x8230,   # R2: plane A name table at $C000
    0x833C,   # R3: window at $F000
    0x8407,   # R4: plane B name table at $E000
    0x856C,   # R5: sprite table at $D800
    0x8600,   # R6
    0x8700,   # R7: backdrop = palette 0, colour 0
    0x8800,   # R8
    0x8900,   # R9
    0x8AFF,   # R10
    0x8B00,   # R11
    0x8C81,   # R12: 40-cell wide screen
    0x8D3F,   # R13: horizontal scroll table at $FC00
    0x8E00,   # R14
    0x8F02,   # R15: auto-increment 2
    0x9001,   # R16: 64x32 planes
    0x9100,   # R17
    0x9200,   # R18
]

# ---------------------------------------------------------------------------
# Tiny 68000 assembler: only the instruction forms this program uses
# ---------------------------------------------------------------------------
_SIZE_BITS = {"b": 0, "w": 1, "l": 2}                 # in ADDQ / CMPI / AND ... encodings
_MOVE_SIZE = {"b": 1, "w": 3, "l": 2}                 # in MOVE
_COND = {"bra": 0x0, "bsr": 0x1, "bhi": 0x2, "bls": 0x3, "bcc": 0x4, "bcs": 0x5,
         "bne": 0x6, "beq": 0x7, "bpl": 0xA, "bmi": 0xB}


def _split_ops(text):
    out, depth, cur = [], 0, ""
    for ch in text:
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


class M68k:
    """a("move.w #$2700,sr"); a.label("loop"); a("bne loop"); ..."""

    def __init__(self, origin, symbols):
        self.origin = origin
        self.syms = symbols
        self.code = bytearray()
        self.labels = {}
        self.fixups = []          # (position, kind, label): kind "abs32" or "rel16"

    def pc(self):
        return self.origin + len(self.code)

    def label(self, name):
        assert name not in self.labels, f"duplicate label {name}"
        self.labels[name] = self.pc()

    def word(self, w):
        self.code.extend(struct.pack(">H", w & 0xFFFF))

    def data(self, b):
        self.code.extend(b)

    def align(self, n):
        while len(self.code) % n:
            self.code.append(0)

    def _num(self, tok):
        tok = tok.strip()
        if tok.startswith("'"):
            return ord(tok[1])
        if tok.startswith("$"):
            return int(tok[1:], 16)
        if tok in self.syms:
            return self.syms[tok]
        try:
            return int(tok, 0)
        except ValueError:
            return None                                     # a label

    # -- effective addresses -------------------------------------------------
    def _ea(self, tok, size):
        """Return ((mode << 3) | reg, extension items). Items: ("w", v), ("l", v), ("abs", label)."""
        tok = tok.strip()
        low = tok.lower()
        if low[0] == "d" and low[1:].isdigit():
            return (0 << 3) | int(low[1:]), []
        if low[0] == "a" and low[1:].isdigit():
            return (1 << 3) | int(low[1:]), []
        if low.startswith("(a") and low.endswith(")+"):
            return (3 << 3) | int(low[2:-2]), []
        if low.startswith("(a") and low.endswith(")"):
            return (2 << 3) | int(low[2:-1]), []
        if tok.startswith("#"):
            value = self._num(tok[1:])
            assert value is not None, f"bad immediate: {tok}"
            if size == "l":
                return (7 << 3) | 4, [("l", value)]
            return (7 << 3) | 4, [("w", value & (0xFF if size == "b" else 0xFFFF))]
        value = self._num(tok)                              # absolute long
        if value is None:
            return (7 << 3) | 1, [("abs", tok)]
        return (7 << 3) | 1, [("l", value)]

    def _ext(self, items):
        for kind, value in items:
            if kind == "w":
                self.word(value)
            elif kind == "l":
                self.code.extend(struct.pack(">I", value & 0xFFFFFFFF))
            else:                                           # label address
                self.fixups.append((len(self.code), "abs32", value))
                self.code.extend(b"\x00\x00\x00\x00")

    # -- instructions --------------------------------------------------------------
    def __call__(self, text):
        mnemonic, _, rest = text.strip().partition(" ")
        mnemonic = mnemonic.lower()
        name, _, sz = mnemonic.partition(".")
        ops = _split_ops(rest)

        if name == "move":
            src, dst = ops
            if dst.lower() == "sr":                         # move #imm,sr
                self.word(0x46FC)
                self._ext(self._ea(src, "w")[1])
                return
            s_ea, s_ext = self._ea(src, sz)
            d_ea, d_ext = self._ea(dst, sz)
            d_mode, d_reg = d_ea >> 3, d_ea & 7
            self.word((_MOVE_SIZE[sz] << 12) | (d_reg << 9) | (d_mode << 6) | s_ea)
            self._ext(s_ext)
            self._ext(d_ext)
        elif name == "moveq":
            value = self._num(ops[0][1:])
            self.word(0x7000 | (int(ops[1][1:]) << 9) | (value & 0xFF))
        elif name == "lea":
            ea, ext = self._ea(ops[0], "l")
            self.word(0x41C0 | (int(ops[1][1:]) << 9) | ea)
            self._ext(ext)
        elif name in ("addq", "subq"):
            value = self._num(ops[0][1:])
            ea, ext = self._ea(ops[1], sz)
            base = 0x5000 if name == "addq" else 0x5100
            self.word(base | ((value & 7) << 9) | (_SIZE_BITS[sz] << 6) | ea)
            self._ext(ext)
        elif name in ("cmpi", "andi"):
            base = 0x0C00 if name == "cmpi" else 0x0200
            ea, ext = self._ea(ops[1], sz)
            self.word(base | (_SIZE_BITS[sz] << 6) | ea)
            self._ext(self._ea(ops[0], sz)[1])
            self._ext(ext)
        elif name in ("and", "or", "add"):                  # <ea>,Dn -> Dn
            base = {"and": 0xC000, "or": 0x8000, "add": 0xD000}[name]
            ea, ext = self._ea(ops[0], sz)
            self.word(base | (int(ops[1][1:]) << 9) | (_SIZE_BITS[sz] << 6) | ea)
            self._ext(ext)
        elif name == "adda":                                # adda.w <ea>,An
            ea, ext = self._ea(ops[0], sz)
            self.word(0xD0C0 | (int(ops[1][1:]) << 9) | ea)
            self._ext(ext)
        elif name in ("not", "tst"):
            base = 0x4600 if name == "not" else 0x4A00
            ea, ext = self._ea(ops[0], sz)
            self.word(base | (_SIZE_BITS[sz] << 6) | ea)
            self._ext(ext)
        elif name == "btst":                                # btst #n,Dn
            ea, _ = self._ea(ops[1], "w")
            self.word(0x0800 | ea)
            self.word(self._num(ops[0][1:]))
        elif name in ("lsl", "lsr"):                        # lsl/lsr #n,Dn
            count = self._num(ops[0][1:])
            reg = int(ops[1][1:])
            direction = 1 if name == "lsl" else 0
            self.word(0xE000 | ((count & 7) << 9) | (direction << 8) | (_SIZE_BITS[sz] << 6)
                      | (1 << 3) | reg)
        elif name in _COND:
            self.word(0x6000 | (_COND[name] << 8))
            self.fixups.append((len(self.code), "rel16", ops[0]))
            self.word(0)
        elif name == "dbra":
            self.word(0x51C8 | int(ops[0][1:]))
            self.fixups.append((len(self.code), "rel16", ops[1]))
            self.word(0)
        elif name == "jsr":
            self.word(0x4EB9)
            self._ext(self._ea(ops[0], "l")[1])
        elif name == "rts":
            self.word(0x4E75)
        elif name == "rte":
            self.word(0x4E73)
        elif name == "nop":
            self.word(0x4E71)
        else:
            raise ValueError(f"unknown instruction: {text}")

    def finish(self):
        for pos, kind, name in self.fixups:
            target = self.labels[name]
            if kind == "abs32":
                self.code[pos:pos + 4] = struct.pack(">I", target)
            else:
                # displacement is relative to the address of the extension word itself
                delta = target - (self.origin + pos)
                assert -0x8000 <= delta < 0x8000, f"branch to {name} out of range"
                self.code[pos:pos + 2] = struct.pack(">h", delta)
        return bytes(self.code)


# ---------------------------------------------------------------------------
# ROM
# ---------------------------------------------------------------------------
def build() -> bytes:
    a = M68k(ENTRY, {**SYMBOLS, "RED": RED, "GREEN": GREEN, "BLUE": BLUE})

    a.label("entry")
    a("move.w #$2700,sr")                     # supervisor mode, interrupts masked

    # Hardware boot check: write the unlock value when the version register says it exists
    a("move.b VERSION,d0")
    a("andi.b #$0F,d0")
    a("beq no_unlock")
    a("move.l #BOOT_SIGNATURE,BOOT_REG")
    a.label("no_unlock")

    a("lea VDP_CTRL,a6")
    a("lea VDP_DATA,a5")
    a("move.w (a6),d0")                       # read status once (resets the command latch)
    a("lea vdp_regs,a0")
    a("moveq #18,d1")
    a.label("vdp_init")
    a("move.w (a0)+,(a6)")
    a("dbra d1,vdp_init")

    a("move.l #$40000000,(a6)")               # VRAM write at address 0
    a("moveq #0,d0")
    a("move.w #$3FFF,d1")                     # 16384 long words = 64 KiB
    a.label("vram_clear")
    a("move.l d0,(a5)")
    a("dbra d1,vram_clear")

    a("move.l #$C0000000,(a6)")               # CRAM write at address 0
    a("move.w #RED,(a5)")                     # first colour

    # Sound generator: silence the tone channels, start the noise, prepare tone 0
    a("lea PSG,a4")
    a("move.b #$9F,(a4)")                     # tone 0 off
    a("move.b #$BF,(a4)")                     # tone 1 off
    a("move.b #$DF,(a4)")                     # tone 2 off
    a("move.b #$E6,(a4)")                     # noise: white, medium rate
    a("move.b #$F6,(a4)")                     # noise volume: quiet but audible
    a("move.b #$82,(a4)")                     # tone 0 divider, low 4 bits
    a("move.b #$02,(a4)")                     # tone 0 divider, high 6 bits (34 -> ~439 Hz)

    a("move.b #$40,PAD_CTRL")                 # pad port 1: TH line is an output

    # Save RAM: enable it, blank -> counter = 1
    a("move.b #1,SAVE_ENABLE")
    a("move.b SAVE_M0,d0")
    a("cmpi.b #'S',d0")
    a("bne save_init")
    a("move.b SAVE_M1,d0")
    a("cmpi.b #'V',d0")
    a("beq save_ok")
    a.label("save_init")
    a("move.b #1,SAVE_LO")
    a("move.b #0,SAVE_HI")
    a("move.b #'S',SAVE_M0")
    a("move.b #'V',SAVE_M1")
    a.label("save_ok")

    # Frames per second: 50 on the 50 Hz standard (version register bit 6), else 60
    a("move.b VERSION,d0")
    a("btst #6,d0")
    a("beq ntsc")
    a("moveq #50,d4")
    a("bra got_rate")
    a.label("ntsc")
    a("moveq #60,d4")
    a.label("got_rate")
    a("move.w d4,d7")                         # frames left in this second
    a("moveq #0,d6")                          # colour index
    a("moveq #0,d5")                          # buttons held last frame

    a("move.w #$8144,(a6)")                   # R1: display on

    # ---- main loop: once per frame -----------------------------------------------
    a.label("main")
    a.label("wait_active")
    a("move.w (a6),d0")
    a("btst #3,d0")
    a("bne wait_active")                      # let the current VBlank (if any) finish
    a.label("wait_vblank")
    a("move.w (a6),d0")
    a("btst #3,d0")
    a("beq wait_vblank")                      # status bit 3 = VBlank: this is the 0 -> 1 edge

    a("bsr read_pad")                         # d0.b = buttons held (bit set = pressed)
    a("move.b d5,d1")
    a("not.b d1")
    a("and.b d0,d1")                          # d1 = buttons whose press starts now
    a("move.b d0,d5")

    a("tst.b d0")                             # tone while any button is held
    a("beq tone_off")
    a("move.b #$90,(a4)")                     # tone 0 full volume
    a("bra tone_done")
    a.label("tone_off")
    a("move.b #$9F,(a4)")                     # tone 0 off
    a.label("tone_done")

    a("tst.b d1")                             # any new press -> next colour right away
    a("beq no_press")
    a("bsr next_color")
    a.label("no_press")

    a("subq.w #1,d7")
    a("bne frame_end")
    a("move.w d4,d7")                         # one second elapsed
    a("bsr next_color")
    a("bsr bump_save")
    a.label("frame_end")
    a("bra main")

    # ---- read_pad: d0.b = pressed buttons (Start, A, C, B, Right, Left, Down, Up) -----
    a.label("read_pad")
    a("move.b #$40,PAD_DATA")                 # TH = 1: C B Right Left Down Up
    a("nop")
    a("nop")
    a("nop")
    a("nop")
    a("move.b PAD_DATA,d1")
    a("move.b #$00,PAD_DATA")                 # TH = 0: Start A Down Up
    a("nop")
    a("nop")
    a("nop")
    a("nop")
    a("move.b PAD_DATA,d2")
    a("not.b d1")
    a("andi.b #$3F,d1")                       # bits 5..0 = C B R L D U (1 = pressed)
    a("not.b d2")
    a("andi.b #$30,d2")                       # bit 5 = Start, bit 4 = A
    a("lsl.b #2,d2")                          # -> bit 7 = Start, bit 6 = A
    a("or.b d2,d1")
    a("move.b d1,d0")
    a("rts")

    # ---- next_color: colour index modulo 3, then fall through to set_backdrop --------
    a.label("next_color")
    a("addq.b #1,d6")
    a("cmpi.b #3,d6")
    a("bcs set_backdrop")
    a("moveq #0,d6")
    a.label("set_backdrop")
    a("move.l #$C0000000,(a6)")               # CRAM write at address 0
    a("moveq #0,d0")
    a("move.b d6,d0")
    a("add.w d0,d0")                          # index * 2
    a("lea colors,a0")
    a("adda.w d0,a0")
    a("move.w (a0),(a5)")
    a("rts")

    # ---- bump_save: 16-bit increment of the counter in save RAM ------------------------
    a.label("bump_save")
    a("moveq #0,d0")
    a("move.b SAVE_HI,d0")
    a("lsl.w #8,d0")
    a("move.b SAVE_LO,d0")
    a("addq.w #1,d0")
    a("move.b d0,SAVE_LO")
    a("lsr.w #8,d0")
    a("move.b d0,SAVE_HI")
    a("rts")

    a.label("rte_stub")
    a("rte")

    # ---- data ----------------------------------------------------------------------------------
    a.align(2)
    a.label("vdp_regs")
    for w in VDP_REGS:
        a.data(struct.pack(">H", w))
    a.label("colors")
    for c in (RED, GREEN, BLUE):
        a.data(struct.pack(">H", c))

    code = a.finish()

    # ---- cartridge image ------------------------------------------------------------------------
    rom = bytearray(ROM_SIZE)

    # vector table: initial stack pointer, entry point, everything else -> rte_stub
    stub = a.labels["rte_stub"]
    for i in range(64):
        struct.pack_into(">I", rom, i * 4, stub)
    struct.pack_into(">I", rom, 0x00, 0x00FFFE00)
    struct.pack_into(">I", rom, 0x04, a.labels["entry"])

    # header
    def field(offset, text, length):
        rom[offset:offset + length] = text.ljust(length).encode("ascii")

    rom[0x100:0x104] = struct.pack(">I", SYMBOLS["BOOT_SIGNATURE"])   # boot-check signature
    rom[0x104:0x110] = b" " * 12
    field(0x110, "(C)TEST 2026.OCT", 16)
    field(0x120, "PICODRIVE SAMPLE ROM", 48)                    # domestic name
    field(0x150, "PICODRIVE SAMPLE ROM", 48)                    # overseas name
    field(0x180, "GM 00000000-00", 14)                        # type and serial
    field(0x190, "J", 16)                                     # supported devices: 3-button pad
    struct.pack_into(">II", rom, 0x1A0, 0x00000000, ROM_SIZE - 1)        # ROM range
    struct.pack_into(">II", rom, 0x1A8, 0x00FF0000, 0x00FFFFFF)          # RAM range
    rom[0x1B0:0x1B4] = b"RA\xF8\x20"                          # save RAM: backed up, 8 bit, odd addresses
    struct.pack_into(">II", rom, 0x1B4, 0x00200001, 0x00203FFF)          # save RAM range
    field(0x1BC, "", 12)                                      # no modem
    field(0x1C8, "", 40)                                      # notes
    field(0x1F0, "JUE", 16)                                   # regions

    assert ENTRY + len(code) <= ROM_SIZE
    rom[ENTRY:ENTRY + len(code)] = code

    checksum = 0
    for i in range(ENTRY, ROM_SIZE, 2):
        checksum = (checksum + struct.unpack_from(">H", rom, i)[0]) & 0xFFFF
    struct.pack_into(">H", rom, 0x18E, checksum)
    return bytes(rom)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "picodrive_sample.bin"
    with open(out, "wb") as f:
        f.write(build())
    print(f"wrote {out}")
