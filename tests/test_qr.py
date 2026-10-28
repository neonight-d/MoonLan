"""The page's QR codes (v0.8.4), read back by a reader written here
from ISO/IEC 18004: the format information and its BCH code, the
function patterns, the zigzag, the blocks and their Reed–Solomon
syndromes, the byte segment. The encoder runs through `node` (skipped
without it); in the browser the same codes were read by jsQR."""

import json
import re
import subprocess
import unittest

from web_source import APP, NODE, function

# error correction codewords per block and blocks, level M (ISO/IEC
# 18004 table 9), versions 1–20
LEVEL_M = {
    1: (10, 1), 2: (16, 1), 3: (26, 1), 4: (18, 2), 5: (24, 2), 6: (16, 4),
    7: (18, 4), 8: (22, 4), 9: (22, 5), 10: (26, 5), 11: (30, 5), 12: (22, 8),
    13: (22, 9), 14: (24, 9), 15: (24, 10), 16: (28, 10), 17: (28, 11),
    18: (26, 13), 19: (26, 14), 20: (26, 16),
}
# all the codewords of a symbol (table 1)
TOTAL = {1: 26, 2: 44, 3: 70, 4: 100, 5: 134, 6: 172, 7: 196, 8: 242, 9: 292,
         10: 346, 11: 404, 12: 466, 13: 532, 14: 581, 15: 655, 16: 733,
         17: 815, 18: 901, 19: 991, 20: 1085}
# alignment pattern centres (annex E)
ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
         7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
         11: [6, 30, 54], 12: [6, 32, 58], 13: [6, 34, 62], 14: [6, 26, 46, 66],
         15: [6, 26, 48, 70], 16: [6, 26, 50, 74], 17: [6, 30, 54, 78],
         18: [6, 30, 56, 82], 19: [6, 30, 58, 86], 20: [6, 34, 62, 90]}
MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def gf_tables():
    exp, log = [0] * 512, [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


EXP, LOG = gf_tables()


def syndromes(block, count):
    """S_i = block(α^i), i < count; all zero for a codeword without an
    error"""
    out = []
    for i in range(count):
        s = 0
        for c in block:
            s = (EXP[(LOG[s] + i) % 255] if s else 0) ^ c
        out.append(s)
    return out


def bch_ok(value, bits, poly):
    """value is data followed by its BCH remainder"""
    rem = value >> bits
    for _ in range(bits):
        rem = (rem << 1) ^ ((rem >> (bits - 1)) * poly)
    return ((value >> bits) << bits | (rem & ((1 << bits) - 1))) == value


def read(m):
    """A matrix of 0/1 rows -> (level bits, mask, the decoded text)"""
    size = len(m)
    ver = (size - 17) // 4
    # format, first copy: column 8 down, then row 8 leftwards
    spots = [(r, 8) for r in range(6)] + [(7, 8), (8, 8), (8, 7)] + \
        [(8, c) for c in (5, 4, 3, 2, 1, 0)]
    word = sum(m[r][c] << i for i, (r, c) in enumerate(spots)) ^ 0x5412
    assert bch_ok(word, 10, 0x537), "format BCH"
    second = [(8, size - 1 - i) for i in range(8)] + \
        [(size - 15 + i, 8) for i in range(8, 15)]
    assert sum(m[r][c] << i for i, (r, c) in enumerate(second)) ^ 0x5412 == word
    level, mask = word >> 13, (word >> 10) & 7
    assert m[size - 8][8] == 1, "dark module"
    if ver >= 7:
        v = sum(m[i // 3][size - 11 + i % 3] << i for i in range(18))
        assert bch_ok(v, 12, 0x1F25) and v >> 12 == ver, "version"
    # what is not data
    fixed = [[False] * size for _ in range(size)]
    for r0, c0 in ((0, 0), (0, size - 8), (size - 8, 0)):
        for r in range(r0, r0 + 8):
            for c in range(c0, c0 + 8):
                fixed[r][c] = True
    for i in range(size):
        fixed[6][i] = fixed[i][6] = True
    for i in range(9):
        fixed[8][i] = fixed[i][8] = True
    for i in range(8):
        fixed[8][size - 1 - i] = fixed[size - 1 - i][8] = True
    corners = {(6, 6), (6, size - 7), (size - 7, 6)}
    for r in ALIGN[ver]:
        for c in ALIGN[ver]:
            if (r, c) in corners:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    fixed[r + dr][c + dc] = True
    if ver >= 7:
        for i in range(6):
            for j in range(size - 11, size - 8):
                fixed[i][j] = fixed[j][i] = True
    bits = []
    col, upward = size - 1, True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for c in (col, col - 1):
                if not fixed[r][c]:
                    bits.append(m[r][c] ^ MASKS[mask](r, c))
        col -= 2
        upward = not upward
    codewords = [int("".join(map(str, bits[i:i + 8])), 2)
                 for i in range(0, TOTAL[ver] * 8, 8)]
    ec, blocks = LEVEL_M[ver]
    short = blocks - TOTAL[ver] % blocks
    data_len = TOTAL[ver] // blocks - ec
    lengths = [data_len + (0 if b < short else 1) for b in range(blocks)]
    data = [[] for _ in range(blocks)]
    at = 0
    for i in range(max(lengths)):
        for b in range(blocks):
            if i < lengths[b]:
                data[b].append(codewords[at])
                at += 1
    eccs = [[] for _ in range(blocks)]
    for i in range(ec):
        for b in range(blocks):
            eccs[b].append(codewords[at])
            at += 1
    for b in range(blocks):
        assert syndromes(data[b] + eccs[b], ec) == [0] * ec, f"RS block {b}"
    stream = "".join(f"{x:08b}" for block in data for x in block)
    assert stream[:4] == "0100", "byte mode"
    width = 8 if ver < 10 else 16
    count = int(stream[4:4 + width], 2)
    payload = stream[4 + width:4 + width + count * 8]
    text = bytes(int(payload[i:i + 8], 2) for i in range(0, len(payload), 8)).decode("utf-8")
    return level, mask, text


TEXTS = [
    "https://t.me/moonlan_test_bot?start=Ab3_dE5-fG7hI9jK1lM2nO4p",
    "otpauth://totp/MoonLan:anna?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP&issuer=MoonLan",
    "Ж",
    "Карта сети MoonLan",
    "x" * 14, "x" * 15, "y" * 180, "y" * 181, "z" * 213, "z" * 214, "w" * 400,
]


@unittest.skipUnless(NODE, "needs node to run the page's JavaScript")
class QrTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tables = re.search(r"^const QR_EC_PER_BLOCK = .*?^const QR_BLOCKS = .*?;$",
                           APP, re.S | re.M).group(0)
        code = "\n".join([tables] + [function(n) for n in (
            "qrDataModules", "qrMul", "qrEcc", "qrMatrix", "qrPenalty")])
        code += ("\nconsole.log(JSON.stringify(%s.map((t) => "
                 "qrMatrix(t).map((row) => row.map(Number)))));" % json.dumps(TEXTS))
        done = subprocess.run([NODE, "-e", code], capture_output=True, text=True,
                              timeout=60, check=True)
        cls.matrices = json.loads(done.stdout)

    def test_read_back(self):
        for text, matrix in zip(TEXTS, self.matrices):
            with self.subTest(text=text[:30], length=len(text)):
                level, _, decoded = read(matrix)
                self.assertEqual(level, 0)  # M
                self.assertEqual(decoded, text)

    def test_the_smallest_version(self):
        versions = [(len(m) - 17) // 4 for m in self.matrices]
        # level M byte capacity: v1 14, v9 180, v10 213
        self.assertEqual(versions[4:10], [1, 2, 9, 10, 10, 11])

    def test_no_library_and_no_service(self):
        self.assertNotIn("QRious", APP)
        self.assertNotIn("cdnjs", APP)
        self.assertIn("qrCanvas(secret.uri, 200)", APP)
        self.assertIn("qrCanvas(myLink.link, 180)", APP)


if __name__ == "__main__":
    unittest.main()
