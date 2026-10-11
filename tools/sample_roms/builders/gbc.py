"""Gambatte / Game Boy Color sample ROM (32 KiB, plain DMG header).

Baseline: enable the LCD, then a tight idle loop.  Ported verbatim from
the original single-file generator; the R/G/B palette cycle, audio, and
battery save are added in follow-up edits.
"""


def build() -> bytes:
    rom = bytearray(0x8000)
    rom[0x000:0x003] = bytes([0xC3, 0x00, 0x01])       # JP $0100
    for vector in (0x0040, 0x0048, 0x0050, 0x0058, 0x0060, 0x0068):
        rom[vector:vector + 3] = bytes([0xC3, 0x00, 0x01])
    rom[0x0100:0x0103] = bytes([0x3E, 0x80])           # LD A, $80
    rom[0x0102:0x0104] = bytes([0xEE, 0x40])           # LDH ($40), A  (LCD on)
    rom[0x0104:0x0107] = bytes([0xC3, 0x04, 0x01])     # JP $0104 (idle)
    rom[0x143:0x146] = b"CGB"
    return bytes(rom)
