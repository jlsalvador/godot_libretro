#!/usr/bin/env python3
"""ffmpeg sample video: a 6-second clip, written from scratch in pure Python.

No SDK, no external library and no external tool: only the standard `struct`
and `sys` modules are used.

Content:

  * video: 320x240, 30 pictures/s, one solid colour per second: red, green,
    blue, twice over
  * audio: white noise all along, 8 kHz, mono, 8-bit

Formats (all open, royalty-free and free of patents):

  * container: Matroska (open specification, RFC 9559), one cluster
  * video codec: baseline JPEG, one intra-coded picture per frame ("V_MJPEG").
    Baseline JPEG patents have expired and the format is an open standard
  * audio codec: raw PCM ("A_PCM/INT/LIT")

How it stays small:

  * each picture is a flat colour: after the transform only the DC value of
    each 8x8 block is non-zero, so the encoder emits one DC symbol and an
    end-of-block marker per block. The Huffman tables are custom and minimal
    (valid prefix codes; the DC table only holds the categories this clip
    needs) and the quantisation table is all ones, which keeps the DC value
    exact

Run: python ffmpeg.py [output.mkv]
"""

import struct
import sys

WIDTH, HEIGHT = 320, 240
VIDEO_FPS = 30
COLORS = ((255, 0, 0), (0, 255, 0), (0, 0, 255))     # red, green, blue
SECONDS = 6
AUDIO_RATE = 8000
AUDIO_CHUNK_MS = 100                                 # one audio block every 100 ms
NOISE_AMPLITUDE = 48                                 # around the 8-bit silence level 128


# ---------------------------------------------------------------------------
# Baseline JPEG encoder for flat-colour pictures
# ---------------------------------------------------------------------------
# Custom Huffman tables. DC: five symbols (categories 0, 8, 9, 10, 11) with
# 3-bit codes 000..100; the all-ones code 111 is never used, as required.
# AC: a single symbol, the end-of-block marker (0x00), with the 1-bit code 0.
DC_SYMBOLS = (0, 8, 9, 10, 11)
DC_CODES = {sym: (i, 3) for i, sym in enumerate(DC_SYMBOLS)}      # symbol -> (code, bit length)
AC_EOB_CODE = (0, 1)


def _ycbcr(rgb):
    """Full-range YCbCr (as used by JPEG), rounded and clamped to 0..255."""
    r, g, b = rgb
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = -0.168736 * r - 0.331264 * g + 0.5 * b + 128
    cr = 0.5 * r - 0.418688 * g - 0.081312 * b + 128
    return tuple(max(0, min(255, int(round(v)))) for v in (y, cb, cr))


class _Bits:
    """MSB-first bit writer with the 0xFF byte stuffing JPEG requires."""

    def __init__(self):
        self.out = bytearray()
        self.acc = 0
        self.n = 0

    def put(self, value, length):
        self.acc = (self.acc << length) | (value & ((1 << length) - 1))
        self.n += length
        while self.n >= 8:
            self.n -= 8
            byte = (self.acc >> self.n) & 0xFF
            self.out.append(byte)
            if byte == 0xFF:
                self.out.append(0x00)
        self.acc &= (1 << self.n) - 1

    def flush(self):
        if self.n:
            self.put((1 << (8 - self.n)) - 1, 8 - self.n)      # pad with 1 bits
        return bytes(self.out)


def encode_flat_jpeg(width, height, rgb):
    """One baseline JPEG, 4:2:0, entirely filled with a single colour."""
    assert width % 16 == 0 and height % 16 == 0
    planes = _ycbcr(rgb)                                       # Y, Cb, Cr
    dc_values = [8 * (v - 128) for v in planes]                # DC with a quantiser of 1

    bits = _Bits()
    pred = [0, 0, 0]
    for _ in range((width // 16) * (height // 16)):            # one MCU = 4 Y + Cb + Cr
        for comp, blocks in ((0, 4), (1, 1), (2, 1)):
            for _ in range(blocks):
                diff = dc_values[comp] - pred[comp]
                pred[comp] = dc_values[comp]
                category = abs(diff).bit_length()
                assert category in DC_CODES, f"DC category {category} is not in the table"
                code, length = DC_CODES[category]
                bits.put(code, length)
                if category:
                    bits.put(diff if diff > 0 else diff + (1 << category) - 1, category)
                bits.put(*AC_EOB_CODE)                         # no AC coefficients
    scan = bits.flush()

    soi = b"\xFF\xD8"
    app0 = (b"\xFF\xE0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01"
            + b"\x00" + struct.pack(">HH", 1, 1) + b"\x00\x00")
    dqt = b"\xFF\xDB" + struct.pack(">H", 67) + b"\x00" + bytes([1] * 64)
    sof = (b"\xFF\xC0" + struct.pack(">HBHHB", 17, 8, height, width, 3)
           + bytes([1, 0x22, 0, 2, 0x11, 0, 3, 0x11, 0]))
    dc_counts = bytes([0, 0, len(DC_SYMBOLS)] + [0] * 13)
    dht_dc = (b"\xFF\xC4" + struct.pack(">H", 2 + 1 + 16 + len(DC_SYMBOLS)) + b"\x00"
              + dc_counts + bytes(DC_SYMBOLS))
    ac_counts = bytes([1] + [0] * 15)
    dht_ac = b"\xFF\xC4" + struct.pack(">H", 2 + 1 + 16 + 1) + b"\x10" + ac_counts + b"\x00"
    sos = (b"\xFF\xDA" + struct.pack(">HB", 12, 3) + bytes([1, 0x00, 2, 0x00, 3, 0x00])
           + bytes([0, 63, 0]))
    return soi + app0 + dqt + sof + dht_dc + dht_ac + sos + scan + b"\xFF\xD9"


# ---------------------------------------------------------------------------
# White noise (xorshift32, deterministic)
# ---------------------------------------------------------------------------
def noise_samples(count):
    x = 0x2545F491
    out = bytearray()
    for _ in range(count):
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        out.append(128 + (((x >> 24) - 128) * NOISE_AMPLITUDE) // 128)
    return bytes(out)


# ---------------------------------------------------------------------------
# Matroska (EBML) writer
# ---------------------------------------------------------------------------
def _size(n):
    """EBML data size: the shortest vint that holds n."""
    for length in range(1, 9):
        if n < (1 << (7 * length)) - 1:
            return (n | (1 << (7 * length))).to_bytes(length, "big")
    raise ValueError("element too large")


def _el(element_id, payload):
    return element_id + _size(len(payload)) + payload


def _uint(n):
    return n.to_bytes(max(1, (n.bit_length() + 7) // 8), "big")


def _float(x):
    return struct.pack(">d", x)


def _text(s):
    return s.encode("ascii")


def _simple_block(track, rel_ms, data):
    # track number as a 1-byte vint, signed 16-bit relative timestamp, flags (keyframe)
    return _el(b"\xA3", bytes([0x80 | track]) + struct.pack(">h", rel_ms) + b"\x80" + data)


def build() -> bytes:
    duration_ms = SECONDS * 1000

    ebml_header = _el(b"\x1A\x45\xDF\xA3",
                      _el(b"\x42\x86", _uint(1))                 # EBMLVersion
                      + _el(b"\x42\xF7", _uint(1))               # EBMLReadVersion
                      + _el(b"\x42\xF2", _uint(4))               # EBMLMaxIDLength
                      + _el(b"\x42\xF3", _uint(8))               # EBMLMaxSizeLength
                      + _el(b"\x42\x82", _text("matroska"))      # DocType
                      + _el(b"\x42\x87", _uint(4))               # DocTypeVersion
                      + _el(b"\x42\x85", _uint(2)))              # DocTypeReadVersion

    info = _el(b"\x15\x49\xA9\x66",
               _el(b"\x2A\xD7\xB1", _uint(1000000))              # timestamps in milliseconds
               + _el(b"\x4D\x80", _text("pure-python sample muxer"))
               + _el(b"\x57\x41", _text("ffmpeg.py"))
               + _el(b"\x44\x89", _float(float(duration_ms))))

    video_track = _el(b"\xAE",
                      _el(b"\xD7", _uint(1))                     # track number
                      + _el(b"\x73\xC5", _uint(1))               # track UID
                      + _el(b"\x83", _uint(1))                   # type: video
                      + _el(b"\x9C", _uint(0))                   # no lacing
                      + _el(b"\x86", _text("V_MJPEG"))
                      + _el(b"\x23\xE3\x83", _uint(1000000000 // VIDEO_FPS))
                      + _el(b"\xE0", _el(b"\xB0", _uint(WIDTH)) + _el(b"\xBA", _uint(HEIGHT))))
    audio_track = _el(b"\xAE",
                      _el(b"\xD7", _uint(2))
                      + _el(b"\x73\xC5", _uint(2))
                      + _el(b"\x83", _uint(2))                   # type: audio
                      + _el(b"\x9C", _uint(0))
                      + _el(b"\x86", _text("A_PCM/INT/LIT"))
                      + _el(b"\xE1", _el(b"\xB5", _float(float(AUDIO_RATE)))   # sampling frequency
                            + _el(b"\x9F", _uint(1))                           # channels
                            + _el(b"\x62\x64", _uint(8))))                     # bit depth
    tracks = _el(b"\x16\x54\xAE\x6B", video_track + audio_track)

    # blocks, in timestamp order (video first when both fall on the same instant)
    events = []
    for i in range(SECONDS * VIDEO_FPS):
        color = COLORS[(i // VIDEO_FPS) % len(COLORS)]
        events.append((i * 1000 // VIDEO_FPS, 1, encode_flat_jpeg(WIDTH, HEIGHT, color)))
    chunk_samples = AUDIO_RATE * AUDIO_CHUNK_MS // 1000
    noise = noise_samples(AUDIO_RATE * duration_ms // 1000)
    for t in range(0, duration_ms, AUDIO_CHUNK_MS):
        first = AUDIO_RATE * t // 1000
        events.append((t, 2, noise[first:first + chunk_samples]))
    events.sort(key=lambda e: (e[0], e[1]))

    cluster = _el(b"\x1F\x43\xB6\x75",
                  _el(b"\xE7", _uint(0))                         # cluster timestamp
                  + b"".join(_simple_block(track, t, data) for t, track, data in events))

    segment = _el(b"\x18\x53\x80\x67", info + tracks + cluster)
    return ebml_header + segment


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "ffmpeg_sample.mkv"
    data = build()
    with open(out, "wb") as f:
        f.write(data)
    print(f"wrote {out} ({len(data)} bytes)")
