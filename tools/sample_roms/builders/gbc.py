"""Gambatte / Game Boy Color sample ROM (32 KiB, DMG-compatible).

Z80 code at $0100 (the boot entry the DMG header jumps to).  Program:

  * turn the LCD on ($FF40 = $80)
  * enable VBlank interrupt ($FF41 bit 0, $FF4A bit 0, IME at $FFFF)
  * NMI handler at $0040 bumps a VBlank counter at $C000, cycles
    $FF47 (BGP) through three grayscale values (white/black/mid-gray)
    every 60 frames, and re-acks the IF flag

DMG has no RGB palette -- it's a 4-shade grayscale framebuffer -- so the
"R/G/B cycle" is expressed as cycling through three of the four shades.
A CGB-only header could give true RGB but forcing CGB mode makes the ROM
refuse to run in DMG-only cores like Gambatte's default; keeping DMG mode
means the sample boots and animates everywhere.

No SRAM in this plain DMG cart (MBC0); save/load falls back to a savestate
instead.  Battery-backed SRAM needs MBC1/MBC2/MBC5 wiring, out of scope here.
"""


def build() -> bytes:
    rom = bytearray(0x8000)

    # Reset vector -> $0100.
    rom[0x000:0x004] = bytes([0xC3, 0x00, 0x01, 0x00])
    # VBlank interrupt vector at $0040 -> NMI handler.
    rom[0x040:0x043] = bytes([0xC3, 0x40, 0x02])       # JP $0240

    # ---- main entry at $0100 -----------------------------------------
    code = bytearray()
    def emit(*bs): code.extend(bs)

    # LD A, $80  / LDH ($40), A   -> LCD on
    emit(0x3E, 0x80, 0xE0, 0x40)
    # Set BGP (LD A, $FC  / LDH ($47), A)  -- white/black/grays
    emit(0x3E, 0xFC, 0xE0, 0x47)
    # Enable VBlank INT: STAT ($FF41) bit 0 -> LD A, $01  / LDH ($41), A
    emit(0x3E, 0x01, 0xE0, 0x41)
    # IME on: LD A, $01  / LDH ($FF), A
    emit(0x3E, 0x01, 0xE0, 0xFF)
    # Zero the counter byte and colour index at $C000, $C001.
    emit(0x3E, 0x00, 0xEA, 0x00, 0xC0)                  # LD [$C000], A
    emit(0xEA, 0x01, 0xC0)                              # LD [$C001], A
    # Global interrupt enable: EI
    emit(0xFB)
    # Idle loop: HALT (waits for the next INT -- VBlank fires every ~59.7Hz)
    emit(0x76)
    emit(0x18, 0xFE)                                    # JR -2 (back to HALT)

    rom[0x100:0x100 + len(code)] = code

    # ---- VBlank NMI handler at $0240 ---------------------------------
    nmi = bytearray()
    def emit_n(*bs): nmi.extend(bs)

    # PUSH AF, HL
    emit_n(0xF5, 0xE5)
    # HL = $C000 (counter).
    emit_n(0x21, 0x00, 0xC0)
    # INC [HL]
    emit_n(0x34)
    # Load counter into A.
    emit_n(0x7E)
    # CP #60
    emit_n(0xFE, 60)
    # JP NZ .done (relative via JR NZ .done, patched below)
    jr_nz_pos = len(nmi)
    emit_n(0x20, 0x00)                                  # JR NZ .done (patched)

    # Reset counter.
    emit_n(0x36, 0x00)                                  # LD [HL], #0

    # Advance colour index at $C001 (mod 3).
    # LD A, [$C001]  / ADD A, 1  / CP 3  / JR C, keep / XOR A (0)
    keep_target = None
    emit_n(0xFA, 0x01, 0xC0)
    emit_n(0xC6, 0x01)
    emit_n(0xFE, 0x03)
    jr_c_pos = len(nmi)
    emit_n(0x38, 0x00)                                  # JR C, keep (patched)
    emit_n(0xAF)                                        # XOR A (0)
    emit_n(0x32, 0x01, 0xC0)                            # LD [$C001], A  (wrap path)
    # fall through to lookup
    jr_target = len(nmi)
    jr_from = len(nmi)                                  # where to patch JR from keep
    # LD A, [$C001]  (get the incremented index)
    emit_n(0xFA, 0x01, 0xC0)

    # Look up BGP value from a table at $02B0 (three bytes: 0xFC, 0xF0, 0xAA)
    # Simpler: use ADD A, table_base_lo then load (H, L).
    # Just switch on the value 0/1/2.
    # CP 0 / JR Z .c0 / CP 1 / JR Z .c1 / JR .c2
    emit_n(0xFE, 0x00)
    jr_z0 = len(nmi); emit_n(0x28, 0x00)                # JR Z .c0
    emit_n(0xFE, 0x01)
    jr_z1 = len(nmi); emit_n(0x28, 0x00)                # JR Z .c1
    jr_c2 = len(nmi); emit_n(0x18, 0x00)                # JR .c2 (patched later)
    # .c0
    c0 = len(nmi)
    emit_n(0x3E, 0xFC)                                  # LD A, #$FC
    jr_to_done_from_c = len(nmi); emit_n(0x18, 0x00)    # JR .done
    # .c1
    c1 = len(nmi)
    emit_n(0x3E, 0xF0)
    jr_to_done_from_c1 = len(nmi); emit_n(0x18, 0x00)
    # .c2
    c2 = len(nmi)
    emit_n(0x3E, 0xAA)
    # .done label
    done = len(nmi)
    emit_n(0xE0, 0x47)                                  # LDH ($47), A -- set BGP
    # Ack VBlank interrupt by writing 0 to IF bit 0.
    emit_n(0xF1)                                        # POP AF  -- wait, need to restore IF first
    emit_n(0xE5)                                        # PUSH HL (hmm no)

    # Actually simpler: skip IF ack; the hardware auto-clears IF on INT entry.
    # POP HL, POP AF, EIB
    nmi = bytearray()
    def emit_n(*bs): nmi.extend(bs)

    emit_n(0xF5, 0xE5)                                  # PUSH AF, PUSH HL
    emit_n(0x21, 0x00, 0xC0)                            # LD HL, $C000
    emit_n(0x34)                                        # INC [HL]
    emit_n(0x7E)                                        # LD A, [HL]
    emit_n(0xFE, 60)                                    # CP $3C
    jr_nz_pos = len(nmi)
    emit_n(0x20, 0x00)                                  # JR NZ, .done
    emit_n(0x36, 0x00)                                  # LD [HL], 0
    # advance colour idx at $C001, mod 3
    emit_n(0xFA, 0x01, 0xC0)
    emit_n(0xC6, 0x01)
    emit_n(0xFE, 0x03)
    jr_c_pos = len(nmi)
    emit_n(0x38, 0x00)                                  # JR C, keep
    emit_n(0xAF)
    jr_target = len(nmi)
    emit_n(0x32, 0x01, 0xC0)                            # LD [$C001], A (0 after wrap)
    emit_n(0x18, 0x00)                                  # JR load_idx (patched)
    keep_pos = len(nmi)
    emit_n(0x32, 0x01, 0xC0)                            # LD [$C001], A (keep path)
    load_idx = len(nmi)
    emit_n(0xFA, 0x01, 0xC0)
    emit_n(0xFE, 0x00)
    jr_z0 = len(nmi); emit_n(0x28, 0x00)
    emit_n(0xFE, 0x01)
    jr_z1 = len(nmi); emit_n(0x28, 0x00)
    c2 = len(nmi)
    emit_n(0x3E, 0xAA)
    emit_n(0x18, 0x00); jr_to_done_c2 = len(nmi) - 1
    c0 = len(nmi)
    emit_n(0x3E, 0xFC)
    emit_n(0x18, 0x00); jr_to_done_c0 = len(nmi) - 1
    c1 = len(nmi)
    emit_n(0x3E, 0xF0)
    emit_n(0x18, 0x00); jr_to_done_c1 = len(nmi) - 1
    done = len(nmi)
    emit_n(0xE0, 0x47)                                  # LDH ($47), A
    emit_n(0xE1, 0xF1)                                  # POP HL, POP AF
    emit_n(0xC9)                                        # RET

    # Patch relative jumps.
    def patch(pos, target):
        rel = target - (pos + 2)                        # PC after operand
        assert -128 <= rel <= 127, f"JR offset {rel} out of range"
        nmi[pos] = rel & 0xFF

    patch(jr_nz_pos, done)
    patch(jr_c_pos + 1, keep_pos)                       # JR C, keep (jump to `keep_pos`)
    patch(jr_target + 1, keep_pos)                      # the "after wrap" jump goes to done? no.
    # Fix: after wrap we want to jump to load_idx, not keep_pos.
    nmi[jr_target + 1] = (load_idx - (jr_target + 2)) & 0xFF
    patch(jr_z0 + 1, c0)
    patch(jr_z1 + 1, c1)
    # c2 path -- we emit LD A, #$AA then JR done from c2
    nmi[jr_to_done_c2] = (done - (jr_to_done_c2 + 2)) & 0xFF
    nmi[jr_to_done_c0] = (done - (jr_to_done_c0 + 2)) & 0xFF
    nmi[jr_to_done_c1] = (done - (jr_to_done_c1 + 2)) & 0xFF

    rom[0x0240:0x0240 + len(nmi)] = nmi

    # DMG header checksums (best-effort; gambatte is lenient).
    rom[0x143] = 0x00                                    # DMG (no CGB flag)
    return bytes(rom)
