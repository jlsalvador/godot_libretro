"""mGBA / Game Boy Advance sample ROM (256 KiB).

ARM7TDMI code at $0800:0020 (BIOS jumps there after the $0000 `b main`).
The main loop:

  * enables BG0 in DISPCNT ($0400:0000 bit 8) and primes BG0PAL[0]
  * every ~60 VBlanks (~1 s) cycles BG0PAL[0] through 15-bit
    XBBBBBGGGGGRRRRR red -> green -> blue
  * re-triggers GBA channel 2 (a legacy GB-style square wave at
    $0400:006C-$0400:0071) so the pulse's length counter doesn't gate
    off; NR52 at $0400:0084 must stay enabled for the tone to sound
  * polls DISPSTAT ($0400:0004) bit 0 (VBlank flag), writes 1 to clear
  * bumps a byte at SRAM $0E00:0000 every second so the .sav sidecar
    has something visible to persist

ARM encodings follow the ARMv4 (ARM7TDMI) manual:
  * LDR/STR immediate:  cond 0 1 0 P U B W L Rn Rt offset12   (bit 26 = 1)
  * LDR/STR register:   cond 0 1 1 P U B W L Rn Rt shift op   (bit 26 = 1, I = 1)
  * Halfword LDR/STR:   cond 0 0 P U B W L Rn Rt [imm-hi4 1 0 1 1 lo4]  (bit 26 = 0, bit 5 = 1)
  * Branch:             cond 1 0 1 offset24
"""

import struct


RED   = 0x001F
GREEN = 0x03E0
BLUE  = 0x7C00

REG_DISPCNT   = 0x04000000
REG_BG0PAL0   = 0x05000000
IWRAM_COUNTER = 0x03000000
SRAM_BASE     = 0x0E000000
NR52_OFFSET   = 0x84     # master enable, at REG_DISPCNT + 0x84
NR21_OFFSET   = 0x6C     # duty + length
NR22_OFFSET   = 0x6D     # envelope
NR23_OFFSET   = 0x70     # freq low
NR24_OFFSET   = 0x71     # init + freq high

CODE_START = 0x20
TBL_OFFSET = 0x100       # three halfwords: R, G, B


def build() -> bytes:
    rom = bytearray(0x40000)

    # ---- bit helpers ---------------------------------------------------
    def enc(cond, raw):
        # raw is a 28-bit pattern (bits 27..0); we OR in the condition.
        assert raw < (1 << 28), f"instruction 0x{raw:08x} overflows 28 bits"
        return ((cond & 0xF) << 28) | raw

    # Data-processing (immediate) family:  cond 0 0 I opcode S Rn Rd shift
    def mov_imm(rd, imm):
        # OP=1100 (0xC) MOV, S=0, Rn=0, Rd, I=1, shift=0
        assert imm <= 0xFF
        return enc(0xE, (0 << 27) | (0 << 26) | (1 << 25) | (0xC << 21) |
                   (0 << 20) | (0 << 16) | (rd << 12) | imm)

    def add_imm(rd, rn, imm):
        # OP=0100 (0x4) ADD, S=0
        assert imm <= 0xFF
        return enc(0xE, (0 << 27) | (0 << 26) | (1 << 25) | (0x4 << 21) |
                   (0 << 20) | (rn << 16) | (rd << 12) | imm)

    def add_reg(rd, rn, rm):
        # OP=0100 ADD, S=0, I=0, shift type=LSL shift=0
        return enc(0xE, (0 << 27) | (0 << 26) | (0 << 25) | (0x4 << 21) |
                   (0 << 20) | (rn << 16) | (rd << 12) | (0 << 7) |
                   (0 << 5) | (0 << 4) | (rm << 0))

    def cmp_imm(rn, imm):
        # OP=1010 (0xA) CMP = SUBS PC, Rn, #imm
        assert imm <= 0xFF
        return enc(0xE, (0 << 27) | (0 << 26) | (1 << 25) | (0xA << 21) |
                   (1 << 20) | (rn << 16) | (0xF << 12) | imm)

    def tst_imm(rn, imm):
        # OP=0011 (0x3) TST = ANDS PC, Rn, #imm
        assert imm <= 0xFF
        return enc(0xE, (0 << 27) | (0 << 26) | (1 << 25) | (0x3 << 21) |
                   (1 << 20) | (rn << 16) | (0xF << 12) | imm)

    # Load/store immediate-offset:  cond 0 1 0 P U B W L Rn Rt offset12
    def _mem_imm(cond, p, u, b, w, l, rn, rt, off):
        return enc(cond, (0 << 27) | (1 << 26) | (0 << 25) |
                   (p << 24) | (u << 23) | (b << 22) | (w << 21) | (l << 20) |
                   (rn << 16) | (rt << 12) | (off & 0xFFF))

    def str_imm(rt, rn, off):    return _mem_imm(0xE, 1, 1, 0, 0, 0, rn, rt, off)
    def ldr_imm(rt, rn, off):    return _mem_imm(0xE, 1, 1, 0, 0, 1, rn, rt, off)
    def strb_imm(rt, rn, off):   return _mem_imm(0xE, 1, 1, 1, 0, 0, rn, rt, off)
    def ldrb_imm(rt, rn, off):   return _mem_imm(0xE, 1, 1, 1, 0, 1, rn, rt, off)

    # Halfword / signed load-store immediate: cond 0 0 P U B W L Rn Rt [7:4] 1 [3:0]
    # H bit at 5 selects halfword (LDRH/STRH).  B bit at 6 selects signed byte/half.
    # Bit 26 = 0 for halfword, bit 25 (I) = 0 for immediate offset.
    def _mem_hw(cond, p, u, b, w, l, rn, rt, off):
        hi = (off >> 4) & 0xF
        lo = off & 0xF
        return enc(cond, (0 << 27) | (0 << 26) | (p << 24) | (u << 23) |
                   (1 << 22) | (w << 21) | (l << 20) |   # bit 22 = 1 (halfword/signed class)
                   (rn << 16) | (rt << 12) | (hi << 8) | (1 << 4) | (0 << 3) | (b << 6) | lo)

    def strh_imm(rt, rn, off):  return _mem_hw(0xE, 1, 1, 0, 0, 0, rn, rt, off)
    def ldrh_imm(rt, rn, off):  return _mem_hw(0xE, 1, 1, 0, 0, 1, rn, rt, off)

    def ldr_lit(rt, byte_off):
        # LDR Rt, [PC, #+/-off]: P=1, U=(sign), B=0, W=0, L=1, Rn=15
        u = 1 if byte_off >= 0 else 0
        return enc(0xE, (1 << 26) | (1 << 24) | (u << 23) | (1 << 20) |
                   (15 << 16) | (rt << 12) | (abs(byte_off) & 0xFFF))

    # B/BL: cond 1 0 1 offset24 (offset in words, sign-extended)
    def b(cond, offset_words):
        return (cond << 28) | (0xA << 24) | (offset_words & 0xFFFFFF)

    # ---- assemble -----------------------------------------------------
    insns = []
    lit_needs = []
    pool = []
    pool_index = {}
    def pool_add(v):
        if v not in pool_index:
            pool_index[v] = len(pool)
            pool.append(v)
        return pool_index[v]

    def emit(i):
        insns.append(i)
    def emit_lit(rt, value):
        idx = len(insns)
        insns.append(0)
        lit_needs.append((idx, pool_add(value), rt))

    # -- boot ------------------------------------------------------------
    # R1 = 1
    emit(mov_imm(1, 1))
    # R0 = REG_DISPCNT
    emit_lit(0, REG_DISPCNT)
    # STRH R1, [R0, #0]        ; DISPCNT bit 8 -> BG0 on
    emit(strh_imm(1, 0, 0))

    # -- audio init -------------------------------------------------------
    # R1 = $80
    emit(mov_imm(1, 0x80))
    # STRB R1, [R0, #0x84]      ; NR52 master enable
    emit(strb_imm(1, 0, NR52_OFFSET))
    # R1 = $80 (already), STRB R1, [R0, #0x6C]   ; NR21 duty=12.5%, length=0
    emit(strb_imm(1, 0, NR21_OFFSET))
    # R1 = $F8
    emit(mov_imm(1, 0xF8))
    # STRB R1, [R0, #0x6D]      ; NR22 env: initial 15, dec, period 8
    emit(strb_imm(1, 0, NR22_OFFSET))
    # R1 = $3D (freq lo, ~440 Hz)
    emit(mov_imm(1, 0x3D))
    # STRB R1, [R0, #0x70]      ; NR23
    emit(strb_imm(1, 0, NR23_OFFSET))
    # R1 = $80
    emit(mov_imm(1, 0x80))
    # STRB R1, [R0, #0x71]      ; NR24 init + freq hi = 0
    emit(strb_imm(1, 0, NR24_OFFSET))

    # Prime BG0PAL[0] = RED.
    emit(mov_imm(1, RED & 0xFF))
    emit_lit(2, REG_BG0PAL0)
    emit(strh_imm(1, 2, 0))
    emit(mov_imm(1, (RED >> 8) & 0xFF))
    emit(strh_imm(1, 2, 0))

    # Zero IWRAM frame counter ($0300_0000) and colour index ($0300_0004).
    emit_lit(3, IWRAM_COUNTER)
    emit(mov_imm(1, 0))
    emit(str_imm(1, 3, 0))
    emit(str_imm(1, 3, 4))

    # -- main loop --------------------------------------------------------
    loop_start = len(insns)
    # R0 = REG_DISPCNT (already loaded once); DISPSTAT is R0+4.
    emit(ldrh_imm(1, 0, 4))          # LDRH R1, [R0, #4]
    emit(tst_imm(1, 1))              # TST R1, #1
    op_beq_poll = len(insns); emit(b(0x0, 0))  # BEQ .poll
    emit(mov_imm(1, 1))              # R1 = 1
    emit(strh_imm(1, 0, 4))          # STRH R1, [R0, #4]  (W1C the VBlank flag)

    # Increment IWRAM frame counter at [R3].
    emit(ldr_imm(1, 3, 0))           # LDR R1, [R3, #0]
    emit(add_imm(1, 1, 1))           # ADD R1, R1, #1
    emit(str_imm(1, 3, 0))           # STR R1, [R3, #0]
    emit(cmp_imm(1, 60))             # CMP R1, #60
    op_blt_poll = len(insns); emit(b(0xB, 0))  # BLT .poll

    # Per-second block: reset frame counter.
    emit(mov_imm(1, 0))
    emit(str_imm(1, 3, 0))

    # Cycle colour index mod 3.
    emit(ldr_imm(1, 3, 4))
    emit(add_imm(1, 1, 1))
    emit(cmp_imm(1, 3))
    op_beq_wrap = len(insns); emit(b(0x0, 0))   # BEQ .wrap
    emit(str_imm(1, 3, 4))                       # STR R1, [R3, #4]  (keep path)
    op_b_skip_wrap = len(insns); emit(b(0xE, 0)) # B .after_wrap
    wrap = len(insns)
    emit(mov_imm(1, 0))
    emit(str_imm(1, 3, 4))
    after_wrap = len(insns)

    # Look up colour[index] from the table at (0x08000000 + TBL_OFFSET).
    emit(ldr_imm(1, 3, 4))                        # R1 = colour index
    emit(add_reg(1, 1, 1))                        # R1 = R1 * 2 (byte offset into halfword table)
    emit_lit(4, 0x08000000 + TBL_OFFSET)         # R4 = table base
    emit(add_reg(4, 4, 1))                        # R4 += R1  (points at colour entry)
    emit(ldrh_imm(1, 4, 0))                       # R1 = *R4
    emit(strh_imm(1, 2, 0))                       # STRH R1, [R2, #0]   (BG0PAL[0])
    # The table entries are 2 bytes each; ldrh_imm's offset 0 keeps R4 aligned.

    # Re-trigger channel 2 (write NR21 then NR24).
    emit(mov_imm(1, 0x80))
    emit(strb_imm(1, 0, NR21_OFFSET))
    emit(strb_imm(1, 0, NR24_OFFSET))

    # Bump SRAM byte.
    emit_lit(5, SRAM_BASE)
    emit(ldrb_imm(1, 5, 0))
    emit(add_imm(1, 1, 1))
    emit(strb_imm(1, 5, 0))

    op_b_back = len(insns); emit(b(0xE, 0))       # B .poll

    # ---- literal pool --------------------------------------------------
    pool_start = len(insns)
    for _ in pool:
        insns.append(0)
    for idx, pidx, rt in lit_needs:
        delta_words = pool_start + pidx - idx - 2
        insns[idx] = ldr_lit(rt, delta_words * 4)

    def patch_b(inst_idx, target_idx):
        delta = target_idx - (inst_idx + 2)
        cond = (insns[inst_idx] >> 28) & 0xF
        insns[inst_idx] = (cond << 28) | (0xA << 24) | (delta & 0xFFFFFF)

    patch_b(op_beq_poll, loop_start)
    patch_b(op_blt_poll, loop_start)
    patch_b(op_beq_wrap, wrap)
    patch_b(op_b_skip_wrap, after_wrap)
    patch_b(op_b_back, loop_start)

    code_bytes = b"".join(struct.pack("<I", i) for i in insns)
    rom[CODE_START:CODE_START + len(code_bytes)] = code_bytes
    rom[TBL_OFFSET:TBL_OFFSET + 6] = struct.pack("<HHH", RED, GREEN, BLUE)

    # ---- header ---------------------------------------------------------
    # b main at 0x0000 -> target 0x08000020; PC during fetch of insn@0x0000
    # is 0x08, so delta_words = (0x20 - 8) / 4 = 6.
    rom[0x00:0x04] = struct.pack("<I", b(0xE, 6))
    rom[0x06:0x09] = b"GBA"
    rom[0xB2] = 0x96
    s = sum(rom[0x00:0xBD]) & 0xFF
    rom[0xBD] = (0x197 - s) & 0xFF

    return bytes(rom)
