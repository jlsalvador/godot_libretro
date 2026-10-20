#!/usr/bin/env python3
"""gambatte sample ROM (32 KiB cartridge image, MBC5 + battery RAM).

Behaviour:

  * the backdrop colour (CGB background palette 0, colour 0) cycles
    red -> green -> blue -> red ... once per second
  * APU channel 4 (noise) plays all the time (quiet hiss)
  * the battery-backed save is a 16-bit little-endian counter at $A000-$A001.
    On a blank SRAM (a "SV" signature at $A002 is missing) it is initialised to
    1; afterwards it is incremented once per second
  * on every new button press (any of the 8 buttons, edge detected per button)
    the colour advances immediately, and APU channel 1 plays a ~440 Hz square
    tone for as long as at least one button is held. The 1-second timer is
    independent of the presses and never restarts

How it works:

  * header at $0100 ($00 NOP, JP $0150), CGB-only flag, cartridge type $1B
    (MBC5+RAM+BATTERY), 8 KiB RAM; the program starts at $0150, interrupts off
  * VRAM (both banks) is cleared with the LCD off, so every background tile is
    colour 0 and the screen shows palette colour 0; the LCD is then turned on
    with the background enabled
  * the main loop syncs to the start of VBlank (LY reaching 144) and does all
    palette writes there: BCPD is not writable while the PPU draws
  * the joypad is read through $FF00: directions in the low nibble, buttons in
    the high nibble of the pad mask (1 = pressed)
  * the 1-second tick comes from the hardware timer, not from counting frames
    (the gambatte core runs at 59.7275 Hz): TAC = 4096 Hz, TIMA overflows every
    256 ticks (1/16 s) and IF bit 2 is polled; 16 overflows = exactly 1 s
  * MBC5 RAM is enabled by writing $0A to $0000
  * audio: NR51 routes channel 4 always, and channel 1 only while a button is
    held; a new press re-triggers channel 1 through NR14

Colours are 15-bit BGR: red $001F, green $03E0, blue $7C00.
"""

import sys

ORG = 0x0150

RED, GREEN, BLUE = 0x001F, 0x03E0, 0x7C00


# ---------------------------------------------------------------------------
# Tiny LR35902 assembler: only what this program needs
# ---------------------------------------------------------------------------
_R8 = {"b": 0, "c": 1, "d": 2, "e": 3, "h": 4, "l": 5, "[hl]": 6, "a": 7}
_R16 = {"bc": 0, "de": 1, "hl": 2, "sp": 3}
_PUSHPOP = {"bc": 0, "de": 1, "hl": 2, "af": 3}
_COND = {"nz": 0, "z": 1, "nc": 2, "c": 3}
_ALU = {"add": 0, "adc": 1, "sub": 2, "sbc": 3, "and": 4, "xor": 5, "or": 6, "cp": 7}
_SIMPLE = {"nop": 0x00, "di": 0xF3, "ei": 0xFB, "halt": 0x76, "ret": 0xC9,
           "reti": 0xD9, "cpl": 0x2F}


class Gb:
    """g("ld a, 0x3E"), g.label("loop"), g("jr nz, loop"), g("call routine")."""

    def __init__(self, origin, symbols):
        self.origin = origin
        self.syms = symbols
        self.code = bytearray()
        self.labels = {}
        self.fixups = []          # (position, label, "abs" | "rel")

    def pc(self):
        return self.origin + len(self.code)

    def label(self, name):
        assert name not in self.labels, f"duplicate label {name}"
        self.labels[name] = self.pc()

    def data(self, *values):
        self.code.extend(values)

    def _const(self, tok):
        tok = tok.strip()
        if tok.startswith("'"):
            return ord(tok[1])
        if tok in self.syms:
            return self.syms[tok]
        try:
            return int(tok, 0)
        except ValueError:
            return None                          # a label

    def _byte(self, tok):
        value = self._const(tok)
        assert value is not None and 0 <= value <= 0xFF, f"bad 8-bit value: {tok}"
        self.code.append(value)

    def _word(self, tok):
        value = self._const(tok)
        if value is None:                        # label, resolved in finish()
            self.fixups.append((len(self.code), tok.strip(), "abs"))
            value = 0
        assert 0 <= value <= 0xFFFF, f"bad 16-bit value: {tok}"
        self.code.extend((value & 0xFF, value >> 8))

    def _is_imm(self, tok):
        return not tok.startswith("[") and tok not in _R8 and tok not in _R16

    @staticmethod
    def _inner(tok):
        assert tok.startswith("[") and tok.endswith("]"), f"expected [..]: {tok}"
        return tok[1:-1].strip()

    def __call__(self, text):
        mnemonic, _, rest = text.strip().lower().partition(" ")
        ops = [o.strip() for o in rest.split(",")] if rest.strip() else []
        # keep original case for symbols/labels: re-split the original text
        _, _, orest = text.strip().partition(" ")
        raw = [o.strip() for o in orest.split(",")] if orest.strip() else []
        # symbols are upper case or label names; registers are compared lower case
        ops = [r.lower() if r.lower() in _R8 or r.lower() in _R16 or r.lower() in _PUSHPOP
               or r.lower() in _COND or r.lower() in ("[hl+]", "[hl-]", "[bc]", "[de]")
               else r for r in raw]
        e = self.code.append

        if mnemonic in _SIMPLE and not ops:
            e(_SIMPLE[mnemonic])
        elif mnemonic == "ret" and ops:
            e(0xC0 | _COND[ops[0]] << 3)
        elif mnemonic in ("jp", "jr", "call"):
            base = {"jp": (0xC3, 0xC2), "jr": (0x18, 0x20), "call": (0xCD, 0xC4)}[mnemonic]
            if len(ops) == 2:
                e(base[1] | _COND[ops[0]] << 3)
                target = ops[1]
            else:
                e(base[0])
                target = ops[0]
            if mnemonic == "jr":
                self.fixups.append((len(self.code), target, "rel"))
                e(0)
            else:
                self._word(target)
        elif mnemonic == "ldh":
            if ops[0].startswith("["):                       # ldh [n], a
                addr = self._const(self._inner(ops[0]))
                e(0xE0)
            else:                                            # ldh a, [n]
                addr = self._const(self._inner(ops[1]))
                e(0xF0)
            assert addr is not None and (addr >= 0xFF00 or addr < 0x100), f"bad ldh address {addr}"
            e(addr & 0xFF)
        elif mnemonic == "ld":
            self._ld(ops)
        elif mnemonic in ("add",) and ops[0] == "hl":
            e(0x09 | _R16[ops[1]] << 4)
        elif mnemonic in _ALU:
            if len(ops) == 2 and ops[0] == "a":
                ops = ops[1:]
            idx = _ALU[mnemonic]
            if ops[0] in _R8:
                e(0x80 | idx << 3 | _R8[ops[0]])
            else:
                e(0xC6 | idx << 3)
                self._byte(ops[0])
        elif mnemonic in ("inc", "dec"):
            if ops[0] in _R8:
                e((0x04 if mnemonic == "inc" else 0x05) | _R8[ops[0]] << 3)
            else:
                e((0x03 if mnemonic == "inc" else 0x0B) | _R16[ops[0]] << 4)
        elif mnemonic in ("push", "pop"):
            e((0xC5 if mnemonic == "push" else 0xC1) | _PUSHPOP[ops[0]] << 4)
        elif mnemonic == "swap":
            e(0xCB)
            e(0x30 | _R8[ops[0]])
        elif mnemonic == "srl":
            e(0xCB)
            e(0x38 | _R8[ops[0]])
        elif mnemonic in ("bit", "res", "set"):
            e(0xCB)
            e({"bit": 0x40, "res": 0x80, "set": 0xC0}[mnemonic] | int(ops[0]) << 3 | _R8[ops[1]])
        else:
            raise ValueError(f"unknown instruction: {text}")

    def _ld(self, ops):
        dst, src = ops
        e = self.code.append
        if dst in _R8 and src in _R8:
            assert not (dst == "[hl]" and src == "[hl]")
            e(0x40 | _R8[dst] << 3 | _R8[src])
        elif dst == "[hl+]" and src == "a":
            e(0x22)
        elif dst == "[hl-]" and src == "a":
            e(0x32)
        elif src == "[hl+]" and dst == "a":
            e(0x2A)
        elif src == "[hl-]" and dst == "a":
            e(0x3A)
        elif dst in _R8 and self._is_imm(src):
            e(0x06 | _R8[dst] << 3)
            self._byte(src)
        elif dst in _R16 and self._is_imm(src):
            e(0x01 | _R16[dst] << 4)
            self._word(src)
        elif dst == "a" and src.startswith("["):
            e(0xFA)
            self._word(self._inner(src))
        elif src == "a" and dst.startswith("["):
            e(0xEA)
            self._word(self._inner(dst))
        else:
            raise ValueError(f"unsupported ld: {dst}, {src}")

    def finish(self):
        for pos, name, kind in self.fixups:
            target = self.labels[name]
            if kind == "abs":
                self.code[pos] = target & 0xFF
                self.code[pos + 1] = target >> 8
            else:
                # relative to the address of the NEXT instruction
                delta = target - (self.origin + pos + 1)
                assert -128 <= delta <= 127, f"jr to {name} out of range ({delta})"
                self.code[pos] = delta & 0xFF
        return bytes(self.code)


# ---------------------------------------------------------------------------
# ROM
# ---------------------------------------------------------------------------
SYMBOLS = {
    # hardware registers
    "JOYP": 0xFF00, "TIMA": 0xFF05, "TMA": 0xFF06, "TAC": 0xFF07, "IF": 0xFF0F,
    "NR10": 0xFF10, "NR11": 0xFF11, "NR12": 0xFF12, "NR13": 0xFF13, "NR14": 0xFF14,
    "NR42": 0xFF21, "NR43": 0xFF22, "NR44": 0xFF23,
    "NR50": 0xFF24, "NR51": 0xFF25, "NR52": 0xFF26,
    "LCDC": 0xFF40, "LY": 0xFF44, "VBK": 0xFF4F, "BCPS": 0xFF68, "BCPD": 0xFF69,
    # HRAM variables
    "V_COLOR": 0xFF80,        # 0 = red, 1 = green, 2 = blue
    "V_PREV": 0xFF81,         # buttons held last frame
    "V_TICKS": 0xFF82,        # timer overflows since the last second (0..15)
    # battery-backed RAM (MBC5)
    "SAVE_CNT": 0xA000, "SAVE_M0": 0xA002, "SAVE_M1": 0xA003,
    "RAM_ENABLE": 0x0000, "RAM_BANK": 0x4000,
    # values
    "NR51_NOISE": 0x88,       # channel 4 on both sides
    "NR51_BOTH": 0x99,        # channel 4 + channel 1 on both sides
    "TONE_LO": 0xD6,          # frequency 1750 -> 131072 / (2048 - 1750) ~ 440 Hz
    "TONE_HI": 0x86,          # trigger + frequency high bits
}


def build() -> bytes:
    g = Gb(ORG, SYMBOLS)

    g.label("start")
    g("di")
    g("ld sp, 0xFFFE")

    # The LCD may only be switched off during VBlank
    g.label("boot_wait")
    g("ldh a, [LY]")
    g("cp 144")
    g("jr c, boot_wait")
    g("xor a")
    g("ldh [LCDC], a")

    # HRAM variables
    g("ldh [V_COLOR], a")
    g("ldh [V_PREV], a")
    g("ldh [V_TICKS], a")

    # Clear both VRAM banks: every tile and map entry becomes 0 -> colour 0
    g("ldh [VBK], a")
    g("call clear_vram")
    g("ld a, 1")
    g("ldh [VBK], a")
    g("call clear_vram")
    g("xor a")
    g("ldh [VBK], a")

    # Save RAM: enable it, blank -> counter = 1
    g("ld a, 0x0A")
    g("ld [RAM_ENABLE], a")
    g("xor a")
    g("ld [RAM_BANK], a")
    g("ld a, [SAVE_M0]")
    g("cp 'S'")
    g("jr nz, save_init")
    g("ld a, [SAVE_M1]")
    g("cp 'V'")
    g("jr z, save_ok")
    g.label("save_init")
    g("ld a, 1")
    g("ld [SAVE_CNT], a")
    g("xor a")
    g("ld [0xA001], a")
    g("ld a, 'S'")
    g("ld [SAVE_M0], a")
    g("ld a, 'V'")
    g("ld [SAVE_M1], a")
    g.label("save_ok")

    # Audio: power on, channel 1 ready (not routed yet), channel 4 noise running
    g("ld a, 0x80")
    g("ldh [NR52], a")
    g("ld a, 0x77")
    g("ldh [NR50], a")
    g("ld a, NR51_NOISE")
    g("ldh [NR51], a")
    g("xor a")
    g("ldh [NR10], a")                # no sweep
    g("ld a, 0x80")
    g("ldh [NR11], a")                # 50% duty
    g("ld a, 0xF0")
    g("ldh [NR12], a")                # volume 15, no envelope
    g("ld a, 0x60")
    g("ldh [NR42], a")                # noise: volume 6, no envelope
    g("ld a, 0x43")
    g("ldh [NR43], a")                # clock shift 4, divisor 3
    g("ld a, 0x80")
    g("ldh [NR44], a")                # trigger (no length limit: runs forever)

    # Timer: 4096 Hz, TIMA 0..255 -> overflow every 1/16 s
    g("xor a")
    g("ldh [TMA], a")
    g("ldh [TIMA], a")
    g("ldh [IF], a")
    g("ld a, 0x04")
    g("ldh [TAC], a")

    g("call set_backdrop")            # first colour (red), LCD still off
    g("ld a, 0x91")
    g("ldh [LCDC], a")                # LCD on, BG on, tile data $8000, map $9800

    # ---- main loop: once per frame -------------------------------------------
    g.label("main")
    g.label("wait_novbl")
    g("ldh a, [LY]")
    g("cp 144")
    g("jr nc, wait_novbl")            # let the current VBlank (if any) finish
    g.label("wait_vbl")
    g("ldh a, [LY]")
    g("cp 144")
    g("jr c, wait_vbl")               # LY reached 144: VBlank just started

    g("call read_pad")                # b = buttons held
    g("ldh a, [V_PREV]")
    g("cpl")
    g("and b")
    g("ld c, a")                      # c = buttons whose press starts now
    g("ld a, b")
    g("ldh [V_PREV], a")

    g("or a")                         # tone routed while any button is held
    g("ld a, NR51_NOISE")
    g("jr z, tone_set")
    g("ld a, NR51_BOTH")
    g.label("tone_set")
    g("ldh [NR51], a")

    g("ld a, c")                      # any new press -> restart tone, next colour
    g("or a")
    g("jr z, no_press")
    g("ld a, TONE_LO")
    g("ldh [NR13], a")
    g("ld a, TONE_HI")
    g("ldh [NR14], a")                # trigger channel 1
    g("call next_color")
    g.label("no_press")

    g("ldh a, [IF]")                  # timer overflow flag
    g("bit 2, a")
    g("jr z, frame_end")
    g("res 2, a")
    g("ldh [IF], a")
    g("ldh a, [V_TICKS]")
    g("inc a")
    g("cp 16")
    g("jr z, second")
    g("ldh [V_TICKS], a")
    g("jr frame_end")
    g.label("second")                 # 16 overflows = exactly one second
    g("xor a")
    g("ldh [V_TICKS], a")
    g("call next_color")
    g("call bump_save")
    g.label("frame_end")
    g("jp main")

    # ---- read_pad: b = pad mask (bits 7..4 = Start,Select,B,A; 3..0 = Down,Up,Left,Right)
    g.label("read_pad")
    g("ld a, 0x20")                   # select directions
    g("ldh [JOYP], a")
    for _ in range(4):
        g("ldh a, [JOYP]")
    g("cpl")
    g("and 0x0F")
    g("ld b, a")
    g("ld a, 0x10")                   # select buttons
    g("ldh [JOYP], a")
    for _ in range(4):
        g("ldh a, [JOYP]")
    g("cpl")
    g("and 0x0F")
    g("swap a")
    g("or b")
    g("ld b, a")
    g("ld a, 0x30")                   # deselect
    g("ldh [JOYP], a")
    g("ret")

    # ---- next_color: colour index modulo 3, then tail-call set_backdrop ----------
    g.label("next_color")
    g("ldh a, [V_COLOR]")
    g("inc a")
    g("cp 3")
    g("jr c, nc_store")
    g("xor a")
    g.label("nc_store")
    g("ldh [V_COLOR], a")
    g("jp set_backdrop")

    # ---- set_backdrop: BG palette 0, colour 0 = palette[V_COLOR] ------------------------
    g.label("set_backdrop")
    g("ldh a, [V_COLOR]")
    g("add a, a")
    g("ld e, a")
    g("ld d, 0")
    g("ld hl, palette")
    g("add hl, de")
    g("ld a, 0x80")                   # index 0, auto-increment
    g("ldh [BCPS], a")
    g("ld a, [hl+]")
    g("ldh [BCPD], a")
    g("ld a, [hl]")
    g("ldh [BCPD], a")
    g("ret")

    # ---- bump_save: 16-bit increment of the SRAM counter -----------------------------------
    g.label("bump_save")
    g("ld hl, SAVE_CNT")
    g("inc [hl]")
    g("ret nz")
    g("inc hl")
    g("inc [hl]")
    g("ret")

    # ---- clear_vram: zero $8000-$9FFF of the current VRAM bank ------------------------------
    g.label("clear_vram")
    g("ld hl, 0x8000")
    g("ld bc, 0x2000")
    g.label("cv_loop")
    g("xor a")
    g("ld [hl+], a")
    g("dec bc")
    g("ld a, b")
    g("or c")
    g("jr nz, cv_loop")
    g("ret")

    g.label("palette")
    for c in (RED, GREEN, BLUE):
        g.data(c & 0xFF, c >> 8)

    code = g.finish()

    # ---- 32 KiB image -------------------------------------------------------------------------
    rom = bytearray(0x8000)
    rom[0x100:0x104] = bytes([0x00, 0xC3, 0x50, 0x01])           # nop; jp $0150
    # logo area (0x104-0x133) left zero: gambatte boots the cartridge directly
    rom[0x134:0x13F] = b"GAMBATTE".ljust(11, b"\x00")           # title (11 bytes)
    rom[0x13F:0x143] = b"ASMP"                                    # manufacturer code
    rom[0x143] = 0xC0                                             # CGB only
    rom[0x144:0x146] = b"00"                                      # new licensee code
    rom[0x146] = 0x00                                             # no SGB
    rom[0x147] = 0x1B                                             # MBC5 + RAM + battery
    rom[0x148] = 0x00                                             # 32 KiB ROM
    rom[0x149] = 0x02                                             # 8 KiB RAM
    rom[0x14A] = 0x01                                             # non-Japanese
    rom[0x14B] = 0x33                                             # use the new licensee code
    rom[0x14C] = 0x00                                             # version
    assert len(code) + ORG <= 0x4000
    rom[ORG:ORG + len(code)] = code

    x = 0
    for b in rom[0x134:0x14D]:
        x = (x - b - 1) & 0xFF
    rom[0x14D] = x
    total = (sum(rom) - rom[0x14E] - rom[0x14F]) & 0xFFFF
    rom[0x14E] = total >> 8
    rom[0x14F] = total & 0xFF
    return bytes(rom)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "gambatte_sample.gbc"
    with open(out, "wb") as f:
        f.write(build())
    print(f"wrote {out}")
