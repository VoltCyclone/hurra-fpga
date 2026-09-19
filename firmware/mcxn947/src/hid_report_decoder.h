// Portable USB HID report-descriptor decoder with LCD-width text output.

#ifndef HURRA_MCXN947_HID_REPORT_DECODER_H
#define HURRA_MCXN947_HID_REPORT_DECODER_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// DISPLAY_SPLIT_COL is 30, but this module stays independent of display.h so
// it can be reused and host-tested without pulling in the rendering pipeline.
#define HID_REPORT_TEXT_LINE_CHARS 30u

typedef void (*hid_report_line_fn)(void *context, const char *line);

typedef struct {
    size_t items;
    size_t lines;
    size_t unclosed_collections;
    bool malformed;
} hid_report_decode_result_t;

// `emit` is called synchronously with a NUL-terminated line no longer than
// HID_REPORT_TEXT_LINE_CHARS. The pointer is valid only for that call. A NULL
// emitter still performs validation and returns the same counts.
hid_report_decode_result_t hid_report_decode(const uint8_t *descriptor,
                                             size_t length,
                                             hid_report_line_fn emit,
                                             void *context);

#endif  // HURRA_MCXN947_HID_REPORT_DECODER_H
