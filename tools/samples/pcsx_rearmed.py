#!/usr/bin/env python3
"""pcsx_rearmed sample ROM (PS-X EXE), written from scratch: no SDK, no BIOS
calls, no proprietary files.

Behaviour (same as the other sample ROMs):

  * the screen is filled with a solid colour that cycles red -> green -> blue ->
    red ... once per second
  * SPU voice 0 plays the hardware noise generator all the time (quiet hiss)
  * the save is a regular memory card file in slot 1 ("BASCUS-00000SAMPLE", one
    block). Its data frame holds a 16-bit little-endian counter followed by the
    signature "SV". A missing save is created with the counter at 1; afterwards
    the counter is incremented and written back once per second. An existing
    save is found by name and its counter is kept
  * on every new press (any of the 16 buttons of pad 1, or a deflected
    stick of the dual-stick rumble gamepad — a virtual 17th press — edge
    detected per press) the colour advances immediately, and SPU voice 1
    plays a ~440 Hz square tone for as long as at least one button or
    stick is held. The 1-second timer is independent of the presses and
    never restarts

How it works:

  * the file is a PS-X EXE: a 0x800-byte header followed by the code, loaded at
    0x80010000. It talks to the hardware registers directly, so it only needs
    a CPU, a GPU, an SPU and the SIO0 port: any emulator that can side-load an
    EXE (with its own HLE BIOS) or a real console running it through a boot
    loader will do. Interrupts are left off and everything is polled
  * GPU: reset, 320x240 NTSC 15-bit display, and a VRAM "fill rectangle"
    command (GP0 0x02) for the solid colour
  * frame sync: the VBlank bit in I_STAT; the 1-second tick comes from root
    counter 2 (system clock / 8 = 4,233,600 ticks per second), sampled in the
    wait loop, so it does not depend on the video standard
  * SPU: one 16-byte ADPCM block (a 28-sample square wave, looping on itself)
    is uploaded to sound RAM; voice 0 is switched to noise mode, voice 1 plays
    the block at pitch 0x478 (~440 Hz) while a button is held (KON on the
    press, KOFF on release)
  * SIO0: the dual-stick rumble gamepad is entered into config mode once
    (0x01/0x43/0x01) and then polled with the 0x01/0x42 protocol, whose
    8-byte response ends in the four 8-bit stick bytes (centre 0x80); a
    stick outside the 0x70..0x90 band counts as a press; the memory card
    uses the 0x81 0x52 (read frame) / 0x81 0x57 (write frame) commands
  * memory card layout: directory entry in frame N (1..15), and the file's
    block N (frames 64N...64N+63): title frame, icon frame, data frame at +2.
    The directory is scanned for an entry with our name, else for a free one.
    If no card or no free entry is present the program just runs without saving
  * the CPU is a MIPS R3000A: every branch/jump has a delay slot and every load
    has a delay slot. The tiny assembler below inserts a NOP after each of them
    so the code can be written naturally
"""

import struct
import sys

LOAD_ADDR = 0x80010000
TICKS_PER_SECOND = 33868800 // 8          # root counter 2 in system clock / 8 mode

SYMBOLS = {
    "STACK": 0x801FFF00,
    "IO": 0x1F800000,
    "BUF": 0x80020000,                    # 128-byte memory card frame buffer
    "CNT": 0x80020100,                    # the seconds counter (16 bit)
    "TICKS_PER_SECOND": TICKS_PER_SECOND,
}

# ---------------------------------------------------------------------------
# Tiny MIPS-I (little endian) assembler
# ---------------------------------------------------------------------------
_REGS = {n: i for i, n in enumerate(
    "zero at v0 v1 a0 a1 a2 a3 t0 t1 t2 t3 t4 t5 t6 t7 "
    "s0 s1 s2 s3 s4 s5 s6 s7 t8 t9 k0 k1 gp sp fp ra".split())}
_R3 = {"addu": 0x21, "subu": 0x23, "and": 0x24, "or": 0x25, "xor": 0x26,
       "nor": 0x27, "slt": 0x2A, "sltu": 0x2B}
_SHIFT_I = {"sll": 0x00, "srl": 0x02, "sra": 0x03}
_ARITH_I = {"addiu": 0x09, "slti": 0x0A, "sltiu": 0x0B, "andi": 0x0C,
            "ori": 0x0D, "xori": 0x0E}
_MEM = {"lb": 0x20, "lh": 0x21, "lw": 0x23, "lbu": 0x24, "lhu": 0x25,
        "sb": 0x28, "sh": 0x29, "sw": 0x2B}
_LOADS = {"lb", "lh", "lw", "lbu", "lhu"}


class Mips:
    """a("addiu $t0, $zero, 5"); a.label("loop"); a("bne $t0, $zero, loop"); ..."""

    def __init__(self, origin, symbols):
        self.origin = origin
        self.syms = symbols
        self.code = bytearray()
        self.labels = {}
        self.fixups = []          # (position, kind, label)

    def pc(self):
        return self.origin + len(self.code)

    def label(self, name):
        assert name not in self.labels, f"duplicate label {name}"
        self.labels[name] = self.pc()

    def word(self, w):
        self.code.extend(struct.pack("<I", w & 0xFFFFFFFF))

    def data(self, b):
        self.code.extend(b)

    def align(self, n):
        while len(self.code) % n:
            self.code.append(0)

    # -- operand parsing ------------------------------------------------------
    @staticmethod
    def _reg(tok):
        tok = tok.strip().lstrip("$").lower()
        return _REGS[tok] if tok in _REGS else int(tok)

    def _val(self, tok):
        tok = tok.strip()
        if not tok:
            return 0
        if tok.startswith("'"):
            return ord(tok[1])
        if tok in self.syms:
            return self.syms[tok]
        try:
            return int(tok, 0)
        except ValueError:
            return None                              # a label

    def _mem(self, tok):
        tok = tok.strip()
        off, _, rest = tok.partition("(")
        base = rest.rstrip(")")
        value = self._val(off)
        assert value is not None and -0x8000 <= value < 0x8000, f"bad offset: {tok}"
        return value, self._reg(base)

    # -- encoders ---------------------------------------------------------------
    def _r(self, rs, rt, rd, sh, fn):
        self.word((rs << 21) | (rt << 16) | (rd << 11) | (sh << 6) | fn)

    def _i(self, op, rs, rt, imm):
        self.word((op << 26) | (rs << 21) | (rt << 16) | (imm & 0xFFFF))

    def _li(self, rd, value):
        value &= 0xFFFFFFFF
        signed = value - (1 << 32) if value & 0x80000000 else value
        if -0x8000 <= signed < 0x8000:
            self._i(0x09, 0, rd, signed)                       # addiu rd, $zero, imm
        elif value <= 0xFFFF:
            self._i(0x0D, 0, rd, value)                        # ori rd, $zero, imm
        else:
            self._i(0x0F, 0, rd, value >> 16)                  # lui
            if value & 0xFFFF:
                self._i(0x0D, rd, rd, value & 0xFFFF)          # ori

    def __call__(self, text):
        mnemonic, _, rest = text.strip().partition(" ")
        mnemonic = mnemonic.lower()
        ops = [o.strip() for o in rest.split(",")] if rest.strip() else []
        delay = False                                         # needs a NOP afterwards

        if mnemonic == "nop":
            self.word(0)
        elif mnemonic == "li":
            self._li(self._reg(ops[0]), self._val(ops[1]))
        elif mnemonic == "la":
            value = self._val(ops[1])
            rd = self._reg(ops[0])
            if value is not None:
                self._li(rd, value)
            else:
                self.fixups.append((len(self.code), "hi", ops[1]))
                self._i(0x0F, 0, rd, 0)                        # lui rd, %hi(label)
                self.fixups.append((len(self.code), "lo", ops[1]))
                self._i(0x0D, rd, rd, 0)                       # ori rd, rd, %lo(label)
        elif mnemonic == "move":
            self._r(self._reg(ops[1]), 0, self._reg(ops[0]), 0, 0x21)   # addu rd, rs, $zero
        elif mnemonic in _R3:
            self._r(self._reg(ops[1]), self._reg(ops[2]), self._reg(ops[0]), 0, _R3[mnemonic])
        elif mnemonic in _SHIFT_I:
            self._r(0, self._reg(ops[1]), self._reg(ops[0]), self._val(ops[2]), _SHIFT_I[mnemonic])
        elif mnemonic in _ARITH_I:
            self._i(_ARITH_I[mnemonic], self._reg(ops[1]), self._reg(ops[0]), self._val(ops[2]))
        elif mnemonic == "lui":
            self._i(0x0F, 0, self._reg(ops[0]), self._val(ops[1]))
        elif mnemonic in _MEM:
            off, base = self._mem(ops[1])
            self._i(_MEM[mnemonic], base, self._reg(ops[0]), off)
            delay = mnemonic in _LOADS                       # load delay slot
        elif mnemonic in ("beq", "bne", "b"):
            if mnemonic == "b":
                rs, rt, target, op = 0, 0, ops[0], 0x04
            else:
                rs, rt, target = self._reg(ops[0]), self._reg(ops[1]), ops[2]
                op = 0x04 if mnemonic == "beq" else 0x05
            self.fixups.append((len(self.code), "br", target))
            self._i(op, rs, rt, 0)
            delay = True
        elif mnemonic in ("j", "jal"):
            self.fixups.append((len(self.code), "j", ops[0]))
            self.word((0x02 if mnemonic == "j" else 0x03) << 26)
            delay = True
        elif mnemonic == "jr":
            self._r(self._reg(ops[0]), 0, 0, 0, 0x08)
            delay = True
        elif mnemonic == "mfc0":
            self.word((0x10 << 26) | (0 << 21) | (self._reg(ops[0]) << 16) | (self._reg(ops[1]) << 11))
            delay = True
        elif mnemonic == "mtc0":
            self.word((0x10 << 26) | (4 << 21) | (self._reg(ops[0]) << 16) | (self._reg(ops[1]) << 11))
            delay = True
        else:
            raise ValueError(f"unknown instruction: {text}")
        if delay:
            self.word(0)

    def finish(self):
        for pos, kind, name in self.fixups:
            target = self.labels[name]
            w = struct.unpack_from("<I", self.code, pos)[0]
            if kind == "hi":
                w |= (target >> 16) & 0xFFFF
            elif kind == "lo":
                w |= target & 0xFFFF
            elif kind == "j":
                w |= (target >> 2) & 0x03FFFFFF
            else:                                            # branch: relative to the delay slot
                delta = (target - (self.origin + pos + 4)) >> 2
                assert -0x8000 <= delta < 0x8000, f"branch to {name} out of range"
                w |= delta & 0xFFFF
            struct.pack_into("<I", self.code, pos, w)
        return bytes(self.code)


# ---------------------------------------------------------------------------
# Data tables
# ---------------------------------------------------------------------------
FILL_COLORS = (0x020000FF, 0x0200FF00, 0x02FF0000)     # GP0 0x02 + BGR: red, green, blue
# One SPU-ADPCM block: shift 1, filter 0, flags = loop start + repeat + end,
# 14 data bytes = 28 samples: 14 x +7 then 14 x -7 -> a square wave
SPU_BLOCK = bytes([0x01, 0x07]) + bytes([0x77] * 7) + bytes([0x99] * 7)
SAVE_NAME = b"BASCUS-00000SAMPLE"                       # 18 bytes
SAVE_TITLE = bytes.fromhex("82728260826C826F826B8264")      # "SAMPLE" in full-width Shift-JIS
SAVE_PALETTE = struct.pack("<16H", 0x0000, 0x001F, 0x03E0, 0x7C00, *([0] * 12))
assert len(SAVE_NAME) == 18 and len(SAVE_TITLE) == 12 and len(SPU_BLOCK) == 16


def build() -> bytes:
    a = Mips(LOAD_ADDR, SYMBOLS)

    def push_ra():
        a("addiu $sp, $sp, -8")
        a("sw $ra, 0($sp)")

    def pop_ra():
        a("lw $ra, 0($sp)")
        a("addiu $sp, $sp, 8")

    # ---- start: registers, interrupts off ---------------------------------------
    # s0 = I/O base, s1 = colour index, s2 = buttons last frame, s3 = timer ticks
    # accumulated, s4 = last timer value, s5 = pending seconds, s6 = save block
    # (0 = no save), fp = buttons whose press starts this frame, gp = ticks/second
    a.label("start")
    a("li $sp, STACK")
    a("li $s0, IO")
    a("li $gp, TICKS_PER_SECOND")
    a("mfc0 $t0, 12")
    a("li $t1, 0xFFFFFFFE")
    a("and $t0, $t0, $t1")
    a("mtc0 $t0, 12")                     # COP0 Status: interrupts off
    a("sw $zero, 0x1074($s0)")            # I_MASK = 0
    a("sw $zero, 0x1070($s0)")            # I_STAT: acknowledge everything

    # ---- GPU: 320x240 NTSC, 15-bit, display on ---------------------------------------
    a("sw $zero, 0x1814($s0)")            # GP1 reset
    for gp1 in (0x08000001, 0x06C60260, 0x07040010, 0x05000000, 0x03000000):
        a(f"li $t0, 0x{gp1:08X}")
        a("sw $t0, 0x1814($s0)")
    for gp0 in (0xE1000400, 0xE3000000, 0xE403BD3F, 0xE5000000):
        a(f"li $t0, 0x{gp0:08X}")
        a("sw $t0, 0x1810($s0)")

    # ---- root counter 2: system clock / 8, free running -----------------------------------
    a("li $t0, 0x0200")
    a("sh $t0, 0x1124($s0)")
    a("sh $zero, 0x1120($s0)")

    # ---- SPU: enable, upload the ADPCM block, set up the two voices ---------------------------
    a("li $a0, 0xC000")
    a("jal spu_set_cnt")                  # SPU on, unmuted
    for off, val in ((0x1D80, 0x3FFF), (0x1D82, 0x3FFF), (0x1D84, 0), (0x1D86, 0),
                     (0x1DB0, 0), (0x1DB2, 0), (0x1DB4, 0), (0x1DB6, 0)):
        a(f"li $t0, 0x{val:X}")
        a(f"sh $t0, 0x{off:X}($s0)")      # main volume, reverb/CD/external volumes
    a("li $t0, 0x200")
    a("sh $t0, 0x1DA6($s0)")              # transfer address = sound RAM 0x1000 (/ 8)
    a("li $t0, 4")
    a("sh $t0, 0x1DAC($s0)")              # transfer control
    a("li $a0, 0xC010")
    a("jal spu_set_cnt")                  # manual write mode
    a("la $t2, spu_block")
    a("li $t3, 8")
    a.label("spu_up")
    a("lhu $t0, 0($t2)")
    a("sh $t0, 0x1DA8($s0)")              # transfer FIFO
    a("addiu $t2, $t2, 2")
    a("addiu $t3, $t3, -1")
    a("bne $t3, $zero, spu_up")
    a("li $t3, 2000")
    a.label("spu_busy")
    a("lhu $t0, 0x1DAE($s0)")
    a("andi $t0, $t0, 0x400")
    a("beq $t0, $zero, spu_idle")
    a("addiu $t3, $t3, -1")
    a("bne $t3, $zero, spu_busy")
    a.label("spu_idle")
    a("li $a0, 0xE000")
    a("jal spu_set_cnt")                  # transfer off, noise clock shift 8
    for off, val in (
            (0x1C00, 0x0600), (0x1C02, 0x0600), (0x1C04, 0x1000), (0x1C06, 0x0200),
            (0x1C08, 0x00FF), (0x1C0A, 0x0000),            # voice 0: noise
            (0x1C10, 0x2800), (0x1C12, 0x2800), (0x1C14, 0x0478), (0x1C16, 0x0200),
            (0x1C18, 0x00FF), (0x1C1A, 0x0000),            # voice 1: tone, ~440 Hz
            (0x1D94, 0x0001),                              # NON: voice 0 = noise
            (0x1D88, 0x0001)):                             # KON: voice 0
        a(f"li $t0, 0x{val:X}")
        a(f"sh $t0, 0x{off:X}($s0)")

    # ---- SIO0 (pad + memory card) ------------------------------------------------------------------
    a("li $t0, 0x40")
    a("sh $t0, 0x104A($s0)")              # reset
    a("li $t0, 0x000D")
    a("sh $t0, 0x1048($s0)")              # mode: x1, 8 bits
    a("li $t0, 0x0088")
    a("sh $t0, 0x104E($s0)")              # baud 250 kHz
    a("sh $zero, 0x104A($s0)")

    a("jal pad_init_analog")             # dual-stick rumble gamepad config mode: sticks readable
    a("jal save_init")                    # find / create the save, load the counter

    # ---- first colour --------------------------------------------------------------------------------
    a("move $s1, $zero")
    a("move $s2, $zero")
    a("move $s3, $zero")
    a("move $s5, $zero")
    a("move $fp, $zero")
    a("lhu $s4, 0x1120($s0)")
    a("jal fill_screen")

    # ---- main loop -------------------------------------------------------------------------------------
    a.label("main")
    a.label("wait_frame")
    a("lhu $t0, 0x1120($s0)")             # root counter 2
    a("subu $t1, $t0, $s4")
    a("andi $t1, $t1, 0xFFFF")
    a("move $s4, $t0")
    a("addu $s3, $s3, $t1")
    a("sltu $t2, $s3, $gp")
    a("bne $t2, $zero, no_second")
    a("subu $s3, $s3, $gp")
    a("addiu $s5, $s5, 1")                # one more second is pending
    a.label("no_second")
    a("lw $t0, 0x1070($s0)")              # I_STAT
    a("andi $t0, $t0, 1")
    a("beq $t0, $zero, wait_frame")       # bit 0 = VBlank
    a("sw $zero, 0x1070($s0)")            # acknowledge

    a("jal read_pad")                     # v0 = buttons held (bit set = pressed)
    a("nor $t1, $s2, $zero")
    a("and $fp, $v0, $t1")                # new presses = now AND NOT before
    a("move $s2, $v0")

    a("bne $v0, $zero, tone_held")        # tone off when nothing is held
    a("li $t0, 2")
    a("sh $t0, 0x1D8C($s0)")              # KOFF voice 1
    a.label("tone_held")
    a("beq $fp, $zero, no_press")
    a("li $t0, 2")
    a("sh $t0, 0x1D88($s0)")              # KON voice 1 (restarts the tone)
    a("jal next_color")
    a.label("no_press")

    a("beq $s5, $zero, frame_end")
    a("addiu $s5, $s5, -1")
    a("jal next_color")
    a("jal tick_save")
    a.label("frame_end")
    a("b main")

    # ---- next_color: colour index modulo 3, then fall through to fill_screen -------------------------------
    a.label("next_color")
    a("addiu $s1, $s1, 1")
    a("sltiu $t0, $s1, 3")
    a("bne $t0, $zero, fill_screen")
    a("move $s1, $zero")

    # ---- fill_screen: VRAM fill rectangle 320x240 at (0,0) with the current colour ---------------------------
    a.label("fill_screen")
    a("lui $t1, 0x0400")
    a.label("fs_wait")
    a("lw $t0, 0x1814($s0)")              # GPUSTAT
    a("and $t0, $t0, $t1")                # bit 26: ready to receive a command
    a("beq $t0, $zero, fs_wait")
    a("la $t2, fill_colors")
    a("sll $t3, $s1, 2")
    a("addu $t2, $t2, $t3")
    a("lw $t4, 0($t2)")
    a("sw $t4, 0x1810($s0)")              # 0x02BBGGRR
    a("sw $zero, 0x1810($s0)")            # y = 0, x = 0
    a("li $t5, 0x00F00140")
    a("sw $t5, 0x1810($s0)")              # height 240, width 320
    a("jr $ra")

    # ---- tick_save: counter += 1, write it to the card ---------------------------------------------------------
    a.label("tick_save")
    a("li $t0, CNT")
    a("lhu $t1, 0($t0)")
    a("addiu $t1, $t1, 1")
    a("sh $t1, 0($t0)")
    a("beq $s6, $zero, ts_done")          # no card / no save
    push_ra()
    a("jal write_counter")
    pop_ra()
    a.label("ts_done")
    a("jr $ra")

    # ---- spu_set_cnt: SPUCNT = a0, wait until SPUSTAT mirrors it ---------------------------------------------------
    a.label("spu_set_cnt")
    a("sh $a0, 0x1DAA($s0)")
    a("andi $t1, $a0, 0x3F")
    a("li $t2, 1000")
    a.label("ssc_wait")
    a("lhu $t0, 0x1DAE($s0)")
    a("andi $t0, $t0, 0x3F")
    a("beq $t0, $t1, ssc_done")
    a("addiu $t2, $t2, -1")
    a("bne $t2, $zero, ssc_wait")
    a.label("ssc_done")
    a("jr $ra")

    # ---- sio_xfer / sio_xfer_last: send a0, return the received byte in v0 ---------------------------------------------
    # (clobbers t0, t1). sio_xfer waits for the /ACK pulse afterwards; the last
    # byte of a transfer has none, so sio_xfer_last does not wait for it.
    a.label("sio_xfer")
    a("li $t1, 600")
    a("b sx_common")
    a.label("sio_xfer_last")
    a("move $t1, $zero")
    a.label("sx_common")
    a("sw $a0, 0x1040($s0)")
    a.label("sx_rx")
    a("lw $t0, 0x1044($s0)")
    a("andi $t0, $t0, 2")
    a("beq $t0, $zero, sx_rx")            # wait for the received byte
    a("lw $v0, 0x1040($s0)")
    a("andi $v0, $v0, 0xFF")
    a("beq $t1, $zero, sx_ack")
    a.label("sx_wait")
    a("lw $t0, 0x1044($s0)")
    a("andi $t0, $t0, 0x200")             # bit 9: /ACK seen
    a("bne $t0, $zero, sx_ack")
    a("addiu $t1, $t1, -1")
    a("bne $t1, $zero, sx_wait")
    a.label("sx_ack")
    a("li $t0, 0x1013")
    a("sh $t0, 0x104A($s0)")              # keep selected, acknowledge the IRQ flag
    a("jr $ra")

    # ---- pad_init_analog: enter the dual-stick rumble gamepad config mode
    # once (0x01/0x43/0x01); from then on the 0x42 read returns the 8-byte
    # response with the sticks
    a.label("pad_init_analog")
    push_ra()
    a("li $t0, 0x1003")
    a("sh $t0, 0x104A($s0)")              # select slot 1
    a("li $a0, 0x01")
    a("jal sio_xfer")
    a("li $a0, 0x43")
    a("jal sio_xfer")
    a("li $a0, 0x01")                     # config mode on
    a("jal sio_xfer")
    a("li $a0, 0x5A")
    a("jal sio_xfer")
    a("li $a0, 0")
    a("jal sio_xfer")
    a("li $a0, 0")
    a("jal sio_xfer_last")
    a("sh $zero, 0x104A($s0)")            # deselect
    pop_ra()
    a("jr $ra")
    # ---- read_pad: v0 = pressed buttons of pad 1 (bit set = pressed); bit 15 is a
    # virtual press set while any stick of the dual-stick rumble gamepad is
    # deflected, so the sticks react like buttons (the 8-byte response is read
    # in config mode)
    a.label("read_pad")
    push_ra()
    a("li $t0, 0x1003")
    a("sh $t0, 0x104A($s0)")              # select slot 1
    a("li $a0, 0x01")
    a("jal sio_xfer")
    a("li $a0, 0x42")
    a("jal sio_xfer")
    a("move $t9, $v0")                    # pad ID (0xFF = nothing connected)
    a("li $a0, 0")
    a("jal sio_xfer")                     # 0x5A
    a("li $a0, 0")
    a("jal sio_xfer")                     # buttons, low byte (0 = pressed)
    a("move $t8, $v0")
    a("li $a0, 0")
    a("jal sio_xfer")                     # buttons, high byte
    a("sll $t1, $v0, 8")
    a("or $t1, $t1, $t8")
    a("move $a2, $t1")                    # buttons (a2 survives the xfers below)
    a("li $a0, 0")
    a("jal sio_xfer")                     # right joy X
    a("move $t2, $v0")
    a("li $a0, 0")
    a("jal sio_xfer")                     # right joy Y
    a("move $t3, $v0")
    a("li $a0, 0")
    a("jal sio_xfer")                     # left joy X
    a("move $t4, $v0")
    a("li $a0, 0")
    a("jal sio_xfer_last")                # left joy Y
    a("sh $zero, 0x104A($s0)")            # deselect
    a("li $t0, 0xFF")
    a("bne $t9, $t0, rp_ok")
    a("move $a2, $zero")                  # nothing connected: rest
    a("li $t2, 0x80")
    a("li $t3, 0x80")
    a("li $t4, 0x80")
    a("move $v0, $zero")
    a("j rp_deflect")
    a.label("rp_ok")
    a.label("rp_deflect")
    a("li $t0, 0")                        # deflected flag
    for axis in ("$t2", "$t3", "$t4", "$v0"):
        a("slti $t5, " + axis + ", 0x70")
        a("or $t0, $t0, $t5")
        a("slti $t6, " + axis + ", 0x91")
        a("xori $t6, $t6, 1")
        a("or $t0, $t0, $t6")
    a("sll $v0, $t0, 15")                # stick bit (the virtual press)
    a("or $v0, $v0, $a2")                # buttons | stick bit
    pop_ra()
    a("jr $ra")

    # ---- mc_read: a2 = frame number -> BUF, v0 = 1 if the transfer was good -----------------------------------------------
    a.label("mc_read")
    push_ra()
    a("li $t0, 0x1003")
    a("sh $t0, 0x104A($s0)")
    a("li $a0, 0x81")
    a("jal sio_xfer")
    a("li $a0, 0x52")
    a("jal sio_xfer")
    a("li $a0, 0")
    a("jal sio_xfer")
    a("move $t9, $v0")                    # 0x5A
    a("li $a0, 0")
    a("jal sio_xfer")                     # 0x5D
    a("srl $a0, $a2, 8")
    a("jal sio_xfer")                     # frame MSB
    a("andi $a0, $a2, 0xFF")
    a("jal sio_xfer")                     # frame LSB
    for _ in range(4):                    # 0x5C, 0x5D, confirmed MSB, confirmed LSB
        a("li $a0, 0")
        a("jal sio_xfer")
    a("srl $t7, $a2, 8")
    a("andi $t6, $a2, 0xFF")
    a("xor $t7, $t7, $t6")                # running checksum
    a("li $t6, BUF")
    a("li $t5, 128")
    a.label("mr_loop")
    a("li $a0, 0")
    a("jal sio_xfer")
    a("sb $v0, 0($t6)")
    a("xor $t7, $t7, $v0")
    a("addiu $t6, $t6, 1")
    a("addiu $t5, $t5, -1")
    a("bne $t5, $zero, mr_loop")
    a("li $a0, 0")
    a("jal sio_xfer")
    a("move $t4, $v0")                    # checksum from the card
    a("li $a0, 0")
    a("jal sio_xfer_last")
    a("move $t3, $v0")                    # end byte, 0x47 = good
    a("sh $zero, 0x104A($s0)")
    a("move $v0, $zero")
    a("li $t0, 0x5A")
    a("bne $t9, $t0, mr_done")
    a("andi $t7, $t7, 0xFF")
    a("bne $t4, $t7, mr_done")
    a("li $t0, 0x47")
    a("bne $t3, $t0, mr_done")
    a("li $v0, 1")
    a.label("mr_done")
    pop_ra()
    a("jr $ra")

    # ---- mc_write: BUF -> frame a2, v0 = 1 if the card answered "good" ---------------------------------------------------------
    a.label("mc_write")
    push_ra()
    a("li $t0, 0x1003")
    a("sh $t0, 0x104A($s0)")
    a("li $a0, 0x81")
    a("jal sio_xfer")
    a("li $a0, 0x57")
    a("jal sio_xfer")
    a("li $a0, 0")
    a("jal sio_xfer")
    a("move $t9, $v0")                    # 0x5A
    a("li $a0, 0")
    a("jal sio_xfer")                     # 0x5D
    a("srl $a0, $a2, 8")
    a("jal sio_xfer")
    a("andi $a0, $a2, 0xFF")
    a("jal sio_xfer")
    a("srl $t7, $a2, 8")
    a("andi $t6, $a2, 0xFF")
    a("xor $t7, $t7, $t6")
    a("li $t6, BUF")
    a("li $t5, 128")
    a.label("mw_loop")
    a("lbu $a0, 0($t6)")
    a("xor $t7, $t7, $a0")
    a("jal sio_xfer")
    a("addiu $t6, $t6, 1")
    a("addiu $t5, $t5, -1")
    a("bne $t5, $zero, mw_loop")
    a("andi $a0, $t7, 0xFF")
    a("jal sio_xfer")                     # checksum
    a("li $a0, 0")
    a("jal sio_xfer")                     # 0x5C
    a("li $a0, 0")
    a("jal sio_xfer")                     # 0x5D
    a("li $a0, 0")
    a("jal sio_xfer_last")
    a("move $t3, $v0")                    # end byte, 0x47 = good
    a("sh $zero, 0x104A($s0)")
    a("move $v0, $zero")
    a("li $t0, 0x5A")
    a("bne $t9, $t0, mw_done")
    a("li $t0, 0x47")
    a("bne $t3, $t0, mw_done")
    a("li $v0, 1")
    a.label("mw_done")
    pop_ra()
    a("jr $ra")

    # ---- small helpers -------------------------------------------------------------------------------------------------------------
    a.label("clear_buf")                  # BUF = 128 zero bytes
    a("li $t0, BUF")
    a("li $t1, 32")
    a.label("cb_loop")
    a("sw $zero, 0($t0)")
    a("addiu $t0, $t0, 4")
    a("addiu $t1, $t1, -1")
    a("bne $t1, $zero, cb_loop")
    a("jr $ra")

    a.label("fix_checksum")               # BUF[127] = XOR of BUF[0..126]
    a("li $t0, BUF")
    a("li $t1, 127")
    a("move $t2, $zero")
    a.label("fc_loop")
    a("lbu $t3, 0($t0)")
    a("xor $t2, $t2, $t3")
    a("addiu $t0, $t0, 1")
    a("addiu $t1, $t1, -1")
    a("bne $t1, $zero, fc_loop")
    a("sb $t2, 0($t0)")
    a("jr $ra")

    a.label("copy")                       # copy a2 bytes from a0 to a1
    a("lbu $t0, 0($a0)")
    a("sb $t0, 0($a1)")
    a("addiu $a0, $a0, 1")
    a("addiu $a1, $a1, 1")
    a("addiu $a2, $a2, -1")
    a("bne $a2, $zero, copy")
    a("jr $ra")

    # ---- write_counter: build the data frame from CNT and write it (frame 64*s6 + 2) --------------------------------------------------
    a.label("write_counter")
    push_ra()
    a("jal clear_buf")
    a("li $t0, BUF")
    a("li $t1, CNT")
    a("lhu $t2, 0($t1)")
    a("sb $t2, 0($t0)")                   # counter, low byte
    a("srl $t2, $t2, 8")
    a("sb $t2, 1($t0)")                   # counter, high byte
    a("li $t2, 'S'")
    a("sb $t2, 2($t0)")
    a("li $t2, 'V'")
    a("sb $t2, 3($t0)")
    a("sll $a2, $s6, 6")
    a("addiu $a2, $a2, 2")
    a("jal mc_write")
    pop_ra()
    a("jr $ra")

    # ---- save_init: find our save (or create it), load the counter into CNT ------------------------------------------------------------
    a.label("save_init")
    push_ra()
    a("move $s6, $zero")                  # block of our save
    a("move $s5, $zero")                  # first free directory entry
    a("li $t0, CNT")
    a("li $t1, 1")
    a("sh $t1, 0($t0)")                   # default counter = 1
    a("li $s1, 1")
    a.label("si_scan")
    a("move $a2, $s1")
    a("jal mc_read")                      # directory frame s1
    a("beq $v0, $zero, si_next")
    a("li $t0, BUF")
    a("lbu $t1, 0($t0)")
    a("li $t2, 0x51")
    a("beq $t1, $t2, si_check")           # 0x51 = first block of a file
    a("li $t2, 0xA0")
    a("bne $t1, $t2, si_next")            # 0xA0 = free entry
    a("bne $s5, $zero, si_next")
    a("move $s5, $s1")
    a("b si_next")
    a.label("si_check")
    a("li $t0, BUF")
    a("addiu $t0, $t0, 10")
    a("la $t1, save_name")
    a("li $t2, 18")
    a.label("si_cmp")
    a("lbu $t3, 0($t0)")
    a("lbu $t4, 0($t1)")
    a("bne $t3, $t4, si_next")
    a("addiu $t0, $t0, 1")
    a("addiu $t1, $t1, 1")
    a("addiu $t2, $t2, -1")
    a("bne $t2, $zero, si_cmp")
    a("move $s6, $s1")                    # found our file
    a("b si_scanned")
    a.label("si_next")
    a("addiu $s1, $s1, 1")
    a("sltiu $t0, $s1, 16")
    a("bne $t0, $zero, si_scan")
    a.label("si_scanned")

    a("bne $s6, $zero, si_load")
    a("beq $s5, $zero, si_done")          # no card or no free entry: no save
    a("move $s6, $s5")                    # create the file in the free entry

    a("jal clear_buf")                    # directory frame
    a("li $t0, BUF")
    a("li $t1, 0x51")
    a("sb $t1, 0($t0)")                   # in use, first block
    a("li $t1, 0x20")
    a("sb $t1, 5($t0)")                   # file size 0x2000 (little endian: 00 20 00 00)
    a("li $t1, 0xFF")
    a("sb $t1, 8($t0)")
    a("sb $t1, 9($t0)")                   # no next block
    a("la $a0, save_name")
    a("addiu $a1, $t0, 10")
    a("li $a2, 18")
    a("jal copy")
    a("jal fix_checksum")
    a("move $a2, $s6")
    a("jal mc_write")

    a("jal clear_buf")                    # title frame (frame 64 * block)
    a("li $t0, BUF")
    a("li $t1, 0x53")
    a("sb $t1, 0($t0)")                   # 'S'
    a("li $t1, 0x43")
    a("sb $t1, 1($t0)")                   # 'C'
    a("li $t1, 0x11")
    a("sb $t1, 2($t0)")                   # one icon frame
    a("li $t1, 1")
    a("sb $t1, 3($t0)")                   # one block
    a("la $a0, save_title")
    a("addiu $a1, $t0, 4")
    a("li $a2, 12")
    a("jal copy")
    a("la $a0, save_palette")
    a("li $a1, BUF")
    a("addiu $a1, $a1, 0x60")
    a("li $a2, 32")
    a("jal copy")
    a("sll $a2, $s6, 6")
    a("jal mc_write")

    a("li $t0, BUF")                      # icon frame: solid palette entry 1
    a("li $t1, 32")
    a("li $t2, 0x11111111")
    a.label("si_icon")
    a("sw $t2, 0($t0)")
    a("addiu $t0, $t0, 4")
    a("addiu $t1, $t1, -1")
    a("bne $t1, $zero, si_icon")
    a("sll $a2, $s6, 6")
    a("addiu $a2, $a2, 1")
    a("jal mc_write")

    a("jal write_counter")                # data frame with the counter at 1
    a("b si_done")

    a.label("si_load")
    a("sll $a2, $s6, 6")
    a("addiu $a2, $a2, 2")
    a("jal mc_read")                      # data frame of the existing save
    a("beq $v0, $zero, si_fresh")
    a("li $t0, BUF")
    a("lbu $t1, 2($t0)")
    a("li $t2, 'S'")
    a("bne $t1, $t2, si_fresh")
    a("lbu $t1, 3($t0)")
    a("li $t2, 'V'")
    a("bne $t1, $t2, si_fresh")
    a("lbu $t1, 0($t0)")
    a("lbu $t2, 1($t0)")
    a("sll $t2, $t2, 8")
    a("or $t1, $t1, $t2")
    a("li $t0, CNT")
    a("sh $t1, 0($t0)")                   # keep the saved counter
    a("b si_done")
    a.label("si_fresh")
    a("jal write_counter")                # signature missing: start over at 1
    a.label("si_done")
    pop_ra()
    a("jr $ra")

    # ---- data -------------------------------------------------------------------------------------------------------------------------------------
    a.align(4)
    a.label("fill_colors")
    for c in FILL_COLORS:
        a.data(struct.pack("<I", c))
    a.label("spu_block")
    a.data(SPU_BLOCK)
    a.label("save_name")
    a.data(SAVE_NAME)
    a.label("save_title")
    a.data(SAVE_TITLE)
    a.label("save_palette")
    a.data(SAVE_PALETTE)
    a.align(4)

    code = a.finish()
    assert LOAD_ADDR + len(code) < SYMBOLS["BUF"], "code overlaps the data buffers"

    # ---- PS-X EXE header (0x800 bytes) + code ----------------------------------------------------------------------------------------------------
    size = (len(code) + 0x7FF) & ~0x7FF
    header = bytearray(0x800)
    header[0:8] = b"PS-X EXE"
    struct.pack_into("<I", header, 0x10, a.labels["start"])      # initial PC
    struct.pack_into("<I", header, 0x14, 0)                      # initial GP
    struct.pack_into("<I", header, 0x18, LOAD_ADDR)              # load address
    struct.pack_into("<I", header, 0x1C, size)                   # size of the code
    struct.pack_into("<I", header, 0x30, SYMBOLS["STACK"])       # initial SP
    # The 0x4C region field is informational and ignored by the loaders we
    # target; keep it blank, space-padded as in a real header.
    header[0x4C:0x83] = b" " * 0x37
    return bytes(header) + code.ljust(size, b"\x00")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "pcsx_rearmed_sample.exe"
    with open(out, "wb") as f:
        f.write(build())
    print(f"wrote {out}")
