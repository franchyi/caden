#define _GNU_SOURCE
#define _POSIX_C_SOURCE 200809L
#define _DARWIN_C_SOURCE
#include "nbd_transport.h"
#include <errno.h>
#include <limits.h>
#include <poll.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

/* Wire layout follows Linux include/uapi/linux/nbd.h and the primary NBD
 * protocol document: https://github.com/NetworkBlockDevice/nbd/blob/master/doc/proto.md
 * Decode explicit bytes instead of relying on packed-struct or host endianness. */
#define REQUEST_MAGIC UINT32_C(0x25609513)
#define REPLY_MAGIC UINT32_C(0x67446698)
enum { CMD_READ = 0, CMD_WRITE = 1, CMD_DISC = 2, CMD_FLUSH = 3, CMD_TRIM = 4 };
enum { WIRE_EIO = 5, WIRE_EINVAL = 22, WIRE_ENOSPC = 28 };

static int fail(int error) { errno = error; return -1; }
static uint32_t get32(const unsigned char *p) {
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 |
           (uint32_t)p[2] << 8 | (uint32_t)p[3];
}
static uint64_t get64(const unsigned char *p) {
    return (uint64_t)get32(p) << 32 | get32(p + 4);
}
static void put32(unsigned char *p, uint32_t value) {
    p[0] = (unsigned char)(value >> 24); p[1] = (unsigned char)(value >> 16);
    p[2] = (unsigned char)(value >> 8); p[3] = (unsigned char)value;
}
static int64_t now_ms(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) return -1;
    return (int64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}
static int wait_ready(int fd, short events, int64_t deadline) {
    for (;;) {
        int timeout = -1; /* Explicit unlimited idle wait, not a polling loop. */
        if (deadline >= 0) {
            int64_t now = now_ms();
            if (now < 0) return -1;
            if (now >= deadline) return fail(ETIMEDOUT);
            int64_t remaining = deadline - now;
            timeout = remaining > INT_MAX ? INT_MAX : (int)remaining;
        }
        struct pollfd p = {.fd = fd, .events = events};
        int ready = poll(&p, 1, timeout);
        if (ready < 0) { if (errno == EINTR) continue; return -1; }
        if (!ready) continue; /* Recheck deadline, including a clamped long idle. */
        if (p.revents & POLLNVAL) return fail(EBADF);
        /* POLLHUP/POLLERR still require recv/send to distinguish queued bytes
         * from EOF. The operation is nonblocking so a readiness race is safe. */
        if (p.revents & (events | POLLHUP | POLLERR)) return 0;
    }
}

static int receive_header(int fd, unsigned char *out, size_t length,
                          uint32_t request_timeout, uint32_t idle_timeout,
                          int64_t *deadline) {
    *deadline = -1;
    if (idle_timeout) {
        int64_t now = now_ms();
        if (now < 0) return -1;
        *deadline = now + idle_timeout;
    }
    size_t done = 0;
    while (done < length) {
        if (wait_ready(fd, POLLIN, *deadline)) return -1;
        ssize_t n = recv(fd, out + done, length - done, MSG_DONTWAIT);
        if (n < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
            return -1;
        }
        if (!n) return fail(done ? EPROTO : ECONNRESET);
        if (!done) {
            /* Start once, even for a one-byte header fragment. Never reset the
             * budget as later header/payload chunks or reply bytes progress. */
            int64_t now = now_ms();
            if (now < 0) return -1;
            *deadline = now + request_timeout;
        }
        done += (size_t)n;
    }
    return 0;
}
static int receive_exact(int fd, unsigned char *out, size_t length, int64_t deadline) {
    size_t done = 0;
    while (done < length) {
        if (wait_ready(fd, POLLIN, deadline)) return -1;
        ssize_t n = recv(fd, out + done, length - done, MSG_DONTWAIT);
        if (n < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
            return -1;
        }
        if (!n) return fail(done ? EPROTO : ECONNRESET);
        done += (size_t)n;
    }
    return 0;
}
static int send_exact(int fd, const unsigned char *data, size_t length, int64_t deadline) {
    size_t done = 0;
    while (done < length) {
        if (wait_ready(fd, POLLOUT, deadline)) return -1;
        int flags = MSG_DONTWAIT;
#ifdef MSG_NOSIGNAL
        flags |= MSG_NOSIGNAL;
#endif
        ssize_t n = send(fd, data + done, length - done, flags);
        if (n < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
            return -1;
        }
        if (!n) return fail(EPIPE);
        done += (size_t)n;
    }
    return 0;
}
static int validate_socket(int fd) {
    int type;
    socklen_t length = sizeof(type);
    if (getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &length)) return -1;
    if (type != SOCK_STREAM) return fail(EPROTOTYPE);
    struct sockaddr_storage address;
    length = sizeof(address);
    if (getpeername(fd, (struct sockaddr *)&address, &length)) return -1;
    if (address.ss_family != AF_UNIX) return fail(EAFNOSUPPORT);
#ifdef SO_NOSIGPIPE
    int enabled = 1;
    if (setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &enabled, sizeof(enabled))) return -1;
#elif !defined(MSG_NOSIGNAL)
    /* Never change the process-wide SIGPIPE disposition behind the caller. */
    return fail(ENOTSUP);
#endif
    return 0;
}
static uint32_t wire_error(int error) {
    if (error == EINVAL) return WIRE_EINVAL;
    if (error == ENOSPC || error == EFBIG || error == EDQUOT) return WIRE_ENOSPC;
    /* Includes checksum/decompression corruption. Never expose host errno
     * numerically: values differ across supported development hosts. */
    return WIRE_EIO;
}

int cs_nbd_serve(cs_store *store, int fd, const cs_nbd_options *o, cs_nbd_stats *out) {
    cs_nbd_stats stats = {0};
    if (out) *out = stats;
    uint32_t maximum = o && o->max_request_bytes ? o->max_request_bytes : CS_NBD_DEFAULT_MAX_REQUEST;
    uint32_t timeout = o && o->request_timeout_ms ? o->request_timeout_ms : 30000;
    uint32_t idle_timeout = o ? o->idle_timeout_ms : 0;
    if (!store || maximum > CS_NBD_HARD_MAX_REQUEST || maximum % CS_NBD_MIN_BLOCK ||
        timeout > 600000) return fail(EINVAL);
    if (validate_socket(fd)) return -1;
    long host_page = sysconf(_SC_PAGESIZE);
    if (host_page <= 0) return fail(EINVAL);
    size_t page = (size_t)host_page;
    if ((size_t)maximum > SIZE_MAX - (page - 1)) return fail(EOVERFLOW);
    size_t buffer_bytes = (((size_t)maximum + page - 1) / page) * page;
    /* A private mapping owns every page we lock. malloc-backed mlock/munlock
     * could otherwise unlock unrelated heap objects sharing an edge page. */
    unsigned char *buffer = mmap(NULL, buffer_bytes, PROT_READ | PROT_WRITE,
                                 MAP_PRIVATE | MAP_ANON, -1, 0);
    if (buffer == MAP_FAILED) return -1;
    int locked = o && o->lock_io_buffer;
    if (locked && mlock(buffer, buffer_bytes)) {
        int error = errno; (void)munmap(buffer, buffer_bytes); return fail(error);
    }
    /* Fault in the complete transport buffer before requests can depend on it. */
    memset(buffer, 0, buffer_bytes);
    stats.io_buffer_bytes = buffer_bytes;
    cs_stats storage_stats;
    cs_get_stats(store, &storage_stats);
    uint64_t logical = storage_stats.logical_bytes;
    int result = -1, saved = 0;
    for (;;) {
        unsigned char request[28], reply[16];
        int64_t deadline;
        if (receive_header(fd, request, sizeof(request), timeout, idle_timeout, &deadline)) {
            stats.protocol_errors += errno == EPROTO;
            break;
        }
        stats.requests++;
        if (get32(request) != REQUEST_MAGIC) {
            stats.protocol_errors++; errno = EPROTO; break;
        }
        uint32_t type = get32(request + 4), command = type & 0xffffu;
        uint64_t offset = get64(request + 16);
        uint32_t length = get32(request + 24);
        if (length > maximum) {
            stats.protocol_errors++; errno = EMSGSIZE; break;
        }
        if (command == CMD_DISC) {
            if (type != CMD_DISC || offset || length) {
                stats.protocol_errors++; errno = EPROTO;
            } else { result = 0; errno = 0; }
            break; /* The protocol forbids replying to DISC. */
        }
        /* Consume bounded WRITE data even when flags/range are invalid, so
         * that the next header is never parsed from its payload. */
        if (command == CMD_WRITE && receive_exact(fd, buffer, length, deadline)) {
            if (errno == ECONNRESET) errno = EPROTO; /* Missing declared payload. */
            stats.protocol_errors += errno == EPROTO;
            break;
        }
        uint32_t error = 0;
        if (type & 0xffff0000u) error = WIRE_EINVAL;
        else if (command != CMD_READ && command != CMD_WRITE && command != CMD_TRIM)
            error = WIRE_EINVAL; /* Includes FLUSH: persistence unsupported. */
        else if (offset % CS_NBD_MIN_BLOCK || length % CS_NBD_MIN_BLOCK)
            error = WIRE_EINVAL;
        else if (offset > logical || length > logical - offset)
            error = command == CMD_WRITE ? WIRE_ENOSPC : WIRE_EINVAL;
        else if (command == CMD_READ) {
            if (cs_read(store, offset, buffer, length)) error = wire_error(errno);
        } else if (command == CMD_WRITE) {
            if (cs_write(store, offset, buffer, length)) error = wire_error(errno);
            else stats.written_bytes += length;
        } else if (command == CMD_TRIM) {
            uint64_t first = offset / CS_PAGE + !!(offset % CS_PAGE);
            uint64_t last = (offset + length) / CS_PAGE;
            if (first < last && cs_trim(store, first * CS_PAGE, (size_t)(last - first) * CS_PAGE))
                error = wire_error(errno);
            else stats.trims++;
        }
        put32(reply, REPLY_MAGIC);
        put32(reply + 4, error);
        memcpy(reply + 8, request + 8, 8); /* Opaque cookie, not integer conversion. */
        if (send_exact(fd, reply, sizeof(reply), deadline)) break;
        if (error) stats.error_replies++;
        else if (command == CMD_READ) {
            if (send_exact(fd, buffer, length, deadline)) break;
            stats.read_bytes += length;
        }
    }
    saved = errno;
    (void)shutdown(fd, SHUT_RDWR);
    (void)munmap(buffer, buffer_bytes);
    if (out) *out = stats;
    errno = saved;
    return result;
}
