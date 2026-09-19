#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "hid_report_decoder.h"

#define CAPTURE_LINES 96u

typedef struct {
    char lines[CAPTURE_LINES][HID_REPORT_TEXT_LINE_CHARS + 1u];
    size_t count;
} capture_t;

static void capture_line(void *context, const char *line)
{
    capture_t *const capture = (capture_t *)context;
    const size_t length = strlen(line);
    assert(length <= HID_REPORT_TEXT_LINE_CHARS);
    assert(capture->count < CAPTURE_LINES);
    memcpy(capture->lines[capture->count], line, length + 1u);
    capture->count++;
}

static bool contains(const capture_t *capture, const char *line)
{
    for (size_t i = 0u; i < capture->count; ++i) {
        if (strcmp(capture->lines[i], line) == 0) {
            return true;
        }
    }
    return false;
}

static hid_report_decode_result_t decode(const uint8_t *bytes, size_t length,
                                         capture_t *capture)
{
    memset(capture, 0, sizeof(*capture));
    return hid_report_decode(bytes, length, capture_line, capture);
}

static void test_boot_mouse_descriptor_decodes_required_items(void)
{
    static const uint8_t descriptor[] = {
        0x05u, 0x01u,        // Usage Page (Generic Desktop)
        0x09u, 0x02u,        // Usage (Mouse)
        0xa1u, 0x01u,        // Collection (Application)
        0x09u, 0x01u,        // Usage (Pointer)
        0xa1u, 0x00u,        // Collection (Physical)
        0x05u, 0x09u,        // Usage Page (Button)
        0x19u, 0x01u,        // Usage Minimum (Button 1)
        0x29u, 0x03u,        // Usage Maximum (Button 3)
        0x15u, 0x00u,        // Logical Minimum (0)
        0x25u, 0x01u,        // Logical Maximum (1)
        0x95u, 0x03u,        // Report Count (3)
        0x75u, 0x01u,        // Report Size (1)
        0x81u, 0x02u,        // Input (Data, Variable, Absolute)
        0x95u, 0x01u,        // Report Count (1)
        0x75u, 0x05u,        // Report Size (5)
        0x81u, 0x03u,        // Input (Constant, Variable, Absolute)
        0x05u, 0x01u,        // Usage Page (Generic Desktop)
        0x09u, 0x30u,        // Usage (X)
        0x09u, 0x31u,        // Usage (Y)
        0x09u, 0x38u,        // Usage (Wheel)
        0x15u, 0x81u,        // Logical Minimum (-127)
        0x25u, 0x7fu,        // Logical Maximum (127)
        0x75u, 0x08u,        // Report Size (8)
        0x95u, 0x03u,        // Report Count (3)
        0x85u, 0x01u,        // Report ID (1)
        0x81u, 0x06u,        // Input (Data, Variable, Relative)
        0xc0u,               // End Collection
        0xc0u,               // End Collection
    };
    capture_t capture;
    const hid_report_decode_result_t result =
        decode(descriptor, sizeof(descriptor), &capture);

    assert(!result.malformed);
    assert(result.items == 28u);
    assert(result.unclosed_collections == 0u);
    assert(contains(&capture, "Usage Page: Generic Desktop"));
    assert(contains(&capture, "Usage: Mouse"));
    assert(contains(&capture, "Usage: Pointer"));
    assert(contains(&capture, "Usage Page: Button"));
    assert(contains(&capture, "Usage Min: Button 1"));
    assert(contains(&capture, "Usage Max: Button 3"));
    assert(contains(&capture, "Collection: Application"));
    assert(contains(&capture, "Collection: Physical"));
    assert(contains(&capture, "Logical Min: -127"));
    assert(contains(&capture, "Logical Max: 127"));
    assert(contains(&capture, "Report Count: 3"));
    assert(contains(&capture, "Report Size: 8"));
    assert(contains(&capture, "Report ID: 1"));
    assert(contains(&capture, "Usage: X"));
    assert(contains(&capture, "Usage: Y"));
    assert(contains(&capture, "Usage: Wheel"));
    assert(contains(&capture, "Input: Data Var Relative"));
    assert(contains(&capture, "  NoWrap Linear Preferred"));
    assert(contains(&capture, "  NoNull BitField"));
    assert(contains(&capture, "End Collection"));
    assert(result.lines == capture.count);
}

static void test_main_item_flags_cover_output_and_feature_bits(void)
{
    static const uint8_t descriptor[] = {
        0x91u, 0x00u,        // Output: all clear
        0xb2u, 0xffu, 0x01u, // Feature: bits 0..8 set
    };
    capture_t capture;
    const hid_report_decode_result_t result =
        decode(descriptor, sizeof(descriptor), &capture);

    assert(!result.malformed);
    assert(contains(&capture, "Output: Data Array Absolute"));
    assert(contains(&capture, "  NoWrap Linear Preferred"));
    assert(contains(&capture, "  NoNull NonVolatile"));
    assert(contains(&capture, "  BitField"));
    assert(contains(&capture, "Feature: Const Var Relative"));
    assert(contains(&capture, "  Wrap NonLinear NoPreferred"));
    assert(contains(&capture, "  Null Volatile"));
    assert(contains(&capture, "  BufferedBytes"));
}

static void test_common_consumer_and_button_names(void)
{
    static const uint8_t descriptor[] = {
        0x05u, 0x0cu,                    // Consumer
        0x09u, 0x01u,                    // Consumer Control
        0x09u, 0xe9u,                    // Volume Up
        0x0bu, 0x38u, 0x02u, 0x0cu, 0u, // 32-bit Usage (Consumer AC Pan)
        0x05u, 0x09u,                    // Button
        0x09u, 0x05u,                    // Button 5
    };
    capture_t capture;
    const hid_report_decode_result_t result =
        decode(descriptor, sizeof(descriptor), &capture);

    assert(!result.malformed);
    assert(contains(&capture, "Usage Page: Consumer"));
    assert(contains(&capture, "Usage: Consumer Control"));
    assert(contains(&capture, "Usage: Volume Up"));
    assert(contains(&capture, "Usage: AC Pan"));
    assert(contains(&capture, "Usage: Button 5"));
}

static void test_logical_max_is_unsigned_when_minimum_is_nonnegative(void)
{
    static const uint8_t descriptor[] = {
        0x15u, 0x00u,       // Logical Minimum 0
        0x26u, 0xffu, 0x00u // Logical Maximum 255
    };
    capture_t capture;
    const hid_report_decode_result_t result =
        decode(descriptor, sizeof(descriptor), &capture);

    assert(!result.malformed);
    assert(contains(&capture, "Logical Min: 0"));
    assert(contains(&capture, "Logical Max: 255"));
}

static void test_unknown_items_degrade_to_hex(void)
{
    static const uint8_t descriptor[] = {
        0xc5u, 0xabu, // Unknown Global tag 0xC
        0xd1u, 0x34u, // Unknown Main tag 0xD
        0xfdu, 0x12u, // Reserved type, tag 0xF
    };
    capture_t capture;
    const hid_report_decode_result_t result =
        decode(descriptor, sizeof(descriptor), &capture);

    assert(!result.malformed);
    assert(contains(&capture, "Global 0xC: 0xAB"));
    assert(contains(&capture, "Main 0xD: 0x34"));
    assert(contains(&capture, "Reserved 0xF: 0x12"));
}

static void test_long_item_is_rendered_as_hex_and_parser_continues(void)
{
    static const uint8_t descriptor[] = {
        0xfeu, 0x04u, 0x7fu, 0xdeu, 0xadu, 0xbeu, 0xefu,
        0x85u, 0x02u,
    };
    capture_t capture;
    const hid_report_decode_result_t result =
        decode(descriptor, sizeof(descriptor), &capture);

    assert(!result.malformed);
    assert(result.items == 2u);
    assert(contains(&capture, "Long 0x7F: DE AD BE EF"));
    assert(contains(&capture, "Report ID: 2"));
}

static void test_truncated_short_item_stops_without_reading_past_end(void)
{
    static const uint8_t descriptor[] = {0x27u, 0x01u, 0x02u};
    capture_t capture;
    const hid_report_decode_result_t result =
        decode(descriptor, sizeof(descriptor), &capture);

    assert(result.malformed);
    assert(result.items == 0u);
    assert(contains(&capture, "Truncated item @0x00"));
}

static void test_truncated_long_items_stop_safely(void)
{
    static const uint8_t no_header[] = {0xfeu};
    static const uint8_t short_data[] = {0xfeu, 0x04u, 0x7fu, 0xaau};
    capture_t capture;

    hid_report_decode_result_t result =
        decode(no_header, sizeof(no_header), &capture);
    assert(result.malformed);
    assert(contains(&capture, "Truncated long item @0x00"));

    result = decode(short_data, sizeof(short_data), &capture);
    assert(result.malformed);
    assert(contains(&capture, "Truncated long item @0x00"));
}

static void test_unterminated_and_unmatched_collections_are_reported(void)
{
    static const uint8_t unterminated[] = {0xa1u, 0x01u};
    static const uint8_t unmatched[] = {0xc0u};
    capture_t capture;

    hid_report_decode_result_t result =
        decode(unterminated, sizeof(unterminated), &capture);
    assert(result.malformed);
    assert(result.unclosed_collections == 1u);
    assert(contains(&capture, "Unclosed Collections: 1"));

    result = decode(unmatched, sizeof(unmatched), &capture);
    assert(result.malformed);
    assert(contains(&capture, "End Collection (unmatched)"));
}

static void test_null_input_and_null_emitter_are_safe(void)
{
    capture_t capture;
    hid_report_decode_result_t result = decode(NULL, 1u, &capture);
    assert(result.malformed);
    assert(contains(&capture, "Invalid descriptor pointer"));

    static const uint8_t descriptor[] = {0x85u, 0x01u};
    result = hid_report_decode(descriptor, sizeof(descriptor), NULL, NULL);
    assert(!result.malformed);
    assert(result.items == 1u);
    assert(result.lines == 1u);

    result = hid_report_decode(NULL, 0u, NULL, NULL);
    assert(!result.malformed);
    assert(result.items == 0u);
}

static void test_every_single_byte_prefix_terminates_safely(void)
{
    capture_t capture;
    for (unsigned value = 0u; value <= UINT8_MAX; ++value) {
        const uint8_t byte = (uint8_t)value;
        const hid_report_decode_result_t result = decode(&byte, 1u, &capture);
        assert(result.lines == capture.count);
        assert(result.items <= 1u);
    }
}

int main(void)
{
    test_boot_mouse_descriptor_decodes_required_items();
    test_main_item_flags_cover_output_and_feature_bits();
    test_common_consumer_and_button_names();
    test_logical_max_is_unsigned_when_minimum_is_nonnegative();
    test_unknown_items_degrade_to_hex();
    test_long_item_is_rendered_as_hex_and_parser_continues();
    test_truncated_short_item_stops_without_reading_past_end();
    test_truncated_long_items_stop_safely();
    test_unterminated_and_unmatched_collections_are_reported();
    test_null_input_and_null_emitter_are_safe();
    test_every_single_byte_prefix_terminates_safely();
    puts("hid_report_decoder_test: all passed");
    return 0;
}
