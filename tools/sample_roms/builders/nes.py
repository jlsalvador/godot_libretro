"""FCEUmm sample ROM (NROM-128 with battery-backed SRAM).

6502 program, all in the 16 KiB PRG bank at $8000-$BFFF:

  * wait for the PPU to settle (two BIT $2002 reads clear the VBlank latch)
  * keep NMI off in $2000: this program polls, and the NMI vector is the reset
    entry, so enabling it would restart the boot code on every VBlank
  * enable the display ($2001 = $06: show background and sprites) and start
    pulse channel 1 at ~440 Hz via $4000-$4003 + $4015, so the audio callback
    receives real samples
  * main loop: poll the VBlank flag in $2002 bit 7, bump a 16-bit counter
    at ZP $02:$03; every 60 VBlanks (~1 s) cycle the backdrop palette
    through red -> green -> blue by rewriting $3F00, and bump the
    battery-backed SRAM byte at $6000 to give save/load something to see

The 2 KiB SRAM at $6000-$67FF is the standard NROM battery save area that
FCEUmm serialises to the `.sram` sidecar via retro_get_memory_data().

Palette entries chosen for the R/G/B cycle: $16 red, $1B green, $22
blue (approximate primaries of the composite encoder).
"""


RESET = 0x8000
IRQ_BRK = 0x8000        # dummy RTI handler not needed; we disable IRQs
NMI = 0x8000           # dummy NMI handler points back at RESET head -- we
                       # don't rely on NMIs (SEI + polling), so this can be
                       # the reset entry itself; harmless if it fires once.

ZP_CNT_LO = 0x02
ZP_CNT_HI = 0x03
ZP_IDX = 0x04

PAL_RED = 0x16
PAL_GREEN = 0x1B
PAL_BLUE = 0x22


def build() -> bytes:
    rom_header = bytearray(16)
    rom_header[0:4] = b"NES\x1a"
    rom_header[4] = 1
    rom_header[5] = 0
    rom_header[6] = 0x02      # battery-backed save RAM

    code = bytearray()
    def emit(*bs):
        code.extend(bs)
    def here():
        return RESET + len(code)
    def patch_abs(pos, target):
        code[pos] = target & 0xFF
        code[pos + 1] = (target >> 8) & 0xFF

    # ---- RESET ---------------------------------------------------------
    emit(0x78, 0xD8)                        # SEI, CLD
    emit(0xA2, 0xFF, 0x9A)                  # LDX #$FF, TXS
    # Zero ZP state.
    emit(0xA9, 0x00)                        # LDA #$00
    emit(0x85, ZP_CNT_LO)                   # STA $02
    emit(0x85, ZP_CNT_HI)                   # STA $03
    emit(0x85, ZP_IDX)                      # STA $04

    # Wait for PPU: two BIT $2002 clears the VBlank latch.
    emit(0x2C, 0x02, 0x20)
    emit(0x2C, 0x02, 0x20)

    # $2000 = 0x00: NMI stays off, because this program polls $2002 instead of
    # taking NMIs, and the NMI vector is the reset entry: enabling it would
    # restart the boot sequence on every VBlank and the frame counter below
    # would never reach 60.
    emit(0xA9, 0x00, 0x8D, 0x00, 0x20)
    # $2001 = 0x06: show background and sprites (bits 1 and 2). The other bits
    # of 0x1E are colour emphasis and pattern-table selects, which do not turn
    # the PPU on; with the display off $2002 never raises its VBlank flag.
    emit(0xA9, 0x06, 0x8D, 0x01, 0x20)

    # Prime $3F00 = red.
    emit(0xA9, 0x3F, 0x8D, 0x06, 0x20)      # PPU addr hi
    emit(0xA9, 0x00, 0x8D, 0x06, 0x20)      # PPU addr lo -> $3F00
    emit(0xA9, PAL_RED, 0x8D, 0x07, 0x20)   # write red

    # Pulse channel 1: $AF = 12.5% duty, constant volume 15, no envelope.
    emit(0xA9, 0xAF, 0x8D, 0x00, 0x40)
    # $4001 sweep off.
    emit(0xA9, 0x00, 0x8D, 0x01, 0x40)
    # $4002 = period low ~63 -> 440 Hz.
    emit(0xA9, 0x3D, 0x8D, 0x02, 0x40)
    # $4003 = 0x80 -> length counter halt, period high = 0.
    emit(0xA9, 0x80, 0x8D, 0x03, 0x40)
    # Enable pulse 1 in $4015 (bit 0).
    emit(0xA9, 0x01, 0x8D, 0x15, 0x40)

    # ---- main loop: poll VBlank, count frames, cycle palette -----------
    # poll_wait:
    poll_wait_addr = here()
    emit(0x2C, 0x02, 0x20)                  # BIT $2002
    emit(0x10, 0x00)                        # BPL poll_wait (patched below)
    bpl_poll_wait_pos = len(code) - 1
    # After this BIT, the VBlank flag is cleared by the read; on the next
    # iteration BIT will see 0 until the next VBlank, so we exit only once
    # per VBlank.

    # Increment 16-bit VBlank counter at $02/$03.
    emit(0xE6, ZP_CNT_LO)                   # INC $02 (zero page: $EE is the
    emit(0xD0, 0x02)                        # BNE skip_hi (+2)
    emit(0xE6, ZP_CNT_HI)                   # INC $03
    # skip_hi:

    # Compare low byte to 60.  (Counter wraps at 256 anyway; we're fine.)
    emit(0xAD if False else 0xA5, ZP_CNT_LO)  # LDA $02 (zero-page)
    emit(0xC9, 60)                           # CMP #$3C
    emit(0xD0, 0x00)                         # BNE poll_wait (patched below)
    bne_poll_wait_pos = len(code) - 1

    # Reset the counter.
    emit(0xA9, 0x00)                         # LDA #$00
    emit(0x85, ZP_CNT_LO)                    # STA $02
    emit(0x85, ZP_CNT_HI)                    # STA $03

    # Advance colour index modulo 3.
    emit(0xA5, ZP_IDX)                       # LDA $04
    emit(0x18)                               # CLC: the CMP #$3C that routed us
                                             # here left Carry set, and ADC
                                             # would then add 2 and skip green.
    emit(0x69, 0x01)                         # ADC #$01
    emit(0xC9, 0x03)                         # CMP #$03
    emit(0x90, 0x02)                         # BCC keep (+2)
    emit(0xA9, 0x00)                         # LDA #$00
    # keep:
    emit(0x85, ZP_IDX)                       # STA $04

    # Poke the new palette entry into $3F00. The PPU address is written first:
    # the LDA #$3F / LDA #$00 pair would otherwise clobber the colour looked up
    # from the table, and $2007 would always receive $00.
    emit(0xA5, ZP_IDX)                       # LDA $04
    emit(0xAA)                               # TAX
    emit(0xA9, 0x3F, 0x8D, 0x06, 0x20)       # PPU address hi
    emit(0xA9, 0x00, 0x8D, 0x06, 0x20)       # PPU address lo
    # Table lives just after the code; we patch the LDA abs,X below.
    emit(0xBD, 0x00, 0x00)                   # LDA <table>, X  (patched)
    lda_table_pos = len(code) - 2
    emit(0x8D, 0x07, 0x20)                   # STA $2007

    # Re-latch pulse 1 (write $4015 again -- the length counter would
    # otherwise eventually gate off; harmless to rewrite every second).
    emit(0xA9, 0x01, 0x8D, 0x15, 0x40)

    # Bump the battery SRAM at $6000 (16-bit little-endian counter so the
    # save sidecar changes visibly across many runs).
    emit(0xEE, 0x00, 0x60)                   # INC $6000
    emit(0xD0, 0x02)                         # BNE skip (+2)
    emit(0xEE, 0x01, 0x60)                   # INC $6001

    # Jump back to poll_wait.
    emit(0x4C, 0x00, 0x00)                   # JMP poll_wait (patched)
    jmp_back_pos = len(code) - 2
    patch_abs(jmp_back_pos, poll_wait_addr)

    # Patch BNE poll_wait with a relative offset (branch back, so negative).
    # The 6502 branch displacement is relative to the PC of the *next*
    # instruction, i.e. one byte past the operand we're about to write.
    delta = poll_wait_addr - (RESET + bne_poll_wait_pos + 1)
    assert -128 <= delta <= 127, f"BNE poll_wait delta {delta} out of range"
    code[bne_poll_wait_pos] = delta & 0xFF

    # Patch BPL poll_wait (also a relative backward branch).
    delta = poll_wait_addr - (RESET + bpl_poll_wait_pos + 1)
    assert -128 <= delta <= 127, f"BPL poll_wait delta {delta} out of range"
    code[bpl_poll_wait_pos] = delta & 0xFF

    # Palette table: R, G, B (indexed by $04 = 0/1/2).
    table_addr = here()
    patch_abs(lda_table_pos, table_addr)
    emit(PAL_RED, PAL_GREEN, PAL_BLUE)

    assert len(code) <= 0x3FFA, "code collides with vectors"

    # ---- pack into a 16 KiB PRG bank ---------------------------------
    prg = bytearray([0xEA] * 0x4000)
    prg[:len(code)] = code
    prg[0x3FFA:0x3FFC] = bytes([NMI & 0xFF, NMI >> 8])
    prg[0x3FFC:0x3FFE] = bytes([RESET & 0xFF, RESET >> 8])
    prg[0x3FFE:0x4000] = bytes([IRQ_BRK & 0xFF, IRQ_BRK >> 8])

    return bytes(rom_header) + bytes(prg)
