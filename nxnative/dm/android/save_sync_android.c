// Android port: the transport of cloud saves (dm/host/save_sync.c packs, compares and decides; save_sync.h): the cloud's folder is a folder in the player's Dropbox, so the same
// save.dmsync files are reached from every device. DM_SAVE_SYNC stays a local path (<internal>/cloud: save_sync.c makes its parent folders), and only its end, <title ID>/save.dmsync,
// names the file there. Off unless the settings are there: a file dropbox.txt in the app's external files folder, KEY=VALUE per line (# comments):
//   app_key=...         the Dropbox app the token was given to
//   refresh_token=...   from the OAuth PKCE flow (token_access_type=offline): stands for the player's account, so it is treated as a password
//   folder=/Saves       optional: the folder holding <title ID>/save.dmsync, from the root of the app's folder
// That folder is open to adb, a computer and any app with all-files access, so the file is moved at the start to <data>/no_backup/dropbox.conf (private to the app, left out of Android's
// backups) and removed from there. Neither token is ever written to the log. DM_SAVE_SYNC=0 in env.txt turns sync off.
// The requests are HTTP/1.1 written here over java.net sockets (JNI, as pad_watch in front_android.c; the APK has no code of its own). The system's TLS does the encryption and its trust
// store the chain; a raw SSLSocket checks no host name by itself, so HTTPS endpoint identification is switched on before the handshake and the session is checked again with the default
// HostnameVerifier after it: a certificate of another site must fail here, or the token goes to whoever presents one. Dropbox replaces a file as a whole (files/upload, overwrite).
// When the cloud's save could not be read at the game's first save mount, the game waits there and a dialog asks whether to play on with this device's save or to close
// (front_save_sync_check; DM_TEST_SYNC_FAIL=1 in env.txt makes the read fail, to see it). The APK has no code of its own for a click listener: each button posts a Message to a
// Handler of a thread that never runs its Looper, and that thread polls hasMessages.
#include "save_sync.h"
#include <errno.h>
#include <fcntl.h>
#include <android/looper.h>
#include <android/native_activity.h>
#include <jni.h>
#include <limits.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define DLOG(...) do { fprintf(stderr, "[dm] save sync: " __VA_ARGS__); fputc('\n', stderr); } while (0)
#define DRED(...) do { fprintf(stderr, "[dm] [RED] save sync: " __VA_ARGS__); fputc('\n', stderr); } while (0)
enum { CONNECT_MS = 5000, READ_MS = 15000, IOBUF = 16384 };
static const char API_HOST[] = "api.dropboxapi.com", CONTENT_HOST[] = "content.dropboxapi.com";

static JavaVM *g_vm;
static ANativeActivity *g_act;
static ALooper *g_main;   // the activity's main thread, where a dialog is made
static _Atomic int g_first_read;   // the first read of the cloud's save: 0 not started, 1 under way, 2 answered, 3 failed
static char g_conf[PATH_MAX];
static pthread_mutex_t g_token_mu = PTHREAD_MUTEX_INITIALIZER;
static char g_token[2048];   // the short-lived access token
static time_t g_token_until;

typedef struct { char app_key[128], refresh[512], folder[512]; } box_t;

// ---------- the settings ----------
static int conf_read(const char *path, box_t *d) {   // 0 on success. Never prints a line: it may hold the token
    memset(d, 0, sizeof *d); strcpy(d->folder, "/Saves");
    FILE *f = fopen(path, "r"); if (!f) return -1;
    char ln[1536]; int r = 0;
    while (!r && fgets(ln, sizeof ln, f)) {
        ln[strcspn(ln, "\r\n")] = 0;
        char *s = ln; while (*s == ' ' || *s == '\t') s++;
        char *v = *s == '#' ? NULL : strchr(s, '='); if (!v) continue;
        *v++ = 0;
        for (const char *c = v; *c; c++) if ((unsigned char)*c <= 0x20 || *c == 0x7f || *c == '"' || *c == '\\' || *c == '&') r = -1;
        if (r) break;
        char *to = !strcmp(s, "app_key") ? d->app_key : !strcmp(s, "refresh_token") ? d->refresh : !strcmp(s, "folder") ? d->folder : NULL;
        size_t cap = to == d->app_key ? sizeof d->app_key : to == d->refresh ? sizeof d->refresh : sizeof d->folder;
        if (to) { if (strlen(v) >= cap) r = -1; else strcpy(to, v); }
    }
    fclose(f); memset(ln, 0, sizeof ln);
    size_t L = strlen(d->folder); while (L && d->folder[L - 1] == '/') d->folder[--L] = 0;
    return r || !d->app_key[0] || !d->refresh[0] || (L && d->folder[0] != '/') ? -1 : 0;
}

// The settings file in the files folder, checked and moved to the private one (written there first: a start that dies halfway loses nothing)
static void conf_take(const char *files) {
    char from[PATH_MAX], tmp[PATH_MAX + 8]; box_t d; snprintf(from, sizeof from, "%s/dropbox.txt", files);
    if (access(from, F_OK)) return;
    if (conf_read(from, &d)) { memset(&d, 0, sizeof d); DRED("%s has no app_key and refresh_token (or a bad value): it was not taken", from); return; }
    memset(&d, 0, sizeof d);
    snprintf(tmp, sizeof tmp, "%s.new", g_conf);
    int in = open(from, O_RDONLY), out = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0600), r = in < 0 || out < 0;
    char b[4096]; ssize_t k;
    while (!r && (k = read(in, b, sizeof b)) != 0) { if (k < 0 || write(out, b, (size_t)k) != k) r = 1; }
    if (out >= 0 && (fsync(out) || close(out))) r = 1;
    if (in >= 0) close(in);
    memset(b, 0, sizeof b);
    if (r || rename(tmp, g_conf)) { unlink(tmp); DRED("cannot store the Dropbox settings (errno=%d)", errno); return; }
    if (unlink(from)) DRED("the settings are stored, but %s cannot be removed (errno=%d): remove it, it holds the token", from, errno);
    DLOG("Dropbox settings taken in");
}

// front_android.c, before env.txt is read (its DM_SAVE_SYNC=0 wins). files: the external files folder, internal: internalDataPath (<data>/files)
void save_sync_android_start(ANativeActivity *act, const char *files, const char *internal) {
    g_act = act; g_vm = act->vm;
    char dir[PATH_MAX]; snprintf(dir, sizeof dir, "%s", internal);
    char *up = strrchr(dir, '/'); if (!up) return;
    snprintf(up, sizeof dir - (size_t)(up - dir), "/no_backup"); mkdir(dir, 0700);
    if (snprintf(g_conf, sizeof g_conf, "%s/dropbox.conf", dir) >= (int)sizeof g_conf) { g_conf[0] = 0; return; }
    conf_take(files);
    if (access(g_conf, F_OK)) return;
    char cloud[PATH_MAX]; snprintf(cloud, sizeof cloud, "%s/cloud", internal); setenv("DM_SAVE_SYNC", cloud, 0);
    if ((g_main = ALooper_forThread())) ALooper_acquire(g_main);
}

// ---------- JNI: a TLS socket and its streams ----------
typedef struct { JNIEnv *E; jobject sock, in, out; jbyteArray jb; jmethodID mRead, mWrite, mFlush; uint8_t buf[IOBUF]; int pos, len, framed; } conn_t;

static int jfail(JNIEnv *E, const char *what) {   // a Java exception pending: logged by its toString (no settings in it) and cleared. 1 when there was one
    if (!(*E)->ExceptionCheck(E)) return 0;
    jthrowable t = (*E)->ExceptionOccurred(E); (*E)->ExceptionClear(E);
    jclass c = (*E)->FindClass(E, "java/lang/Object"); jmethodID m = c ? (*E)->GetMethodID(E, c, "toString", "()Ljava/lang/String;") : NULL;
    jstring s = m && t ? (jstring)(*E)->CallObjectMethod(E, t, m) : NULL;
    if ((*E)->ExceptionCheck(E)) { (*E)->ExceptionClear(E); s = NULL; }
    const char *u = s ? (*E)->GetStringUTFChars(E, s, NULL) : NULL;
    DRED("%s: %s", what, u ? u : "(a Java exception)");
    if (u) (*E)->ReleaseStringUTFChars(E, s, u);
    return 1;
}

static void conn_close(conn_t *c) {
    JNIEnv *E = c->E;
    if (c->sock) {
        jclass k = (*E)->FindClass(E, "java/net/Socket"); jmethodID m = k ? (*E)->GetMethodID(E, k, "close", "()V") : NULL;
        if (m) (*E)->CallVoidMethod(E, c->sock, m);
        if ((*E)->ExceptionCheck(E)) (*E)->ExceptionClear(E);
    }
    if (c->framed) (*E)->PopLocalFrame(E, NULL);
    c->sock = c->in = c->out = NULL; c->framed = 0;
}

// Wraps a connected socket in TLS, with the host name checked twice (see the top). 0 on success
static int tls_wrap(conn_t *c, const char *hostname, jstring host, int port) {
    JNIEnv *E = c->E;
    jclass cF = (*E)->FindClass(E, "javax/net/ssl/SSLSocketFactory"), cS = (*E)->FindClass(E, "javax/net/ssl/SSLSocket"), cP = (*E)->FindClass(E, "javax/net/ssl/SSLParameters");
    jclass cH = (*E)->FindClass(E, "javax/net/ssl/HttpsURLConnection"), cV = (*E)->FindClass(E, "javax/net/ssl/HostnameVerifier");
    jclass cN = (*E)->FindClass(E, "javax/net/ssl/SNIHostName"), cC = (*E)->FindClass(E, "java/util/Collections");
    if (jfail(E, "TLS classes")) return -1;
    jobject f = (*E)->CallStaticObjectMethod(E, cF, (*E)->GetStaticMethodID(E, cF, "getDefault", "()Ljavax/net/SocketFactory;"));
    if (jfail(E, "the TLS socket factory")) return -1;
    jobject s = (*E)->CallObjectMethod(E, f, (*E)->GetMethodID(E, cF, "createSocket", "(Ljava/net/Socket;Ljava/lang/String;IZ)Ljava/net/Socket;"), c->sock, host, port, JNI_TRUE);
    if (jfail(E, "the TLS socket")) return -1;
    c->sock = s;
    jobject p = (*E)->CallObjectMethod(E, s, (*E)->GetMethodID(E, cS, "getSSLParameters", "()Ljavax/net/ssl/SSLParameters;"));
    if (jfail(E, "the TLS parameters")) return -1;
    (*E)->CallVoidMethod(E, p, (*E)->GetMethodID(E, cP, "setEndpointIdentificationAlgorithm", "(Ljava/lang/String;)V"), (*E)->NewStringUTF(E, "HTTPS"));
    jobject sni = (*E)->NewObject(E, cN, (*E)->GetMethodID(E, cN, "<init>", "(Ljava/lang/String;)V"), host);
    jobject l = sni ? (*E)->CallStaticObjectMethod(E, cC, (*E)->GetStaticMethodID(E, cC, "singletonList", "(Ljava/lang/Object;)Ljava/util/List;"), sni) : NULL;
    if (l) (*E)->CallVoidMethod(E, p, (*E)->GetMethodID(E, cP, "setServerNames", "(Ljava/util/List;)V"), l);
    (*E)->CallVoidMethod(E, s, (*E)->GetMethodID(E, cS, "setSSLParameters", "(Ljavax/net/ssl/SSLParameters;)V"), p);
    if (jfail(E, "setting the TLS parameters")) return -1;
    (*E)->CallVoidMethod(E, s, (*E)->GetMethodID(E, cS, "startHandshake", "()V"));
    if (jfail(E, "the TLS handshake (a certificate of another host, or one Android does not trust, ends here)")) return -1;
    jobject ses = (*E)->CallObjectMethod(E, s, (*E)->GetMethodID(E, cS, "getSession", "()Ljavax/net/ssl/SSLSession;"));
    jobject v = (*E)->CallStaticObjectMethod(E, cH, (*E)->GetStaticMethodID(E, cH, "getDefaultHostnameVerifier", "()Ljavax/net/ssl/HostnameVerifier;"));
    if (jfail(E, "the host name verifier")) return -1;
    jboolean ok = (*E)->CallBooleanMethod(E, v, (*E)->GetMethodID(E, cV, "verify", "(Ljava/lang/String;Ljavax/net/ssl/SSLSession;)Z"), host, ses);
    if (jfail(E, "checking the host name")) return -1;
    if (!ok) { DRED("the certificate of %s is not for that name: nothing was sent", hostname); return -1; }
    return 0;
}

static int conn_open(conn_t *c, JNIEnv *E, const char *hostname) {   // 0 on success; on failure conn_close has run
    c->E = E; c->sock = c->in = c->out = NULL; c->pos = c->len = 0; c->framed = 0;
    if ((*E)->PushLocalFrame(E, 64)) { (*E)->ExceptionClear(E); return -1; }
    c->framed = 1;
    jclass cS = (*E)->FindClass(E, "java/net/Socket"), cA = (*E)->FindClass(E, "java/net/InetSocketAddress");
    jclass cI = (*E)->FindClass(E, "java/io/InputStream"), cO = (*E)->FindClass(E, "java/io/OutputStream");
    if (jfail(E, "socket classes")) goto fail;
    jstring host = (*E)->NewStringUTF(E, hostname);
    jobject addr = (*E)->NewObject(E, cA, (*E)->GetMethodID(E, cA, "<init>", "(Ljava/lang/String;I)V"), host, 443);
    c->sock = (*E)->NewObject(E, cS, (*E)->GetMethodID(E, cS, "<init>", "()V"));
    if (jfail(E, "making a socket")) goto fail;
    (*E)->CallVoidMethod(E, c->sock, (*E)->GetMethodID(E, cS, "setSoTimeout", "(I)V"), READ_MS);
    (*E)->CallVoidMethod(E, c->sock, (*E)->GetMethodID(E, cS, "connect", "(Ljava/net/SocketAddress;I)V"), addr, CONNECT_MS);
    if (jfail(E, "connecting to Dropbox")) goto fail;
    if (tls_wrap(c, hostname, host, 443)) goto fail;
    c->in = (*E)->CallObjectMethod(E, c->sock, (*E)->GetMethodID(E, cS, "getInputStream", "()Ljava/io/InputStream;"));
    c->out = (*E)->CallObjectMethod(E, c->sock, (*E)->GetMethodID(E, cS, "getOutputStream", "()Ljava/io/OutputStream;"));
    c->mRead = (*E)->GetMethodID(E, cI, "read", "([BII)I"); c->mWrite = (*E)->GetMethodID(E, cO, "write", "([BII)V"); c->mFlush = (*E)->GetMethodID(E, cO, "flush", "()V");
    c->jb = (*E)->NewByteArray(E, IOBUF);
    if (jfail(E, "the socket's streams")) goto fail;
    return 0;
fail:
    conn_close(c); return -1;
}

static int conn_write(conn_t *c, const void *b, size_t n) {
    JNIEnv *E = c->E;
    for (const jbyte *p = b; n; ) {
        jsize k = n > IOBUF ? IOBUF : (jsize)n;
        (*E)->SetByteArrayRegion(E, c->jb, 0, k, p); (*E)->CallVoidMethod(E, c->out, c->mWrite, c->jb, 0, k);
        if (jfail(E, "sending to Dropbox")) return -1;
        p += k; n -= (size_t)k;
    }
    return 0;
}

static int conn_fill(conn_t *c) {   // 1 data in the buffer, 0 the end of the stream, -1 failed
    JNIEnv *E = c->E;
    jint k = (*E)->CallIntMethod(E, c->in, c->mRead, c->jb, 0, IOBUF);
    if (jfail(E, "reading from Dropbox")) return -1;
    if (k <= 0) return 0;
    (*E)->GetByteArrayRegion(E, c->jb, 0, k, (jbyte *)c->buf); c->pos = 0; c->len = k;
    return 1;
}

static int conn_line(conn_t *c, char *out, size_t n) {   // a line without its CR LF; 0 on success
    size_t L = 0;
    for (;;) {
        if (c->pos == c->len && conn_fill(c) <= 0) return -1;
        char ch = (char)c->buf[c->pos++];
        if (ch == '\n') break;
        if (L + 1 < n) out[L++] = ch;
    }
    if (L && out[L - 1] == '\r') L--;
    out[L] = 0; return 0;
}

// ---------- HTTP ----------
typedef struct { int status, chunked; long long length; } resp_t;
typedef struct { int fd; char *mem; size_t cap, used; } sink_t;   // where a body goes: a file (fd >= 0), else memory, the rest dropped (always ended with a 0)

// Sends one POST (body: memory, or a file of body_len bytes) and reads the answer's head. headers: lines with their CR LF. 0 on success
static int post(conn_t *c, const char *host, const char *path, const char *headers, const void *mem, int fd, long long body_len, resp_t *r) {
    char head[4096];
    int hn = snprintf(head, sizeof head, "POST %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: nx2android\r\nConnection: close\r\nContent-Length: %lld\r\n%s\r\n", path, host, body_len, headers);
    int bad = hn < 0 || hn >= (int)sizeof head || conn_write(c, head, (size_t)hn);
    memset(head, 0, sizeof head);
    if (bad) return -1;
    if (mem && conn_write(c, mem, (size_t)body_len)) return -1;
    if (fd >= 0) {
        long long left = body_len; ssize_t k;
        while (left > 0 && (k = read(fd, c->buf, (size_t)(left > IOBUF ? IOBUF : left))) != 0) {
            if (k < 0) { if (errno == EINTR) continue; return -1; }
            if (conn_write(c, c->buf, (size_t)k)) return -1;
            left -= k;
        }
        if (left) return -1;   // the file came out shorter than it said
    }
    c->pos = c->len = 0;
    (*c->E)->CallVoidMethod(c->E, c->out, c->mFlush);
    if (jfail(c->E, "sending to Dropbox")) return -1;
    char ln[2048];
    for (;;) {   // the status line; a 1xx comes before the real answer
        if (conn_line(c, ln, sizeof ln) || sscanf(ln, "HTTP/%*d.%*d %d", &r->status) != 1) return -1;
        r->chunked = 0; r->length = -1;
        for (;;) {
            if (conn_line(c, ln, sizeof ln)) return -1;
            if (!ln[0]) break;
            char *v = strchr(ln, ':'); if (!v) continue;
            *v++ = 0; while (*v == ' ' || *v == '\t') v++;
            if (!strcasecmp(ln, "content-length")) r->length = atoll(v);
            else if (!strcasecmp(ln, "transfer-encoding")) r->chunked = !strcasecmp(v, "chunked");
        }
        if (r->status >= 200) return 0;
    }
}

static int body_part(conn_t *c, sink_t *s, long long n) {   // n bytes of the body to the sink
    while (n > 0) {
        if (c->pos == c->len && conn_fill(c) <= 0) return -1;
        int k = c->len - c->pos; if (k > n) k = (int)n;
        if (s->fd >= 0) { for (int o = 0; o < k; ) { ssize_t w = write(s->fd, c->buf + c->pos + o, (size_t)(k - o)); if (w < 0) { if (errno == EINTR) continue; return -1; } o += (int)w; } }
        else if (s->mem) { size_t m = s->cap - 1 - s->used; if (m > (size_t)k) m = (size_t)k; memcpy(s->mem + s->used, c->buf + c->pos, m); s->used += m; s->mem[s->used] = 0; }
        c->pos += k; n -= k;
    }
    return 0;
}

static int read_body(conn_t *c, const resp_t *r, sink_t *s) {   // the whole body: cut off anywhere is a failure, except a body with no length, which ends with the connection
    if (r->chunked) {
        char ln[128];
        for (;;) {
            if (conn_line(c, ln, sizeof ln)) return -1;
            char *e; long long n = strtoll(ln, &e, 16);
            if (e == ln || n < 0) return -1;
            if (!n) return 0;   // trailers do not matter: the connection closes
            if (body_part(c, s, n) || conn_line(c, ln, sizeof ln) || ln[0]) return -1;
        }
    }
    if (r->length >= 0) return body_part(c, s, r->length);
    for (;;) {
        if (c->pos == c->len) { int f = conn_fill(c); if (f < 0) return -1; if (!f) return 0; }
        if (body_part(c, s, c->len - c->pos)) return -1;
    }
}

// ---------- Dropbox ----------
static int token_of(JNIEnv *E, const box_t *d, char *out, size_t n) {   // a valid access token, asked for when the last one is near its end. 0 on success
    pthread_mutex_lock(&g_token_mu);
    int r = -1;
    if (g_token[0] && time(NULL) + 60 < g_token_until) { snprintf(out, n, "%s", g_token); pthread_mutex_unlock(&g_token_mu); return 0; }
    conn_t c; resp_t rs; char body[800], *ans = malloc(8192);
    int bn = snprintf(body, sizeof body, "grant_type=refresh_token&refresh_token=%s&client_id=%s", d->refresh, d->app_key);
    if (ans && !conn_open(&c, E, API_HOST)) {
        ans[0] = 0; sink_t s = {.fd = -1, .mem = ans, .cap = 8192};
        if (!post(&c, API_HOST, "/oauth2/token", "Content-Type: application/x-www-form-urlencoded\r\n", body, -1, bn, &rs) && !read_body(&c, &rs, &s)) {
            char *t = rs.status == 200 ? strstr(ans, "\"access_token\"") : NULL, *e = strstr(ans, "\"expires_in\"");
            if (t && (t = strchr(t + 14, '"'))) {
                size_t L = strcspn(++t, "\"");
                if (L && L < sizeof g_token) { memcpy(g_token, t, L); g_token[L] = 0; g_token_until = time(NULL) + (e && (e = strchr(e, ':')) ? atol(e + 1) : 3600); snprintf(out, n, "%s", g_token); r = 0; }
            }
            if (r) DRED("Dropbox refused the refresh token (HTTP %d): sign in again", rs.status);
        }
        conn_close(&c);
    }
    if (ans) { memset(ans, 0, 8192); free(ans); }
    memset(body, 0, sizeof body);
    pthread_mutex_unlock(&g_token_mu);
    return r;
}

static void token_drop(void) { pthread_mutex_lock(&g_token_mu); g_token[0] = 0; g_token_until = 0; pthread_mutex_unlock(&g_token_mu); }

static int tail_of(const char *cloud, char *out, size_t n) {   // the end of the local path: /<title ID>/save.dmsync. 0 on success
    const char *a = strrchr(cloud, '/'); if (!a || a == cloud) return -1;
    while (--a > cloud && *a != '/') {}
    if (*a != '/' || strlen(a) >= n) return -1;
    for (const char *p = a; *p; p++) if (!strchr("/-_.", *p) && !(*p >= '0' && *p <= '9') && !(*p >= 'A' && *p <= 'Z') && !(*p >= 'a' && *p <= 'z')) return -1;
    strcpy(out, a); return 0;
}

static JNIEnv *jni_on(int *attached) {
    JNIEnv *E = NULL; *attached = 0;
    if (!g_vm) return NULL;
    if ((*g_vm)->GetEnv(g_vm, (void **)&E, JNI_VERSION_1_6) == JNI_OK) return E;
    if ((*g_vm)->AttachCurrentThread(g_vm, &E, NULL) != JNI_OK) { DRED("cannot attach to the Java VM"); return NULL; }
    *attached = 1; return E;
}
static void jni_off(int attached) { if (attached) (*g_vm)->DetachCurrentThread(g_vm); }

// ---------- the two calls save_sync.c makes ----------
// 1 = copied, 0 = there is none, -1 = failed
static int fetch_(const char *cloud, const char *local);
int front_save_sync_fetch(const char *cloud, const char *local) {
    int expect = 0, first = atomic_compare_exchange_strong(&g_first_read, &expect, 1);
    const char *t = getenv("DM_TEST_SYNC_FAIL");
    int r = t && *t == '1' ? -1 : fetch_(cloud, local);
    if (first) atomic_store(&g_first_read, r < 0 ? 3 : 2);
    return r;
}
static int fetch_(const char *cloud, const char *local) {
    box_t d; char tail[128], token[2048], headers[3000]; int attached, result = -1;
    if (conf_read(g_conf, &d) || tail_of(cloud, tail, sizeof tail)) { DRED("no usable Dropbox settings (dropbox.txt in the files folder)"); return -1; }
    JNIEnv *E = jni_on(&attached);
    for (int attempt = 0; E && attempt < 2; attempt++) {
        if (token_of(E, &d, token, sizeof token)) break;
        snprintf(headers, sizeof headers, "Authorization: Bearer %s\r\nDropbox-API-Arg: {\"path\":\"%s%s\"}\r\n", token, d.folder, tail);
        conn_t c; resp_t r; char err[1024] = ""; sink_t none = {.fd = -1, .mem = err, .cap = sizeof err};
        if (conn_open(&c, E, CONTENT_HOST)) break;
        if (post(&c, CONTENT_HOST, "/2/files/download", headers, NULL, -1, 0, &r)) { conn_close(&c); DRED("the request for the cloud's save failed"); break; }
        if (r.status == 200) {
            int fd = open(local, O_WRONLY | O_CREAT | O_TRUNC, 0600); sink_t s = {.fd = fd};
            int bad = fd < 0 || read_body(&c, &r, &s); conn_close(&c);
            if (fd >= 0 && close(fd)) bad = 1;
            if (bad) { unlink(local); DRED("the cloud's save was cut off"); break; }
            result = 1; break;
        }
        read_body(&c, &r, &none); conn_close(&c);
        if (r.status == 409 && strstr(err, "not_found")) { result = 0; break; }
        if (r.status == 401 && !attempt) { token_drop(); continue; }
        DRED("reading the cloud's save: HTTP %d from Dropbox", r.status); break;
    }
    if (E) jni_off(attached);
    memset(&d, 0, sizeof d); memset(token, 0, sizeof token); memset(headers, 0, sizeof headers);
    return result;
}

// 1 = done
int front_save_sync_store(const char *local, const char *cloud) {
    box_t d; char tail[128], token[2048], headers[3000]; int attached = 0, ok = 0;
    if (conf_read(g_conf, &d) || tail_of(cloud, tail, sizeof tail)) return 0;
    int fd = open(local, O_RDONLY); struct stat st;
    JNIEnv *E = fd >= 0 && !fstat(fd, &st) ? jni_on(&attached) : NULL;
    for (int attempt = 0; E && attempt < 2; attempt++) {
        if (token_of(E, &d, token, sizeof token) || lseek(fd, 0, SEEK_SET)) break;
        snprintf(headers, sizeof headers, "Authorization: Bearer %s\r\nContent-Type: application/octet-stream\r\nDropbox-API-Arg: {\"path\":\"%s%s\",\"mode\":\"overwrite\",\"mute\":true}\r\n", token, d.folder, tail);
        conn_t c; resp_t r; sink_t none = {.fd = -1};
        if (conn_open(&c, E, CONTENT_HOST)) break;
        int bad = post(&c, CONTENT_HOST, "/2/files/upload", headers, NULL, fd, st.st_size, &r);
        if (!bad) read_body(&c, &r, &none);
        conn_close(&c);
        if (bad) { DRED("sending the save failed"); break; }
        if (r.status == 200) { ok = 1; break; }
        if (r.status == 401 && !attempt) { token_drop(); continue; }
        DRED("sending the save: HTTP %d from Dropbox", r.status); break;
    }
    if (E) jni_off(attached);
    if (fd >= 0) close(fd);
    memset(&d, 0, sizeof d); memset(token, 0, sizeof token); memset(headers, 0, sizeof headers);
    return ok;
}

// ---------- the question when the cloud's save cannot be read ----------
enum { ANS_CONTINUE = 1, ANS_CLOSE = 2 };
static jobject g_ask_handler;   // the waiting thread's Handler, a global reference
static int g_ask_pipe[2];
static _Atomic int g_ask_shown;   // 1 the dialog is up, -1 it could not be made

static int ask_on_main(int fd, int events, void *data) {   // on the activity's main thread, once
    (void)events; (void)data; char b; if (read(fd, &b, 1) < 0) b = 0;
    JNIEnv *E = g_act->env;
    jclass cB = (*E)->FindClass(E, "android/app/AlertDialog$Builder"), cD = (*E)->FindClass(E, "android/app/AlertDialog"), cM = (*E)->FindClass(E, "android/os/Message");
    jobject bu = (*E)->NewObject(E, cB, (*E)->GetMethodID(E, cB, "<init>", "(Landroid/content/Context;)V"), g_act->clazz);
    (*E)->CallObjectMethod(E, bu, (*E)->GetMethodID(E, cB, "setTitle", "(Ljava/lang/CharSequence;)Landroid/app/AlertDialog$Builder;"), (*E)->NewStringUTF(E, "Cloud save not available"));
    (*E)->CallObjectMethod(E, bu, (*E)->GetMethodID(E, cB, "setMessage", "(Ljava/lang/CharSequence;)Landroid/app/AlertDialog$Builder;"),
                           (*E)->NewStringUTF(E, "The save could not be read from Dropbox. Play on with the save on this device? If you save, it may replace a newer save from another device."));
    (*E)->CallObjectMethod(E, bu, (*E)->GetMethodID(E, cB, "setCancelable", "(Z)Landroid/app/AlertDialog$Builder;"), JNI_FALSE);
    jobject d = (*E)->CallObjectMethod(E, bu, (*E)->GetMethodID(E, cB, "create", "()Landroid/app/AlertDialog;"));
    jmethodID obtain = (*E)->GetStaticMethodID(E, cM, "obtain", "(Landroid/os/Handler;I)Landroid/os/Message;"), set = (*E)->GetMethodID(E, cD, "setButton", "(ILjava/lang/CharSequence;Landroid/os/Message;)V");
    (*E)->CallVoidMethod(E, d, set, -1, (*E)->NewStringUTF(E, "Play on"), (*E)->CallStaticObjectMethod(E, cM, obtain, g_ask_handler, ANS_CONTINUE));   // BUTTON_POSITIVE
    (*E)->CallVoidMethod(E, d, set, -2, (*E)->NewStringUTF(E, "Close the game"), (*E)->CallStaticObjectMethod(E, cM, obtain, g_ask_handler, ANS_CLOSE));   // BUTTON_NEGATIVE
    (*E)->CallVoidMethod(E, d, (*E)->GetMethodID(E, cD, "show", "()V"));
    atomic_store(&g_ask_shown, jfail(E, "showing the question") ? -1 : 1);
    return 0;   // the callback once
}

static void *ask_thread(void *arg) {   // the answer through arg; "play on" when no dialog can be shown: the game must not hang on it
    int *ans = arg, attached; *ans = ANS_CONTINUE;
    JNIEnv *E = jni_on(&attached); if (!E) return NULL;
    jclass cL = (*E)->FindClass(E, "android/os/Looper"), cH = (*E)->FindClass(E, "android/os/Handler");
    (*E)->CallStaticVoidMethod(E, cL, (*E)->GetStaticMethodID(E, cL, "prepare", "()V"));
    jobject lo = (*E)->CallStaticObjectMethod(E, cL, (*E)->GetStaticMethodID(E, cL, "myLooper", "()Landroid/os/Looper;"));
    jobject h = jfail(E, "preparing the question") ? NULL : (*E)->NewObject(E, cH, (*E)->GetMethodID(E, cH, "<init>", "(Landroid/os/Looper;)V"), lo);
    if (h && !jfail(E, "preparing the question") && !pipe(g_ask_pipe)) {
        g_ask_handler = (*E)->NewGlobalRef(E, h);
        jmethodID has = (*E)->GetMethodID(E, cH, "hasMessages", "(I)Z");
        ALooper_addFd(g_main, g_ask_pipe[0], ALOOPER_POLL_CALLBACK, ALOOPER_EVENT_INPUT, ask_on_main, NULL);
        if (write(g_ask_pipe[1], "x", 1) != 1) atomic_store(&g_ask_shown, -1);
        while (atomic_load(&g_ask_shown) >= 0) {
            if ((*E)->CallBooleanMethod(E, h, has, ANS_CLOSE)) { *ans = ANS_CLOSE; break; }
            if ((*E)->CallBooleanMethod(E, h, has, ANS_CONTINUE)) break;
            usleep(100000);
        }
    }
    jfail(E, "the question");
    jni_off(attached);
    return NULL;
}

// hle_fs.c, after the game's first save mount brought the cloud's save in or could not: the game waits here for the answer. Asks once
void front_save_sync_check(void) {
    int st = atomic_load(&g_first_read); static _Atomic int asked;
    if ((st != 1 && st != 3) || !g_main || atomic_exchange(&asked, 1)) return;
    DLOG("the cloud's save cannot be read: asking");
    int ans = ANS_CONTINUE; pthread_t t;
    if (pthread_create(&t, NULL, ask_thread, &ans)) { DRED("no thread for the question: playing on"); return; }
    pthread_join(t, NULL);
    DLOG("the player chose: %s", ans == ANS_CLOSE ? "close the game" : "play on with this device's save");
    if (ans == ANS_CLOSE) _exit(0);
}
