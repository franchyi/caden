#ifndef CRATE_NBD_TRANSPORT_H
#define CRATE_NBD_TRANSPORT_H

#include "coldstore.h"
#include <stdint.h>

#define CS_NBD_DEFAULT_MAX_REQUEST (1u << 20)
#define CS_NBD_HARD_MAX_REQUEST (16u << 20)
#define CS_NBD_MIN_BLOCK 512u
/* These are protocol values, independent of host errno / struct layout. */
#define CS_NBD_EXPORT_FLAGS ((1u << 0) | (1u << 5)) /* HAS_FLAGS | SEND_TRIM */

typedef struct {
    uint32_t max_request_bytes; /* 0: 1 MiB; multiple of 512, at most 16 MiB. */
    uint32_t request_timeout_ms; /* 0: 30 s; maximum 10 min after first header byte. */
    int lock_io_buffer;         /* Fail closed if requested mlock fails. */
    uint32_t idle_timeout_ms;   /* 0/default: unlimited between requests; otherwise milliseconds. */
} cs_nbd_options;

typedef struct {
    uint64_t requests, read_bytes, written_bytes, trims;
    /* io_buffer_bytes is the actual page-rounded private mapping size. */
    uint64_t error_replies, protocol_errors, io_buffer_bytes;
} cs_nbd_stats;

/* Transmission phase only. No negotiation, listening, device attachment,
 * ioctl, swapon, host configuration or device access is performed here.
 * The caller provides a connected AF_UNIX/SOCK_STREAM descriptor and owns it
 * throughout. This function shuts the socket down on all serving-loop exits;
 * the caller still closes it. Returns 0 only for a valid DISC, -1 with errno
 * for transport/protocol/setup failure. Optional stats are written on return.
 *
 * The caller must negotiate 512-byte minimum / max_request_bytes maximum
 * request sizes and at most CS_NBD_EXPORT_FLAGS, never FLUSH, FUA, structured
 * replies, extended headers or multi-connection capability. READ/WRITE require
 * sector alignment. TRIM discards only complete 4-KiB pages inside its range.
 * FLUSH and every nonzero command flag receive EINVAL: this ephemeral store
 * cannot promise nonvolatile persistence. Restart/close loses the store index.
 *
 * One bounded, page-rounded private mapping (optionally locked) is allocated
 * before serving; cleanup unmaps only that mapping, never shared heap pages. There
 * are no transport allocations in the request loop. request_timeout_ms is one
 * total budget starting with the first received header bytes (remaining header,
 * payload, processing and reply), not per chunk. Idle time between requests is
 * independently controlled by idle_timeout_ms; zero waits indefinitely in poll,
 * without spinning. EOF or caller shutdown(socket_fd, SHUT_RDWR) cancels that
 * idle wait; do not close/reuse the descriptor until this function has returned.
 * An oversized request is disconnected without draining arbitrary payload.
 * Other invalid bounded WRITE requests have their payload drained before an
 * error reply, preserving framing. A truncated WRITE never reaches cs_write.
 * Requests are sequential, but multi-page writes are not transaction-atomic.
 * This adapter does not demonstrate sandbox paging or process restoration.
 */
int cs_nbd_serve(cs_store *store, int socket_fd, const cs_nbd_options *options,
                 cs_nbd_stats *stats);

#endif
