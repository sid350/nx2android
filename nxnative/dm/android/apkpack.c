// Android port: the game's data read in place from the app's own APK (a `porter port --single-apk` port; the API and how the readers use it: dm/host/packfs.h).
// The APK is a plain zip (dm/android/apk/mkapk.py): packfs_init reads its central directory once and keeps an index of the entries under assets/nx2a/, path → (data offset,
// size), with a directory tree for listings (each directory's entries sorted by name). The index is built before the host starts and never changes, so lookups take no lock.
// Every entry must be stored with its data at a 16 KB aligned offset, as mkapk.py --pack writes them: a compressed entry cannot be read in place, and shaders.dmsv is mapped
// straight from the APK (gfx_vk_shader.c), which takes a page-aligned offset. Such an entry means the APK was not made by the porter, and the app stops with a message.
// Reads are pread on one fd of the APK (dup'd for a caller that keeps a file open: a game's FileHandle is an fd, hle_fs.c), so the bytes come from the APK's own pages
// in the page cache: nothing is extracted, and /data is read directly rather than through the external storage's FUSE layer.
#include "dm.h"
#include "packfs.h"
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define PACK_PREFIX "assets/nx2a/"
#define PACK_ALIGN 16384   // mkapk.py PACK_ALIGN: a page on both 4 KB and 16 KB kernels

typedef struct { char *path; packfs_ent e; int64_t off; uint32_t parent, nk, ck; uint32_t *kid; packfs_ent *ents; } node_t;   // path: relative to the pack ("" the root)
static node_t *g_node; static uint32_t g_nnode, g_cnode;
static uint32_t *g_hash; static uint32_t g_hmask;   // open addressing over node index + 1 (0 = empty), keyed by the relative path
static int g_fd = -1;
static uint64_t fnv(const char *s, size_t n) { uint64_t h = 1469598103934665603ull; while (n--) { h ^= (uint8_t)*s++; h *= 1099511628211ull; } return h; }
static uint32_t *slot_of(const char *rel, size_t n) {
    for (uint32_t i = (uint32_t)fnv(rel, n) & g_hmask;; i = (i + 1) & g_hmask) {
        uint32_t k = g_hash[i];
        if (!k || (strlen(g_node[k - 1].path) == n && !memcmp(g_node[k - 1].path, rel, n))) return &g_hash[i];
    }
}
static void rehash(uint32_t want) {
    free(g_hash); g_hmask = 1023; while (g_hmask + 1 < want * 2) g_hmask = g_hmask * 2 + 1;
    if (!(g_hash = calloc(g_hmask + 1, sizeof *g_hash))) die("pack index allocation failed");
    for (uint32_t i = 0; i < g_nnode; i++) *slot_of(g_node[i].path, strlen(g_node[i].path)) = i + 1;
}
// the node of rel[0..n), made (with its parents, as directories) when it is not there yet; -1 when a file stands where a directory is needed or the other way round
static int64_t node_get(const char *rel, size_t n, int isdir) {
    uint32_t *s = slot_of(rel, n);
    if (*s) return g_node[*s - 1].e.isdir == isdir ? (int64_t)*s - 1 : -1;
    int64_t parent = -1;
    if (n) { size_t k = n; while (k && rel[k - 1] != '/') k--; if ((parent = node_get(rel, k ? k - 1 : 0, 1)) < 0) return -1; }
    if (g_nnode == g_cnode && !(g_node = realloc(g_node, (g_cnode = g_cnode ? g_cnode * 2 : 256) * sizeof *g_node))) die("pack index allocation failed");
    node_t *x = &g_node[g_nnode]; memset(x, 0, sizeof *x);
    if (!(x->path = strndup(rel, n))) die("pack index allocation failed");
    const char *base = strrchr(x->path, '/'); x->e.name = base ? base + 1 : x->path; x->e.isdir = isdir;
    x->parent = parent < 0 ? 0 : (uint32_t)parent;
    if (parent >= 0) { node_t *p = &g_node[parent];
        if (p->nk == p->ck && !(p->kid = realloc(p->kid, (p->ck = p->ck ? p->ck * 2 : 8) * sizeof *p->kid))) die("pack index allocation failed");
        p->kid[p->nk++] = g_nnode; }
    uint32_t i = g_nnode++;
    if (g_nnode * 2 > g_hmask) rehash(g_nnode); else *slot_of(rel, n) = i + 1;   // a rehash puts the new node in too
    return i;
}
static int ent_cmp(const void *a, const void *b) { return strcmp(((const packfs_ent *)a)->name, ((const packfs_ent *)b)->name); }
static uint16_t u16(const uint8_t *p) { return (uint16_t)(p[0] | p[1] << 8); }
static uint32_t u32(const uint8_t *p) { return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24; }
static int pread_all(int fd, void *b, size_t n, int64_t off) {
    for (size_t got = 0; got < n;) { ssize_t r = pread(fd, (uint8_t *)b + got, n - got, off + (int64_t)got); if (r < 0 && errno == EINTR) continue; if (r <= 0) return -1; got += (size_t)r; }
    return 0;
}

int packfs_init(const char *apk) {
    int fd = open(apk, O_RDONLY | O_CLOEXEC); struct stat st;
    if (fd < 0 || fstat(fd, &st)) { LOG("[RED] pack: cannot open the app's APK %s (errno=%d): the game's data is looked for in the files folder", apk, errno); if (fd >= 0) close(fd); return 0; }
    // the end of central directory record: the last 22 bytes, or before a comment of up to 64 KB
    int64_t size = st.st_size, tail = size < 65557 ? size : 65557; uint8_t *t = malloc((size_t)tail), *cd = NULL; int64_t eocd = -1;
    if (!t || pread_all(fd, t, (size_t)tail, size - tail)) { LOG("[RED] pack: cannot read the end of %s", apk); free(t); close(fd); return 0; }
    for (int64_t i = tail - 22; i >= 0; i--) if (u32(t + i) == 0x06054b50 && i + 22 + u16(t + i + 20) == tail) { eocd = i; break; }
    if (eocd < 0) { LOG("[RED] pack: %s has no zip directory", apk); free(t); close(fd); return 0; }
    uint32_t count = u16(t + eocd + 10), cdsize = u32(t + eocd + 12), cdoff = u32(t + eocd + 16); free(t);
    if (count == 0xffff || cdoff == 0xffffffffu || (int64_t)cdoff + cdsize > size || !(cd = malloc(cdsize ? cdsize : 1)) || pread_all(fd, cd, cdsize, cdoff)) {   // a Zip64 APK is not mkapk.py's
        LOG("[RED] pack: cannot read the zip directory of %s (%u entries at %#x)", apk, count, cdoff); free(cd); close(fd); return 0; }
    g_nnode = 0; rehash(2 * count + 16); node_get("", 0, 1);
    uint64_t files = 0, bytes = 0; size_t plen = strlen(PACK_PREFIX);
    for (uint32_t i = 0, p = 0; i < count; i++) {
        if (p + 46 > cdsize || u32(cd + p) != 0x02014b50) die("pack: the zip directory of %s is damaged at entry %u: install the game's APK again", apk, i);
        const uint8_t *h = cd + p; uint16_t method = u16(h + 10), nl = u16(h + 28); uint32_t csz = u32(h + 20), usz = u32(h + 24), loff = u32(h + 42);
        const char *name = (const char *)h + 46; p += 46 + nl + u16(h + 30) + u16(h + 32);
        if (p > cdsize) die("pack: the zip directory of %s is damaged at entry %u: install the game's APK again", apk, i);
        if (nl <= plen || memcmp(name, PACK_PREFIX, plen)) continue;   // the manifest, the host library, the gamepad's theme
        const char *rel = name + plen; size_t rl = nl - plen; int isdir = rel[rl - 1] == '/';
        if (isdir) rl--;
        if (!rl) continue;
        if (method || csz != usz)   // the porter stores every entry: a compressed one would have to be extracted, which this mode exists to avoid
            die("pack: %.*s in %s is compressed (method %u): the game's data must be stored in the APK; port the game again with porter port --single-apk", (int)nl, name, apk, method);
        uint8_t lh[30];
        if (pread_all(fd, lh, sizeof lh, loff) || u32(lh) != 0x04034b50) die("pack: the local header of %.*s in %s is damaged: install the game's APK again", (int)nl, name, apk);
        int64_t data = (int64_t)loff + 30 + u16(lh + 26) + u16(lh + 28);
        if (data + usz > size) die("pack: %.*s runs past the end of %s: install the game's APK again", (int)nl, name, apk);
        if (usz && data % PACK_ALIGN)   // shaders.dmsv is mapped straight from the APK, at a page-aligned offset
            die("pack: %.*s in %s starts at offset %#llx, not %d-byte aligned: port the game again with porter port --single-apk", (int)nl, name, apk, (unsigned long long)data, PACK_ALIGN);
        int64_t k = node_get(rel, rl, isdir);
        if (k < 0) die("pack: %.*s in %s is both a file and a folder: port the game again", (int)nl, name, apk);
        if (!isdir) { g_node[k].off = data; g_node[k].e.size = usz; files++; bytes += usz; }
    }
    free(cd);
    for (uint32_t i = 0; i < g_nnode; i++) {   // each directory's listing, sorted by name (the names point into the nodes' paths, which stay)
        node_t *x = &g_node[i]; if (!x->e.isdir) continue;
        if (!(x->ents = malloc((x->nk ? x->nk : 1) * sizeof *x->ents))) die("pack index allocation failed");
        for (uint32_t j = 0; j < x->nk; j++) x->ents[j] = g_node[x->kid[j]].e;
        qsort(x->ents, x->nk, sizeof *x->ents, ent_cmp); free(x->kid); x->kid = NULL;
    }
    if (!files) { close(fd); return 0; }   // an APK beside a data pack: the files folder holds the game
    g_fd = fd;
    LOG("pack: %llu files, %.1f MB, read in place from %s (%s, as %s)", (unsigned long long)files, bytes / 1048576.0, apk, PACK_PREFIX, PACKFS_ROOT);
    return (int)(files > 0x7fffffff ? 0x7fffffff : files);
}
int packfs_on(void) { return g_fd >= 0; }
int packfs_is(const char *path) {
    size_t n = sizeof PACKFS_ROOT - 1;
    return g_fd >= 0 && path && !strncmp(path, PACKFS_ROOT, n) && (path[n] == '/' || !path[n]);
}
// the node of a pack path, or NULL with errno ENOENT. Repeated and trailing slashes are dropped; the host hands over paths built a component at a time (hle_fs.c) or env.txt's
static const node_t *find(const char *path) {
    if (!packfs_is(path)) { errno = ENOENT; return NULL; }
    char rel[1024]; size_t n = 0;
    for (const char *s = path + sizeof PACKFS_ROOT - 1; *s; s++) {
        if (*s == '/' && (!n || rel[n - 1] == '/')) continue;
        if (n + 1 >= sizeof rel) { errno = ENAMETOOLONG; return NULL; }
        rel[n++] = *s;
    }
    while (n && rel[n - 1] == '/') n--;
    uint32_t k = *slot_of(rel, n);
    if (!k) { errno = ENOENT; return NULL; }
    return &g_node[k - 1];
}
int packfs_stat(const char *path, int *isdir, int64_t *size) {
    const node_t *x = find(path); if (!x) return -1;
    if (isdir) *isdir = x->e.isdir;
    if (size) *size = x->e.size;
    return 0;
}
int packfs_open(const char *path, int64_t *base, int64_t *size) {
    const node_t *x = find(path); if (!x) return -1;
    if (x->e.isdir) { errno = EISDIR; return -1; }
    int fd = fcntl(g_fd, F_DUPFD_CLOEXEC, 0);   // its own fd, which the caller closes (a game's FileHandle); pread leaves the shared file position alone
    if (fd < 0) return -1;
    *base = x->off; *size = x->e.size; return fd;
}
int packfs_dir(const char *path, const packfs_ent **ents) {
    const node_t *x = find(path); if (!x) return -1;
    if (!x->e.isdir) { errno = ENOTDIR; return -1; }
    *ents = x->ents; return (int)x->nk;
}

// a pack file as a stdio stream (funopen: bionic has no fopencookie): pread on the APK's fd at the file's offset, clamped at its end
typedef struct { int64_t base, size, pos; } pf_t;
static int pf_read(void *c, char *b, int n) {
    pf_t *f = c; if (f->pos >= f->size || n <= 0) return 0;
    if ((int64_t)n > f->size - f->pos) n = (int)(f->size - f->pos);
    ssize_t r; do r = pread(g_fd, b, (size_t)n, f->base + f->pos); while (r < 0 && errno == EINTR);
    if (r > 0) f->pos += r;
    return (int)r;
}
static fpos_t pf_seek(void *c, fpos_t off, int whence) {
    pf_t *f = c; int64_t to = whence == SEEK_SET ? off : whence == SEEK_CUR ? f->pos + off : whence == SEEK_END ? f->size + off : -1;
    if (to < 0) { errno = EINVAL; return -1; }
    return f->pos = to;
}
static int pf_close(void *c) { free(c); return 0; }
FILE *packfs_fopen(const char *path) {
    const node_t *x = find(path); if (!x) return NULL;
    if (x->e.isdir) { errno = EISDIR; return NULL; }
    pf_t *c = malloc(sizeof *c); if (!c) return NULL;
    *c = (pf_t){x->off, x->e.size, 0};
    FILE *f = funopen(c, pf_read, NULL, pf_seek, pf_close);
    if (!f) free(c);
    return f;
}
