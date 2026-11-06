# A profile's SPIR-V store corrections (nxnative/dm/android/tools/spv_patch.py): the one matching store changes, and a module that does not match stops the build.
import struct, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'nxnative/dm/android/tools'))
import spv_patch   # noqa: E402


def module(*instructions):
    words = [0x07230203, 0x10000, 0, 100, 0]
    for op, *args in instructions: words += [(len(args) + 1) << 16 | op, *args]
    return struct.pack(f'<{len(words)}I', *words)


TRUE = (spv_patch.OP_CONSTANT_TRUE, 1, 9)
STORE = (spv_patch.OP_STORE, 5, 7)


class ReplaceStore(unittest.TestCase):
    def test_the_store_takes_the_constant(self):
        self.assertEqual(spv_patch.replace_store(module(TRUE, STORE), 5, 7, 9), module(TRUE, (spv_patch.OP_STORE, 5, 9)))

    def test_other_stores_stay(self):
        other = (spv_patch.OP_STORE, 6, 7)
        self.assertEqual(spv_patch.replace_store(module(TRUE, other, STORE), 5, 7, 9), module(TRUE, other, (spv_patch.OP_STORE, 5, 9)))

    def test_no_match_refused(self):
        with self.assertRaisesRegex(ValueError, 'found 0'): spv_patch.replace_store(module(TRUE, (spv_patch.OP_STORE, 5, 8)), 5, 7, 9)

    def test_two_matches_refused(self):
        with self.assertRaisesRegex(ValueError, 'found 2'): spv_patch.replace_store(module(TRUE, STORE, STORE), 5, 7, 9)

    def test_new_value_must_be_a_constant(self):
        with self.assertRaisesRegex(ValueError, 'not a constant'): spv_patch.replace_store(module(STORE), 5, 7, 9)

    def test_broken_module_refused(self):
        with self.assertRaisesRegex(ValueError, 'invalid SPIR-V'): spv_patch.replace_store(module(TRUE) + struct.pack('<I', 9 << 16 | spv_patch.OP_STORE), 5, 7, 9)


class Table(unittest.TestCase):
    def test_rows(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 't.tsv'; p.write_text('# comment\nabc\t183\t681\t650\nabc\t1\t2\t3\n', encoding='utf-8')
            self.assertEqual(spv_patch.read_table(p), {'abc': [(183, 681, 650), (1, 2, 3)]})


if __name__ == '__main__': unittest.main()
