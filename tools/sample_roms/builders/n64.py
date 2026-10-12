"""mupen64plus-next sample ROM (.v64, 4 MiB).

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
    # m64p_rom_header: build it in .z64 (big-endian native) form, then
    # halfword-swap the whole block into .v64 file form -- swap_copy_rom
    # undoes the swap on load, so the core reads each field exactly as
    # written here.  (Writing the fields in .z64 order directly is what
    # made the name come back byte-paired and Release/PC shifted.)
    z64 = bytearray(0x40)
    z64[0x00:0x04] = bytes([0x80, 0x37, 0x12, 0x40])  # .z64 signature
    z64[0x04:0x08] = struct.pack(">I", 0x04000000)    # ClockRate
    z64[0x08:0x0C] = struct.pack(">I", 0x40)          # PC (fixed 0x40 by HLE)
    z64[0x0C:0x10] = struct.pack(">I", 1)             # Release
    z64[0x10:0x18] = b"\x00" * 8                      # CRC1, CRC2
    z64[0x18:0x20] = b"\x00" * 8                      # Unknown[2]
    z64[0x20:0x34] = b"m64plus-next Sample "          # Name[20] (exactly 20 B)
    z64[0x34:0x38] = b"\x00" * 4                      # unknown
    z64[0x38:0x3C] = b"\x00" * 4                      # Manufacturer_ID
    z64[0x3C:0x40] = b"\x00" * 4                      # Cartridge_ID, Country,
                                                     # Version (Country stays
                                                     # Demo 0: it only feeds
                                                     # the tlb_speed_hacks path)
    v64_header = bytearray(0x40)
    v64_header[0::2], v64_header[1::2] = z64[1::2], z64[0::2]
    rom[0x00:0x40] = v64_header

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

    def beq(rs, rt, off):
        return (0x04 << 26) | (rs << 21) | (rt << 16) | (off & 0xFFFF)

    def lw(rt, base, off):
        return (0x23 << 26) | (base << 21) | (rt << 16) | (off & 0xFFFF)

    def andi(rs, rt, imm):
        return (0x0C << 26) | (rs << 21) | (rt << 16) | (imm & 0xFFFF)

    def or_i(rs, rt, imm):
        return (0x0D << 26) | (rs << 21) | (rt << 16) | (imm & 0xFFFF)

    def sll(rt, rd, sh):
        # SPECIAL, funct=0, shamt in bits 10:6, rd in bits 15:11, rt in bits 20:16
        return (rd << 11) | (rt << 16) | ((sh & 0x1F) << 6)

    def addu(rd, rs, rt):
        # SPECIAL, funct=0x21: rs (bits 25:21), rt (20:16), rd (15:11).
        return (rs << 21) | (rt << 16) | (rd << 11) | 0x21

    def j(index_word):
        # J-op 2: 26-bit word index; upper 4 bits come from (PC+4)&0xF0000000.
        return (2 << 26) | (index_word & 0x03FFFFFF)

    def xori_op(rs, rt, imm):
        # XOR immediate (zero-extends), op = 0x0E -- handy for a 16-bit
        # unsigned load via `xori $rt, $zero, imm`.
        return (0x0E << 26) | (rs << 21) | (rt << 16) | (imm & 0xFFFF)

    def label_of(ws_index):
        # Absolute CPU address of ws[ws_index] -- boot code lives at KSEG1
        # 0xA4000040 in RSP memory (HLE bootrom's memcpy target).
        return 0xA400_0040 + ws_index * 4

    def rel_branch(ws_index, target_ws_index):
        # Signed word offset for a CJUMP at ws[ws_index] targeting ws[target].
        # MIPS semantics: target = (branch_PC + 4) + offset*4.
        return target_ws_index - ws_index - 1

    def j_to(ws_index, target_ws_index):
        return j(label_of(target_ws_index) >> 2)

    NOP = 0x00000000

    T0, T1, T2 = 8, 9, 10
    ZR = 0

    # RGB555 primaries (15-bit, XBBBBBGGGGGRRRRR).
    RED, GREEN, BLUE = 0xF800, 0x07E0, 0x001F
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

    # --- 4. Colour table at ROM offset 0x340 (KSEG1 0xA4000340 in RSP mem). -
    # Three u32 words, each holding the RGB555 halfword duplicated.  Emitted
    # after `code`; see `rom[0x340:...]` below.

    # --- 5. Polling loop that repaints the framebuffer ---------------------
    # $t0 = MI base (0xA4300000)     -- MI_INTR_REG is at +0x08
    # $t1 = VI base (0xA4400000)     -- VI_CURRENT_REG at +0x10 clears MI.VI
    # $t2 = colour index (0/1/2)     -- mod 3, cycled every 60 VIs (~1 s)
    # $t3 = frame counter (0..60)
    # $t4 = colour table base (0xA4000340 in RSP mem, HLE-copied)
    # $t5 = framebuffer write cursor (0xA0100000 during fill)
    # $t6 = current colour word
    # $t7 = scratch
    # $t8 = fill loop counter
    # $t9 = MI read value
    T3, T4, T5, T6, T7 = 11, 12, 13, 14, 15
    T8, T9 = 24, 25

    ws += [lui(T0, 0xA430), ori(T0, T0, 0)]         # MI base
    ws += [lui(T1, 0xA440), ori(T1, T1, 0)]         # VI base  (overwrites $t1 -- that's fine, no more stores)
    ws += [addiu(T2, ZR, 0), addiu(T3, ZR, 0)]      # colour idx = 0, frame counter = 0
    ws += [lui(T4, 0xA400), ori(T4, T4, 0x340)]     # colour table base

    poll_index = len(ws)
    # lw $t9, 8($t0)
    ws += [lw(T9, T0, 8)]
    # andi $t9, $t9, 0x08
    ws += [andi(T9, T9, 0x08)]
    # beq $t9, $zero, poll_index  -> branch offset patched below
    beq_wait_index = len(ws)
    ws += [0]                                       # placeholder for beq
    ws += [NOP]                                     # delay slot
    # sw $zero, 0x10($t1)  -- VI_CURRENT_REG <- 0 clears MI_INTR_VI
    ws += [sw(ZR, T1, 0x10)]
    # Preload $t7 = 60 for the frame-counter compare (addiu sign-extends,
    # but 60 is small enough to be positive).
    ws += [addiu(T7, ZR, 60)]
    ws += [addiu(T3, T3, 1)]                        # frame counter += 1
    bne_poll1_index = len(ws)
    ws += [bne(T3, T7, 0)]                          # != 60: back to poll (offset patched below)
    ws += [NOP]

    # 60 frames elapsed.  Reset counter and advance colour index.
    ws += [addiu(T3, ZR, 0)]                        # counter = 0
    ws += [addiu(T2, T2, 1)]                        # colour idx += 1
    ws += [addiu(T7, ZR, 3)]                        # $t7 = 3
    bne_wrap_index = len(ws)
    ws += [bne(T2, T7, 0)]                          # != 3: skip wrap (offset patched)
    ws += [NOP]
    ws += [addiu(T2, ZR, 0)]                        # colour idx = 0

    # Load colour word: $t7 = $t2 << 2; $t7 += $t4; $t6 = lw $t7
    ws += [sll(T7, T2, 2)]                          # byte offset within table
    ws += [addu(T7, T4, T7)]                        # &table[i]
    ws += [lw(T6, T7, 0)]                           # $t6 = colour word

    # Framebuffer fill loop:
    #   $t5 = 0xA0100000 (byte address of RDRAM frame buffer)
    #   $t8 = 38400 (word-writes = 320 * 240 pixels / 2 per word)
    ws += [lui(T5, 0xA010), ori(T5, T5, 0)]
    # 38400 = 0x9600 word-writes (320*240 pixels / 2 per u32 word).  addiu
    # sign-extends, so use xori $zero, imm which zero-extends the 16-bit imm.
    ws += [xori_op(T8, ZR, 0x9600)]

    fill_index = len(ws)
    ws += [sw(T6, T5, 0)]                           # store colour word to fb
    ws += [addiu(T5, T5, 4)]                        # advance cursor
    ws += [addiu(T8, T8, -1)]                       # decrement
    bne_fill_index = len(ws)
    ws += [bne(T8, ZR, -4)]                         # back to fill
    ws += [NOP]

    # back to poll
    j_poll_index = len(ws)
    ws += [0]                                       # placeholder for j poll

    # Patch branches now that we know all indices.
    ws[beq_wait_index] = beq(T9, ZR, rel_branch(beq_wait_index, poll_index))
    ws[bne_poll1_index] = bne(T3, T7, rel_branch(bne_poll1_index, poll_index))
    # bne_wrap skip target = 1 word past the delay-slot, i.e. past the
    # 1-word wrap body (`addiu $t2, $zero, 0`).
    ws[bne_wrap_index] = bne(T2, T7, rel_branch(bne_wrap_index, bne_wrap_index + 2))
    ws[j_poll_index] = j_to(j_poll_index, poll_index)

    # Colour table lives at ROM offset 0x340 -> HLE copies to RSP mem offset
    # 0x340 which the CPU fetches at KSEG1 0xA4000340.  Three u32 words,
    # each holding a duplicated RGB555 halfword so a single `sw` writes two
    # pixels of the same colour.
    red_word   = (RED   & 0xFFFF) * 0x10001
    green_word = (GREEN & 0xFFFF) * 0x10001
    blue_word  = (BLUE  & 0xFFFF) * 0x10001
    code = words(ws)
    assert len(code) % 4 == 0 and len(code) > 0
    # Colour table sits at ROM offset 0x340, so boot code must fit before it.
    assert len(code) <= 0x300, f"boot code ({len(code)} B) collides with colour table at 0x340"
    rom[0x40:0x40 + len(code)] = code

    # Write the three-entry colour table (each is a duplicated RGB555 halfword).
    table_bytes = v64_32(red_word) + v64_32(green_word) + v64_32(blue_word)
    rom[0x340:0x340 + len(table_bytes)] = table_bytes
    return bytes(rom)
