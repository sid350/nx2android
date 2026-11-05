"""The save.dmsync format, written here on its own: the hashes must stay the ones nxnative's save_sync.c gives. And the Android clonefile keeps what a real clone keeps."""
import hashlib
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
T = 1767315845
BLOB = bytes(range(256)) * 300
BASIC = [('save/Zed.txt', b'B'), ('save/alpha.txt', b'a'), ('save/empty.dat', b''), ('save/emptydir', None), ('save/slot0.bin', b'hello save\n'), ('save/sub', None), ('save/sub/deep', None),
         ('save/sub/deep/blob.bin', BLOB), ('save_device/cfg', None), ('save_device/cfg/common.sav', b'device\n')]
COLLISION = [('save/.dm-case-1', b'UPPER\n', b'FILE.BIN'), ('save/file.bin', b'lower\n', b'')]


def pack(ents):
    sha, body = hashlib.sha256(), b''
    for e in ents:
        path, data = e[0].encode(), e[1]
        guest = e[2] if len(e) > 2 else b''
        kind, size = (1, 0) if data is None else (2, len(data))
        h = struct.pack('<BBHHHQq', kind, 0, len(path), len(guest), 0, size, T)
        sha.update(h[:16]); sha.update(path); sha.update(guest); sha.update(data or b'')
        body += h + path + guest + (data or b'')
    digest = sha.hexdigest()[:32].encode()
    hdr = b'DMSYNC1\n' + struct.pack('<II', 1, len(ents)) + digest + struct.pack('<q', T)
    hdr += b'0100000000DM5A9C'.ljust(24, b'\0') + b'Mac16,5'.ljust(64, b'\0')
    return digest.decode(), hdr + body + b'DMSYNCE\n'


class Format(unittest.TestCase):
    def test_same_files_as_a_mac_packs(self):
        for ents, digest, sha in ((BASIC, 'dab62fd220016767cb308082a5cdbbf5', '3a3ec7b976b05b245feedcb167bfc431e8e50940212be20c2b796026bee13028'),
                                  (COLLISION, '801ffd4607c81b656dc40afa49f8e460', 'dee9f746b4ea49b930f18cfeef9b6bbfd157d99c641bdaeccf778255c5c37472')):
            d, blob = pack(ents)
            self.assertEqual(d, digest)
            self.assertEqual(hashlib.sha256(blob).hexdigest(), sha)


@unittest.skipUnless(sys.platform == 'linux' and shutil.which('cc'), 'Linux and a host C compiler required')
class CloneKeeps(unittest.TestCase):
    def test_dates_and_user_xattrs(self):
        source = (ROOT / 'nxnative/dm/android/compat/darwin_compat.c').read_text()
        part = source[source.index('// ---------------- clonefile'):source.index('int renamex_np')]
        code = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/xattr.h>
#include <time.h>
#include <unistd.h>
#define CLONE_NOFOLLOW 1
''' + part + r'''
int main(int c, char **v) { char a[4096], b[4096], x[16] = {0}; struct stat s; struct timespec t[2] = {{1000, 0}, {2000, 0}};
    mkdir(v[1], 0700); snprintf(a, sizeof a, "%s/f", v[1]); fclose(fopen(a, "w"));
    if (setxattr(a, "user.guest", "NAME", 4, 0)) return 77;
    assert(!utimensat(AT_FDCWD, a, t, 0)); assert(!utimensat(AT_FDCWD, v[1], t, 0));
    assert(!clonefile(v[1], v[2], 0)); snprintf(b, sizeof b, "%s/f", v[2]);
    assert(getxattr(b, "user.guest", x, sizeof x) == 4 && !strcmp(x, "NAME"));
    assert(!stat(b, &s) && s.st_mtime == 2000); assert(!stat(v[2], &s) && s.st_mtime == 2000);
    return 0; }
'''
        with tempfile.TemporaryDirectory() as d:
            src, exe = Path(d) / 't.c', Path(d) / 't'
            src.write_text(code)
            subprocess.run(['cc', '-o', exe, src], check=True)
            r = subprocess.run([exe, Path(d) / 'a', Path(d) / 'b']).returncode
            if r == 77: self.skipTest('no user xattrs on this file system')
            self.assertEqual(r, 0)


if __name__ == '__main__':
    unittest.main()
