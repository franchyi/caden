#define _GNU_SOURCE
#include "coldstore.h"
#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#include <zlib.h>

static uint64_t ns(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * UINT64_C(1000000000) + (uint64_t)t.tv_nsec;
}
static uint64_t number(const char *text) {
    char *end;
    if (!text[0] || text[0] == '-') { fprintf(stderr, "invalid byte count\n"); exit(2); }
    errno = 0;
    unsigned long long value = strtoull(text, &end, 10);
    if (errno || *end) { fprintf(stderr, "invalid byte count\n"); exit(2); }
    return value;
}
static int64_t anon_rss(void) {
    FILE *file = fopen("/proc/self/status", "r");
    if (!file) return -1;
    char line[256];
    unsigned long long kib;
    int64_t result = -1;
    while (fgets(line, sizeof(line), file)) {
        if (sscanf(line, "RssAnon: %llu kB", &kib) == 1) {
            result = (int64_t)(kib * 1024); break;
        }
    }
    fclose(file);
    return result;
}
static uint32_t checksum(const unsigned char *data, size_t length) {
    uLong result = crc32(0, NULL, 0);
    while (length) {
        unsigned chunk = length > (1u << 20) ? (1u << 20) : (unsigned)length;
        result = crc32(result, data, chunk);
        data += chunk; length -= chunk;
    }
    return (uint32_t)result;
}
int main(int argc, char **argv) {
    if ((argc != 7 && argc != 9) || (strcmp(argv[1], "--emulate-file") && strcmp(argv[1], "--write-reserved-dax"))) {
        fprintf(stderr, "Usage: %s --emulate-file|--write-reserved-dax PATH OFFSET CAPACITY LOGICAL_BYTES zeros|mixed|random [--codec none|lz4]\n", argv[0]);
        return 2;
    }
    cs_options options = {.path = argv[2], .offset = number(argv[3]),
        .capacity = number(argv[4]), .logical_bytes = number(argv[5]),
        .emulate_file = !strcmp(argv[1], "--emulate-file"), .lock_metadata = 1};
    if (argc == 9) {
        if (strcmp(argv[7], "--codec") || (strcmp(argv[8], "none") && strcmp(argv[8], "lz4"))) {
            fprintf(stderr, "unknown codec option\n"); return 2;
        }
        options.codec = !strcmp(argv[8], "lz4") ? CS_CODEC_LZ4 : CS_CODEC_NONE;
    }
    const char *pattern = argv[6];
    if (strcmp(pattern, "zeros") && strcmp(pattern, "mixed") && strcmp(pattern, "random")) {
        fprintf(stderr, "unknown pattern\n"); return 2;
    }
    /* Initial hardware qualification is deliberately bounded. Expanding the
     * tested footprint needs a separately reviewed experiment configuration. */
    if (options.capacity > (UINT64_C(256) << 20) || options.logical_bytes > (UINT64_C(128) << 20)) {
        fprintf(stderr, "probe cap is 256 MiB mapped / 128 MiB source, not the whole reserved half\n"); return 2;
    }
    cs_store *store = NULL;
    if (cs_open(&store, &options)) { perror("cs_open"); return 1; }
    size_t size = (size_t)options.logical_bytes;
    long page = sysconf(_SC_PAGESIZE);
    size_t vector_size = (size + (size_t)page - 1) / (size_t)page;
    unsigned char *residency = calloc(vector_size, 1);
    unsigned char *source = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    if (!residency || source == MAP_FAILED) { perror("source allocation"); cs_close(store); free(residency); return 1; }
    uint32_t random = 0x12abc789;
    for (size_t n = 0; n < size; n++) {
        random ^= random << 13; random ^= random >> 17; random ^= random << 5;
        source[n] = !strcmp(pattern, "zeros") ? 0 :
            (!strcmp(pattern, "mixed") && n % CS_PAGE >= 1024 ? 0x45 : (unsigned char)random);
    }
    uint32_t expected = checksum(source, size);
    int64_t hot_anon = anon_rss();
    uint64_t started = ns();
    if (cs_write(store, 0, source, size)) { perror("cold-store write"); munmap(source, size); free(residency); cs_close(store); return 1; }
    uint64_t encoded = ns() - started;
    /* Only this probe owns the source buffer and it is quiescent. This is NOT
     * injection into another process or proof of transparent sandbox paging. */
    if (munmap(source, size)) { perror("source release"); free(residency); cs_close(store); return 1; }
    /* Darwin's mincore does not use Linux's ENOMEM contract for holes. Report
     * independent absence verification only where that contract is available. */
#ifdef __linux__
    errno = 0;
    int absent = mincore(source, size, (void *)residency) == -1 && errno == ENOMEM;
    const char *absence_json = absent ? "true" : "false";
#else
    int absent = 1; /* munmap succeeded; independent inspection unavailable. */
    const char *absence_json = "null";
#endif
    int64_t cold_anon = anon_rss();
    unsigned char *restored = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    if (restored == MAP_FAILED) { perror("restore allocation"); free(residency); cs_close(store); return 1; }
    started = ns();
    int read_ok = cs_read(store, 0, restored, size) == 0;
    uint64_t restored_ns = ns() - started;
    uint32_t actual = read_ok ? checksum(restored, size) : 0;
    int64_t restored_anon = anon_rss();
    cs_stats stats;
    if (cs_get_stats(store, &stats)) { perror("store stats"); munmap(restored, size); free(residency); cs_close(store); return 1; }
    int success = absent && read_ok && expected == actual;
    printf("{\"schema\":\"crate-cold-tier-backend-probe-v2\",\"success\":%s,"
           "\"backend\":\"%s\",\"scope\":\"cooperative-owned-buffer; not transparent sandbox paging\","
           "\"pattern\":\"%s\",\"offset\":%" PRIu64 ",\"mapped_capacity\":%" PRIu64 ","
           "\"source_bytes\":%zu,\"source_release_munmap_succeeded\":true,\"source_mapping_absent_after_release\":%s,"
           "\"crc32_expected\":%u,\"crc32_restored\":%u,"
           "\"hot_rss_anon_bytes\":%" PRId64 ",\"cold_rss_anon_bytes\":%" PRId64 ","
           "\"restored_rss_anon_bytes\":%" PRId64 ",\"store_ns\":%" PRIu64 ",\"restore_ns\":%" PRIu64 ","
           "\"payload_bytes\":%" PRIu64 ",\"allocator_bytes\":%" PRIu64 ",\"metadata_mapping_bytes\":%" PRIu64 ","
           "\"codec\":\"%s\",\"compression_calls\":%" PRIu64 ",\"decompression_calls\":%" PRIu64 ","
           "\"codec_state_bytes\":%" PRIu64 ",\"raw_pages\":%" PRIu64 ",\"zero_pages\":%" PRIu64 "}\n",
           success ? "true" : "false", options.emulate_file ? "regular-file-emulation" : "device-dax",
           pattern, options.offset, options.capacity, size, absence_json, expected, actual,
           hot_anon, cold_anon, restored_anon, encoded, restored_ns,
           stats.payload_bytes, stats.allocated_bytes, stats.metadata_bytes,
           stats.codec == CS_CODEC_NONE ? "none" : "lz4", stats.compression_calls,
           stats.decompression_calls, stats.codec_state_bytes, stats.raw_pages, stats.zero_pages);
    munmap(restored, size); free(residency); cs_close(store);
    return success ? 0 : 1;
}
