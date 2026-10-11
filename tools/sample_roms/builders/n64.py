"""mupen64plus-next / N64 sample ROM (.v64, 4 MiB).

Boot flow (mupen64plus-next + HLE bootrom):

  1. HLE bootrom copies ROM[0x40:0x1000] into RSP DMEM[0x40:0x1000] and
     sets PC = KSEG1 0xA4000040.  The new_dynarec maps KSEG1
     0xA4000000-0xA4001000 to RSP memory, so the *boot code* lives at
     ROM offset 0x40 and is fetched from there.
  2. The CPU sits in a tight `bne $t0, $zero, -1` spin loop while the VI
     counter advances.  When CP0 COUNT crosses the scheduled VI_INT
     event, `gen_interrupt` runs `vi_vertical_interrupt_event`, which
     calls `gfx.updateScreen()` (Angrylion fills the frontend's
     framebuffer + flips libretro_swap_buffer) and then `new_vi()`,
     which does `retro_return()` -> `co_switch(foreground_thread)`.
     The frontend's retro_run returns, video_cb is dispatched, and the
     next run_frame() re-enters the CPU coroutine right where it left
     off -- the spin loop resumes.  We never take an R4300 exception,
     so no `eret` handler is installed at KSEG0 0x80000180.

What makes a frame actually appear in the frontend:
  * Every VI register (VI_STATUS, VI_ORIGIN, VI_WIDTH, VI_V_INTR,
    VI_V_CURRENT_LINE, VI_TIMING, VI_V_SYNC, VI_H_SYNC, VI_H_SYNC_LEAP,
    VI_H_VIDEO, VI_V_VIDEO, VI_V_BURST, VI_X_SCALE, VI_Y_SCALE) must be
    programmed with real NTSC values, not just VI_V_SYNC and VI_V_INTR.
  * VI_ORIGIN must be non-zero -- Angrylion's n64video/vi/vi.c:642
    short-circuits with `screen_swap(true)` when the framebuffer word
    address is 0, so the frontend's libretro_swap_buffer never flips on.
  * PIF_CTRL <- 8 releases the CIC lockup; MI_INTR_MASK bit 7 unmasks
    the VI interrupt.
  * CP0 exception vectors are NOT needed for frame delivery:
    `vi_vertical_interrupt_event` runs `gfx.updateScreen() -> new_vi()`
    first, and `new_vi()` does the co_switch back to the frontend.  The
    RCP interrupt fires *after* that yield.

R4300 opcode reference:
    https://www.zophar.net/fileuploads/2/10655uytsm/N64ops03.txt
"""

import struct


def build() -> bytes:
    rom_size = 4 * 1024 * 1024
    rom = bytearray(rom_size)

    # --- m64p_rom_header ------------------------------------------------
    rom[0x00:0x04] = bytes([0x37, 0x80, 0x40, 0x12])  # .v64 signature
    rom[0x04:0x08] = struct.pack(">I", 0x00040000)    # ClockRate
    rom[0x08:0x0C] = struct.pack(">I", 0x0040)        # PC (fixed 0x40 by HLE)
    rom[0x0C:0x10] = struct.pack(">I", 1)             # Release
    rom[0x10:0x18] = b"\x00" * 8                      # CRC1, CRC2
    rom[0x18:0x20] = b"\x00" * 8                      # Unknown[2]
    rom[0x20:0x34] = b"Libretro N64 Sample "          # Name[20] (exactly 20 B)
    rom[0x34:0x38] = b"\x00" * 4                      # unknown
    rom[0x38:0x3C] = b"\x00" * 4                      # Manufacturer_ID
    rom[0x3C:0x3E] = b"\x00" * 2                      # Cartridge_ID
    # Country_code (0x3E) and Version (0x3F) are one 16-bit halfword; the
    # .v64 byteswap swaps them on load, so what we write at file[0x3F] is
    # what ROM_HEADER.Country_code reads after swap_copy_rom.  Country
    # only matters for the Goldeneye tlb_speed_hacks path -- the boot code
    # runs from RSP memory either way, so we keep it Demo (0).
    rom[0x3E] = 0x00                                  # -> Version after swap
    rom[0x3F] = 0x00                                  # -> Country_code (Demo)

    # --- .v64 word encoding ---------------------------------------------
    # mupen's boot path is (a) swap_copy_rom byte-swaps each 16-bit halfword
    # (this converts the file to .z64 BE-native bytes and is what populates
    # ROM_HEADER via memcpy), then (b) main_run's swap_buffer(mem, 4, ...)
    # byte-reverses each 32-bit word so the dynarec's u_int reads see the
    # native MIPS word.  Net: file bytes for MIPS word W must satisfy
    # byteswap16(byteswap32(file)) == BE bytes of W, so
    #   file = byteswap16(BE(W))
    #        = [(W>>16)&0xFF, (W>>24)&0xFF, W&0xFF, (W>>8)&0xFF].
    def v64_32(w: int) -> bytes:
        w &= 0xFFFFFFFF
        return bytes([(w >> 16) & 0xFF, (w >> 24) & 0xFF,
                      w & 0xFF,           (w >> 8) & 0xFF])

    def words(ws):
        return b"".join(v64_32(x) for x in ws)

    # --- R4300 encoders -------------------------------------------------
    def lui(rt, upper):
        return (0x0F << 26) | (rt << 16) | (upper & 0xFFFF)

    def ori(rs, rt, imm):
        return (0x0D << 26) | (rs << 21) | (rt << 16) | (imm & 0xFFFF)

    def addiu(rs, rt, imm):
        return (0x09 << 26) | (rs << 21) | (rt << 16) | (imm & 0xFFFF)

    def sw(rt, base, off):
        return (0x2B << 26) | (base << 21) | (rt << 16) | (off & 0xFFFF)


    def bne(rs, rt, off):
        return (0x05 << 26) | (rs << 21) | (rt << 16) | (off & 0xFFFF)

    NOP = 0x00000000

    T0, T1, T2 = 8, 9, 10
    ZR = 0
    ws = []

    # -- Full boot sequence ------------------------------------------------
    # The R4300's PC starts at KSEG1 0xA4000040 (HLE bootrom's memcpy target).
    # vi_vertical_interrupt_event calls gfx.updateScreen() then new_vi() which
    # does co_switch(foreground) -> the frontend emits the frame.  That yield
    # happens BEFORE the RCP interrupt would be raised, so we don't need to
    # install an exception handler or set CP0.Status: the CPU just needs to
    # sit in a tight spin loop while the VI counter fires periodically.
    #
    # Sequence:
    #   1. PIF_CTRL <- 8         (release CIC lockup)
    #   2. MI_INTR_MASK |= VI    (unmask VI so it can raise IP2; even though
    #                              we don't take the exception, some paths
    #                              still want the bit set)
    #   3. Program every VI register; VI_ORIGIN must be non-zero (Angrylion
    #      rejects the frame at vi.c:642 if the framebuffer pointer is 0).
    #   4. Fill RDRAM[0x100000..0x100000+320*240*2] with RED halfwords so
    #      the rendered frame isn't pure black.
    #   5. bne self-loop (dynarec-friendly: rs1 != rs2 to dodge the
    #      "beq $x, $x, odd" branch-target rewrite at new_dynarec.c:9408).

    # --- 1. PIF_CTRL <- 8 (KSEG1 0xBFC007FC) ---
    ws += [addiu(ZR, T1, 8), lui(T0, 0xBFC0), ori(T0, T0, 0x7FC), sw(T1, T0, 0)]

    # --- 2. MI_INTR_MASK |= MI_INTR_VI (KSEG1 0xA430000C) ---
    # update_mi_intr_mask: setting the VI-mask bit requires writing 0x80
    # (bit 7 of the write value -> mask |= MI_INTR_VI).  See mi_controller.c.
    ws += [addiu(ZR, T1, 0x80), lui(T0, 0xA430), ori(T0, T0, 0x0C), sw(T1, T0, 0)]

    # --- 3. VI registers ---
    ws += [lui(T0, 0xA440)]                            # $t0 = VI base

    def li_full(rt, val):
        """Load a 32-bit immediate into rt as [lui upper, ori lower]."""
        return [lui(rt, (val >> 16) & 0xFFFF), ori(rt, rt, val & 0xFFFF)]

    def store(reg_byte_offset, val):
        """lui/ori loads `val` into $t1, then `sw $t1, offset($t0)`."""
        return li_full(T1, val) + [sw(T1, T0, reg_byte_offset)]

    ws += store(0x00, 0x0002)                          # VI_STATUS   = BPP16
    ws += store(0x04, 0x00100000)                      # VI_ORIGIN   = 1 MB into RDRAM (byte addr)
    ws += store(0x08, 320)                            # VI_WIDTH
    ws += store(0x0C, 0x200)                          # VI_V_INTR
    ws += store(0x10, 0)                              # VI_V_CURRENT_LINE
    ws += store(0x14, 0x03E52239)                     # VI_TIMING
    ws += store(0x18, 0x20D)                          # VI_V_SYNC   = 525
    ws += store(0x1C, 0xC15)                          # VI_H_SYNC
    ws += store(0x20, 0xC150C15)                      # VI_H_SYNC_LEAP
    ws += store(0x24, 0x6C02EC)                       # VI_H_VIDEO
    ws += store(0x28, 0x2501FF)                       # VI_V_VIDEO
    ws += store(0x2C, 0xE0204)                        # VI_V_BURST
    ws += store(0x30, 0x200)                          # VI_X_SCALE  = 100*320/160
    ws += store(0x34, 0x400)                          # VI_Y_SCALE  = 100*240/60

    # --- 4. Fill a small region of the framebuffer with RED halfwords ---
    # VI_ORIGIN = 0x100000 (byte offset), so the R4300-side framebuffer
    # address is KSEG1 0xA0100000 (uncached RDRAM mirror).  Only fill one
    # scanline (320 halfwords = 640 bytes) to keep the boot loop small;
    # the rest of the "frame" reads whatever the pif HLE left there (0 =
    # black).  Angrylion's vi.c:642 gate is `frame_buffer != 0`, not the
    # actual pixel content, so a single filled line is enough to make the
    # VI produce a non-blank screen_swap.
    ws += li_full(T0, 0xA0100000)                      # $t0 = framebuffer byte address
    ws += li_full(T1, 0x0000F800)                     # $t1 = 5x RED halfwords (0xF800F800)
    ws += [addiu(ZR, T2, 80)]                          # 80 word-writes = 160 halfwords = 320 px

    # .fill_loop:
    fill_loop_pc_words = 0     # set below; the branch offset uses this label
    ws += [sw(T1, T0, 0)]                              # store two red pixels
    ws += [addiu(T0, T0, 4)]                           # advance by a word
    ws += [addiu(T2, T2, -1)]                          # decrement counter
    ws += [bne(T2, ZR, -4)]                            # back to .fill_loop (4 words)
    ws += [NOP]                                        # delay slot

    # --- 5. Spin ---
    # `bne $t0, $zero, -1`: at this point $t0 = 0xA0100500 (nonzero), so the
    # branch is always taken and the delay slot is a NOP.  Not using
    # `beq $x, $x, -1` because the dynarec heuristic at new_dynarec.c:9408
    # rewrites the target of "CJUMP && rs1==rs2 && imm LSB=1" to PC+8,
    # silently breaking self-loops.
    ws += [bne(T0, ZR, -1)]
    ws += [NOP]

    code = words(ws)
    assert len(code) % 4 == 0 and len(code) > 0
    assert len(code) <= 0xFC0, f"boot code ({len(code)} B) exceeds HLE-copied 0xFC0"
    rom[0x40:0x40 + len(code)] = code
    return bytes(rom)
