"""mupen64plus-next / N64 sample ROM (.v64, 4 MiB).

Boot flow (mupen64plus-next + HLE bootrom):

  1. HLE bootrom copies ROM[0x40:0x1000] into RSP DMEM[0x40:0x1000] and
     sets PC = KSEG1 0xA4000040.  The new_dynarec maps KSEG1
     0xA4000000-0xA4001000 to RSP memory, so the *boot code* lives at
     ROM offset 0x40 and is fetched from there.
  2. On any RCP (VI/AI/...) interrupt the R4300 jumps to KSEG0 0x80000180,
     which the dynarec maps to RDRAM offset 0x180.  ROM bytes are NOT
     visible at 0x80000180, so the boot code installs a minimal `eret`
     handler into RDRAM via `sw` before arming the VI.  Each vertical
     blank -> eret -> returns to the spin loop; the updateScreen()
     callback that runs inside vi_vertical_interrupt_event is what
     actually delivers the frame to the frontend.

Known limitation: producing *visible* pixels needs a real RSP display
list routed through the DPC/DPS -> RDP -> VI pipeline (Angrylion's
vdac_sync only fires once per processed display list).  This ROM boots
and arms the VI without crashing, but doesn't currently emit frames
into the frontend's video_cb; wiring the full display-list path is
tracked separately.

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

    def mtc0(rt, rd):
        # MTC0 lives in the COP0 sub-opcode table with rs_field=4 (see
        # idec.c:365-366, pure_interp.c:446).  rt = GPR source (bits 20:16),
        # rd = CP0 destination (bits 15:11).
        return (0x10 << 26) | (0x04 << 21) | (rt << 16) | (rd << 11)

    def bne(rs, rt, off):
        return (0x05 << 26) | (rs << 21) | (rt << 16) | (off & 0xFFFF)

    NOP = 0x00000000

    T0, T1, T2, T3 = 8, 9, 10, 11
    ZR = 0
    ws = []

    # Install the interrupt handler at KSEG0 0x80000180 (RDRAM offset 0x180).
    ws += [lui(T0, 0x8000), ori(T0, T0, 0x180)]
    ws += [lui(T1, 0x4200), ori(T1, T1, 0x18)]      # $t1 = 0x42000018 (eret)
    ws += [sw(T1, T0, 0)]
    ws += [addiu(ZR, T2, 0), sw(T2, T0, 4)]         # nop (delay slot)

    # Video interface registers (KSEG1, uncached) at 0xA4400000.
    # vi_reg = (address & 0x3f) >> 2, so register N is at byte offset 4*N.
    ws += [lui(T0, 0xA440)]
    ws += [sw(ZR,  T0, 0x04)]                        # VI_ORIGIN = 0
    ws += [addiu(ZR, T1, 320),   sw(T1, T0, 0x08)]   # VI_WIDTH
    ws += [addiu(ZR, T1, 299),   sw(T1, T0, 0x18)]   # VI_V_SYNC
    ws += [addiu(ZR, T1, 1),     sw(T1, T0, 0x0C)]   # VI_V_INTR (< V_SYNC)
    ws += [addiu(ZR, T1, 0x200), sw(T1, T0, 0x1C)]   # VI_X_SCALE
    ws += [addiu(ZR, T1, 0x400), sw(T1, T0, 0x20)]   # VI_Y_SCALE
    ws += [addiu(ZR, T1, 0x0001), sw(T1, T0, 0x00)]  # VI_STATUS = enable
    ws += [addiu(ZR, T1, 0x300), sw(T1, T0, 0x08)]   # re-latch VI_WIDTH

    # CP0 Status: IE (bit 0) | KX (bit 7) | IM2 (bit 10) = 0x0481.
    ws += [addiu(ZR, T3, 0x0481), mtc0(T3, 0)]

    # Spin with `bne $t0, $zero, -1` (not `beq $t0, $t0, -1`): the
    # new_dynarec heuristic at new_dynarec.c:9408-9410 detects
    # (CJUMP && rs1==rs2 && imm LSB=1) and rewrites the branch target to
    # PC+8, silently breaking any "beq $x, $x, odd" self-loop.
    ws += [bne(T0, ZR, -1)]
    ws += [NOP]

    code = words(ws)
    assert len(code) % 4 == 0 and len(code) > 0
    assert len(code) <= 0xFC0, f"boot code ({len(code)} B) exceeds HLE-copied 0xFC0"
    rom[0x40:0x40 + len(code)] = code
    return bytes(rom)
