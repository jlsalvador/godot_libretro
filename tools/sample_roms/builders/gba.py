"""mGBA / Game Boy Advance sample ROM (256 KiB).

ARM7TDMI code at $0800:0020 (BIOS jumps there after the $0000 `b main`).
The main loop:

  * enables BG0 in DISPCNT ($0400:0000 bit 8)
  * writes BG0PAL[0] ($0500:0000 halfword) to red, then every ~60 VBlanks
    cycles it red -> green -> blue (15-bit XBBBBBGGGGGRRRRR primaries)
  * polls DISPSTAT ($0400:0004) bit 0 (VBlank flag), writes 1 to clear
  * bumps the byte at SRAM $0E00:0000 every second so the save sidecar
    (mgba uses SRAM/Flash; the base cart type is SRAM 32K by default in
    mGBA when no chip is declared in the header) has something visible
    to persist

We hand-encode the handful of ARMv4 instructions we need.  Constants
that don't fit in an 8-bit rotated immediate are materialised via
`MOV32 rd, #imm32` (four halfword-OR ops via MOVT/MOV) or by an inline
LDR-pc-relative to a literal pool at the end of the block.
"""

import struct


RED   = 0x001F
GREEN = 0x03E0
BLUE  = 0x7C00

REG_DISPCNT   = 0x04000000
REG_BG0PAL0   = 0x05000000
IWRAM_COUNTER = 0x03000000
SRAM_BASE     = 0x0E000000

CODE_START = 0x20                       # ROM file offset where our code goes
CODE_BASE  = 0x08000000 + CODE_START    # matching GBA address
TBL_OFFSET = 0x100                      # ROM offset holding the 3 colour entries


def build() -> bytes:
    rom = bytearray(0x40000)

    # ---- tiny ARM encoder ----------------------------------------------
    def mk(cond, raw):
        return ((cond & 0xF) << 28) | (raw & 0x0FFFFFFF)

    def mov_imm(rd, imm):
        assert imm <= 0xFF
        return mk(0xE, (0 << 25) | (1 << 21) | (0 << 20) | (rd << 12) | imm)

    def mvn_imm(rd, imm):
        assert imm <= 0xFF
        return mk(0xE, (0 << 25) | (1 << 21) | (1 << 20) | (rd << 12) | imm)

    def add_imm(rd, rn, imm):
        assert imm <= 0xFF
        return mk(0xE, (0 << 25) | (1 << 21) | (1 << 20) | (rn << 16) | (rd << 12) | imm)

    def cmp_imm(rn, imm):
        assert imm <= 0xFF
        return mk(0xE, (0 << 25) | (1 << 21) | (1 << 20) | (rn << 16) | (0xF << 12) | imm)

    def tst_imm(rn, imm):
        assert imm <= 0xFF
        return mk(0xE, (0 << 25) | (1 << 21) | (1 << 20) | (rn << 16) | (0xF << 12) | imm)

    def str_imm(rt, rn, off):
        return mk(0xE, (1 << 24) | (1 << 22) | (1 << 20) | (rn << 16) | (rt << 12) | off)

    def strb_imm(rt, rn, off):
        return mk(0xE, (1 << 24) | (1 << 22) | (0 << 21) | (0 << 20) | (rn << 16) | (rt << 12) | off)

    def ldr_imm(rt, rn, off):
        return mk(0xE, (1 << 24) | (1 << 22) | (0 << 21) | (1 << 20) | (rn << 16) | (rt << 12) | off)

    def ldrb_imm(rt, rn, off):
        return mk(0xE, (1 << 24) | (1 << 22) | (0 << 21) | (0 << 20) | (rn << 16) | (rt << 12) | off)

    def strh_imm(rt, rn, off):
        hi = (off >> 4) & 0xF
        lo = off & 0xF
        return mk(0xE, (1 << 24) | (1 << 22) | (0 << 21) | (1 << 20) | (1 << 6) |
                  (hi << 8) | (rn << 16) | (rt << 12) | lo)

    def ldrh_imm(rt, rn, off):
        hi = (off >> 4) & 0xF
        lo = off & 0xF
        return mk(0xE, (1 << 24) | (1 << 22) | (0 << 21) | (1 << 20) | (1 << 6) | (1 << 5) |
                  (hi << 8) | (rn << 16) | (rt << 12) | lo)

    def ldr_lit(rt, byte_off):
        # LDR Rt, [PC, #+/-off]  (bit 23 = U, 1 = add)
        u = 1 if byte_off >= 0 else 0
        return mk(0xE, (1 << 24) | (u << 23) | (1 << 22) | (1 << 20) |
                  (15 << 16) | (rt << 12) | (abs(byte_off) & 0xFFF))

    def b(cond, offset_words):
        return (cond << 28) | (0xA << 24) | (offset_words & 0xFFFFFF)

    # ---- assemble the program ------------------------------------------
    # Layout: [instructions...] [literal pool of 4-word constants] [pad to TBL_OFFSET]
    # All LDR-literal offsets are relative to (pc of the LDR + 8).
    # We track positions, then compute pool-relative offsets at the end.
    insns = []                    # list of ints
    lit_needs = []                # list of (insn_index, pool_index)
    pool = [REG_DISPCNT, REG_BG0PAL0, IWRAM_COUNTER, SRAM_BASE]
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
        insns.append(0)           # placeholder
        lit_needs.append((idx, pool_add(value), rt))

    # --- main @ CODE_BASE ------------------------------------------------
    # MOV R1, #1
    emit(mov_imm(1, 1))
    # LDR R0, =REG_DISPCNT
    emit_lit(0, REG_DISPCNT)
    # STRH R1, [R0, #0]        ; DISPCNT bit 8 -> BG0 on
    emit(strh_imm(1, 0, 0))
    # MOV R1, #0x0100          ; won't fit in 8-bit imm -- use MVN of #0xFE?
    # Simpler: reload R0 = REG_BG0PAL0 and STRH RED.
    code_red_lo = RED & 0xFF     # 0x1F
    emit(mov_imm(1, code_red_lo))
    emit_lit(2, REG_BG0PAL0)
    emit(strh_imm(1, 2, 0))

    # Zero IWRAM_COUNTER and IWRAM_COL_IDX (word @ base+0 and base+4)
    emit_lit(3, IWRAM_COUNTER)
    emit(mov_imm(1, 0))
    emit(str_imm(1, 3, 0))       # STR R1, [R3, #0]
    emit(str_imm(1, 3, 4))       # STR R1, [R3, #4]

    loop_start = len(insns)
    # Poll DISPSTAT bit 0.  R0 still = REG_DISPCNT base; DISPSTAT = +4.
    emit(ldrh_imm(1, 0, 4))      # LDRH R1, [R0, #4]
    emit(tst_imm(1, 1))          # TST R1, #1
    beq_poll = len(insns)
    emit(b(0x0, 0))              # BEQ .poll (patched below)
    emit(mov_imm(1, 1))
    emit(strh_imm(1, 0, 4))      # clear VBlank flag (W1C)

    # Increment IWRAM frame counter.
    emit(ldr_imm(1, 3, 0))       # LDR R1, [R3, #0]
    emit(add_imm(1, 1, 1))       # ADD R1, R1, #1
    emit(str_imm(1, 3, 0))       # STR R1, [R3, #0]
    emit(cmp_imm(1, 60))         # CMP R1, #60
    blt_poll = len(insns)
    emit(b(0xB, 0))              # BLT .poll (patched below; signed imm24)

    # Per-second: reset counter, cycle colour, bump SRAM.
    emit(mov_imm(1, 0))
    emit(str_imm(1, 3, 0))

    # Advance colour index at [R3+4] mod 3.
    emit(ldr_imm(1, 3, 4))
    emit(add_imm(1, 1, 1))
    emit(cmp_imm(1, 3))
    beq_wrap = len(insns)
    emit(b(0x0, 0))              # BEQ .wrap (patched)
    emit(str_imm(1, 3, 4))
    b_skip = len(insns)
    emit(b(0xE, 0))              # B .after_wrap (patched)
    # .wrap
    emit(mov_imm(1, 0))
    emit(str_imm(1, 3, 4))
    # .after_wrap

    # Look up colour[idx] from the table (which lives at ROM offset TBL_OFFSET).
    # Compute byte offset = idx * 2 into a halfword array.
    tbl_addr = 0x08000000 + TBL_OFFSET
    emit_lit(4, tbl_addr)        # R4 = colour table base
    emit(ldr_imm(1, 3, 4))       # R1 = colour index (byte offset -- we double)
    emit(add_imm(1, 1, 1))       # R1 = R1 * 2
    emit(ldrh_imm(1, 4, 0))      # needs a register offset form; simpler:
    # ...LDRH R1, [R4, R1]  (register-offset encoding, not immediate)
    # Encode it manually:
    insns[-1] = (0xE << 28) | (1 << 24) | (0 << 23) | (0 << 22) | (0 << 21) | (1 << 20) | \
                (1 << 6) | (1 << 5) | (0 << 4) | (4 << 16) | (1 << 12) | (1 << 0) | (0 << 7)
    # Hmm -- that's still not right.  Use shifted-register-offset form.
    # LDRH R1, [R4, R1, LSL #1] -- actually we already scaled R1 by *2.
    # Register-offset LDRH:  cond 000 P U B 1 L 1111 Rt Rn (0)(0) shift_type shift_amount 0 1
    # Simpler: use two separate addresses via a MOV+LDH-imm with computed offset --
    # not possible since offset must be constant.
    # Revert: use LDRH R1, [R4, #0] with an index-adjusted base pointer.
    # We'll instead reload R4 = tbl_addr + idx*2 via ADD R4, R4, R1 (R1 = idx*2).
    insns[-2] = add_imm(4, 4, 0)
    # Need ADD R4, R4, R1 (register, not immediate).
    insns[-2] = (0xE << 28) | (0 << 25) | (0 << 21) | (0 << 20) | (4 << 16) | (4 << 12) | (1 << 0)
    # Now: LDRH R1, [R4, #0]
    insns[-1] = ldrh_imm(1, 4, 0)

    emit(strh_imm(1, 2, 0))      # STRH R1, [R2, #0]  (R2 still = REG_BG0PAL0)

    # Bump SRAM.  R5 = SRAM_BASE.
    emit_lit(5, SRAM_BASE)
    emit(ldrb_imm(1, 5, 0))
    emit(add_imm(1, 1, 1))
    emit(strb_imm(1, 5, 0))

    emit(b(0xE, 0))              # B .poll  (patched)
    b_end = len(insns) - 1

    # ---- literal pool ---------------------------------------------------
    pool_start = len(insns)
    for _ in pool:
        insns.append(0)
    for idx, pidx, rt in lit_needs:
        # LDR Rt, [PC, #offset]: offset = (pool_start + pidx - idx - 2) * 4
        delta_words = pool_start + pidx - idx - 2
        insns[idx] = ldr_lit(rt, delta_words * 4)

    # ---- patch branch targets -------------------------------------------
    def patch_b(inst_idx, target_idx):
        delta = target_idx - (inst_idx + 2)   # ARM: PC = insn + 8 = (inst+2)*4
        raw = insns[inst_idx] & 0x0FFFFFFF
        cond = (insns[inst_idx] >> 28) & 0xF
        insns[inst_idx] = (cond << 28) | (0xA << 24) | (delta & 0xFFFFFF)

    patch_b(beq_poll, loop_start)
    patch_b(blt_poll, loop_start)
    patch_b(beq_wrap, pool_start - 3)         # .wrap is 3 slots before pool
    patch_b(b_skip, pool_start - 1)           # .after_wrap is 1 slot before pool
    patch_b(b_end, loop_start)

    # ---- serialise ------------------------------------------------------
    code_bytes = b"".join(struct.pack("<I", i) for i in insns)
    rom[CODE_START:CODE_START + len(code_bytes)] = code_bytes

    # Colour table at ROM offset TBL_OFFSET.
    rom[TBL_OFFSET:TBL_OFFSET + 6] = struct.pack("<HHH", RED, GREEN, BLUE)

    # ---- header ---------------------------------------------------------
    # First instruction: `b main`  (target = CODE_START, PC-relative from 0)
    # ARM: PC during execute = 8, target = 0x20, delta_words = (0x20 - 8) / 4 = 6.
    rom[0x00:0x04] = struct.pack("<I", b(0xE, 6))
    rom[0x06:0x09] = b"GBA"
    rom[0xB2] = 0x96                              # fixed Nintendo logo checksum

    # Header checksum (byte at $0BD): sum of bytes $00-$BC + x = 0x197 (mod 0x100)
    s = sum(rom[0x00:0xBD]) & 0xFF
    rom[0xBD] = (0x197 - s) & 0xFF

    return bytes(rom)
