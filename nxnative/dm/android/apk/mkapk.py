#!/usr/bin/env python3
# mkapk.py: build and sign a code-less NativeActivity APK with the Python standard library only (no JDK, no Android SDK).
#   binary AndroidManifest.xml (AXML) -> stored ZIP with the .so page-aligned -> APK Signature Scheme v2 (RSA-2048, SHA-256) with a self-signed cert.
# Usage: mkapk.py <lib.so> <out.apk> [key.json] [--package P] [--label L] [--lib NAME] [--landscape] [--icon icon.png] [--release] [--version-code N] [--version-name V] [--pack DIR]
#   (the key is created on first use and reused, so reinstalls keep the same signer). The entries are copied and hashed in pieces, so --pack takes a game of gigabytes in little memory.
import hashlib, io, json, os, secrets, struct, sys, time, zlib

import argparse as _ap
_p = _ap.ArgumentParser(); _p.add_argument('so'); _p.add_argument('out'); _p.add_argument('key', nargs='?'); _p.add_argument('--package', default='org.nx2a.probe')
_p.add_argument('--label', default='NX2A Probe'); _p.add_argument('--lib', default='probe'); _p.add_argument('--landscape', action='store_true')
_p.add_argument('--extra-so', action='append', default=[], help='another arm64 library packed beside the main one (a Vulkan validation layer)')
_p.add_argument('--icon', help='launcher icon (a PNG, used at its own size: nodpi)')
_p.add_argument('--asset', action='append', default=[], metavar='NAME=FILE', help='a file the app reads with AAssetManager as NAME (assets/NAME in the APK): the gamepad theme')
_p.add_argument('--pack', metavar='DIR', help='a --single-apk port: every file under DIR (a data pack) as assets/nx2a/<path>, stored, its data 16 KB aligned, read in place by the app')
_p.add_argument('--release', action='store_true', help='not debuggable')
_p.add_argument('--version-code', type=int, default=1, help='versionCode: a later port of the game must have a higher one to install over it')
_p.add_argument('--version-name', default='1.0', help='versionName: the version the player sees (the game\'s display version)'); ARGS = _p.parse_args()
PKG, LIB, LABEL = ARGS.package, ARGS.lib, ARGS.label
MIN_SDK, TARGET_SDK = 29, 36
ANDROID_NS = 'http://schemas.android.com/apk/res/android'

# ---------------- binary XML ----------------
ATTR_IDS = {  # android: attribute resource IDs (frameworks/base/core/res/res/values/public.xml)
    'label': 0x01010001, 'icon': 0x01010002, 'name': 0x01010003, 'hasCode': 0x0101000c, 'debuggable': 0x0101000f, 'exported': 0x01010010,
    'configChanges': 0x0101001f, 'screenOrientation': 0x0101001e, 'value': 0x01010024, 'minSdkVersion': 0x0101020c, 'versionCode': 0x0101021b,
    'versionName': 0x0101021c, 'targetSdkVersion': 0x01010270, 'isGame': 0x010103f4, 'extractNativeLibs': 0x010104ea, 'appCategory': 0x01010545,
    'enableOnBackInvokedCallback': 0x0101066c}
T_REF, T_STRING, T_INT_DEC, T_INT_HEX, T_BOOL = 0x01, 0x03, 0x10, 0x11, 0x12
ICON_ID = 0x7f010000   # drawable/icon in resources.arsc (res_table)

# (tag, [(ns_android?, name, value)], children)
MANIFEST = ('manifest', [(False, 'package', PKG), (True, 'versionCode', ARGS.version_code), (True, 'versionName', ARGS.version_name)], [
    ('uses-sdk', [(True, 'minSdkVersion', MIN_SDK), (True, 'targetSdkVersion', TARGET_SDK)], []),
    ('uses-permission', [(True, 'name', 'android.permission.VIBRATE')], []),   # rumble and the touch gamepad's taps (a normal permission: granted at install)
    ('application', [(True, 'label', LABEL)] + ([(True, 'icon', ('ref', ICON_ID))] if ARGS.icon else []) + [(True, 'hasCode', False), (True, 'debuggable', not ARGS.release), (True, 'isGame', True), (True, 'extractNativeLibs', True),
        (True, 'appCategory', 0),   # a game (appCategory game = 0, and the older isGame): Android's game mode and game dashboard, and the device maker's game tools, apply to it
        # no predictive back: from target SDK 36 the system takes the back gesture itself and finishes the activity, which ends the game (front_android.c on_destroy), without
        # dispatching KEYCODE_BACK; opted out, the gesture comes as the Back key, which opens the front end's menu
        (True, 'enableOnBackInvokedCallback', False)], [
        ('activity', [(True, 'name', 'android.app.NativeActivity'), (True, 'exported', True), (True, 'configChanges', ('hex', 0x4a0 | 0x80 | 0x1000))] + ([(True, 'screenOrientation', 6)] if ARGS.landscape else []), [
            ('meta-data', [(True, 'name', 'android.app.lib_name'), (True, 'value', LIB)], []),
            ('intent-filter', [], [
                ('action', [(True, 'name', 'android.intent.action.MAIN')], []),
                ('category', [(True, 'name', 'android.intent.category.LAUNCHER')], [])])])])])

def axml(root):
    # string pool: attribute names that carry resource IDs come first, in resource-map order
    res_names = sorted({n for _, n, _ in walk_attrs(root) if n in ATTR_IDS}, key=lambda n: ATTR_IDS[n])
    strings = list(res_names)
    def s(x):
        if x not in strings: strings.append(x)
        return strings.index(x)
    s('android'); s(ANDROID_NS)
    body = []
    def chunk(t, hsize, payload): return struct.pack('<HHI', t, hsize, 8 + len(payload)) + payload
    def node(n, line=[1]):
        tag, attrs, kids = n
        enc = []
        for is_ns, name, v in attrs:
            ns = s(ANDROID_NS) if is_ns else 0xffffffff
            if isinstance(v, bool): raw, dt, data = 0xffffffff, T_BOOL, 0xffffffff if v else 0
            elif isinstance(v, int): raw, dt, data = 0xffffffff, T_INT_DEC, v
            elif isinstance(v, tuple): raw, dt, data = 0xffffffff, T_REF if v[0] == 'ref' else T_INT_HEX, v[1]
            else: raw, dt, data = s(v), T_STRING, s(v)
            enc.append((ATTR_IDS.get(name, 0xffffffff) if is_ns else 0xffffffff, struct.pack('<IIIHBBI', ns, s(name), raw, 8, 0, dt, data)))
        enc.sort(key=lambda a: a[0])   # resource-ID attributes ascending, plain ones (package) last
        a = b''.join(e for _, e in enc)
        body.append(chunk(0x0102, 16, struct.pack('<II', line[0], 0xffffffff) + struct.pack('<IIHHHHHH', 0xffffffff, s(tag), 20, 20, len(attrs), 0, 0, 0) + a))
        line[0] += 1
        for k in kids: node(k)
        body.append(chunk(0x0103, 16, struct.pack('<IIII', line[0], 0xffffffff, 0xffffffff, s(tag)))); line[0] += 1
    ns = struct.pack('<IIII', 1, 0xffffffff, s('android'), s(ANDROID_NS))
    node(root)
    nodes = chunk(0x0100, 16, ns) + b''.join(body) + chunk(0x0101, 16, ns)
    # UTF-16 string pool
    offs, data = [], b''
    for x in strings:
        offs.append(len(data)); u = x.encode('utf-16-le'); data += struct.pack('<H', len(x)) + u + b'\0\0'
    data += b'\0' * (-len(data) % 4)
    hdr = struct.pack('<IIIII', len(strings), 0, 0, 28 + 4 * len(strings), 0)
    pool = chunk(0x0001, 28, hdr + b''.join(struct.pack('<I', o) for o in offs) + data)
    rmap = chunk(0x0180, 8, b''.join(struct.pack('<I', ATTR_IDS[n]) for n in res_names))
    return chunk(0x0003, 8, pool + rmap + nodes)

# ---------------- resources.arsc: one drawable (the icon), any density ----------------
def rchunk(t, hsize, header, body=b''): return struct.pack('<HHI', t, hsize, hsize + len(body)) + header + body
def string_pool(strings):   # UTF-16 string pool chunk
    offs, data = [], b''
    for x in strings:
        offs.append(len(data)); data += struct.pack('<H', len(x)) + x.encode('utf-16-le') + b'\0\0'
    data += b'\0' * (-len(data) % 4)
    return rchunk(0x0001, 28, struct.pack('<IIIII', len(strings), 0, 0, 28 + 4 * len(strings), 0), b''.join(struct.pack('<I', o) for o in offs) + data)
def res_table(path):
    types, keys = string_pool(['drawable']), string_pool(['icon'])
    spec = rchunk(0x0202, 16, struct.pack('<BBHI', 1, 0, 0, 1), struct.pack('<I', 0))
    config = struct.pack('<I', 64) + b'\0' * 10 + struct.pack('<H', 0xffff) + b'\0' * 48   # ResTable_config: density nodpi, the rest any
    entry = struct.pack('<HHI', 8, 0, 0) + struct.pack('<HBBI', 8, 0, T_STRING, 0)        # key "icon" -> global string 0 (the file's path)
    typ = rchunk(0x0201, 20 + 64, struct.pack('<BBHII', 1, 0, 0, 1, 20 + 64 + 4) + config, struct.pack('<I', 0) + entry)
    name = PKG.encode('utf-16-le')[:254].ljust(256, b'\0')
    pkg = rchunk(0x0200, 288, struct.pack('<I', 0x7f) + name + struct.pack('<IIIII', 288, 1, 288 + len(types), 1, 0), types + keys + spec + typ)
    return rchunk(0x0002, 12, struct.pack('<I', 1), string_pool([path]) + pkg)

def walk_attrs(n):
    for a in n[1]: yield a
    for k in n[2]: yield from walk_attrs(k)

# ---------------- RSA, DER, self-signed certificate ----------------
def is_prime(n):
    if n < 2: return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0: return n == p
    d, r = n - 1, 0
    while d % 2 == 0: d //= 2; r += 1
    for _ in range(40):
        x = pow(secrets.randbelow(n - 3) + 2, d, n)
        if x in (1, n - 1): continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1: break
        else: return False
    return True
def gen_prime(bits):
    while True:
        p = secrets.randbits(bits) | (3 << (bits - 2)) | 1
        if p % 65537 != 1 and is_prime(p): return p
def gen_key():
    p, q = gen_prime(1024), gen_prime(1024)
    n, e = p * q, 65537
    return {'n': n, 'e': e, 'd': pow(e, -1, (p - 1) * (q - 1))}

def der(tag, body):
    l = len(body)
    ln = bytes([l]) if l < 0x80 else (lambda b: bytes([0x80 | len(b)]) + b)(l.to_bytes((l.bit_length() + 7) // 8, 'big'))
    return bytes([tag]) + ln + body
def der_int(v): return der(0x02, v.to_bytes(v.bit_length() // 8 + 1, 'big'))
def der_seq(*xs): return der(0x30, b''.join(xs))
def der_oid(s):
    a = [int(x) for x in s.split('.')]; out = bytes([a[0] * 40 + a[1]])
    for v in a[2:]:
        b = [v & 0x7f]; v >>= 7
        while v: b.append(0x80 | (v & 0x7f)); v >>= 7
        out += bytes(reversed(b))
    return der(0x06, out)
NULL = b'\x05\x00'
ALG_SHA256_RSA = der_seq(der_oid('1.2.840.113549.1.1.11'), NULL)
def spki(k): return der_seq(der_seq(der_oid('1.2.840.113549.1.1.1'), NULL), der(0x03, b'\0' + der_seq(der_int(k['n']), der_int(k['e']))))
def rsa_sign(k, msg):
    di = bytes.fromhex('3031300d060960864801650304020105000420') + hashlib.sha256(msg).digest()
    kl = (k['n'].bit_length() + 7) // 8
    em = b'\x00\x01' + b'\xff' * (kl - len(di) - 3) + b'\x00' + di
    return pow(int.from_bytes(em, 'big'), k['d'], k['n']).to_bytes(kl, 'big')
def cert(k):
    name = der_seq(der(0x31, der_seq(der_oid('2.5.4.3'), der(0x0c, b'nx2a probe'))))
    validity = der_seq(der(0x17, b'260101000000Z'), der(0x18, b'20560101000000Z'))
    tbs = der_seq(der(0xa0, der_int(2)), der_int(secrets.randbits(62) | 1), ALG_SHA256_RSA, name, validity, name, spki(k))
    return der_seq(tbs, ALG_SHA256_RSA, der(0x03, b'\0' + rsa_sign(k, tbs)))

# ---------------- ZIP (stored) ----------------
# Written straight to the output file, a file entry copied in 4 MB pieces (a --pack entry can be gigabytes): its CRC is known only after the copy, so it goes into
# the local header afterwards. The APK stays a plain zip, never Zip64 (offsets and sizes 32-bit, at most 65,534 entries): a layout past that is refused before
# anything is written, as are names Android's zip reader would refuse.
ZIP_MAX = 0xffffffff
class ApkError(Exception): pass
def entry_size(src): return len(src) if isinstance(src, (bytes, bytearray)) else os.path.getsize(src)
def entry_pad(off, nb, align):   # the alignment extra field's length before an entry's data, whose local header starts at off
    pad = -(off + 30 + len(nb)) % align
    return pad + align if 0 < pad < 4 else pad   # too small for an extra field header: go to the next alignment step
def zip_plan(entries):   # where the central directory starts and how long it is, checked against the 32-bit limits
    if len(entries) >= 0xffff: raise ApkError(f'{len(entries):,} entries: a zip without Zip64 holds at most 65,534; the game has too many files for one APK')
    off = cd = 0
    for name, src, align in entries:
        nb = name.encode()
        if not name or len(nb) > 0xffff or name.startswith('/') or '\\' in name or '..' in name.split('/'): raise ApkError(f'a name Android would refuse in an APK: {name!r}')
        off += 30 + len(nb) + entry_pad(off, nb, align) + entry_size(src); cd += 46 + len(nb)
        if off > ZIP_MAX: raise ApkError(f'{name} ends past 4 GiB in the APK: a zip without Zip64 holds at most 4 GiB; the game is too big for one APK')
    return off, cd
def build_zip(f, entries):   # entries: [(name, bytes or a file path, align)], written to f from its start; returns (end of the entries, central directory, end of central directory)
    zip_plan(entries)
    cd = bytearray()
    dos_time, dos_date = 0, (2026 - 1980) << 9 | 1 << 5 | 1
    for name, src, align in entries:
        nb = name.encode(); flags = 0 if nb.isascii() else 0x800   # bit 11: the name is UTF-8 (a game file's name may not be ASCII)
        off = f.tell(); pad = entry_pad(off, nb, align)
        extra = struct.pack('<HH', 0xd935, pad - 4) + b'\0' * (pad - 4) if pad else b''   # 0xd935: zipalign's alignment extra field
        size = entry_size(src); crc = zlib.crc32(src) if isinstance(src, (bytes, bytearray)) else 0
        f.write(struct.pack('<IHHHHHIIIHH', 0x04034b50, 10, flags, 0, dos_time, dos_date, crc, size, size, len(nb), len(extra)) + nb + extra)
        if isinstance(src, (bytes, bytearray)): f.write(src)
        else:
            done = 0
            with open(src, 'rb') as s:
                while chunk := s.read(4 << 20): crc = zlib.crc32(chunk, crc); f.write(chunk); done += len(chunk)
            if done != size: raise ApkError(f'{src} changed while it was copied ({size} bytes, then {done})')
            end = f.tell(); f.seek(off + 14); f.write(struct.pack('<I', crc)); f.seek(end)
        cd += struct.pack('<IHHHHHHIIIHHHHHII', 0x02014b50, 10, 10, flags, 0, dos_time, dos_date, crc, size, size, len(nb), 0, 0, 0, 0, 0, off) + nb
    end = f.tell()
    eocd = struct.pack('<IHHHHIIH', 0x06054b50, 0, 0, len(entries), len(entries), len(cd), end, 0)
    return end, bytes(cd), eocd

# ---------------- APK Signature Scheme v2 ----------------
def lp(b): return struct.pack('<I', len(b)) + b
def chunk_digests(read, n, out):   # the 1 MB chunks of one section, each hashed with its length; read(k) gives the next k bytes
    while n > 0:
        c = read(min(n, 1 << 20)); n -= len(c)
        if not c: raise ApkError('the APK ended while it was hashed')
        out.append(hashlib.sha256(b'\xa5' + struct.pack('<I', len(c)) + c).digest())
def sign_v2(f, end, cd, eocd, k, crt):   # f holds the entries [0, end); the signing block, the central directory and the EOCD are appended after them
    ALG = 0x0103   # RSASSA-PKCS1-v1_5 with SHA2-256
    digests = []
    f.seek(0); chunk_digests(f.read, end, digests)   # the entries are read back from the file: they can be gigabytes
    for sec in (cd, eocd): chunk_digests(io.BytesIO(sec).read, len(sec), digests)   # EOCD's CD offset still points at the CD start = where the signing block will begin
    digest = hashlib.sha256(b'\x5a' + struct.pack('<I', len(digests)) + b''.join(digests)).digest()
    signed = lp(lp(struct.pack('<I', ALG) + lp(digest))) + lp(lp(crt)) + lp(b'')
    signer = lp(signed) + lp(lp(struct.pack('<I', ALG) + lp(rsa_sign(k, signed)))) + lp(spki(k))
    value = lp(lp(signer))
    pair = struct.pack('<QI', 4 + len(value), 0x7109871a) + value
    size = len(pair) + 8 + 16
    block = struct.pack('<Q', size) + pair + struct.pack('<Q', size) + b'APK Sig Block 42'
    if end + len(block) + len(cd) + len(eocd) > ZIP_MAX: raise ApkError(f'the signed APK would be {(end + len(block) + len(cd) + len(eocd)) / 2**30:.2f} GiB: a zip without Zip64 holds at most 4 GiB')
    new_eocd = eocd[:16] + struct.pack('<I', end + len(block)) + eocd[20:]
    f.seek(end); f.write(block + cd + new_eocd); f.truncate()

# --pack: a --single-apk port's data pack, every file as assets/nx2a/<path>, stored and with its data at a 16 KB aligned offset, so the host reads it in place
# (dm/android/apkpack.c) and maps shaders.dmsv straight from the APK; an empty folder gets an entry of its own, as nothing else would name it
PACK_PREFIX, PACK_ALIGN = 'assets/nx2a/', 16384
def pack_entries(root):
    out = []
    for d, dirs, files in os.walk(root):
        dirs.sort(); rel = os.path.relpath(d, root).replace(os.sep, '/')
        rel = '' if rel == '.' else rel + '/'
        if rel and not dirs and not files: out.append((PACK_PREFIX + rel, b'', PACK_ALIGN))
        out += [(PACK_PREFIX + rel + f, os.path.join(d, f), PACK_ALIGN) for f in sorted(files)]
    return out

def main():
    so, out = ARGS.so, ARGS.out
    keyf = ARGS.key if ARGS.key else os.path.join(os.path.dirname(os.path.abspath(out)), 'probe-signing-key.json')
    if os.path.exists(keyf): k = {a: int(v) for a, v in json.load(open(keyf)).items() if a != 'cert'}; crt = bytes.fromhex(json.load(open(keyf))['cert'])
    else:
        t = time.time(); k = gen_key(); crt = cert(k)
        json.dump({**{a: str(v) for a, v in k.items()}, 'cert': crt.hex()}, open(keyf, 'w'))
        print(f'new signing key {keyf} ({time.time() - t:.1f} s)')
    entries = [('AndroidManifest.xml', axml(MANIFEST), 4), (f'lib/arm64-v8a/lib{LIB}.so', open(so, 'rb').read(), 16384)]
    entries += [(f'lib/arm64-v8a/{os.path.basename(x)}', open(x, 'rb').read(), 16384) for x in ARGS.extra_so]
    entries += [(f'assets/{n}', open(f, 'rb').read(), 4) for n, _, f in (x.partition('=') for x in ARGS.asset)]   # stored, as every entry: AAsset reads them in place
    if ARGS.icon: entries += [('resources.arsc', res_table('res/drawable-nodpi-v4/icon.png'), 4), ('res/drawable-nodpi-v4/icon.png', open(ARGS.icon, 'rb').read(), 4)]
    if ARGS.pack: entries += pack_entries(ARGS.pack)
    part = out + '.partial'   # a failed or interrupted build leaves no APK that looks whole
    try:
        with open(part, 'w+b') as f:
            end, cd, eocd = build_zip(f, entries)
            sign_v2(f, end, cd, eocd, k, crt)
            size = f.tell()
        os.replace(part, out)
    except ApkError as e:
        if os.path.exists(part): os.remove(part)
        sys.exit(f'mkapk: {e}')
    except BaseException:
        if os.path.exists(part): os.remove(part)
        raise
    print(f'{out}: {size} bytes' + (f', {len(entries) - 2:,} more entries ({PACK_PREFIX}: {sum(1 for e in entries if e[0].startswith(PACK_PREFIX)):,})' if ARGS.pack else '')
          + f', signer SHA-256 {hashlib.sha256(crt).hexdigest()[:16]}...')

if __name__ == '__main__': main()
