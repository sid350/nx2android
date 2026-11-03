# --large-text: the game's text drawn larger. The profile's android.tsv names the TrueType fonts the game draws its text with and a patch pak (large_text); the fonts are read
# from the game's Unreal paks, scaled, and written into that pak in the RomFS, which the engine mounts over the game's own (a *_<n>_P.pak, the higher n last).
# A font is scaled by dividing its unitsPerEm by the scale: every glyph and advance grows by it while the game's windows, frames and pictures keep their size. Unreal Engine 4.18
# takes a line's height and baseline from the font's bounding box (head yMin / yMax), not from hhea or OS/2: the box is scaled back down, so the lines keep their spacing (OCTOPATH
# TRAVELER's dialogue boxes: with the box left alone the lines were 1.5 times as far apart); hhea and OS/2 follow it for other readers.
import hashlib, os, re, struct
from pathlib import Path

from porter.upstream import BuildError, uepak


def _checksum(d, off, ln, head):
    b = bytes(d[off:off + ln]) + b'\0' * (-ln % 4)
    if head: b = b[:8] + b'\0\0\0\0' + b[12:]   # head's checkSumAdjustment counts as 0
    return sum(struct.unpack(f'>{len(b) // 4}I', b)) & 0xffffffff


def scale_font(data, scale, name='font'):
    """The TrueType / OpenType font data with its glyphs scale times larger and its line spacing as it was"""
    d = bytearray(data)
    if d[:4] not in (b'\0\1\0\0', b'OTTO', b'true'): raise BuildError(f'{name}: not a TrueType or OpenType font')
    tables = {}
    for i in range(struct.unpack_from('>H', d, 4)[0]):
        tag, _, off, ln = struct.unpack_from('>4sIII', d, 12 + 16 * i); tables[tag] = (i, off, ln)
    for t in (b'head', b'hhea', b'OS/2'):
        if t not in tables: raise BuildError(f'{name}: no {t.decode()} table')
    _, h, _ = tables[b'head']
    upm = struct.unpack_from('>H', d, h + 18)[0]; new = round(upm / scale); k = new / upm
    struct.pack_into('>H', d, h + 18, new)
    for o in (h + 38, h + 42): struct.pack_into('>h', d, o, round(struct.unpack_from('>h', d, o)[0] * k))   # yMin, yMax
    _, o, _ = tables[b'hhea']
    struct.pack_into('>hhh', d, o + 4, *(round(v * k) for v in struct.unpack_from('>hhh', d, o + 4)))   # ascender, descender, line gap
    _, o, _ = tables[b'OS/2']
    ta, td, tg, wa, wd = struct.unpack_from('>hhhHH', d, o + 68)
    struct.pack_into('>hhhHH', d, o + 68, round(ta * k), round(td * k), round(tg * k), round(wa * k), round(wd * k))
    for t in (b'head', b'hhea', b'OS/2'):
        i, off, ln = tables[t]; struct.pack_into('>I', d, 12 + 16 * i + 4, _checksum(d, off, ln, t == b'head'))
    return bytes(d)


def write_pak(path, files):
    """A version 4 Unreal pak of stored files mounted at ../../../, as the game's own: [(path in the game, bytes)]"""
    def fstr(s): b = s.encode() + b'\0'; return struct.pack('<i', len(b)) + b
    def entry(off, d): return struct.pack('<qqqi', off, len(d), len(d), 0) + hashlib.sha1(d).digest() + struct.pack('<BI', 0, 0)
    body, index = bytearray(), bytearray(fstr('../../../') + struct.pack('<i', len(files)))
    for name, d in files:
        off = len(body); body += entry(0, d) + d; index += fstr(name) + entry(off, d)
    Path(path).write_bytes(bytes(body) + bytes(index) + struct.pack('<BIiqq', 0, 0x5A6F12E1, 4, len(body), len(index)) + hashlib.sha1(index).digest())


def _pak_order(p):   # the engine's mount order: the base paks, then the patch paks by their number (Kingship-Switch_0_P.pak before _1_P)
    m = re.search(r'_(\d+)_P\.pak$', p.name)
    return (1, int(m.group(1)), p.name) if m else (0, 0, p.name)


def build(romfs, pak, fonts, scale, out):
    """The patch pak for the profile's large_text: fonts read from the RomFS (loose files, else the newest Unreal pak that has them), scaled. → the pak's path (out)"""
    found = {f: (romfs / f).read_bytes() for f in fonts if (romfs / f).is_file()}
    if len(found) < len(fonts):
        for p in sorted((p for p in romfs.rglob('*.pak') if p.name != Path(pak).name), key=_pak_order):
            try: pk = uepak.Pak(str(p))
            except ValueError: continue
            for name in pk.files:
                full = os.path.normpath(os.path.join(pk.mount, name)).replace(os.sep, '/').lstrip('./')   # the mount point is ../../../, the game's root
                if full in fonts: found[full] = pk.read(name)
    missing = [f for f in fonts if f not in found]
    if missing: raise BuildError('large text: fonts not in the RomFS or its paks: ' + ', '.join(missing))
    out.parent.mkdir(parents=True, exist_ok=True)
    write_pak(out, [(f, scale_font(found[f], scale, f)) for f in fonts])
    return out
