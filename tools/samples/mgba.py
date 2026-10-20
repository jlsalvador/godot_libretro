#!/usr/bin/env python3
"""mGBA sample ROM (32 KiB cartridge image with battery-backed SRAM).

Behaviour:

  * the backdrop colour (palette entry 0) cycles red -> green -> blue -> red ...
    once per second
  * PSG channel 4 (noise) plays all the time (quiet hiss)
  * the battery-backed save is a 16-bit little-endian counter at the start of
    SRAM (0x0E000000). On a blank SRAM (a "SV" signature at +2 is missing) it
    is initialised to 1; afterwards it is incremented once per second
  * on every new button press (any of the 10 buttons, edge detected per
    button) the colour advances immediately, and PSG channel 1 plays a ~440 Hz
    square tone for as long as at least one button is held. The 1-second
    timer is independent of the presses and never restarts

How it works:

  * the program is plain ARM code at 0x08000000 (header + code, no BIOS calls,
    no interrupts); state lives in registers r4-r11
  * the display is left in mode 0 with every layer off, so the screen shows
    the backdrop colour from palette RAM
  * the main loop syncs to the start of VBlank (DISPSTAT bit 0, 0 -> 1 edge)
    and reads KEYINPUT (active low)
  * the 1-second tick comes from hardware timers, not from counting frames
    (the mGBA core runs at 59.7275 Hz): timer 0 at F/1024 overflows every 16384
    ticks = exactly 1 s, and timer 1 counts those overflows (cascade); a
    change of timer 1 means "one more second"
  * SRAM is 8 bit wide, so it is only ever accessed with LDRB/STRB. The string
    "SRAM_V113" in the ROM is what emulators and flash carts use to detect the
    save type

Colours are 15-bit BGR: red $001F, green $03E0, blue $7C00.
"""

import sys

BASE = 0x08000000
CODE_START = 0xC0             # right after the 192-byte header

RED, GREEN, BLUE = 0x001F, 0x03E0, 0x7C00


# ---------------------------------------------------------------------------
# Tiny ARM (32-bit mode) assembler: only what this program needs
# ---------------------------------------------------------------------------
_COND = {"EQ": 0, "NE": 1, "CS": 2, "CC": 3, "MI": 4, "PL": 5, "VS": 6, "VC": 7,
         "HI": 8, "LS": 9, "GE": 10, "LT": 11, "GT": 12, "LE": 13, "": 14}
_DP = {"AND": 0, "EOR": 1, "SUB": 2, "RSB": 3, "ADD": 4, "ADC": 5, "SBC": 6, "RSC": 7,
       "TST": 8, "TEQ": 9, "CMP": 10, "CMN": 11, "ORR": 12, "MOV": 13, "BIC": 14, "MVN": 15}
_NO_RD = {"TST", "TEQ", "CMP", "CMN"}
_NO_RN = {"MOV", "MVN"}
_MEM = ["LDRB", "LDRH", "STRB", "STRH", "LDR", "STR"]
_SHIFT = {"LSL": 0, "LSR": 1, "ASR": 2, "ROR": 3}
_REGS = {f"R{i}": i for i in range(16)}
_REGS.update({"SP": 13, "LR": 14, "PC": 15})


def _split_args(text):
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _encode_imm(value):
    value &= 0xFFFFFFFF
    for rot in range(16):
        v = ((value << (2 * rot)) | (value >> (32 - 2 * rot))) & 0xFFFFFFFF if rot else value
        if v <= 0xFF:
            return (rot << 8) | v
    raise ValueError(f"constant 0x{value:X} is not encodable as an ARM immediate")


class Arm:
    def __init__(self, origin, symbols=None):
        self.origin = origin
        self.syms = symbols or {}
        self.code = bytearray()
        self.labels = {}
        self.branch_fix = []      # (pos, label)
        self.ldr_fix = []         # (pos, key)
        self.pool_entries = {}    # key -> position in code (after pool())
        self.pool_fix = []        # (pos, label) words in the pool holding a label address

    def pc(self):
        return self.origin + len(self.code)

    def label(self, name):
        assert name not in self.labels, f"duplicate label {name}"
        self.labels[name] = self.pc()

    def word(self, w):
        self.code.extend((w & 0xFFFFFFFF).to_bytes(4, "little"))

    def data(self, b):
        self.code.extend(b)

    def align(self, n):
        while len(self.code) % n:
            self.code.append(0)

    # -- operand helpers ----------------------------------------------------
    def _reg(self, tok):
        return _REGS[tok.strip().upper()]

    def _num(self, tok):
        tok = tok.strip().lstrip("#")
        if tok.startswith("'"):
            return ord(tok[1])
        if tok in self.syms:
            return self.syms[tok]
        return int(tok, 0)

    def _op2(self, args):
        """Return (I bit, operand2 field) for 'rm', 'rm, lsl #n' or '#imm'."""
        if args[0].startswith("#"):
            return 1, _encode_imm(self._num(args[0]))
        rm = self._reg(args[0])
        if len(args) == 1:
            return 0, rm
        kind, _, amount = args[1].partition(" ")
        return 0, rm | (_SHIFT[kind.upper()] << 5) | (self._num(amount) << 7)

    # -- instruction dispatch -----------------------------------------------
    def __call__(self, text):
        mnemonic, _, rest = text.strip().partition(" ")
        mnemonic = mnemonic.upper()
        args = _split_args(rest)

        # branches
        branches = {"B": (0, ""), "BL": (1, "")}
        for c in _COND:
            if c:
                branches["B" + c] = (0, c)
        if mnemonic in branches:
            link, cond = branches[mnemonic]
            self.branch_fix.append((len(self.code), args[0]))
            self.word((_COND[cond] << 28) | 0x0A000000 | (link << 24))
            return
        if mnemonic == "BX":
            self.word(0xE12FFF10 | self._reg(args[0]))
            return

        # loads and stores (longest base first)
        for base in _MEM:
            if mnemonic.startswith(base) and mnemonic[len(base):] in _COND:
                self._mem(base, _COND[mnemonic[len(base):]], args)
                return

        # data processing: <op>[S][cond]
        for base in sorted(_DP, key=len, reverse=True):
            if not mnemonic.startswith(base):
                continue
            tail = mnemonic[len(base):]
            s = 0
            if tail.startswith("S") and tail[1:] in _COND:
                s, tail = 1, tail[1:]
            if tail not in _COND:
                continue
            cond = _COND[tail]
            if base in _NO_RD:
                rd, rn, ops = 0, self._reg(args[0]), args[1:]
                s = 1
            elif base in _NO_RN:
                rd, rn, ops = self._reg(args[0]), 0, args[1:]
            else:
                rd, rn, ops = self._reg(args[0]), self._reg(args[1]), args[2:]
            i, op2 = self._op2(ops)
            self.word((cond << 28) | (i << 25) | (_DP[base] << 21) | (s << 20)
                      | (rn << 16) | (rd << 12) | op2)
            return
        raise ValueError(f"unknown instruction: {text}")

    def _mem(self, base, cond, args):
        load = base.startswith("LDR")
        rd = self._reg(args[0])
        if args[1].startswith("="):                       # LDR rd, =constant|label
            assert base == "LDR"
            key = args[1][1:].strip()
            self.ldr_fix.append((len(self.code), key))
            self.word((_COND[""] if cond == 14 else cond) << 28 | 0x059F0000 | (rd << 12))
            return
        inside = _split_args(args[1].strip()[1:-1])
        rn = self._reg(inside[0])
        off = self._num(inside[1]) if len(inside) > 1 else 0
        up = 1 if off >= 0 else 0
        off = abs(off)
        if base in ("LDR", "LDRB", "STR", "STRB"):
            assert off < 4096
            self.word((cond << 28) | 0x04000000 | (1 << 24) | (up << 23)
                      | ((base.endswith("B")) << 22) | (load << 20) | (rn << 16) | (rd << 12) | off)
        else:                                             # LDRH / STRH, immediate offset
            assert off < 256, f"halfword offset {off} too large"
            self.word((cond << 28) | (1 << 24) | (up << 23) | (1 << 22) | (load << 20)
                      | (rn << 16) | (rd << 12) | ((off >> 4) << 8) | 0xB0 | (off & 0xF))

    def pool(self):
        """Emit the literal pool (must not be reachable by fall-through)."""
        self.align(4)
        keys = []
        for _, key in self.ldr_fix:
            if key not in keys:
                keys.append(key)
        for key in keys:
            self.pool_entries[key] = len(self.code)
            self.word(0)

    def finish(self):
        for pos, name in self.branch_fix:
            delta = (self.labels[name] - (self.origin + pos + 8)) >> 2
            assert -(1 << 23) <= delta < (1 << 23)
            w = int.from_bytes(self.code[pos:pos + 4], "little") | (delta & 0xFFFFFF)
            self.code[pos:pos + 4] = w.to_bytes(4, "little")
        for key, ppos in self.pool_entries.items():
            if key in self.labels:
                value = self.labels[key]
            else:
                value = self._num(key)
            self.code[ppos:ppos + 4] = (value & 0xFFFFFFFF).to_bytes(4, "little")
        for pos, key in self.ldr_fix:
            offset = self.pool_entries[key] - (pos + 8)
            assert 0 <= offset < 4096, f"literal pool out of reach for {key}"
            w = int.from_bytes(self.code[pos:pos + 4], "little") | offset
            self.code[pos:pos + 4] = w.to_bytes(4, "little")
        return bytes(self.code)


# ---------------------------------------------------------------------------
# ROM
# ---------------------------------------------------------------------------
# Hardware constants used as symbols inside the assembly text
SYMBOLS = {
    "IO": 0x04000000,
    "TIMERS": 0x04000100,
    "SRAM": 0x0E000000,
    "PALETTE": 0x05000000,
    "KEY_MASK": 0x3FF,
    # SOUNDCNT_L: master volume 7/7 + noise (ch4) on both sides = $8877;
    # with channel 1 (tone) on both sides as well = $9977
    "SND_NOISE_ONLY": 0x8877,
    "SND_NOISE_TONE": 0x9977,
    # SOUND1CNT_X: restart + frequency 1750 -> 131072 / (2048 - 1750) ~ 440 Hz
    "TONE_RESTART": 0x8000 | 1750,
    # TM0 reload so that it overflows after 16384 ticks of F/1024 = 1 s
    "TM0_RELOAD": 0x10000 - 16384,
}


def build() -> bytes:
    a = Arm(BASE + CODE_START, SYMBOLS)

    a.label("start")
    a("ldr r7, =IO")
    a("ldr r11, =TIMERS")
    a("ldr r8, =SRAM")
    a("ldr r9, =PALETTE")
    a("ldr r10, =KEY_MASK")
    a("mov r4, #0")                       # colour index
    a("mov r5, #0")                       # buttons held last frame

    a("mov r0, #0")
    a("strh r0, [r7]")                    # DISPCNT = 0: mode 0, all layers off

    # ---- save: blank -> counter = 1 -----------------------------------------
    a("ldrb r0, [r8, #2]")
    a("cmp r0, #'S'")
    a("bne save_init")
    a("ldrb r0, [r8, #3]")
    a("cmp r0, #'V'")
    a("beq save_ok")
    a.label("save_init")
    a("mov r0, #1")
    a("strb r0, [r8]")
    a("mov r0, #0")
    a("strb r0, [r8, #1]")
    a("mov r0, #'S'")
    a("strb r0, [r8, #2]")
    a("mov r0, #'V'")
    a("strb r0, [r8, #3]")
    a.label("save_ok")

    # ---- audio: master on, tone channel ready, noise channel running ------------
    a("mov r0, #0x80")
    a("strh r0, [r7, #0x84]")             # SOUNDCNT_X: master enable
    a("mov r0, #2")
    a("strh r0, [r7, #0x82]")             # SOUNDCNT_H: PSG at 100%
    a("mov r0, #0x08")
    a("strh r0, [r7, #0x60]")             # SOUND1CNT_L: sweep off
    a("ldr r0, =0xF080")
    a("strh r0, [r7, #0x62]")             # SOUND1CNT_H: 50% duty, volume 15, no envelope
    a("ldr r0, =0x6000")
    a("strh r0, [r7, #0x78]")             # SOUND4CNT_L: volume 6, no envelope
    a("ldr r0, =0x8043")
    a("strh r0, [r7, #0x7C]")             # SOUND4CNT_H: restart, shift 4, ratio 3 (runs forever)
    a("ldr r0, =SND_NOISE_ONLY")
    a("strh r0, [r7, #0x80]")             # SOUNDCNT_L: noise on

    # ---- timers: TM0 = F/1024 overflowing every second, TM1 counts the overflows --
    a("mov r0, #0")
    a("strh r0, [r11, #4]")               # TM1 reload 0
    a("mov r0, #0x84")
    a("strh r0, [r11, #6]")               # TM1: enable + cascade (count-up)
    a("ldr r0, =TM0_RELOAD")
    a("strh r0, [r11]")                   # TM0 reload
    a("mov r0, #0x83")
    a("strh r0, [r11, #2]")               # TM0: enable, prescaler F/1024
    a("ldrh r6, [r11, #4]")               # seconds counter as last seen

    a("bl set_backdrop")                  # first colour (red)

    # ---- main loop: once per frame --------------------------------------------------
    a.label("main")
    a.label("wait_novbl")
    a("ldrh r0, [r7, #4]")                # DISPSTAT
    a("tst r0, #1")
    a("bne wait_novbl")                   # let the current VBlank (if any) finish
    a.label("wait_vbl")
    a("ldrh r0, [r7, #4]")
    a("tst r0, #1")
    a("beq wait_vbl")                     # 0 -> 1 edge: VBlank just started

    a("ldrh r0, [r11, #0x30]")            # KEYINPUT (0 = pressed)
    a("mvn r0, r0")
    a("and r0, r0, r10")                  # r0 = buttons held (bit set = pressed)
    a("bic r1, r0, r5")                   # r1 = buttons whose press starts now
    a("mov r5, r0")

    a("ldr r2, =SND_NOISE_ONLY")          # tone while any button is held
    a("cmp r0, #0")
    a("ldrne r2, =SND_NOISE_TONE")
    a("strh r2, [r7, #0x80]")

    a("cmp r1, #0")                       # any new press -> restart tone, next colour
    a("beq no_press")
    a("ldr r2, =TONE_RESTART")
    a("strh r2, [r7, #0x64]")             # SOUND1CNT_X
    a("bl next_color")
    a.label("no_press")

    a("ldrh r0, [r11, #4]")               # TM1 = seconds elapsed
    a("cmp r0, r6")
    a("beq frame_end")
    a("mov r6, r0")
    a("bl next_color")
    a("bl bump_save")
    a.label("frame_end")
    a("b main")

    # ---- next_color: colour index modulo 3, then fall through to set_backdrop -----
    a.label("next_color")
    a("add r4, r4, #1")
    a("cmp r4, #3")
    a("moveq r4, #0")
    a.label("set_backdrop")
    a("ldr r0, =palette")
    a("add r0, r0, r4, lsl #1")
    a("ldrh r0, [r0]")
    a("strh r0, [r9]")                    # palette entry 0 = backdrop
    a("bx lr")

    # ---- bump_save: 16-bit increment of the SRAM counter (byte accesses only) ------
    a.label("bump_save")
    a("ldrb r0, [r8]")
    a("ldrb r1, [r8, #1]")
    a("orr r0, r0, r1, lsl #8")
    a("add r0, r0, #1")
    a("mov r1, r0, lsr #8")
    a("strb r0, [r8]")
    a("strb r1, [r8, #1]")
    a("bx lr")

    a.pool()

    # ---- data -----------------------------------------------------------------------
    a.label("palette")
    for c in (RED, GREEN, BLUE):
        a.data(c.to_bytes(2, "little"))
    a.align(4)
    a.data(b"SRAM_V113\x00")              # save type marker (SRAM, 32 KiB)
    a.align(4)

    code = a.finish()

    # ---- header (0x00-0xBF) ------------------------------------------------------------
    header = bytearray(CODE_START)
    header[0:4] = (0xEA000000 | ((CODE_START - 8) >> 2)).to_bytes(4, "little")   # B start
    # logo area (0x04-0x9F) left zero: mGBA then skips the BIOS and boots the ROM directly
    header[0xA0:0xAC] = b"MGBA SAMPLE".ljust(12, b"\x00")
    header[0xAC:0xB0] = b"ASMP"
    header[0xB0:0xB2] = b"00"
    header[0xB2] = 0x96
    chk = 0
    for b in header[0xA0:0xBD]:
        chk -= b
    header[0xBD] = (chk - 0x19) & 0xFF

    rom = bytearray(0x8000)
    rom[:CODE_START] = header
    rom[CODE_START:CODE_START + len(code)] = code
    assert CODE_START + len(code) <= len(rom)
    return bytes(rom)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "mgba_sample.gba"
    with open(out, "wb") as f:
        f.write(build())
    print(f"wrote {out}")
