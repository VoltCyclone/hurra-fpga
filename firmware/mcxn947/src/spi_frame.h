#ifndef SPI_FRAME_H
#define SPI_FRAME_H

#include <stdint.h>

#include "injection_wire.h"

#define SPI_FRAME_OFF_SOF 0u
#define SPI_FRAME_OFF_TYPE 1u
#define SPI_FRAME_OFF_SEQUENCE 2u
#define SPI_FRAME_OFF_LENGTH 3u
#define SPI_FRAME_OFF_PAYLOAD INJ_FRAME_HEADER_SIZE
#define SPI_FRAME_OFF_CRC (INJ_FRAME_SIZE - 2u)

typedef enum {
    SPI_FRAME_OK = 0,
    SPI_FRAME_ERR_SOF,
    SPI_FRAME_ERR_LEN,
    SPI_FRAME_ERR_CRC,
    /* Append only. Nothing serializes these, but spi_frame_test.c compares them
     * by name and the link layer will log them, so never renumber a member. */
    SPI_FRAME_ERR_TYPE,
    /* Not an error: the frame is a well-formed IDLE keepalive, but it is not
     * deliverable, matching spi_link.py's rx_type != INJ_TYPE_IDLE predicate. */
    SPI_FRAME_IDLE,
} spi_frame_result_t;

typedef struct {
    uint8_t type;
    uint8_t sequence;
    uint8_t length;
    uint8_t payload[INJ_FRAME_PAYLOAD_SIZE];
} inj_frame_t;

_Static_assert(INJ_FRAME_HEADER_SIZE == 4u, "codec requires a four-byte header");
_Static_assert(SPI_FRAME_OFF_CRC == 30u, "codec requires a 32-byte slot");

uint16_t spi_frame_crc16(const uint8_t *data, uint32_t length);

/* Per-type wire payload length. IDLE is the only zero-length message: it is a
 * bare keepalive with no payload on the wire, even though the contract gives
 * INJ_TYPE_IDLE a 26-byte reserved payload struct. That asymmetry is NOT in
 * report_injection_wire.json -- it lives here, in injection_wire.py's
 * pack_slot template, and in spi_link.py's expected_length Mux. Keep all
 * three in step; tests/test_wire_cross_language.py enforces it. */
uint8_t spi_frame_expected_payload_length(uint8_t type);

/* Build one 32-byte slot.
 *
 * `length` is the EXACT wire payload length for `type`, not a maximum: 0 for
 * INJ_TYPE_IDLE and INJ_FRAME_PAYLOAD_SIZE for every other assigned type.
 * `type` must be one of the 15 the contract assigns (inj_type_is_known).
 *
 * Returns SPI_FRAME_ERR_TYPE for an unassigned type and SPI_FRAME_ERR_LEN for
 * the wrong length. Both are checked before any byte of `slot` is written, so a
 * rejected call leaves the caller's buffer untouched. Failing here is the point:
 * a short-but-correctly-CRC'd frame is dropped by the FPGA against a counter the
 * MCU cannot read, so the injection is lost with no local error at all. */
spi_frame_result_t spi_frame_pack(uint8_t slot[INJ_FRAME_SIZE],
                                  uint8_t type,
                                  uint8_t sequence,
                                  const uint8_t *payload,
                                  uint8_t length);

/* Parse one 32-byte slot.
 *
 * Checks SOF -> type -> length -> CRC, which is cheap byte comparisons before a
 * 30-byte CRC. That order differs from unpack_slot()'s in injection_wire.py, so
 * the two ends can name a *different reason* for a frame with two faults.
 * Reasons are diagnostics; admissibility is the contract.
 *
 * SPI_FRAME_OK means the frame is well-formed and deliverable, exactly matching
 * the FPGA master's valid_return predicate (spi_link.py:150-157).
 * SPI_FRAME_IDLE means the frame is a well-formed keepalive, not an error, but
 * is not deliverable; no out-parameter is written. SPI_FRAME_ERR_* means the
 * frame is malformed and is likewise not deliverable. */
spi_frame_result_t spi_frame_unpack(const uint8_t slot[INJ_FRAME_SIZE],
                                    uint8_t *type,
                                    uint8_t *sequence,
                                    const uint8_t **payload,
                                    uint8_t *length);
/* Transport sequence classification.
 *
 * A pure function of delta = (sequence - previous) & 0xFF, matching the
 * executable reference classify_sequence() in injection_wire.py:
 *
 *   delta 0x00        DUPLICATE  gap 0  drain, do not act, do not advance
 *   delta 0x01        NEXT       gap 0  act, advance
 *   delta 0x02..0x7F  GAP        d-1    act, advance
 *   delta 0x80..0xFF  STALE      gap 0  drain, do not act, never move back
 *
 * Precondition: `sequence` must come from a frame for which spi_frame_unpack()
 * returned SPI_FRAME_OK. IDLE keepalives are not sequenced; feeding their
 * hardwired sequence 0 into this classifier makes a healthy sparse-traffic
 * link report as almost entirely SPI_FRAME_SEQ_STALE.
 *
 * These enum values are internal. They are NOT wire-visible: the contract's
 * SequenceDisposition members are strings, and the generated header emits no
 * classifier at all. Do not "align" them into injection_wire.h.
 */
typedef enum {
    SPI_FRAME_SEQ_NEXT = 0,
    SPI_FRAME_SEQ_DUPLICATE,
    SPI_FRAME_SEQ_GAP,
    SPI_FRAME_SEQ_STALE,
} spi_frame_seq_class_t;

spi_frame_seq_class_t spi_frame_seq_classify(uint8_t previous_sequence,
                                             uint8_t sequence,
                                             uint8_t *gap);
uint8_t spi_frame_seq_gap(uint8_t previous_sequence, uint8_t sequence);

#endif
