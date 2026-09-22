#define _GNU_SOURCE
#include "coldstore.h"
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <lz4.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/file.h>
#include <sys/stat.h>
#ifdef __linux__
#include <sys/sysmacros.h>
#endif
#include <time.h>
#include <unistd.h>
#include <zlib.h>

/* Each 4-KiB physical frame has 16 independently allocated 256-byte cells.
 * Payloads use power-of-two size classes. An extra physical frame guarantees
 * space for atomic page replacement even for incompressible input. No unsafe
 * advertised-capacity overcommit is used by this first implementation. */
typedef struct {
    uint64_t offset, generation;
    uint32_t crc;
    uint16_t length, units;
} cs_slot;
#define CS_CLASSES 5u
#define CS_LEVELS 11u /* ceil(64 / log2(64)), sufficient for a uint64_t index. */
typedef struct {
    unsigned char encoded[LZ4_COMPRESSBOUND(CS_PAGE)];
    LZ4_stream_t encoder;
} cs_lz4;
struct cs_store {
    int fd, mutex_ready;
    uint64_t capacity, pages, frames;
    unsigned char *mapping;
    cs_slot *slots;
    uint16_t *used;
    uint64_t *availability, *available[CS_CLASSES][CS_LEVELS];
    size_t level_words[CS_LEVELS];
    unsigned levels;
    size_t self_bytes, slot_bytes, bitmap_bytes, availability_bytes;
    pthread_mutex_t mutex;
    cs_stats stats;
    unsigned char page[CS_PAGE];
    cs_lz4 *lz4; /* No compressor state/scratch mapping in the raw path. */
};

static uint64_t nanos(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000000000u + (uint64_t)t.tv_nsec;
}
static int fail(int error) { errno = error; return -1; }
static int checked_range(uint64_t offset, size_t length, uint64_t end) {
    return offset <= end && (uint64_t)length <= end - offset;
}
static size_t rounded(size_t bytes, size_t page) {
    if (bytes > SIZE_MAX - page + 1) return 0;
    return ((bytes + page - 1) / page) * page;
}
static int dax_size(const struct stat *st, uint64_t *size) {
#ifdef __linux__
    FILE *f = fopen("/sys/bus/dax/devices/dax0.0/dev", "r");
    unsigned device_major, device_minor;
    if (!f) return -1;
    int ok = fscanf(f, "%u:%u", &device_major, &device_minor) == 2;
    fclose(f);
    if (!ok || major(st->st_rdev) != device_major || minor(st->st_rdev) != device_minor)
        return fail(ENODEV);
    f = fopen("/sys/bus/dax/devices/dax0.0/size", "r");
    unsigned long long value;
    if (!f) return -1;
    ok = fscanf(f, "%llu", &value) == 1;
    fclose(f);
    if (!ok) return fail(EINVAL);
    *size = value;
    return 0;
#else
    (void)st; (void)size;
    return fail(ENOTSUP);
#endif
}

/* Leaf bits identify frames with an aligned free segment of a size class.
 * Each higher bit identifies a nonempty word below it. All searches descend
 * from one root word, so even a full, fragmented store costs O(log64 frames),
 * never a linear scan for the spare frame. Only DRAM metadata is initialized. */
static int init_availability(cs_store *s, size_t host_page) {
    size_t total_words = 0;
    uint64_t items = s->frames;
    do {
        if (s->levels == CS_LEVELS) return fail(EOVERFLOW);
        uint64_t words = items / 64 + !!(items % 64);
        if (words > SIZE_MAX - total_words) return fail(EOVERFLOW);
        s->level_words[s->levels++] = (size_t)words;
        total_words += (size_t)words;
        items = words;
    } while (items > 1);
    if (total_words > SIZE_MAX / (CS_CLASSES * sizeof(uint64_t))) return fail(EOVERFLOW);
    s->availability_bytes = rounded(total_words * CS_CLASSES * sizeof(uint64_t), host_page);
    if (!s->availability_bytes) return fail(EOVERFLOW);
    s->availability = mmap(NULL, s->availability_bytes, PROT_READ | PROT_WRITE,
                          MAP_PRIVATE | MAP_ANON, -1, 0);
    if (s->availability == MAP_FAILED) return -1;
    uint64_t *next = s->availability;
    for (unsigned cls = 0; cls < CS_CLASSES; cls++) {
        items = s->frames;
        for (unsigned level = 0; level < s->levels; level++) {
            size_t words = s->level_words[level];
            s->available[cls][level] = next;
            memset(next, 0xff, words * sizeof(uint64_t));
            if (items % 64) next[words - 1] = (UINT64_C(1) << (items % 64)) - 1;
            next += words;
            items = words;
        }
    }
    return 0;
}

static void set_available(cs_store *s, unsigned cls, uint64_t index, int present) {
    for (unsigned level = 0; level < s->levels; level++) {
        uint64_t *word = &s->available[cls][level][index / 64];
        uint64_t old = *word, mask = UINT64_C(1) << (index % 64);
        if (present) *word |= mask;
        else *word &= ~mask;
        if ((!old) == (!*word)) break; /* Parent's nonempty bit is unchanged. */
        present = !!*word;
        index /= 64;
    }
}

static void update_frame(cs_store *s, uint64_t frame) {
    for (unsigned cls = 0; cls < CS_CLASSES; cls++) {
        unsigned units = 1u << cls, present = 0;
        for (unsigned position = 0; position < 16; position += units) {
            unsigned mask = ((1u << units) - 1u) << position;
            if (!(s->used[frame] & mask)) { present = 1; break; }
        }
        set_available(s, cls, frame, (int)present);
    }
}

int cs_open(cs_store **out, const cs_options *o) {
    if (!out) return fail(EINVAL);
    *out = NULL;
    if (!o || !o->path || (o->codec != CS_CODEC_NONE && o->codec != CS_CODEC_LZ4)) return fail(EINVAL);
    long host_page = sysconf(_SC_PAGESIZE);
    if (host_page <= 0 || o->offset % (uint64_t)host_page) return fail(EINVAL);
    if (!o->logical_bytes || o->logical_bytes % CS_PAGE || o->capacity % CS_PAGE ||
        o->capacity <= CS_PAGE || o->logical_bytes > o->capacity - CS_PAGE ||
        o->capacity > SIZE_MAX || o->capacity > INT64_MAX ||
        o->offset > (uint64_t)INT64_MAX - o->capacity)
        return fail(EINVAL);
    if (!o->emulate_file && (strcmp(o->path, "/dev/dax0.0") ||
        o->offset < CS_CXL_START || o->offset > CS_CXL_END ||
        o->capacity > CS_CXL_END - o->offset ||
        o->offset % (2u << 20) || o->capacity % (2u << 20)))
        return fail(EPERM);
    size_t self_bytes = rounded(sizeof(cs_store), (size_t)host_page);
    cs_store *s = mmap(NULL, self_bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    if (s == MAP_FAILED) return -1;
    s->self_bytes = self_bytes;
    s->fd = -1;
    s->mapping = MAP_FAILED;
    s->capacity = o->capacity;
    s->pages = o->logical_bytes / CS_PAGE;
    s->frames = o->capacity / CS_PAGE;
    if (s->pages > SIZE_MAX / sizeof(cs_slot) || s->frames > SIZE_MAX / sizeof(uint16_t)) {
        errno = EOVERFLOW; goto error;
    }
    s->slot_bytes = rounded((size_t)s->pages * sizeof(cs_slot), (size_t)host_page);
    s->bitmap_bytes = rounded((size_t)s->frames * sizeof(uint16_t), (size_t)host_page);
    if (!s->slot_bytes || !s->bitmap_bytes) { errno = EOVERFLOW; goto error; }
    s->slots = mmap(NULL, s->slot_bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    s->used = mmap(NULL, s->bitmap_bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    if (s->slots == MAP_FAILED || s->used == MAP_FAILED) goto error;
    int pe = pthread_mutex_init(&s->mutex, NULL);
    if (pe) { errno = pe; goto error; }
    s->mutex_ready = 1;
    s->fd = open(o->path, O_RDWR | O_CLOEXEC | O_NOFOLLOW);
    if (s->fd < 0) goto error;
    struct stat st;
    if (fstat(s->fd, &st)) goto error;
    uint64_t size;
    if (o->emulate_file) {
        if (!S_ISREG(st.st_mode) || st.st_size < 0) { errno = EINVAL; goto error; }
        size = (uint64_t)st.st_size;
    } else {
        if (!S_ISCHR(st.st_mode)) { errno = ENODEV; goto error; }
        if (dax_size(&st, &size)) goto error;
    }
    if (!checked_range(o->offset, (size_t)o->capacity, size)) { errno = ERANGE; goto error; }
#ifdef F_OFD_SETLK
    struct flock lock = {.l_type = F_WRLCK, .l_whence = SEEK_SET,
                         .l_start = (off_t)o->offset, .l_len = (off_t)o->capacity};
    if (fcntl(s->fd, F_OFD_SETLK, &lock)) goto error;
#else
    /* Never silently fall back to per-process POSIX locks: closing an unrelated
     * FD can release them. Test files may use a conservative whole-file lock. */
    if (!o->emulate_file) { errno = ENOTSUP; goto error; }
    if (flock(s->fd, LOCK_EX | LOCK_NB)) goto error;
#endif
    /* Only the reserved interval is mapped. No truncation, device conversion,
     * whole-device clearing, or shared-host memory-policy change. */
    s->mapping = mmap(NULL, (size_t)o->capacity, PROT_READ | PROT_WRITE,
                      MAP_SHARED, s->fd, (off_t)o->offset);
    if (s->mapping == MAP_FAILED) goto error;
    if (init_availability(s, (size_t)host_page)) goto error;
    s->stats.codec = o->codec;
    if (o->codec == CS_CODEC_LZ4) {
        s->stats.codec_state_bytes = rounded(sizeof(cs_lz4), (size_t)host_page);
        s->lz4 = mmap(NULL, s->stats.codec_state_bytes, PROT_READ | PROT_WRITE,
                      MAP_PRIVATE | MAP_ANON, -1, 0);
        if (s->lz4 == MAP_FAILED) goto error;
        if (o->lock_metadata && mlock(s->lz4, s->stats.codec_state_bytes)) goto error;
    }
    if (o->lock_metadata && (mlock(s, s->self_bytes) || mlock(s->slots, s->slot_bytes) ||
                             mlock(s->used, s->bitmap_bytes) ||
                             mlock(s->availability, s->availability_bytes))) goto error;
    s->stats.logical_bytes = o->logical_bytes;
    s->stats.metadata_bytes = s->self_bytes + s->slot_bytes + s->bitmap_bytes + s->availability_bytes + s->stats.codec_state_bytes;
    *out = s;
    return 0;
error: {
    int saved = errno;
    cs_close(s);
    return fail(saved);
}}

static void free_slot(cs_store *s, const cs_slot *slot) {
    if (!slot->units) return;
    uint64_t frame = slot->offset / CS_PAGE;
    unsigned position = (unsigned)(slot->offset % CS_PAGE) / 256;
    unsigned mask = ((1u << slot->units) - 1u) << position;
    s->used[frame] &= (uint16_t)~mask;
    update_frame(s, frame);
}
static int allocate(cs_store *s, unsigned length, cs_slot *slot) {
    unsigned units = 1, cls = 0;
    while (units * 256 < length) { units *= 2; cls++; }
    uint64_t frame = 0;
    for (unsigned level = s->levels; level-- > 0;) {
        uint64_t word = s->available[cls][level][frame];
        s->stats.allocation_search_words++;
        if (!word) return fail(level == s->levels - 1 ? ENOSPC : EILSEQ);
        frame = frame * 64 + (unsigned)__builtin_ctzll(word);
    }
    if (frame >= s->frames) return fail(EILSEQ);
    for (unsigned position = 0; position < 16; position += units) {
        unsigned mask = ((1u << units) - 1u) << position;
        if (!(s->used[frame] & mask)) {
            s->used[frame] |= (uint16_t)mask;
            update_frame(s, frame);
            slot->offset = frame * CS_PAGE + position * 256;
            slot->units = (uint16_t)units;
            s->stats.allocations++;
            return 0;
        }
    }
    return fail(EILSEQ); /* Availability metadata disagrees with cell ownership. */
}
static int get_page(cs_store *s, uint64_t index, unsigned char *out) {
    const cs_slot *slot = &s->slots[index];
    uint64_t start = nanos();
    if (!slot->generation || !slot->length) memset(out, 0, CS_PAGE);
    else {
        const char *source = (const char *)s->mapping + slot->offset;
        if (slot->length == CS_PAGE) memcpy(out, source, CS_PAGE);
        else {
            if (s->stats.codec != CS_CODEC_LZ4) return fail(EILSEQ);
            s->stats.decompression_calls++;
            if (LZ4_decompress_safe(source, (char *)out, slot->length, CS_PAGE) != CS_PAGE)
                return fail(EILSEQ);
        }
        if ((uint32_t)crc32(0, out, CS_PAGE) != slot->crc) return fail(EILSEQ);
    }
    s->stats.decode_ns += nanos() - start;
    return 0;
}
static int put_page(cs_store *s, uint64_t index, const unsigned char *data) {
    cs_slot next = {0}, old = s->slots[index];
    uint64_t start = nanos();
    if (old.generation == UINT64_MAX) return fail(EOVERFLOW);
    next.generation = old.generation + 1;
    next.crc = (uint32_t)crc32(0, data, CS_PAGE);
    const unsigned char *payload = data;
    next.length = CS_PAGE;
    if (s->stats.codec == CS_CODEC_LZ4) {
        unsigned any = 0;
        for (unsigned n = 0; n < CS_PAGE; n++) any |= data[n];
        if (!any) next.length = 0;
        else {
            s->stats.compression_calls++;
            int encoded = LZ4_compress_fast_extState(&s->lz4->encoder, (const char *)data,
                            (char *)s->lz4->encoded, CS_PAGE, sizeof(s->lz4->encoded), 1);
            if (encoded <= 0) return fail(EIO);
            if (encoded < (int)CS_PAGE) {
                next.length = (uint16_t)encoded;
                payload = s->lz4->encoded;
            }
        }
    }
    if (next.length) {
        if (allocate(s, next.length, &next)) return -1;
        memcpy(s->mapping + next.offset, payload, next.length);
    }
    /* Commit the index only after a complete payload write. Not persistent
     * storage: CRC and the live owner protect reads, not crash recovery. */
    s->slots[index] = next;
    free_slot(s, &old);
    s->stats.written_pages += !old.generation;
    s->stats.payload_bytes += next.length;
    s->stats.payload_bytes -= old.length;
    s->stats.allocated_bytes += next.units * 256u;
    s->stats.allocated_bytes -= old.units * 256u;
    s->stats.raw_pages += next.length == CS_PAGE;
    s->stats.raw_pages -= old.generation && old.length == CS_PAGE;
    s->stats.zero_pages += !next.length;
    s->stats.zero_pages -= old.generation && !old.length;
    s->stats.encode_ns += nanos() - start;
    return 0;
}
int cs_read(cs_store *s, uint64_t offset, void *buffer, size_t length) {
    if (!s || (!buffer && length) || !checked_range(offset, length, s->stats.logical_bytes)) return fail(EINVAL);
    unsigned char *data = buffer;
    pthread_mutex_lock(&s->mutex);
    int result = 0;
    while (length) {
        unsigned inner = (unsigned)(offset % CS_PAGE);
        size_t take = CS_PAGE - inner;
        if (take > length) take = length;
        if (get_page(s, offset / CS_PAGE, s->page)) { result = -1; break; }
        memcpy(data, s->page + inner, take);
        data += take; offset += take; length -= take;
    }
    s->stats.reads++;
    pthread_mutex_unlock(&s->mutex);
    return result;
}
int cs_write(cs_store *s, uint64_t offset, const void *buffer, size_t length) {
    if (!s || (!buffer && length) || !checked_range(offset, length, s->stats.logical_bytes)) return fail(EINVAL);
    const unsigned char *data = buffer;
    pthread_mutex_lock(&s->mutex);
    int result = 0;
    while (length) {
        unsigned inner = (unsigned)(offset % CS_PAGE);
        size_t take = CS_PAGE - inner;
        if (take > length) take = length;
        if ((inner || take != CS_PAGE) && get_page(s, offset / CS_PAGE, s->page)) { result = -1; break; }
        memcpy(s->page + inner, data, take);
        if (put_page(s, offset / CS_PAGE, s->page)) { result = -1; break; }
        data += take; offset += take; length -= take;
    }
    s->stats.writes++;
    pthread_mutex_unlock(&s->mutex);
    return result;
}
int cs_trim(cs_store *s, uint64_t offset, size_t length) {
    if (!s || offset % CS_PAGE || length % CS_PAGE || !checked_range(offset, length, s->stats.logical_bytes)) return fail(EINVAL);
    pthread_mutex_lock(&s->mutex);
    for (uint64_t n = offset / CS_PAGE; n < (offset + length) / CS_PAGE; n++) {
        cs_slot old = s->slots[n];
        if (!old.generation) continue;
        free_slot(s, &old);
        memset(&s->slots[n], 0, sizeof(cs_slot));
        s->stats.written_pages--;
        s->stats.payload_bytes -= old.length;
        s->stats.allocated_bytes -= old.units * 256u;
        s->stats.raw_pages -= old.length == CS_PAGE;
        s->stats.zero_pages -= !old.length;
    }
    s->stats.trims++;
    pthread_mutex_unlock(&s->mutex);
    return 0;
}
int cs_get_stats(cs_store *s, cs_stats *out) {
    if (!s || !out) return fail(EINVAL);
    pthread_mutex_lock(&s->mutex);
    *out = s->stats;
    pthread_mutex_unlock(&s->mutex);
    return 0;
}
void cs_close(cs_store *s) {
    if (!s) return;
    if (s->mapping != MAP_FAILED) munmap(s->mapping, (size_t)s->capacity);
    if (s->fd >= 0) close(s->fd);
    if (s->mutex_ready) pthread_mutex_destroy(&s->mutex);
    if (s->slots && s->slots != MAP_FAILED) munmap(s->slots, s->slot_bytes);
    if (s->used && s->used != MAP_FAILED) munmap(s->used, s->bitmap_bytes);
    if (s->availability && s->availability != MAP_FAILED) munmap(s->availability, s->availability_bytes);
    if (s->lz4 && s->lz4 != MAP_FAILED) munmap(s->lz4, s->stats.codec_state_bytes);
    munmap(s, s->self_bytes);
}
