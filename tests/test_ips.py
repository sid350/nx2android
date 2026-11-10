"""porter/ips.py: Atmosphere ExeFS patches as the rewriter's cuts"""
import struct
import tempfile
import unittest
from pathlib import Path

from porter import ips

BID = 'd4b150b29a931cd381e2a30318fc299f'


def ips_file(records, wide=False):
    d = bytearray(b'IPS32' if wide else b'PATCH')
    for off, data, rle in records:
        d += struct.pack('>I', off) if wide else off.to_bytes(3, 'big')
        d += struct.pack('>H', 0) + struct.pack('>H', len(data)) + data[:1] if rle else struct.pack('>H', len(data)) + data
    d += b'EEOF' if wide else b'EOF'
    return bytes(d)


class IpsTests(unittest.TestCase):
    def write(self, name, data):
        p = Path(self.tmp.name) / name; p.write_bytes(data); return p

    def setUp(self): self.tmp = tempfile.TemporaryDirectory()
    def tearDown(self): self.tmp.cleanup()

    def test_records_offsets_count_from_the_nso_header(self):
        img = bytes(range(256)) * 4
        p = self.write(BID[:16].upper() + '0' * 16 + '.ips', ips_file([(0x110, b'\xaa\xbb', False), (0x200, b'\xcc\xcc\xcc', True)]))
        rows = ips.cuts(p, img, BID)
        self.assertEqual([(o, a, b) for o, a, b, _ in rows], [(0x10, img[0x10:0x12].hex(), 'aabb'), (0x100, img[0x100:0x103].hex(), 'cccccc')])

    def test_ips32(self):
        img = bytes(0x2000)
        p = self.write(BID + '.ips', ips_file([(0x1100, b'\x01\x02', False)], wide=True))
        self.assertEqual(ips.cuts(p, img, BID)[0][:3], (0x1000, '0000', '0102'))

    def test_other_build_and_outside_main_are_refused(self):
        p = self.write('0123456789abcdef.ips', ips_file([(0x110, b'\x00', False)]))
        with self.assertRaises(ips.Refused): ips.cuts(p, bytes(0x100), BID)
        p = self.write(BID + '.ips', ips_file([(0x1000, b'\x00', False)]))
        with self.assertRaises(ips.Refused): ips.cuts(p, bytes(0x100), BID)

    def test_patches_file_is_loadable(self):
        src = ips.patches_py(BID, [(0x10, '00', '01', 'r')], ['x.ips'])
        ns = {}; exec(src, ns)
        self.assertEqual(ns['CUTS'][BID], [(0x10, '00', '01', 'r')])


if __name__ == '__main__':
    unittest.main()
