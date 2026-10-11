"""Gambatte / Game Boy Color sample ROM (32 KiB, MBC1 + battery SRAM).

Cartridge layout:
  * Header byte $147 = $03 -> MBC1 + Battery + SRAM
  * Header byte $149 = $02 -> 32 KiB external SRAM (4 x 8 KiB)
  * Header byte $143 = $00 -> DMG-compatible.  gambatte boots this in DMG
    mode, whose palette is 4 shades of grey (not CGB's 15-bit RGB); the
    sample cycles through three of them once per second.  Forcing CGB
    mode would give true RGB but the header flag also disables the DMG
    boot path, and gambatte's default is more permissive.

Z80 code layout:

  $0100: boot -- MBC1 SRAM enable ($2000 <- $0D), LCD on ($FF40 <- $80),
         initial BGP ($FF47 <- $FC), VBlank interrupt armed ($FF41 bit 0
         + $FF4F bit 0 + EI), channel 2 pulse at ~440 Hz, zero the state
         page $C000-$C003, then `halt; jr -2`.
  $0040: JP $0240 (VBlank ISR vector).
  $0240: VBlank ISR -- bumps a VBlank counter; every 60 VBlanks (~1 s)
         it cycles a colour index ($C002) mod 3, writes one of
         $FC/$F0/$AA to BGP, re-triggers channel 2 (so its length counter
         doesn't gate off), increments a seconds counter ($C003), and
         mirrors that byte into SRAM at $A000 (MBC1 SRAM window
         $A000-$BFFF, bank fixed to 0).

The SRAM byte is what gambatte serialises into the `.sav` sidecar via
retro_get_memory_data(), so save/load visibly changes the backdrop's
starting frame after reload.
"""


def build() -> bytes:
    rom = bytearray(0x8000)

    rom[0x147] = 0x03                 # MBC1+BATT+SRAM
    rom[0x148] = 0x00                 # ROM size: 32 KiB
    rom[0x149] = 0x02                 # SRAM size: 32 KiB

    # Vectors.
    rom[0x000:0x004] = bytes([0xC3, 0x00, 0x01, 0x00])  # JP $0100
    rom[0x040:0x043] = bytes([0xC3, 0x40, 0x02])        # JP $0240

    # ---- boot at $0100 ------------------------------------------------
    code = bytearray()
    def e(*bs): code.extend(bs)

    e(0x3E, 0x0D)                     # LD A, $0D
    e(0xEA, 0x00, 0x20)               # LD [$2000], A         ; MBC1 SRAM enable
    e(0x3E, 0x80, 0xE0, 0x40)         # LD A, $80 ; LDH ($40), A  ; LCD on
    e(0x3E, 0xFC, 0xE0, 0x47)         # LD A, $FC ; LDH ($47), A  ; BGP
    e(0x3E, 0x01, 0xE0, 0x41)         # STAT: VBlank INT enable
    e(0x3E, 0x01, 0xE0, 0x4F)         # IE: VBlank
    e(0x3E, 0x80, 0xE0, 0x26)         # NR52 master enable
    e(0x3E, 0x80, 0xE0, 0x17)         # NR21: duty 12.5%, length 0
    e(0x3E, 0xF8, 0xE0, 0x18)         # NR22: env 15 -> 0 over 8 ticks
    e(0x3E, 0x3D, 0xE0, 0x19)         # NR23: freq low  (440 Hz-ish)
    e(0x3E, 0x80, 0xE0, 0x1A)         # NR24: init + freq high 0
    e(0xAF)                            # XOR A
    e(0xEA, 0x00, 0xC0, 0xEA, 0x01, 0xC0)   # zero $C000, $C001
    e(0xEA, 0x02, 0xC0, 0xEA, 0x03, 0xC0)   # zero $C002, $C003
    e(0xFB)                            # EI
    e(0x76)                            # HALT
    e(0x18, 0xFE)                      # JR -2 (back to HALT)

    rom[0x100:0x100 + len(code)] = code

    # ---- VBlank ISR at $0240 ------------------------------------------
    # Tiny label-aware assembler: every branch is emitted with a placeholder
    # operand byte whose index we record, then patch_branch(target) fixes it.
    nmi = bytearray()
    def e_n(*bs): nmi.extend(bs)
    def jr(cond_op):
        """Emit a 2-byte JR with the given condition opcode; return the
        operand byte's index so we can patch its target later."""
        e_n(cond_op, 0x00)
        return len(nmi) - 1            # operand position

    def jp_hl():
        e_n(0xE9)

    def patch(operand_pos, target):
        # Z80 JR: PC-after-operand = operand_pos + 1 (as data address, but the
        # PC during evaluate is ISR base + operand_pos + 1 + 1).
        # Simpler: displacement = target - (operand_pos + 1) in the ISR's
        # own index space.  We work in ISR-relative indices here.
        rel = target - (operand_pos + 1)
        assert -128 <= rel <= 127, f"JR rel {rel} out of range"
        nmi[operand_pos] = rel & 0xFF

    # --- body -----------------------------------------------------------
    e_n(0xF5, 0xE5)                   # PUSH AF, PUSH HL
    e_n(0x21, 0x00, 0xC0)             # LD HL, $C000
    e_n(0x34)                          # INC [HL]  ; frame counter low
    e_n(0x7E)                          # LD A, [HL]
    e_n(0xFE, 60)                      # CP 60
    op_nz_done = jr(0x20)              # JR NZ, .done

    # -- per-second path --
    e_n(0x36, 0x00)                    # LD [HL], 0    ; reset frame counter
    # Colour index at $C002 -> (idx + 1) mod 3, stored back.
    e_n(0xFA, 0x02, 0xC0)              # LD A, [$C002]
    e_n(0xC6, 0x01)                    # ADD A, 1
    e_n(0xFE, 0x03)                    # CP 3
    op_c_store = jr(0x38)              # JR C, .store
    e_n(0xAF)                          # XOR A  (wrap to 0)
    e_n(0x32, 0x02, 0xC0)              # LD [$C002], A
    op_skip_store = jr(0x18)           # JR .store_done
    store = len(nmi)                   # .store:
    e_n(0x32, 0x02, 0xC0)              # LD [$C002], A
    store_done = len(nmi)              # .store_done:

    # Seconds counter at $C003 (we bump it once per palette switch).
    e_n(0xFA, 0x03, 0xC0)              # LD A, [$C003]
    e_n(0x3C)                          # INC A
    e_n(0x32, 0x03, 0xC0)              # LD [$C003], A

    # Switch A (= colour index) to the BGP byte we want.
    e_n(0xFA, 0x02, 0xC0)              # LD A, [$C002]
    e_n(0xFE, 0x00)                    # CP 0
    op_z_c0 = jr(0x28)                 # JR Z, .c0
    e_n(0xFE, 0x01)                    # CP 1
    op_z_c1 = jr(0x28)                 # JR Z, .c1
    # .c2 (fall-through):
    e_n(0x3E, 0xAA)                    # LD A, $AA
    op_c2_done = jr(0x18)              # JR .done
    # .c0:
    c0 = len(nmi)
    e_n(0x3E, 0xFC)                    # LD A, $FC
    op_c0_done = jr(0x18)              # JR .done
    # .c1:
    c1 = len(nmi)
    e_n(0x3E, 0xF0)                    # LD A, $F0
    # falls through to .done

    done = len(nmi)                    # .done:
    e_n(0xE0, 0x47)                    # LDH ($47), A   ; update BGP

    # Re-trigger channel 2 (write NR21 then NR24 to reload the length
    # counter and set the "initial" bit so the envelope restarts).
    e_n(0x3E, 0x80, 0xE0, 0x17)        # NR21 = $80
    e_n(0x3E, 0x80, 0xE0, 0x1A)        # NR24 = $80 (init)

    # Mirror the seconds byte into SRAM at $A000 (bank 0 of the MBC1 SRAM).
    e_n(0xFA, 0x03, 0xC0)               # LD A, [$C003]
    e_n(0xEA, 0x00, 0xA0)               # LD [$A000], A

    e_n(0xE1, 0xF1)                     # POP HL, POP AF
    e_n(0xD9)                           # RETI

    # Patch all branches now that labels are final.
    patch(op_nz_done, done)
    patch(op_c_store, store)
    patch(op_skip_store, store_done)
    patch(op_z_c0, c0)
    patch(op_z_c1, c1)
    patch(op_c2_done, done)
    patch(op_c0_done, done)

    rom[0x0240:0x0240 + len(nmi)] = nmi
    assert len(nmi) <= 0x400 - 0x40, f"ISR grew past $0400 (len={len(nmi)})"

    # Header checksum + complement (gambatte checks these).
    s = 0
    for b in rom[0x134:0x14D]:
        s = (s - b - 1) & 0xFF
    rom[0x14D] = s

    return bytes(rom)
