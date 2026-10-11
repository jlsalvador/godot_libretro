"""bsnes / SNES sample ROM (64 KiB loROM).

Baseline: display on, black backdrop, wait-VBlank idle loop.  Ported
verbatim from the original single-file generator; the R/G/B cycle and
save/load behaviour are added in follow-up edits.
"""

import struct
import zlib


def build() -> bytes:
    prg = bytearray(0x10000)

    # Vectors: NMI/RESET/IRQ -> $0040 (bank 00 loROM mirror; the ROM
    # header at $7FC0-$7FFF provides the boot-time fetch).
    prg[0x00:0x02] = bytes([0x48, 0x40])       # JMP $0040 (unused -- see below)
    prg[0x7FFC:0x7FFE] = bytes([0x40, 0x00])   # NMI -> $0040
    prg[0x7FFD] = 0x00                          # RESET low byte shares
    prg[0x7FFE:0x8000] = bytes([0x40, 0x00])   # SWI/IRQ -> $0040

    # 65816: SEI, CLD, LDA #$00, STA $2133 (backdrop colour 0),
    # LDA #$80, STA $2100 (display on), then wait for VBlank.
    code = bytes([
        0x78,                    # SEI
        0xD8,                    # CLD
        0xA9, 0x00,              # LDA #$00
        0x8B, 0x33, 0x21,        # STA $2133 (via long-mode absolute)
        0xA9, 0x80,              # LDA #$80
        0x8B, 0x00, 0x21,        # STA $2100 (INIDISP: screen off + force blank)
        0xAD, 0x10, 0x42,        # LDA $4210 (VBlank flag)
        0x29, 0x80,              # AND #$80
        0x10, 0xF9,              # BPL back to LDA $4210
        0x4C, 0x0C, 0x00,        # JMP $004C (idle)
    ])
    prg[0x0040:0x0040 + len(code)] = code

    header = bytearray(0x40)
    header[0x00:0x14] = b"Libretro sample      "
    header[0x14:0x24] = b"Libretro          "
    header[0x24:0x29] = b"LIBRE"
    header[0x29] = 0x00
    header[0x2A] = 0x33
    header[0x2B] = 0x00
    header[0x2C] = 0x00          # Mapper: loROM.
    header[0x2D] = 0x20          # PRG size: 64 KiB.
    header[0x2E] = 0x00          # RAM size: none.
    header[0x2F] = 0x00          # SRAM size: none.
    header[0x30] = 0x00          # Region: NTSC America.
    header[0x31] = 0x00          # Revision.
    rom = bytes(header) + bytes(prg[0x40:])
    checksum = sum(rom[0x12:0x32]) & 0xFF
    crc = zlib.crc32(rom) & 0xFFFFFFFF
    fixed = bytearray(rom)
    fixed[0x32] = checksum
    fixed[0x33] = 0x00
    fixed[0x34:0x38] = struct.pack("<I", crc)
    return bytes(fixed)
