# A profile's SPIR-V corrections for a shader the Vulkan backend draws differently from the Apple chain (dm/games/<id>/android_shader_stores.tsv, read by shader_vk.py):
# one OpStore's value swapped for a constant of the module. The ids are the translator's own, so a row names both the old and the new value, and a module that no longer
# matches stops the build: a correction that silently stopped applying would bring the bug back, and one applied to other code would add a new one.
import struct

OP_CONSTANT_TRUE, OP_CONSTANT_FALSE, OP_CONSTANT, OP_STORE = 41, 42, 43, 62


def read_table(path):
    """{shader key: [(pointer id, old value id, new value id), ...]} from rows of key, pointer, old, new; # comments"""
    rows = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        if line and not line.startswith('#'):
            key, pointer, old, new = line.split()
            rows.setdefault(key, []).append((int(pointer), int(old), int(new)))
    return rows


def replace_store(spv, pointer, old, new):
    """The module with its one `OpStore %pointer %old` storing the constant %new instead; ValueError when the module does not hold exactly that"""
    words = list(struct.unpack(f'<{len(spv) // 4}I', spv))
    stores, constant, i = [], False, 5
    while i < len(words):
        count, op = words[i] >> 16, words[i] & 0xffff
        if not count or i + count > len(words): raise ValueError('invalid SPIR-V instruction')
        if op == OP_STORE and words[i + 1] == pointer and words[i + 2] == old: stores.append(i)
        elif op in (OP_CONSTANT_TRUE, OP_CONSTANT_FALSE) and words[i + 2] == new: constant = True
        elif op == OP_CONSTANT and words[i + 2] == new: constant = True
        i += count
    if len(stores) != 1: raise ValueError(f'OpStore %{pointer} %{old}: found {len(stores)}, expected 1')
    if not constant: raise ValueError(f'%{new} is not a constant of the module')
    words[stores[0] + 2] = new
    return struct.pack(f'<{len(words)}I', *words)
