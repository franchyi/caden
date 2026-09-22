#define _GNU_SOURCE
#define _DARWIN_C_SOURCE
#define _POSIX_C_SOURCE 200809L
#include "nbd_transport.h"
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

enum { READ = 0, WRITE = 1, DISC = 2, FLUSH = 3, TRIM = 4 };
static const unsigned char cookie[8] = {0x80, 0xff, 0x00, 0x7f, 0x13, 0xab, 0x45, 0x01};
typedef struct {
    char path[64];
    int fd;
    size_t guard, capacity, logical;
    cs_store *store;
} fixture;
typedef struct {
    cs_store *store;
    int fd, result, error;
    cs_nbd_options options;
    cs_nbd_stats stats;
    pthread_t thread;
} server;

static void put32(unsigned char *p, uint32_t value) {
    p[0] = (unsigned char)(value >> 24); p[1] = (unsigned char)(value >> 16);
    p[2] = (unsigned char)(value >> 8); p[3] = (unsigned char)value;
}
static uint32_t get32(const unsigned char *p) {
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 |
           (uint32_t)p[2] << 8 | p[3];
}
static void encode_request(unsigned char *header, uint32_t type, uint64_t offset, uint32_t length) {
    put32(header, UINT32_C(0x25609513)); put32(header + 4, type);
    memcpy(header + 8, cookie, sizeof(cookie));
    put32(header + 16, (uint32_t)(offset >> 32));
    put32(header + 20, (uint32_t)offset); put32(header + 24, length);
}
static void send_chunks(int fd, const void *data, size_t length, size_t chunk) {
    const unsigned char *p = data;
    while (length) {
        size_t take = length < chunk ? length : chunk;
        ssize_t n = send(fd, p, take, 0);
        if (n < 0 && errno == EINTR) continue;
        assert(n > 0);
        p += n; length -= (size_t)n;
    }
}
static void receive_chunks(int fd, void *data, size_t length, size_t chunk) {
    unsigned char *p = data;
    while (length) {
        size_t take = length < chunk ? length : chunk;
        ssize_t n = recv(fd, p, take, 0);
        if (n < 0 && errno == EINTR) continue;
        assert(n > 0);
        p += n; length -= (size_t)n;
    }
}
static void request(int fd, uint32_t type, uint64_t offset, uint32_t length) {
    unsigned char header[28];
    encode_request(header, type, offset, length);
    send_chunks(fd, header, sizeof(header), 1);
}
static void reply(int fd, uint32_t error) {
    unsigned char header[16];
    receive_chunks(fd, header, sizeof(header), 3);
    assert(get32(header) == UINT32_C(0x67446698));
    assert(get32(header + 4) == error);
    assert(!memcmp(header + 8, cookie, sizeof(cookie)));
}
static void fixture_open(fixture *f, cs_codec codec) {
    memset(f, 0, sizeof(*f));
    strcpy(f->path, "/tmp/crate-nbd-test-XXXXXX");
    f->fd = mkstemp(f->path); assert(f->fd >= 0);
    long page = sysconf(_SC_PAGESIZE); assert(page > 0);
    f->guard = (size_t)page;
    f->capacity = 1u << 20;
    f->logical = f->capacity - CS_PAGE;
    assert(!ftruncate(f->fd, (off_t)(2 * f->guard + f->capacity)));
    unsigned char mark[512]; memset(mark, 0xb6, sizeof(mark));
    assert(pwrite(f->fd, mark, sizeof(mark), 0) == (ssize_t)sizeof(mark));
    assert(pwrite(f->fd, mark, sizeof(mark), (off_t)(f->guard + f->capacity)) == (ssize_t)sizeof(mark));
    cs_options options = {.path = f->path, .offset = f->guard, .capacity = f->capacity,
                          .logical_bytes = f->logical, .emulate_file = 1, .codec = codec};
    assert(!cs_open(&f->store, &options));
}
static void fixture_close(fixture *f) {
    cs_close(f->store);
    unsigned char mark[512];
    assert(pread(f->fd, mark, sizeof(mark), 0) == (ssize_t)sizeof(mark));
    for (size_t n = 0; n < sizeof(mark); n++) assert(mark[n] == 0xb6);
    assert(pread(f->fd, mark, sizeof(mark), (off_t)(f->guard + f->capacity)) == (ssize_t)sizeof(mark));
    for (size_t n = 0; n < sizeof(mark); n++) assert(mark[n] == 0xb6);
    assert(!close(f->fd)); assert(!unlink(f->path));
}
static void *serve_thread(void *opaque) {
    server *s = opaque;
    s->result = cs_nbd_serve(s->store, s->fd, &s->options, &s->stats);
    s->error = errno;
    return NULL;
}
static int start_server_options(server *s, cs_store *store, uint32_t maximum,
                               uint32_t timeout, uint32_t idle_timeout, int lock_io) {
    memset(s, 0, sizeof(*s));
    int sockets[2]; assert(!socketpair(AF_UNIX, SOCK_STREAM, 0, sockets));
    s->fd = sockets[0]; s->store = store;
    s->options.max_request_bytes = maximum;
    s->options.request_timeout_ms = timeout;
    s->options.idle_timeout_ms = idle_timeout;
    s->options.lock_io_buffer = lock_io;
    int small = 1024;
    assert(!setsockopt(s->fd, SOL_SOCKET, SO_SNDBUF, &small, sizeof(small)));
    assert(!setsockopt(s->fd, SOL_SOCKET, SO_RCVBUF, &small, sizeof(small)));
    struct timeval limit = {.tv_sec = 3};
    assert(!setsockopt(sockets[1], SOL_SOCKET, SO_RCVTIMEO, &limit, sizeof(limit)));
    assert(!setsockopt(sockets[1], SOL_SOCKET, SO_SNDTIMEO, &limit, sizeof(limit)));
    assert(!pthread_create(&s->thread, NULL, serve_thread, s));
    return sockets[1];
}
static int start_server_with_idle(server *s, cs_store *store, uint32_t maximum,
                                  uint32_t timeout, uint32_t idle_timeout) {
    return start_server_options(s, store, maximum, timeout, idle_timeout, 0);
}
static int start_server(server *s, cs_store *store, uint32_t maximum, uint32_t timeout) {
    return start_server_with_idle(s, store, maximum, timeout, 0);
}
static void finish_server(server *s, int client, int result, int error) {
    assert(!pthread_join(s->thread, NULL));
    assert(s->result == result);
    if (error && s->error != error)
        fprintf(stderr, "server error=%d expected=%d requests=%llu\n", s->error, error, (unsigned long long)s->stats.requests);
    if (error) assert(s->error == error);
    assert(!close(s->fd)); /* Descriptor ownership remains with the caller. */
    unsigned char byte;
    ssize_t n = recv(client, &byte, 1, 0);
    if (!(n == 0 || (n < 0 && errno == ECONNRESET)))
        fprintf(stderr, "unexpected close result=%d error=%d recv=%zd errno=%d byte=%u\n", s->result, s->error, n, errno, (unsigned)byte);
    assert(n == 0 || (n < 0 && errno == ECONNRESET));
    assert(!close(client));
}

static void test_round_trip(fixture *f) {
    server s; int client = start_server(&s, f->store, 65536, 2000);
    unsigned char source[65536], dest[65536];
    uint32_t random = 65173;
    for (size_t n = 0; n < sizeof(source); n++) {
        random ^= random << 13; random ^= random >> 17; random ^= random << 5;
        source[n] = (unsigned char)random;
    }
    request(client, READ, 0, sizeof(dest)); reply(client, 0);
    receive_chunks(client, dest, sizeof(dest), 23);
    for (size_t n = 0; n < sizeof(dest); n++) assert(!dest[n]);
    request(client, WRITE, 0, sizeof(source));
    send_chunks(client, source, sizeof(source), 17); reply(client, 0);
    request(client, READ, 0, sizeof(dest)); reply(client, 0);
    receive_chunks(client, dest, sizeof(dest), 23);
    assert(!memcmp(source, dest, sizeof(source)));
    /* Sector-aligned sub-page overwrite spans an internal 4-KiB boundary. */
    memset(source + 3584, 0x51, 1024);
    request(client, WRITE, 3584, 1024); send_chunks(client, source + 3584, 1024, 7); reply(client, 0);
    request(client, READ, 0, sizeof(dest)); reply(client, 0);
    receive_chunks(client, dest, sizeof(dest), 23);
    assert(!memcmp(source, dest, sizeof(source)));
    /* Unsupported FUA/unknown flags consume WRITE payload but never modify. */
    memset(dest, 0x19, 512);
    request(client, WRITE | (1u << 16), 0, 512); send_chunks(client, dest, 512, 11); reply(client, 22);
    request(client, WRITE | (1u << 31), 0, 512); send_chunks(client, dest, 512, 11); reply(client, 22);
    request(client, READ | (1u << 16), 0, 512); reply(client, 22);
    request(client, FLUSH, 0, 0); reply(client, 22);
    request(client, FLUSH, 512, 512); reply(client, 22);
    request(client, 0xffff, 0, 0); reply(client, 22);
    request(client, READ, 0, 512); reply(client, 0); receive_chunks(client, dest, 512, 13);
    assert(!memcmp(source, dest, 512));
    /* Invalid bounded writes are drained; a subsequent READ stays synchronized. */
    request(client, WRITE, f->logical, 512); send_chunks(client, dest, 512, 13); reply(client, 28);
    request(client, WRITE, 1, 512); send_chunks(client, dest, 512, 13); reply(client, 22);
    request(client, READ, UINT64_MAX - 511, 512); reply(client, 22);
    request(client, READ, f->logical, 512); reply(client, 22);
    request(client, READ, 0, 513); reply(client, 22);
    request(client, READ, 1, 512); reply(client, 22);
    request(client, TRIM, f->logical, 512); reply(client, 22);
    request(client, TRIM, 1, 512); reply(client, 22);
    /* TRIM [512, 8704) only discards [4096,8192), preserving edge pages. */
    request(client, TRIM, 512, 8192); reply(client, 0);
    memset(source + 4096, 0, 4096);
    request(client, READ, 0, sizeof(dest)); reply(client, 0);
    receive_chunks(client, dest, sizeof(dest), 23); assert(!memcmp(source, dest, sizeof(source)));
    request(client, TRIM, 512, 512); reply(client, 0); /* No complete page: legal no-op. */
    request(client, READ, f->logical, 0); reply(client, 0);
    request(client, WRITE, f->logical, 0); reply(client, 0);
    request(client, TRIM, f->logical, 0); reply(client, 0);
    request(client, DISC, 0, 0);
    finish_server(&s, client, 0, 0);
    assert(s.stats.requests == 27 && s.stats.error_replies == 14);
    assert(s.stats.read_bytes == 4 * sizeof(source) + 512);
    assert(s.stats.written_bytes == sizeof(source) + 1024);
    assert(s.stats.trims == 3 && !s.stats.protocol_errors && s.stats.io_buffer_bytes == 65536);
}

static void test_malformed_and_disconnect(fixture *f) {
    server s; unsigned char header[28], before[512], after[512];
    assert(!cs_read(f->store, 0, before, sizeof(before)));
    int client = start_server(&s, f->store, 512, 1000);
    encode_request(header, READ, 0, 512); header[0] ^= 1;
    send_chunks(client, header, sizeof(header), 2);
    finish_server(&s, client, -1, EPROTO); assert(s.stats.protocol_errors == 1);
    client = start_server(&s, f->store, 512, 1000);
    request(client, WRITE, 0, 1024); /* No payload drain on over-limit request. */
    finish_server(&s, client, -1, EMSGSIZE); assert(s.stats.protocol_errors == 1);
    client = start_server(&s, f->store, 512, 1000);
    request(client, READ, 0, UINT32_MAX);
    finish_server(&s, client, -1, EMSGSIZE);
    client = start_server(&s, f->store, 512, 1000);
    encode_request(header, READ, 0, 512); send_chunks(client, header, 7, 1);
    assert(!shutdown(client, SHUT_WR)); finish_server(&s, client, -1, EPROTO);
    client = start_server(&s, f->store, 512, 1000);
    request(client, WRITE, 0, 512); send_chunks(client, before, 255, 7);
    assert(!shutdown(client, SHUT_WR)); finish_server(&s, client, -1, EPROTO);
    assert(s.stats.protocol_errors == 1);
    assert(!cs_read(f->store, 0, after, sizeof(after))); assert(!memcmp(before, after, sizeof(before)));
    client = start_server(&s, f->store, 512, 1000);
    request(client, WRITE, 0, 512);
    assert(!shutdown(client, SHUT_WR)); finish_server(&s, client, -1, EPROTO);
    client = start_server(&s, f->store, 512, 1000);
    request(client, DISC, 512, 0); finish_server(&s, client, -1, EPROTO);
    client = start_server(&s, f->store, 512, 1000);
    request(client, DISC | (1u << 16), 0, 0); finish_server(&s, client, -1, EPROTO);
    client = start_server(&s, f->store, 512, 1000);
    request(client, DISC, 0, 512); finish_server(&s, client, -1, EPROTO);
    client = start_server(&s, f->store, 512, 1000);
    assert(!shutdown(client, SHUT_WR)); finish_server(&s, client, -1, ECONNRESET);
    client = start_server_with_idle(&s, f->store, 512, 1000, 40);
    finish_server(&s, client, -1, ETIMEDOUT);
    client = start_server(&s, f->store, 512, 40);
    request(client, WRITE, 0, 512); send_chunks(client, before, 31, 7);
    finish_server(&s, client, -1, ETIMEDOUT);
    assert(!cs_read(f->store, 0, after, sizeof(after))); assert(!memcmp(before, after, sizeof(before)));
    /* A client that never reads a bounded, large reply cannot block forever. */
    client = start_server(&s, f->store, 65536, 40);
    request(client, READ, 0, 65536);
    assert(!pthread_join(s.thread, NULL));
    assert(s.result == -1 && s.error == ETIMEDOUT);
    assert(!close(s.fd)); assert(!close(client));
    /* A peer that cannot receive must not kill the process with SIGPIPE. */
    client = start_server(&s, f->store, 512, 1000);
    assert(!shutdown(client, SHUT_RD)); request(client, READ, 0, 512);
    finish_server(&s, client, -1, 0);
    assert(s.error == EPIPE || s.error == ECONNRESET || s.error == ETIMEDOUT);
}

static void test_idle_and_partial_header_deadlines(fixture *f) {
    server s;
    int client = start_server(&s, f->store, 512, 40);
    /* A completed exchange proves the serving thread is running before the
     * intentionally long idle. poll observes unexpected data/closure rather
     * than assuming a sleeping test thread implies an initialized server. */
    request(client, READ, 0, 0); reply(client, 0);
    struct pollfd peer = {.fd = client, .events = POLLIN};
    assert(poll(&peer, 1, 200) == 0); /* Five request budgets; idle stays alive. */
    request(client, READ, 0, 512); reply(client, 0);
    unsigned char page[512]; receive_chunks(client, page, sizeof(page), sizeof(page));
    assert(poll(&peer, 1, 200) == 0);
    request(client, DISC, 0, 0); finish_server(&s, client, 0, 0);
    assert(s.stats.requests == 3);
    assert(s.stats.io_buffer_bytes == (uint64_t)sysconf(_SC_PAGESIZE));

    /* Even one byte starts the request budget. Unlimited idle does not let a
     * partially framed request live indefinitely. No command is dispatched. */
    client = start_server(&s, f->store, 512, 40);
    unsigned char header[28]; encode_request(header, READ, 0, 512);
    send_chunks(client, header, 1, 1);
    finish_server(&s, client, -1, ETIMEDOUT);
    assert(s.stats.requests == 0);

    /* An explicitly configured finite idle limit also applies after a complete
     * request, independently of its much larger processing deadline. */
    client = start_server_with_idle(&s, f->store, 512, 1000, 40);
    request(client, READ, 0, 0); reply(client, 0);
    finish_server(&s, client, -1, ETIMEDOUT);
    assert(s.stats.requests == 1);

    /* Finite idle and request budgets are independent: a partial header must
     * not wait for this much longer idle deadline. */
    client = start_server_with_idle(&s, f->store, 512, 40, 1000);
    send_chunks(client, header, 7, 7);
    peer.fd = client;
    assert(poll(&peer, 1, 500) == 1);
    finish_server(&s, client, -1, ETIMEDOUT);

    /* Cancellation of an unlimited idle wait works through the caller-owned
     * server descriptor. The serving loop never closes/reuses that descriptor. */
    client = start_server(&s, f->store, 512, 40);
    request(client, READ, 0, 0); reply(client, 0);
    assert(!shutdown(s.fd, SHUT_RDWR));
    finish_server(&s, client, -1, ECONNRESET);

    /* Idle values above poll's signed-int limit are clamped safely, not turned
     * into a negative/expired request deadline. A queued request still works. */
    client = start_server_with_idle(&s, f->store, 512, 40, UINT32_MAX);
    request(client, DISC, 0, 0); finish_server(&s, client, 0, 0);
}

static void test_page_rounded_locked_buffer(fixture *f) {
    long host_page = sysconf(_SC_PAGESIZE); assert(host_page > 0);
    size_t page = (size_t)host_page;
    void *probe = mmap(NULL, page, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    assert(probe != MAP_FAILED);
    int can_lock = !mlock(probe, page);
    assert(!munmap(probe, page)); /* Releases only our probe's lock, if acquired. */
    if (!can_lock) {
        puts("nbd transport: optional locked-buffer test skipped (host mlock limit/permission)");
        return;
    }
    server s;
    int client = start_server_options(&s, f->store, 512, 1000, 0, 1);
    request(client, READ, 0, 512); reply(client, 0);
    unsigned char data[512]; receive_chunks(client, data, sizeof(data), sizeof(data));
    request(client, DISC, 0, 0); finish_server(&s, client, 0, 0);
    assert(s.stats.io_buffer_bytes == page && s.stats.io_buffer_bytes >= 512);
}

static void test_setup_validation(fixture *f) {
    cs_nbd_options options = {.max_request_bytes = 513};
    cs_nbd_stats stats = {.requests = 42};
    assert(cs_nbd_serve(f->store, f->fd, &options, &stats) == -1 && errno == EINVAL);
    assert(stats.requests == 0);
    options.max_request_bytes = CS_NBD_HARD_MAX_REQUEST + 512;
    assert(cs_nbd_serve(f->store, f->fd, &options, NULL) == -1 && errno == EINVAL);
    options.max_request_bytes = 512; options.request_timeout_ms = 600001;
    assert(cs_nbd_serve(f->store, f->fd, &options, NULL) == -1 && errno == EINVAL);
    assert(cs_nbd_serve(NULL, f->fd, NULL, NULL) == -1 && errno == EINVAL);
    assert(cs_nbd_serve(f->store, f->fd, NULL, NULL) == -1 && errno == ENOTSOCK);
    int sockets[2]; assert(!socketpair(AF_UNIX, SOCK_DGRAM, 0, sockets));
    assert(cs_nbd_serve(f->store, sockets[0], NULL, NULL) == -1 && errno == EPROTOTYPE);
    close(sockets[0]); close(sockets[1]);
    /* Zero options select defaults; caller-supplied nonblocking FDs work. */
    server s; int client = start_server(&s, f->store, 0, 0);
    assert(!fcntl(s.fd, F_SETFL, O_NONBLOCK));
    request(client, DISC, 0, 0); finish_server(&s, client, 0, 0);
    assert(s.stats.io_buffer_bytes == CS_NBD_DEFAULT_MAX_REQUEST);
}

int main(void) {
    alarm(30); /* Tests must never hang on malformed or half-closed streams. */
    for (cs_codec codec = CS_CODEC_NONE; codec <= CS_CODEC_LZ4; codec++) {
    fixture f; fixture_open(&f, codec);
    test_setup_validation(&f);
    test_round_trip(&f);
    test_malformed_and_disconnect(&f);
    test_idle_and_partial_header_deadlines(&f);
    test_page_rounded_locked_buffer(&f);
    cs_stats stats; assert(!cs_get_stats(f.store, &stats));
    assert(stats.codec == codec);
    if (codec == CS_CODEC_NONE) assert(!stats.compression_calls && !stats.decompression_calls);
    fixture_close(&f);
    }
    puts("nbd transport: round-trip, opaque cookies, partial I/O, strict bounds/flags, trim, disconnect, independent idle/request deadlines and framing tests passed (SOCKETPAIR + FILE EMULATION ONLY; NO KERNEL NBD/SWAP)");
    return 0;
}
