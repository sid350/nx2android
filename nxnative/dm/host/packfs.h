// The game's data read in place from the app's own APK: an Android port made with `porter port --single-apk` carries what the data pack holds (mods/, RomFS/, aoc/, shaders.dmsv,
// the button-icon font, fpslock.tsv, env.txt, settings.txt) as assets/nx2a/<path>, every entry stored (never compressed) with its data at a 16 KB aligned offset of the APK
// (dm/android/apk/mkapk.py --pack). The front end opens it once (dm/android/apkpack.c, before the host starts) and points DM_ROMFS, DM_VK_SHADERS and the modules' folder at
// PACKFS_ROOT; env.txt's own paths (DM_AOC_DIR, DM_SHARED_FONT_EXT, DM_FPS_LOCKS) name it too. Every reader of those files asks packfs_is() first and reads the bytes from the APK:
// a file is an fd of the APK, the file's offset in it and its size, so nothing is extracted and the page cache holds the APK's own pages.
// Names are exact (case-sensitive), as Horizon's and the host's directory index (hle_fs.c dir_index_get). The pack is read-only and never changes while the app runs.
// Elsewhere (Mac, iOS, an Android build with no pack) packfs_is() is always false and the readers take their own paths unchanged.
#pragma once
#include <stdint.h>
#include <stdio.h>

#define PACKFS_ROOT "/nx2a-apk"   // the pack's paths: PACKFS_ROOT/RomFS/..., a name no real path of an Android app starts with
typedef struct { const char *name; int isdir; int64_t size; } packfs_ent;   // one entry of a pack directory (size 0 for a directory)

#ifdef DM_ANDROID
int packfs_init(const char *apk);   // the pack in this APK: its file count, 0 when the APK has none (an APK beside a data pack); once, before the host starts
int packfs_on(void);                // a pack is open
int packfs_is(const char *path);    // path is PACKFS_ROOT or below it, and a pack is open
int packfs_stat(const char *path, int *isdir, int64_t *size);       // 0, or -1 with errno ENOENT; isdir / size may be NULL
int packfs_open(const char *path, int64_t *base, int64_t *size);    // an fd of the APK of its own (the caller closes it) and the file's data offset and size in it; -1 with errno ENOENT / EISDIR
int packfs_dir(const char *path, const packfs_ent **ents);           // a directory's entries sorted by name (strcmp), resident; -1 with errno ENOENT / ENOTDIR
FILE *packfs_fopen(const char *path);                                // the file as a read-only stdio stream (pread on the APK: no copy); NULL with errno
#else
static inline int packfs_on(void) { return 0; }
static inline int packfs_is(const char *path) { (void)path; return 0; }
static inline int packfs_stat(const char *path, int *isdir, int64_t *size) { (void)path; (void)isdir; (void)size; return -1; }
static inline int packfs_open(const char *path, int64_t *base, int64_t *size) { (void)path; (void)base; (void)size; return -1; }
static inline int packfs_dir(const char *path, const packfs_ent **ents) { (void)path; (void)ents; return -1; }
static inline FILE *packfs_fopen(const char *path) { (void)path; return NULL; }
#endif

// fopen(path, "r") that also reads the pack: the host's tables beside the modules (fixups, gates, nro.tsv) and fpslock.tsv
static inline FILE *packfs_fopen_r(const char *path) { return packfs_is(path) ? packfs_fopen(path) : fopen(path, "r"); }
