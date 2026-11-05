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


@unittest.skipUnless(sys.platform == 'linux' and shutil.which('cc'), 'Linux and a host C compiler required')
class OldBackslashNames(unittest.TestCase):
    """A save another host wrote before backslashes separated paths (seedds/nxnative 7c23cc0, abd7014): save:\\savedata0.bin was the file "\\savedata0.bin". hle_fs.c's mount renames it
    to the name the game asks for, and a backslash path reaches the same file a slash path does, so a save from an iPhone or a Mac and one from Android are the same files."""
    def test_mount_renames_and_backslash_paths_resolve(self):
        fs = (ROOT / 'nxnative/dm/host/hle_fs.c').read_text()
        compat = (ROOT / 'nxnative/dm/android/compat/darwin_compat.c').read_text()
        part = fs[fs.index('typedef struct { char name[0x301]'):fs.index('static Result err_res(')]
        clone = compat[compat.index('// ---------------- clonefile'):compat.index('int renamex_np')]
        code = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/clonefile.h>
#include <sys/xattr.h>
#include <time.h>
#include <unistd.h>
typedef uint32_t Result;
#define RES_OK 0
#define RES_FAIL 1
#define LOG(...) (fprintf(stderr, __VA_ARGS__), fputc('\n', stderr))
static void die(const char *f, ...) { fprintf(stderr, "die: %s\n", f); exit(70); }
#define RES_PATH_NOT_FOUND 0x202u
#define MAX_MOUNT 8
#define CLONE_NOFOLLOW 1
''' + clone + r'''
static int rm_tree(const char *p) { char c[4200]; snprintf(c, sizeof c, "rm -rf '%s'", p); return system(c); }
static void put(const char *p, const char *s) { FILE *f = fopen(p, "w"); assert(f); fputs(s, f); fclose(f); }
static int has(const char *p, const char *s) { char b[64] = {0}; FILE *f = fopen(p, "r"); if (!f) return 0; size_t k = fread(b, 1, sizeof b - 1, f); fclose(f); b[k] = 0; return !strcmp(b, s); }
''' + part + r'''
int main(int c, char **v) {
    char d[2048], p[2048], hp[1024]; int ro;
    snprintf(d, sizeof d, "%s/save", v[1]); mkdir(d, 0755);
    snprintf(p, sizeof p, "%s/\\savedata0.bin", d); put(p, "old");           // Link's Awakening's save from a host before 7c23cc0
    snprintf(p, sizeof p, "%s/\\dir\\f", d); put(p, "nested");
    snprintf(p, sizeof p, "%s/\\taken", d); put(p, "stale");               // its new name is taken: it stays
    snprintf(p, sizeof p, "%s/taken", d); put(p, "new");
    snprintf(p, sizeof p, "%s/\\..\\up", d); put(p, "up");                  // would leave its folder: it stays
    if (setxattr(d, "user.probe", "x", 1, 0, 0)) return 77;
    if (mount_ex("save", d, 0, 1) != RES_OK) return 1;
    snprintf(p, sizeof p, "%s/savedata0.bin", d); if (!has(p, "old")) return 2;
    snprintf(p, sizeof p, "%s/dir/f", d); if (!has(p, "nested")) return 3;
    snprintf(p, sizeof p, "%s/\\taken", d); if (!has(p, "stale")) return 4;
    snprintf(p, sizeof p, "%s/taken", d); if (!has(p, "new")) return 5;
    snprintf(p, sizeof p, "%s/\\..\\up", d); if (!has(p, "up")) return 6;
    if (!host_path("save:\\savedata0.bin", hp, sizeof hp, &ro) || !has(hp, "old")) return 7;   // backslash and slash paths reach the same file
    if (!host_path("save:/savedata0.bin", hp, sizeof hp, &ro) || !has(hp, "old")) return 8;
    if (!host_path("save:\\dir\\f", hp, sizeof hp, &ro) || !has(hp, "nested")) return 9;
    if (!host_path_ex("save:\\dir\\g", hp, sizeof hp, &ro, 1)) return 10;   // a file the game makes with a backslash path lands in dir/
    put(hp, "made"); snprintf(p, sizeof p, "%s.work/dir/g", d); if (!has(p, "made")) return 11;
    return 0; }
'''
        with tempfile.TemporaryDirectory() as d:
            src, exe = Path(d) / 't.c', Path(d) / 't'
            src.write_text(code)
            subprocess.run(['cc', '-w', '-I', ROOT / 'nxnative/dm/android/compat/include', '-o', exe, src], check=True)
            r = subprocess.run([exe, d], capture_output=True, text=True)
            if r.returncode == 77: self.skipTest('no user xattrs on this file system')
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == '__main__':
    unittest.main()
