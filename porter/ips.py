# Atmosphere ExeFS code patches (IPS / IPS32, `atmosphere/exefs_patches/<name>/<build ID>.ips`) as the rewriter's cuts: a translation or other mod that changes a game's
# code bytes is applied to the module before it is rewritten (patch_nso.py DM_PATCHES_EXTRA), so the rewritten module in the data pack carries it and the APK does not.
# An IPS record's offset counts from the start of the NSO file's 0x100-byte header, as Atmosphere applies it to the module's memory image: image offset = record offset - 0x100.
import struct
from pathlib import Path

NSO_HEADER = 0x100


class Refused(Exception): pass


def records(data):
    """[(offset, bytes)] of an IPS or IPS32 file (RLE records expanded)"""
    if data[:5] == b'PATCH': o, wide, eof = 5, False, b'EOF'
    elif data[:5] == b'IPS32': o, wide, eof = 5, True, b'EEOF'
    else: raise Refused('not an IPS or IPS32 file')
    out = []
    while True:
        if data[o:o + len(eof)] == eof and o + len(eof) >= len(data) - 3: return out
        if wide: off = struct.unpack_from('>I', data, o)[0]; o += 4
        else: off = int.from_bytes(data[o:o + 3], 'big'); o += 3
        n = struct.unpack_from('>H', data, o)[0]; o += 2
        if n: b = data[o:o + n]; o += n
        else: cnt = struct.unpack_from('>H', data, o)[0]; b = data[o + 2:o + 3] * cnt; o += 3
        if len(b) != (n or len(b)): raise Refused(f'record at {off:#x} runs past the end of the file')
        out.append((off, bytes(b)))


def build_id_of(path):
    """the build ID an Atmosphere patch file names (its file name: 32 or more hex digits, the rest zero)"""
    s = Path(path).stem.lower()
    if len(s) < 16 or any(c not in '0123456789abcdef' for c in s): raise Refused(f'{Path(path).name}: the file name is not a build ID')
    return (s + '0' * 32)[:32]


def cuts(path, image, build_id):
    """The patch file's records against main's memory image (fpslocker.nso_image) as CUTS rows (offset, original hex, new hex, reason); refused when it names another build"""
    want = build_id_of(path)
    if want[:16] != build_id.lower()[:16]: raise Refused(f'{Path(path).name} is for build {want[:16]}, main is {build_id[:16]}')
    out = []
    for off, b in records(Path(path).read_bytes()):
        o = off - NSO_HEADER
        if o < 0 or o + len(b) > len(image): raise Refused(f'record at {off:#x} ({len(b)} bytes) is outside main')
        out.append((o, bytes(image[o:o + len(b)]).hex(), b.hex(), f'ExeFS patch {Path(path).name}'))
    return out


def patches_py(build_id, rows, sources):
    body = ''.join(f'    ({o:#x}, {a!r}, {b!r}, {w!r}),\n' for o, a, b, w in rows)
    return f'# Written by nx2android\'s porter from the ExeFS patch(es) {", ".join(sources)} (porter/ips.py)\nCUTS = {{{build_id!r}: [\n{body}]}}\n'
