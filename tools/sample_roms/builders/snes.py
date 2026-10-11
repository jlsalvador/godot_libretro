"""bsnes / SNES sample ROM (64 KiB loROM).

65816 in *emulation* mode (what the CPU resets to): 8-bit accumulator +
index, direct page at $0000, absolute addresses in bank $00.  Code lives
at $0040 (loROM PRG offset 0x40), vectors at $FFFA-$FFFF.

Program:

  * SEI, CLD
  * Set MADCTR ($2181-$2184) so the memory-mapped port can write CGRAM
    indirectly... actually simpler: use the classic PPU path
      - STA $2121 (CGADD) with index 0
      - STA $2122 (CGDATA) twice (lo/hi of the 15-bit colour)
  * INIDISP = $8F (screen on)
  * Wait for VBlank via $4210 bit 7
  * Bump a VBlank counter in ZP; every 60 VBlanks cycle CGRAM[0]
    through 15-bit XBBBBBGGGGGRRRRR: red -> green -> blue.

Save/load: the header declares 8 Kbit battery-backed SRAM at $7FD5 = $82
so the core writes a `.srm` sidecar; the ISR mirrors a seconds byte into
$00:6000 each palette flip.

Audio: no SPC700 program is loaded, so bsnes emits silent samples.  Real
audio needs a 256-byte SPC boot image uploaded via $2180-$2183 DMA and a
small DSP-register-writing loop; that's a bigger lift, tracked separately.
"""

import struct
import zlib


RED   = 0x001F
GREEN = 0x03E0
BLUE  = 0x7C00


def build() -> bytes:
    prg = bytearray(0x10000)

    # Vectors: point everything to our code at $0040.
    prg[0x7FFC:0x7FFE] = bytes([0x40, 0x00])   # NMI -> $0040
    prg[0x7FFE:0x8000] = bytes([0x40, 0x00])   # RESET -> $0040
    # Declare 8 Kbit SRAM with battery (header byte $15 = bit7=battery, low
    # nibble=2 for 8Kb).  $7FC0 is the loROM header base; $7FD5 is the
    # RAM/feature field.  bsnes exposes $00:6000-$00:7FFF as a save area
    # when this byte is non-zero; we mirror the seconds counter there.
    # Best-effort: some core revisions key off the *file* offset (0xFFD5
    # for a 64 KiB loROM) rather than the bank-00 mirror, so set both.
    prg[0x7FD5] = 0x82
    prg[0xFFD5] = 0x82
    # Emulation-mode RESET is at $FFFC; that's the address we're actually
    # at -- the loROM mirror makes $FFFC show through at $7FFC when the
    # core fetches from bank 00 upper area.  bsnes handles both.

    code = bytearray()
    def emit(*bs): code.extend(bs)
    def here(): return 0x40 + len(code)

    # ---- boot ----------------------------------------------------------
    emit(0x78)                       # SEI
    emit(0xD8)                       # CLD
    # Emulation mode already: 8-bit A/X/Y, memory; direct page $0000.
    # (If the core brought us up in native mode we'd XCE + SEP/REP here,
    # but bsnes resets to emulation mode per the 65816 spec.)

    # INIDISP = $8F (screen on, max brightness).
    emit(0xA9, 0x8F)                 # LDA #$8F
    emit(0x8D, 0x00, 0x21)           # STA $2100

    # Zero ZP scratch: $00 = frame counter, $01 = colour index, $02 = seconds.
    emit(0xA9, 0x00)                 # LDA #$00
    emit(0x85, 0x00)                 # STA $00
    emit(0x85, 0x01)                 # STA $01
    emit(0x85, 0x02)                 # STA $02

    # Prime CGRAM[0] = RED via the classic CGADD / CGDATA path.
    emit(0x8D, 0x21, 0x21)           # STA $2121 (CGADD = 0)
    emit(0xA9, RED & 0xFF)           # LDA #RED_LO
    emit(0x8D, 0x22, 0x21)           # STA $2122
    emit(0xA9, (RED >> 8) & 0xFF)    # LDA #RED_HI
    emit(0x8D, 0x22, 0x21)           # STA $2122

    # ---- main loop ------------------------------------------------------
    poll_addr = here()
    # LDA $4210  (VBlank status; read once clears the flag)
    emit(0xAD, 0x10, 0x42)
    emit(0x29, 0x80)                 # AND #$80
    emit(0xF0, 0x00)                 # BEQ poll_addr (patched)
    beq_poll_pos = len(code) - 1

    # Increment frame counter at ZP $00.
    emit(0xEE, 0x00, 0x00)           # INC $00
    # Compare with 60.
    emit(0xA5, 0x00)                 # LDA $00
    emit(0xC9, 60)                   # CMP #60
    emit(0xD0, 0x00)                 # BNE poll_addr (patched)
    bne_poll_pos = len(code) - 1

    # Per-second: reset counter, cycle colour, write CGRAM[0].
    emit(0xA9, 0x00)                 # LDA #$00
    emit(0x85, 0x00)                 # STA $00

    # Colour index mod 3 at $01.
    emit(0xEE, 0x01, 0x00)           # INC $01
    emit(0xA5, 0x01)                 # LDA $01
    emit(0xC9, 0x03)                 # CMP #3
    emit(0x90, 0x02)                 # BCC keep (+2)
    emit(0xA9, 0x00)                 # LDA #0 (wrap)
    # keep:
    emit(0x85, 0x01)                 # STA $01

    # Load 15-bit colour via a 2-byte table lookup.  We switch by CMP+BEQ.
    emit(0xA5, 0x01)                 # LDA $01
    emit(0xC9, 0x00)                 # CMP #0
    emit(0xF0, 0x00)                 # BEQ .c_red (patched)
    beq_c_red = len(code) - 1
    emit(0xC9, 0x01)                 # CMP #1
    emit(0xF0, 0x00)                 # BEQ .c_green
    beq_c_green = len(code) - 1
    emit(0xC9, 0x02)                 # CMP #2 -- implicit, fall to .c_blue
    c_blue = here()
    emit(0xA9, BLUE & 0xFF)          # LDA #BLUE_LO
    emit(0x8D, 0x22, 0x21)           # STA $2122
    emit(0xA9, (BLUE >> 8) & 0xFF)   # LDA #BLUE_HI
    emit(0x8D, 0x22, 0x21)           # STA $2122
    emit(0x80, 0x00)                 # BRA .done (patched)
    bra_done_blue = len(code) - 1

    c_red = here()
    emit(0xA9, RED & 0xFF)
    emit(0x8D, 0x22, 0x21)
    emit(0xA9, (RED >> 8) & 0xFF)
    emit(0x8D, 0x22, 0x21)
    emit(0x80, 0x00)                 # BRA .done
    bra_done_red = len(code) - 1

    c_green = here()
    emit(0xA9, GREEN & 0xFF)
    emit(0x8D, 0x22, 0x21)
    emit(0xA9, (GREEN >> 8) & 0xFF)
    emit(0x8D, 0x22, 0x21)

    done = here()
    # Re-latch CGADD to 0 for next cycle (already 0, but be safe).
    emit(0xA9, 0x00)
    emit(0x8D, 0x21, 0x21)
    # Bump the seconds byte in ZP $02 and mirror it into SRAM at $6000.
    # bsnes treats $00:6000-$00:7FFF as loROM external SRAM when the
    # header's RAM field ($7FD5) has bit 7 (battery) + a non-zero size;
    # see the header block below.
    emit(0xEE, 0x02, 0x00)           # INC $0002  (seconds counter)
    emit(0xA5, 0x02)                 # LDA $02
    emit(0x8D, 0x00, 0x60)           # STA $6000  (external SRAM byte 0)
    # Back to poll.
    emit(0x80, 0x00)                 # BRA poll_addr
    bra_poll_end = len(code) - 1

    def patch(pos, target):
        rel = target - (0x40 + pos + 1)
        assert -128 <= rel <= 127, f"branch rel {rel} out of range"
        code[pos] = rel & 0xFF

    patch(beq_poll_pos, poll_addr)
    patch(bne_poll_pos, poll_addr)
    patch(beq_c_red, c_red)
    patch(beq_c_green, c_green)
    patch(bra_done_blue, done)
    patch(bra_done_red, done)
    patch(bra_poll_end, poll_addr)

    prg[0x40:0x40 + len(code)] = code

    header = bytearray(0x40)
    header[0x00:0x14] = b"Libretro SNES Sample"
    header[0x14:0x24] = b"Libretro          "
    header[0x24:0x29] = b"LIBRE"
    header[0x2A] = 0x33
    header[0x2C] = 0x00                 # loROM
    header[0x2D] = 0x20                 # PRG 64 KiB (2^5 = 32 KiB per header byte? convention)
    header[0x30] = 0x00                 # NTSC
    rom = bytes(header) + bytes(prg[0x40:])
    fixed = bytearray(rom)
    fixed[0x32] = 0x00                  # checksum lo (leave 0; bsnes tolerates)
    fixed[0x33] = 0xFF                  # checksum hi
    fixed[0x34:0x38] = struct.pack("<I", zlib.crc32(rom) & 0xFFFFFFFF)
    return bytes(fixed)
