#ifndef CRATE_COLDSTORE_H
#define CRATE_COLDSTORE_H
#include <stddef.h>
#include <stdint.h>

#define CS_PAGE 4096u
#define CS_CXL_START (UINT64_C(256) << 30)
#define CS_CXL_END (UINT64_C(512) << 30)
typedef struct cs_store cs_store;
typedef enum { CS_CODEC_NONE = 0, CS_CODEC_LZ4 = 1 } cs_codec;
typedef struct {
    const char *path;
    uint64_t offset, capacity, logical_bytes;
    int emulate_file; /* Explicit test mode; never report it as CXL. */
    int lock_metadata;
    cs_codec codec; /* Zero/default: store all written pages verbatim, including zeros. */
} cs_options;
typedef struct {
    uint64_t logical_bytes, written_pages, payload_bytes, allocated_bytes;
    uint64_t metadata_bytes, writes, reads, trims, raw_pages, zero_pages;
    uint64_t encode_ns, decode_ns;
    uint64_t allocations, allocation_search_words;
    cs_codec codec;
    uint64_t compression_calls, decompression_calls, codec_state_bytes;
} cs_stats;

/* Ephemeral, single-owner store. Closing loses the in-DRAM index. The caller
 * must not close/restart while consumers depend on any stored page. Range locks
 * exclude cooperating owners on this host only, not another host sharing DAX.
 * Operations are serialized. A multi-page write is not a transaction: an error
 * may leave preceding pages committed; each page replacement is atomic. */
int cs_open(cs_store **out, const cs_options *options);
int cs_read(cs_store *store, uint64_t offset, void *data, size_t length);
int cs_write(cs_store *store, uint64_t offset, const void *data, size_t length);
int cs_trim(cs_store *store, uint64_t offset, size_t length);
int cs_get_stats(cs_store *store, cs_stats *out);
void cs_close(cs_store *store);
#endif
