"""Gambatte / Game Boy Color sample ROM (32 KiB, DMG-compatible).

Z80 code layout:

  $0100: boot -- turn on LCD ($FF40=$80), set BGP ($FF47=$FC), enable
         VBlank interrupt (STAT bit 0 via $FF41, IME via $FF0F), zero
         the frame counter / colour index at $C000/$C001, `ei`, then
         `halt; jr -2` idle.
  $0040: JP -> $0240 (VBlank ISR vector).
  $0240: VBlank handler -- bumps the frame counter; every 60 VBlanks
         (~1 s) advances a colour index mod 3 and writes one of three
         DMG BGP values ($FC white/black contrast, $F0 mostly black,
         $AA checkerboard) into $FF47.

DMG has no RGB palette -- it's a 4-shade grayscale framebuffer -- so the
"R/G/B cycle" is expressed as cycling through three of the four shades.
Forcing CGB mode would give true RGB but breaks the DMG-only path in
gambatte, so we stay DMG-compatible.

No SRAM: this plain DMG cart is MBC0, so save/load falls back to a
savestate.  Battery-backed SRAM needs an MBC1/MBC2/MBC5 wiring, which is
out of scope here.
"""


def build() -> bytes:
    rom = bytearray(0x8000)

    # Reset and VBlank vectors.
    rom[0x000:0x004] = bytes([0xC3, 0x00, 0x01, 0x00])  # JP $0100
    rom[0x040:0x043] = bytes([0xC3, 0x40, 0x02])        # JP $0240

    # ---- boot at $0100 -------------------------------------------------
    code = bytearray()
    def e(*bs): code.extend(bs)

    e(0x3E, 0x80, 0xE0, 0x40)       # LD A, $80  ; LDH ($40), A  -- LCD on
    e(0x3E, 0xFC, 0xE0, 0x47)       # LD A, $FC  ; LDH ($47), A  -- BGP = white0..black3
    e(0x3E, 0x01, 0xE0, 0x41)       # LD A, $01  ; LDH ($41), A  -- STAT: VBlank INT enable
    e(0x3E, 0x01, 0xE0, 0xFF)       # LD A, $01  ; LDH ($FF), A  -- IE: VBlank
    e(0xAF, 0xEA, 0x00, 0xC0, 0xEA, 0x01, 0xC0)
                                    # XOR A; LD [$C000], A; LD [$C001], A  -- zero state
    e(0xFB)                          # EI
    e(0x76)                          # HALT
    e(0x18, 0xFE)                    # JR -2  (back to HALT)

    rom[0x100:0x100 + len(code)] = code

    # ---- VBlank handler at $0240 ---------------------------------------
    nmi = bytearray()
    def en(*bs): nmi.extend(bs)

    en(0xF5, 0xE5)                    # PUSH AF; PUSH HL
    en(0x21, 0x00, 0xC0)              # LD HL, $C000
    en(0x34)                          # INC [HL]
    en(0x7E)                          # LD A, [HL]
    en(0xFE, 60)                      # CP $3C
    jr_nz_pos = len(nmi); en(0x20, 0x00)   # JR NZ, .done (patched)

    # Per-second block:
    en(0x36, 0x00)                    # LD [HL], 0  (reset counter)
    # Advance colour index at $C001 mod 3.
    en(0xFA, 0x01, 0xC0)              # LD A, [$C001]
    en(0xC6, 0x01)                    # ADD A, 1
    en(0xFE, 0x03)                    # CP 3
    jr_c_pos = len(nmi); en(0x38, 0x00)   # JR C, .keep (patched)
    en(0xAF)                          # XOR A (wrap to 0)
    keep_pos = len(nmi)
    en(0x32, 0x01, 0xC0)              # LD [$C001], A
    load_idx = len(nmi)
    en(0xFA, 0x01, 0xC0)              # LD A, [$C001]

    # Switch on A: 0 -> $FC, 1 -> $F0, 2 -> $AA.
    en(0xFE, 0x00); jr_z0 = len(nmi) - 1; en(0x28, 0x00)  # JR Z, .c0 (patched)
    en(0xFE, 0x01); jr_z1 = len(nmi) - 1; en(0x28, 0x00)  # JR Z, .c1 (patched)
    # .c2 (fall-through):
    c2 = len(nmi)
    en(0x3E, 0xAA)
    jr_to_done_c2 = len(nmi); en(0x18, 0x00)               # JR .done (patched)
    c0 = len(nmi)
    en(0x3E, 0xFC)
    jr_to_done_c0 = len(nmi); en(0x18, 0x00)
    c1 = len(nmi)
    en(0x3E, 0xF0)
    jr_to_done_c1 = len(nmi); en(0x18, 0x00)

    done = len(nmi)
    en(0xE0, 0x47)                    # LDH ($47), A -- write BGP
    en(0xE1, 0xF1)                    # POP HL; POP AF
    en(0xD9)                          # RETI

    def patch(pos, target):
        rel = target - (pos + 2)      # Z80: PC after operand
        assert -128 <= rel <= 127, f"JR rel {rel} out of range"
        nmi[pos] = rel & 0xFF

    patch(jr_nz_pos + 1, done)
    patch(jr_c_pos + 1, keep_pos)
    patch(jr_z0 + 1, c0)
    patch(jr_z1 + 1, c1)
    patch(jr_to_done_c2 + 1, done)
    patch(jr_to_done_c0 + 1, done)
    patch(jr_to_done_c1 + 1, done)

    rom[0x0240:0x0240 + len(nmi)] = nmi
    return bytes(rom)
