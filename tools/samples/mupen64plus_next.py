#!/usr/bin/env python3
"""mupen64plus-next sample ROM (self-contained, emulator-oriented MIPS cartridge image).

The generated program displays a solid colour that cycles red, green, and blue
once per second and on each new controller-button press. It continuously plays
quiet pseudo-random noise and adds a square-wave tone while any button is held.
A 16-bit little-endian SRAM counter is initialized to one on blank SRAM and is
incremented once per second.
The cartridge header carries the advanced homebrew ROM header marker
(0x4445 at 0x3C) and save type 3 (256K SRAM) in the high nibble of 0x3F,
so the core routes cartridge saves to SRAM.

The script assembles both its small IPL3-style bootstrap and the main program
using only the Python standard library. The bootstrap targets emulators whose
PIF boot emulation enters the cartridge IPL3 and whose memory model exposes the
cartridge and RDRAM through their normal address ranges. It is not intended to
boot on physical hardware, which verifies IPL3 against a cartridge security chip
and requires full RDRAM initialization.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

ROM_SIZE = 0x200000
ROM_HEADER_SIZE = 0x40
IPL3_OFFSET = 0x40
IPL3_SIZE = 0xFC0
PROGRAM_ROM_OFFSET = 0x1000
PROGRAM_RAM_ORIGIN = 0xA0000400
BOOT_RAM_ORIGIN = 0xA4000040
BOOT_COPY_SIZE = 0x100000
CRC_REGION_OFFSET = 0x1000
CRC_REGION_SIZE = 0x100000
CRC_SEED_6102 = 0xF8CA4DDC
MASK32 = 0xFFFFFFFF

FRAMEBUFFER = 0xA0110000
FRAMEBUFFER_PHYS = 0x00110000
FRAMEBUFFER_WORDS = 320 * 240 // 2
AUDIO_BUFFER0 = 0xA0140000
AUDIO_BUFFER1 = 0xA0141000
AUDIO_BUFFER0_PHYS = 0x00140000
AUDIO_BUFFER1_PHYS = 0x00141000
AUDIO_FRAMES = 640
AUDIO_BYTES = AUDIO_FRAMES * 4
AUDIO_RATE = 32000
PIF_BUFFER = 0xA0150000
PIF_BUFFER_PHYS = 0x00150000
PIF_RAM = 0x1FC007C0

VI_BASE = 0xA4400000
SI_BASE = 0xA4800000
AI_BASE = 0xA4500000
SRAM_BASE = 0xA8000000

RED = 0xF801
GREEN = 0x07C1
BLUE = 0x003F

REGISTERS = {
    "zero": 0,
    "at": 1,
    "v0": 2,
    "v1": 3,
    "a0": 4,
    "a1": 5,
    "a2": 6,
    "a3": 7,
    "t0": 8,
    "t1": 9,
    "t2": 10,
    "t3": 11,
    "t4": 12,
    "t5": 13,
    "t6": 14,
    "t7": 15,
    "s0": 16,
    "s1": 17,
    "s2": 18,
    "s3": 19,
    "s4": 20,
    "s5": 21,
    "s6": 22,
    "s7": 23,
    "t8": 24,
    "t9": 25,
    "k0": 26,
    "k1": 27,
    "gp": 28,
    "sp": 29,
    "fp": 30,
    "ra": 31,
}


class MipsAssembler:
    """Small big-endian MIPS III assembler for the instructions used here."""

    def __init__(self, origin: int) -> None:
        self.origin = origin
        self.words: list[int] = []
        self.labels: dict[str, int] = {}
        self.fixups: list[tuple[int, str, str]] = []

    @staticmethod
    def reg(name: str) -> int:
        try:
            return REGISTERS[name.lstrip("$").lower()]
        except KeyError as exc:
            raise ValueError(f"unknown register: {name}") from exc

    def pc(self) -> int:
        return self.origin + len(self.words) * 4

    def label(self, name: str) -> None:
        if name in self.labels:
            raise ValueError(f"duplicate label: {name}")
        self.labels[name] = self.pc()

    def emit(self, word: int) -> None:
        self.words.append(word & MASK32)

    def r_type(self, rs: str, rt: str, rd: str, shamt: int, funct: int) -> None:
        self.emit(
            (self.reg(rs) << 21)
            | (self.reg(rt) << 16)
            | (self.reg(rd) << 11)
            | ((shamt & 31) << 6)
            | funct
        )

    def i_type(self, opcode: int, rs: str, rt: str, imm: int) -> None:
        self.emit(
            (opcode << 26)
            | (self.reg(rs) << 21)
            | (self.reg(rt) << 16)
            | (imm & 0xFFFF)
        )

    def addiu(self, rt: str, rs: str, imm: int) -> None:
        self.i_type(0x09, rs, rt, imm)

    def andi(self, rt: str, rs: str, imm: int) -> None:
        self.i_type(0x0C, rs, rt, imm)

    def ori(self, rt: str, rs: str, imm: int) -> None:
        self.i_type(0x0D, rs, rt, imm)

    def xori(self, rt: str, rs: str, imm: int) -> None:
        self.i_type(0x0E, rs, rt, imm)

    def lui(self, rt: str, imm: int) -> None:
        self.i_type(0x0F, "zero", rt, imm)

    def lw(self, rt: str, offset: int, base: str) -> None:
        self.i_type(0x23, base, rt, offset)

    def sw(self, rt: str, offset: int, base: str) -> None:
        self.i_type(0x2B, base, rt, offset)

    def lbu(self, rt: str, offset: int, base: str) -> None:
        self.i_type(0x24, base, rt, offset)

    def sb(self, rt: str, offset: int, base: str) -> None:
        self.i_type(0x28, base, rt, offset)

    def sh(self, rt: str, offset: int, base: str) -> None:
        self.i_type(0x29, base, rt, offset)

    def addu(self, rd: str, rs: str, rt: str) -> None:
        self.r_type(rs, rt, rd, 0, 0x21)

    def subu(self, rd: str, rs: str, rt: str) -> None:
        self.r_type(rs, rt, rd, 0, 0x23)

    def and_(self, rd: str, rs: str, rt: str) -> None:
        self.r_type(rs, rt, rd, 0, 0x24)

    def or_(self, rd: str, rs: str, rt: str) -> None:
        self.r_type(rs, rt, rd, 0, 0x25)

    def xor(self, rd: str, rs: str, rt: str) -> None:
        self.r_type(rs, rt, rd, 0, 0x26)

    def nor(self, rd: str, rs: str, rt: str) -> None:
        self.r_type(rs, rt, rd, 0, 0x27)

    def sll(self, rd: str, rt: str, shamt: int) -> None:
        self.r_type("zero", rt, rd, shamt, 0x00)

    def srl(self, rd: str, rt: str, shamt: int) -> None:
        self.r_type("zero", rt, rd, shamt, 0x02)

    def sltiu(self, rt: str, rs: str, imm: int) -> None:
        self.i_type(0x0B, rs, rt, imm)

    def beq(self, rs: str, rt: str, label: str) -> None:
        self.fixups.append((len(self.words), label, "branch"))
        self.i_type(0x04, rs, rt, 0)
        self.emit(0)

    def bne(self, rs: str, rt: str, label: str) -> None:
        self.fixups.append((len(self.words), label, "branch"))
        self.i_type(0x05, rs, rt, 0)
        self.emit(0)

    def bltz(self, rs: str, label: str) -> None:
        self.fixups.append((len(self.words), label, "bltz"))
        self.i_type(0x01, rs, "zero", 0)
        self.emit(0)

    def j(self, label: str) -> None:
        self.fixups.append((len(self.words), label, "jump"))
        self.emit(0x08000000)
        self.emit(0)

    def jal(self, label: str) -> None:
        self.fixups.append((len(self.words), label, "jal"))
        self.emit(0x0C000000)
        self.emit(0)

    def jr(self, rs: str) -> None:
        self.r_type(rs, "zero", "zero", 0, 0x08)
        self.emit(0)

    def li32(self, rt: str, value: int) -> None:
        value &= MASK32
        self.lui(rt, value >> 16)
        self.ori(rt, rt, value & 0xFFFF)

    def finish(self) -> bytes:
        for position, name, kind in self.fixups:
            if name not in self.labels:
                raise ValueError(f"undefined label: {name}")
            target = self.labels[name]
            word = self.words[position]
            if kind in ("branch", "bltz"):
                delta = (target - (self.origin + position * 4 + 4)) // 4
                if not -0x8000 <= delta <= 0x7FFF:
                    raise ValueError(f"branch to {name} is out of range")
                self.words[position] = (word & 0xFFFF0000) | (delta & 0xFFFF)
            else:
                if ((self.origin + position * 4 + 4) & 0xF0000000) != (target & 0xF0000000):
                    raise ValueError(f"jump to {name} crosses a 256 MiB region")
                self.words[position] = (word & 0xFC000000) | ((target >> 2) & 0x03FFFFFF)
        return b"".join(struct.pack(">I", word) for word in self.words)


def build_ipl3() -> bytes:
    """Build an emulator-oriented bootstrap that copies the main image to RDRAM."""
    a = MipsAssembler(BOOT_RAM_ORIGIN)

    # Read from the cartridge's uncached address range and copy to uncached RDRAM.
    # This avoids needing a data-cache flush before transferring execution.
    a.li32("t0", 0xB0000000 + PROGRAM_ROM_OFFSET)
    a.li32("t1", PROGRAM_RAM_ORIGIN)
    a.li32("t2", BOOT_COPY_SIZE // 4)
    a.label("copy_loop")
    a.lw("t3", 0, "t0")
    a.sw("t3", 0, "t1")
    a.addiu("t0", "t0", 4)
    a.addiu("t1", "t1", 4)
    a.addiu("t2", "t2", -1)
    a.bne("t2", "zero", "copy_loop")
    a.li32("t9", PROGRAM_RAM_ORIGIN)
    a.jr("t9")

    code = a.finish()
    if len(code) > IPL3_SIZE:
        raise ValueError("bootstrap does not fit in the IPL3 region")
    return code.ljust(IPL3_SIZE, b"\x00")


def build_program() -> bytes:
    a = MipsAssembler(PROGRAM_RAM_ORIGIN)

    # Main-state registers: s0=color, s1=previous buttons, s2=current buttons,
    # s3=frame counter, s5=next audio buffer, s6=tone phase, s7=noise state.
    a.addiu("s0", "zero", 0)
    a.addiu("s1", "zero", 0)
    a.addiu("s2", "zero", 0)
    a.addiu("s3", "zero", 0)
    a.addiu("s5", "zero", 0)
    a.addiu("s6", "zero", 0)
    a.li32("s7", 0xACE1)

    # Check the save marker and initialize blank save RAM.
    a.li32("s4", SRAM_BASE)
    a.lbu("t0", 2, "s4")
    a.addiu("t1", "zero", ord("S"))
    a.bne("t0", "t1", "save_init")
    a.lbu("t0", 3, "s4")
    a.addiu("t1", "zero", ord("V"))
    a.beq("t0", "t1", "save_ready")
    a.label("save_init")
    a.addiu("t0", "zero", 1)
    a.sb("t0", 0, "s4")
    a.addiu("t0", "zero", 0)
    a.sb("t0", 1, "s4")
    a.addiu("t0", "zero", ord("S"))
    a.sb("t0", 2, "s4")
    a.addiu("t0", "zero", ord("V"))
    a.sb("t0", 3, "s4")
    a.label("save_ready")

    # Draw the initial frame before enabling video output.
    a.jal("draw_color")
    a.li32("t0", VI_BASE)
    a.addiu("t1", "zero", 0)
    a.sw("t1", 0x00, "t0")
    a.li32("t1", FRAMEBUFFER_PHYS)
    a.sw("t1", 0x04, "t0")
    a.addiu("t1", "zero", 320)
    a.sw("t1", 0x08, "t0")
    a.addiu("t1", "zero", 2)
    a.sw("t1", 0x0C, "t0")
    a.li32("t1", 0x000E0204)
    a.sw("t1", 0x14, "t0")
    a.addiu("t1", "zero", 0x020D)
    a.sw("t1", 0x18, "t0")
    a.addiu("t1", "zero", 0x0C15)
    a.sw("t1", 0x1C, "t0")
    a.li32("t1", 0x0C150C15)
    a.sw("t1", 0x20, "t0")
    a.li32("t1", 0x006C02EC)
    a.sw("t1", 0x24, "t0")
    a.li32("t1", 0x002501FF)
    a.sw("t1", 0x28, "t0")
    a.li32("t1", 0x000E0204)
    a.sw("t1", 0x2C, "t0")
    a.addiu("t1", "zero", 0x0200)
    a.sw("t1", 0x30, "t0")
    a.addiu("t1", "zero", 0x0400)
    a.sw("t1", 0x34, "t0")
    a.li32("t1", 0x0000320E)
    a.sw("t1", 0x00, "t0")

    # Configure direct PCM output at 32 kHz, signed 16-bit stereo.
    a.li32("t0", AI_BASE)
    a.addiu("t1", "zero", 1520)
    a.sw("t1", 0x10, "t0")
    a.addiu("t1", "zero", 15)
    a.sw("t1", 0x14, "t0")
    a.addiu("t1", "zero", 1)
    a.sw("t1", 0x08, "t0")

    a.li32("a0", AUDIO_BUFFER0)
    a.jal("fill_audio")
    a.li32("a0", AUDIO_BUFFER1)
    a.jal("fill_audio")

    # Start playback with two queued audio blocks.
    a.li32("t0", AI_BASE)
    a.li32("t1", AUDIO_BUFFER0_PHYS)
    a.sw("t1", 0x00, "t0")
    a.addiu("t1", "zero", AUDIO_BYTES)
    a.sw("t1", 0x04, "t0")
    a.li32("t1", AUDIO_BUFFER1_PHYS)
    a.sw("t1", 0x00, "t0")
    a.addiu("t1", "zero", AUDIO_BYTES)
    a.sw("t1", 0x04, "t0")

    a.label("main")
    a.jal("wait_vblank")
    a.jal("read_controller")
    a.addu("s2", "v0", "zero")
    a.nor("t0", "s1", "zero")
    a.and_("t0", "t0", "s2")
    a.addu("s1", "s2", "zero")
    a.beq("t0", "zero", "no_new_press")
    a.addiu("s0", "s0", 1)
    a.sltiu("t1", "s0", 3)
    a.bne("t1", "zero", "press_color_ready")
    a.addiu("s0", "zero", 0)
    a.label("press_color_ready")
    a.jal("draw_color")

    a.label("no_new_press")
    a.addiu("s3", "s3", 1)
    a.sltiu("t0", "s3", 60)
    a.bne("t0", "zero", "no_second_tick")
    a.addiu("s3", "zero", 0)
    a.addiu("s0", "s0", 1)
    a.sltiu("t1", "s0", 3)
    a.bne("t1", "zero", "tick_color_ready")
    a.addiu("s0", "zero", 0)
    a.label("tick_color_ready")
    a.jal("draw_color")
    a.jal("bump_save")
    a.label("no_second_tick")
    a.jal("queue_audio")
    a.j("main")

    # Wait for the start of vertical blank, once per displayed frame.
    a.label("wait_vblank")
    a.li32("t0", VI_BASE)
    a.label("wait_visible")
    a.lw("t1", 0x10, "t0")
    a.andi("t1", "t1", 0x03FF)
    a.sltiu("t1", "t1", 240)
    a.beq("t1", "zero", "wait_visible")
    a.label("wait_blank")
    a.lw("t1", 0x10, "t0")
    a.andi("t1", "t1", 0x03FF)
    a.sltiu("t1", "t1", 240)
    a.bne("t1", "zero", "wait_blank")
    a.jr("ra")

    # Read controller one through the serial interface and return its 16-bit pad.
    a.label("read_controller")
    a.li32("t0", PIF_BUFFER)
    a.addiu("t1", "zero", 16)
    a.label("pif_clear_loop")
    a.sw("zero", 0, "t0")
    a.addiu("t0", "t0", 4)
    a.addiu("t1", "t1", -1)
    a.bne("t1", "zero", "pif_clear_loop")
    a.li32("t0", PIF_BUFFER)
    a.addiu("t1", "zero", 1)
    a.sb("t1", 0, "t0")
    a.addiu("t1", "zero", 4)
    a.sb("t1", 1, "t0")
    a.addiu("t1", "zero", 1)
    a.sb("t1", 2, "t0")
    a.addiu("t1", "zero", 0xFE)
    a.sb("t1", 7, "t0")
    a.addiu("t1", "zero", 1)
    a.sb("t1", 63, "t0")

    a.li32("t4", SI_BASE)
    a.li32("t5", PIF_BUFFER_PHYS)
    a.sw("t5", 0x00, "t4")
    a.li32("t5", PIF_RAM)
    a.sw("t5", 0x10, "t4")
    a.label("si_wait_write")
    a.lw("t6", 0x18, "t4")
    a.andi("t6", "t6", 3)
    a.bne("t6", "zero", "si_wait_write")
    a.li32("t5", PIF_BUFFER_PHYS)
    a.sw("t5", 0x00, "t4")
    a.li32("t5", PIF_RAM)
    a.sw("t5", 0x04, "t4")
    a.label("si_wait_read")
    a.lw("t6", 0x18, "t4")
    a.andi("t6", "t6", 3)
    a.bne("t6", "zero", "si_wait_read")
    a.li32("t0", PIF_BUFFER)
    a.lbu("t2", 3, "t0")
    a.lbu("t3", 4, "t0")
    a.sll("t2", "t2", 8)
    a.or_("v0", "t2", "t3")
    a.jr("ra")

    # Fill a 320x240 RGBA5551 framebuffer with the selected colour.
    a.label("draw_color")
    a.beq("s0", "zero", "draw_red")
    a.addiu("t1", "zero", 1)
    a.beq("s0", "t1", "draw_green")
    a.li32("t0", BLUE)
    a.j("draw_fill")
    a.label("draw_green")
    a.li32("t0", GREEN)
    a.j("draw_fill")
    a.label("draw_red")
    a.li32("t0", RED)
    a.label("draw_fill")
    a.sll("t1", "t0", 16)
    a.or_("t0", "t0", "t1")
    a.li32("t1", FRAMEBUFFER)
    a.li32("t2", FRAMEBUFFER_WORDS)
    a.label("draw_loop")
    a.sw("t0", 0, "t1")
    a.addiu("t1", "t1", 4)
    a.addiu("t2", "t2", -1)
    a.bne("t2", "zero", "draw_loop")
    a.jr("ra")

    # Increment the little-endian 16-bit counter using byte accesses only.
    a.label("bump_save")
    a.lbu("t0", 0, "s4")
    a.lbu("t1", 1, "s4")
    a.sll("t1", "t1", 8)
    a.or_("t0", "t0", "t1")
    a.addiu("t0", "t0", 1)
    a.sb("t0", 0, "s4")
    a.srl("t1", "t0", 8)
    a.sb("t1", 1, "s4")
    a.jr("ra")

    # Queue one audio block when the hardware FIFO has a free slot.
    a.label("queue_audio")
    a.addu("t9", "ra", "zero")
    a.li32("t0", AI_BASE)
    a.lw("t1", 0x0C, "t0")
    a.bltz("t1", "audio_queue_done")
    a.beq("s5", "zero", "audio_buffer_zero")
    a.li32("a0", AUDIO_BUFFER1)
    a.jal("fill_audio")
    a.li32("t0", AI_BASE)
    a.li32("t1", AUDIO_BUFFER1_PHYS)
    a.sw("t1", 0x00, "t0")
    a.addiu("t1", "zero", AUDIO_BYTES)
    a.sw("t1", 0x04, "t0")
    a.xori("s5", "s5", 1)
    a.j("audio_queue_done")
    a.label("audio_buffer_zero")
    a.li32("a0", AUDIO_BUFFER0)
    a.jal("fill_audio")
    a.li32("t0", AI_BASE)
    a.li32("t1", AUDIO_BUFFER0_PHYS)
    a.sw("t1", 0x00, "t0")
    a.addiu("t1", "zero", AUDIO_BYTES)
    a.sw("t1", 0x04, "t0")
    a.xori("s5", "s5", 1)
    a.label("audio_queue_done")
    a.jr("t9")

    # Produce low-level LFSR noise and a 440 Hz square wave when a button is held.
    a.label("fill_audio")
    a.addu("t1", "a0", "zero")
    a.addiu("t0", "zero", AUDIO_FRAMES)
    a.label("audio_sample_loop")
    a.andi("t2", "s7", 1)
    a.srl("s7", "s7", 1)
    a.beq("t2", "zero", "audio_lfsr_ready")
    a.li32("t3", 0xB400)
    a.xor("s7", "s7", "t3")
    a.label("audio_lfsr_ready")
    a.andi("t3", "s7", 1)
    a.addiu("t4", "zero", 300)
    a.beq("t3", "zero", "audio_noise_ready")
    a.subu("t4", "zero", "t4")
    a.label("audio_noise_ready")
    a.addiu("s6", "s6", 440)
    a.sltiu("t5", "s6", AUDIO_RATE)
    a.bne("t5", "zero", "audio_phase_ready")
    a.addiu("s6", "s6", -AUDIO_RATE)
    a.label("audio_phase_ready")
    a.beq("s2", "zero", "audio_noise_only")
    a.sltiu("t5", "s6", AUDIO_RATE // 2)
    a.beq("t5", "zero", "audio_tone_negative")
    a.addiu("t6", "zero", 6000)
    a.addu("t7", "t4", "t6")
    a.j("audio_write_sample")
    a.label("audio_tone_negative")
    a.addiu("t6", "zero", 6000)
    a.subu("t7", "t4", "t6")
    a.j("audio_write_sample")
    a.label("audio_noise_only")
    a.addu("t7", "t4", "zero")
    a.label("audio_write_sample")
    a.sh("t7", 0, "t1")
    a.sh("t7", 2, "t1")
    a.addiu("t1", "t1", 4)
    a.addiu("t0", "t0", -1)
    a.bne("t0", "zero", "audio_sample_loop")
    a.jr("ra")

    return a.finish()


def cic6102_checksum(data: bytes, seed: int = CRC_SEED_6102) -> tuple[int, int]:
    """Calculate the legacy checksum pair for a 6102-compatible boot block."""
    if len(data) < CRC_REGION_OFFSET + CRC_REGION_SIZE:
        raise ValueError("image is too small for the checksum region")
    t1 = t2 = t3 = t4 = t5 = t6 = seed
    for offset in range(CRC_REGION_OFFSET, CRC_REGION_OFFSET + CRC_REGION_SIZE, 4):
        value = int.from_bytes(data[offset:offset + 4], "big")
        total = (t6 + value) & MASK32
        if total < value:
            t4 = (t4 + 1) & MASK32
        t6 = total
        if value < t1:
            t3 ^= t6 ^ value
        else:
            t3 ^= t4 ^ value
        shift = value & 31
        rotated = value if shift == 0 else ((value << shift) | (value >> (32 - shift))) & MASK32
        t5 = (t5 + rotated) & MASK32
        if t2 > value:
            t2 ^= rotated
        else:
            t2 ^= t6 ^ value
    return (t6 ^ t4 ^ t3) & MASK32, (t5 ^ t2 ^ t1) & MASK32


def build() -> bytes:
    ipl3 = build_ipl3()
    program = build_program()
    if len(program) > CRC_REGION_SIZE:
        raise ValueError("program does not fit in the boot-loaded region")

    rom = bytearray(ROM_SIZE)
    rom[0:4] = bytes.fromhex("80371240")
    rom[4:8] = (0x0000000F).to_bytes(4, "big")
    rom[8:12] = PROGRAM_RAM_ORIGIN.to_bytes(4, "big")
    rom[12:16] = bytes(4)
    rom[0x20:0x34] = b"MUPEN64PLUS-NEXT".ljust(20, b"\x00")
    rom[0x38:0x3C] = b"OPEN"
    rom[0x3C:0x3E] = b"\x44\x45"
    rom[0x3E] = ord("E")
    rom[0x3F] = 0x30
    rom[IPL3_OFFSET:IPL3_OFFSET + IPL3_SIZE] = ipl3
    rom[PROGRAM_ROM_OFFSET:PROGRAM_ROM_OFFSET + len(program)] = program

    crc1, crc2 = cic6102_checksum(rom)
    rom[0x10:0x14] = crc1.to_bytes(4, "big")
    rom[0x14:0x18] = crc2.to_bytes(4, "big")
    return bytes(rom)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the mupen64plus-next sample ROM.")
    parser.add_argument("output", nargs="?", type=Path, default=Path("sample.z64"), help="output image path")
    args = parser.parse_args(argv)

    try:
        rom = build()
        args.output.write_bytes(rom)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"wrote {args.output} ({len(rom)} bytes)")
    print(f"bootstrap size: {len(build_ipl3())} bytes")
    print(f"program size: {len(build_program())} bytes")
    print(f"checksum: {int.from_bytes(rom[0x10:0x14], 'big'):08X} "
          f"{int.from_bytes(rom[0x14:0x18], 'big'):08X}")
    print("target: emulator boot emulation; physical hardware is not supported")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
