#define _GNU_SOURCE
#include "coldstore.h"
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

static uint32_t next_random(uint32_t *state) {
    *state ^= *state << 13; *state ^= *state >> 17; *state ^= *state << 5;
    return *state;
}

static void random_operations(cs_store *store, size_t logical) {
    unsigned char *model = calloc(1, logical), *input = malloc(logical), *output = malloc(logical);
    assert(model && input && output && !cs_trim(store, 0, logical));
    uint32_t random = 0x2d64a357;
    for (unsigned step = 0; step < 6000; step++) {
        size_t offset = next_random(&random) % logical;
        size_t length = next_random(&random) % 18000;
        if (length > logical - offset) length = logical - offset;
        unsigned action = next_random(&random) % 10;
        if (!action) {
            offset = offset / CS_PAGE * CS_PAGE; length = length / CS_PAGE * CS_PAGE;
            assert(!cs_trim(store, offset, length)); memset(model + offset, 0, length);
        } else if (action < 8) {
            unsigned mode = next_random(&random) % 7;
            for (size_t n = 0; n < length; n++) {
                input[n] = mode == 0 ? 0 : mode == 1 ? 0xad : mode == 2 ? n % 251 :
                    mode == 3 ? (n % CS_PAGE < 512 ? next_random(&random) : 0) :
                    mode == 4 ? (n % CS_PAGE < 2048 ? next_random(&random) : 0) : next_random(&random);
            }
            assert(!cs_write(store, offset, input, length)); memcpy(model + offset, input, length);
        } else {
            assert(!cs_read(store, offset, output, length));
            assert(!memcmp(model + offset, output, length));
        }
        if (!(step % 37)) {
            assert(!cs_read(store, 0, output, logical) && !memcmp(model, output, logical));
            cs_stats stats; assert(!cs_get_stats(store, &stats));
            assert(stats.written_pages <= logical / CS_PAGE);
            assert(stats.raw_pages + stats.zero_pages <= stats.written_pages);
            assert(stats.payload_bytes <= stats.allocated_bytes && stats.allocated_bytes <= logical);
        }
    }
    free(model); free(input); free(output);
}

typedef struct { cs_store *store; unsigned id; } thread_context;
static void *thread_operations(void *arg) {
    thread_context *context = arg;
    unsigned char input[CS_PAGE], output[CS_PAGE];
    for (unsigned n = 0; n < 1000; n++) {
        memset(input, (n + context->id) % 256, sizeof(input));
        input[0] = (unsigned char)context->id;
        uint64_t offset = context->id * CS_PAGE;
        assert(!cs_write(context->store, offset, input, sizeof(input)));
        assert(!cs_read(context->store, offset, output, sizeof(output)));
        assert(!memcmp(input, output, sizeof(input)));
        cs_stats stats; assert(!cs_get_stats(context->store, &stats));
        assert(stats.written_pages <= 8 && stats.allocated_bytes <= 8 * CS_PAGE);
        if (!(n % 13)) assert(!cs_trim(context->store, offset, CS_PAGE));
    }
    return NULL;
}

static void concurrent_operations(cs_store *store, size_t logical) {
    assert(!cs_trim(store, 0, logical));
    pthread_t threads[8]; thread_context contexts[8];
    for (unsigned n = 0; n < 8; n++) {
        contexts[n] = (thread_context){.store = store, .id = n};
        assert(!pthread_create(&threads[n], NULL, thread_operations, &contexts[n]));
    }
    for (unsigned n = 0; n < 8; n++) assert(!pthread_join(threads[n], NULL));
}

/* A non-power-of-two frame count crosses a summary boundary and leaves unused
 * tail bits at every level. A nearly full store retains exactly one free frame;
 * random replacements formerly scanned roughly half the frame array each time.
 * Count bitmap words, not wall time, to enforce a deterministic complexity bound. */
static void full_random_replacements(size_t frames, cs_codec codec) {
    char path[] = "/tmp/crate-coldstore-full-XXXXXX";
    int fd = mkstemp(path);
    size_t capacity = frames * CS_PAGE, logical = capacity - CS_PAGE;
    assert(fd >= 0 && !ftruncate(fd, (off_t)capacity));
    cs_options options = {.path = path, .capacity = capacity, .logical_bytes = logical, .emulate_file = 1, .codec = codec};
    cs_store *store; assert(!cs_open(&store, &options));
    uint32_t random = 1234567;
    unsigned char input[CS_PAGE], output[CS_PAGE];
    for (unsigned n = 0; n < sizeof(input); n++) input[n] = (unsigned char)next_random(&random);
    for (size_t page = 0; page < frames - 1; page++) assert(!cs_write(store, page * CS_PAGE, input, sizeof(input)));
    cs_stats before, after; assert(!cs_get_stats(store, &before));
    assert(before.raw_pages == frames - 1);
    const unsigned replacements = 10000;
    for (unsigned n = 0; n < replacements; n++) {
        size_t page = next_random(&random) % (frames - 1);
        assert(!cs_write(store, page * CS_PAGE, input, sizeof(input)));
        assert(!cs_read(store, page * CS_PAGE, output, sizeof(output)));
        assert(!memcmp(input, output, sizeof(input)));
    }
    assert(!cs_get_stats(store, &after));
    unsigned levels = 0;
    size_t words = frames;
    do { words = words / 64 + !!(words % 64); levels++; } while (words > 1);
    assert(after.allocations - before.allocations == replacements);
    assert(after.allocation_search_words - before.allocation_search_words == (uint64_t)replacements * levels);
    cs_close(store); close(fd); assert(!unlink(path));
}

static void test_codec(cs_codec codec) {
    char path[] = "/tmp/crate-coldstore-XXXXXX";
    int fd = mkstemp(path);
    assert(fd >= 0);
    const size_t guard = (size_t)sysconf(_SC_PAGESIZE), capacity = 1u << 20, logical = capacity - CS_PAGE;
    assert(!ftruncate(fd, (off_t)(capacity + 2 * guard)));
    unsigned char before[4096], after[4096], input[4096], output[4096];
    memset(before, 0x59, sizeof(before));
    assert(pwrite(fd, before, sizeof(before), 0) == (ssize_t)sizeof(before));
    assert(pwrite(fd, before, sizeof(before), (off_t)(guard + capacity)) == (ssize_t)sizeof(before));
    cs_options options = {.path = path, .offset = guard, .capacity = capacity,
                          .logical_bytes = logical, .emulate_file = 1, .codec = codec};
    cs_store *store;
    int opened = cs_open(&store, &options);
    if (opened) perror("cs_open test fixture");
    assert(!opened);
    assert(!cs_read(store, 0, output, sizeof(output)));
    memset(input, 0, sizeof(input));
    assert(!memcmp(input, output, sizeof(input)));
    memset(input, 'q', sizeof(input));
    assert(!cs_write(store, 0, input, sizeof(input)));
    assert(!cs_read(store, 0, output, sizeof(output)));
    assert(!memcmp(input, output, sizeof(input)));
    cs_stats stats;
    assert(cs_get_stats(NULL, &stats) == -1 && errno == EINVAL);
    assert(cs_get_stats(store, NULL) == -1 && errno == EINVAL);
    assert(!cs_read(store, logical, NULL, 0) && !cs_write(store, logical, NULL, 0));
    assert(cs_read(NULL, 0, NULL, 0) == -1 && errno == EINVAL);
    cs_get_stats(store, &stats);
    assert(stats.codec == codec && stats.written_pages == 1);
    if (codec == CS_CODEC_LZ4) {
        assert(stats.payload_bytes < 256 && stats.allocated_bytes == 256 && stats.raw_pages == 0);
        assert(stats.compression_calls == 1 && stats.decompression_calls == 1 && stats.codec_state_bytes > 0);
    } else {
        assert(stats.payload_bytes == CS_PAGE && stats.allocated_bytes == CS_PAGE && stats.raw_pages == 1);
        assert(!stats.compression_calls && !stats.decompression_calls && !stats.codec_state_bytes);
    }
    /* Partial overwrite, including a page boundary, preserves untouched bytes. */
    memset(input, 0x67, sizeof(input));
    assert(!cs_write(store, 4000, input, 192));
    assert(!cs_read(store, 3999, output, 194));
    assert(output[0] == 'q' && output[193] == 0);
    for (unsigned n = 1; n <= 192; n++) assert(output[n] == 0x67);
    assert(cs_write(store, logical, input, 1) == -1 && errno == EINVAL);
    assert(cs_read(store, UINT64_MAX, output, 16) == -1 && errno == EINVAL);
    assert(cs_trim(store, 1, CS_PAGE) == -1 && errno == EINVAL);
    /* Fill advertised capacity with incompressible data, then overwrite every
     * page while full. The extra physical frame is required for replacement. */
    uint32_t random = 1234567;
    for (unsigned n = 0; n < sizeof(input); n++) {
        random ^= random << 13; random ^= random >> 17; random ^= random << 5;
        input[n] = (unsigned char)random;
    }
    for (unsigned rep = 0; rep < 3; rep++) {
        for (size_t offset = 0; offset < logical; offset += CS_PAGE) {
            assert(!cs_write(store, offset, input, sizeof(input)));
            assert(!cs_read(store, offset, output, sizeof(output)));
            assert(!memcmp(input, output, sizeof(input)));
        }
    }
    cs_get_stats(store, &stats);
    assert(stats.raw_pages == logical / CS_PAGE);
    assert(stats.payload_bytes == logical && stats.allocated_bytes == logical);
    assert(!cs_trim(store, 0, logical));
    cs_get_stats(store, &stats);
    assert(stats.written_pages == 0 && stats.allocated_bytes == 0 && stats.payload_bytes == 0);
    memset(input, 0, sizeof(input));
    assert(!cs_write(store, 0, input, CS_PAGE));
    cs_get_stats(store, &stats);
    if (codec == CS_CODEC_LZ4) assert(stats.zero_pages == 1 && stats.payload_bytes == 0);
    else assert(stats.zero_pages == 0 && stats.raw_pages == 1 && stats.payload_bytes == CS_PAGE);
    random_operations(store, logical);
    concurrent_operations(store, logical);
    assert(!cs_get_stats(store, &stats));
    if (codec == CS_CODEC_NONE)
        assert(!stats.compression_calls && !stats.decompression_calls && !stats.codec_state_bytes);
    /* Corruption is rejected, a partial update cannot silently preserve corrupt
     * data, and an explicit full-page overwrite can repair that logical page. */
    assert(!cs_trim(store, 0, logical));
    for (unsigned n = 0; n < sizeof(input); n++) input[n] = (unsigned char)next_random(&random);
    assert(!cs_write(store, 0, input, sizeof(input)));
    assert(!cs_get_stats(store, &stats) && stats.raw_pages == 1);
    unsigned char corrupt = input[0] ^ 1;
    assert(pwrite(fd, &corrupt, 1, (off_t)guard) == 1);
    assert(cs_read(store, 0, output, sizeof(output)) == -1 && errno == EILSEQ);
    assert(cs_write(store, 1, input, 1) == -1 && errno == EILSEQ);
    assert(!cs_write(store, 0, input, sizeof(input)));
    assert(!cs_read(store, 0, output, sizeof(output)) && !memcmp(input, output, sizeof(input)));
    cs_store *other = NULL;
    assert(cs_open(&other, &options) == -1 && !other);
    /* Another process cannot take ownership of the same mapped interval. */
    pid_t pid = fork();
    assert(pid >= 0);
    if (!pid) {
        cs_store *other;
        _exit(cs_open(&other, &options) == -1 ? 0 : 1);
    }
    int child;
    assert(waitpid(pid, &child, 0) == pid && WIFEXITED(child) && WEXITSTATUS(child) == 0);
    cs_close(store);
    assert(pread(fd, after, sizeof(after), 0) == (ssize_t)sizeof(after) && !memcmp(before, after, sizeof(after)));
    assert(pread(fd, after, sizeof(after), (off_t)(guard + capacity)) == (ssize_t)sizeof(after) && !memcmp(before, after, sizeof(after)));
    /* Explicit emulation and reserved-range checks cannot silently target DAX. */
    options.emulate_file = 0;
    assert(cs_open(&store, &options) == -1 && errno == EPERM);
    options.emulate_file = 1;
    options.capacity = UINT64_MAX;
    assert(cs_open(&store, &options) == -1);
    options.capacity = UINT64_C(1) << 63;
    assert(cs_open(&store, &options) == -1 && errno == EINVAL);
    options.capacity = capacity; options.offset = 1;
    assert(cs_open(&store, &options) == -1 && errno == EINVAL);
    options.offset = guard; options.logical_bytes = capacity;
    assert(cs_open(&store, &options) == -1 && errno == EINVAL);
    options.logical_bytes = logical; options.codec = (cs_codec)99;
    assert(cs_open(&store, &options) == -1 && errno == EINVAL && !store);
    close(fd);
    assert(!unlink(path));
    full_random_replacements(2, codec);
    full_random_replacements(63, codec);
    full_random_replacements(64, codec);
    full_random_replacements(65, codec);
    full_random_replacements(4099, codec);
}

int main(void) {
    cs_options defaults = {0};
    assert(defaults.codec == CS_CODEC_NONE);
    test_codec(CS_CODEC_NONE);
    test_codec(CS_CODEC_LZ4);
    puts("coldstore: NONE/LZ4 round-trip, explicit zero/raw accounting, zero codec calls in NONE, bounds, randomized partial I/O, full replacement, corruption, concurrency, ownership and guards passed (FILE EMULATION ONLY)");
    return 0;
}
