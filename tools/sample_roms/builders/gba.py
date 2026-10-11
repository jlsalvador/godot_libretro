"""mGBA / Game Boy Advance sample ROM (256 KiB).

Baseline: ARM branch into an infinite loop at the boot PC.  Ported
verbatim from the original single-file generator; the R/G/B palette
cycle, audio, and SRAM save are added in follow-up edits.
"""

import struct


def build() -> bytes:
    rom = bytearray(0x40000)
    rom[0x00:0x04] = bytes([0x00, 0x00, 0x00, 0xEA])       # b 0x0008
    rom[0x04:0x08] = b"GBA\x00"                              # SWI code
    rom[0x08:0x0C] = bytes([0x00, 0x00, 0xF0, 0xEA])       # b . (infinite loop)
    rom[0x40:0x66] = bytes(38)                              # logo left blank
    rom[0xB2] = 0x96                                        # Nintendo logo checksum
    header_bytes = rom[0x00:0xBD]
    rom[0xBD] = (0x19 - sum(header_bytes)) & 0xFF
    crc = _crc16(header_bytes + rom[0xBD:0xBE])
    rom[0xBE:0xC0] = struct.pack(">H", crc)
    return bytes(rom)


def _crc16(data: bytes) -> int:
    polynomial = 0x1021
    crc = 0x0000
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ polynomial) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc
