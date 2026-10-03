// Host test for src/hid_item.c -- the HID report-descriptor item tokenizer.

#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "hid_item.h"

static hid_item_t read_one(const uint8_t *bytes, size_t length, size_t *pos)
{
    hid_item_t item;
    memset(&item, 0xA5, sizeof(item));
    assert(hid_item_next(bytes, length, pos, &item) == HID_ITEM_OK);
    return item;
}

// Size code 3 means FOUR bytes, not three -- the one irregular encoding.
static void test_size_codes_zero_one_two_and_four(void)
{
    const uint8_t d[] = {
        0xC0,                          // End Collection, size 0
        0x05, 0x01,                    // Usage Page 0x01, size 1
        0x16, 0x01, 0x80,              // Logical Minimum 0x8001, size 2
        0x27, 0xFF, 0xFF, 0x00, 0x00,  // Logical Maximum 0x0000FFFF, size 4
    };
    size_t pos = 0u;

    hid_item_t item = read_one(d, sizeof(d), &pos);
    assert(item.type == HID_ITEM_TYPE_MAIN && item.tag == 0x0Cu && item.size == 0u);
    assert(item.value == 0u && item.svalue == 0);
    assert(pos == 1u);

    item = read_one(d, sizeof(d), &pos);
    assert(item.type == HID_ITEM_TYPE_GLOBAL && item.tag == 0x0u && item.size == 1u);
    assert(item.value == 0x01u && item.svalue == 1);
    assert(pos == 3u);

    item = read_one(d, sizeof(d), &pos);
    assert(item.type == HID_ITEM_TYPE_GLOBAL && item.tag == 0x1u && item.size == 2u);
    assert(item.value == 0x8001u && item.svalue == -32767);
    assert(pos == 6u);

    item = read_one(d, sizeof(d), &pos);
    assert(item.type == HID_ITEM_TYPE_GLOBAL && item.tag == 0x2u && item.size == 4u);
    assert(item.value == 0x0000FFFFu && item.svalue == 65535);
    assert(pos == sizeof(d));

    assert(hid_item_next(d, sizeof(d), &pos, &item) == HID_ITEM_END);
}

// Sign extension is from the item's OWN width: 0x81 in one byte is -127, the
// same bits in two bytes (0x0081) are +129.
static void test_sign_extension_follows_item_width(void)
{
    const uint8_t d[] = {0x15, 0x81, 0x16, 0x81, 0x00, 0x17, 0x00, 0x00, 0x00, 0x80};
    size_t pos = 0u;
    assert(read_one(d, sizeof(d), &pos).svalue == -127);
    assert(read_one(d, sizeof(d), &pos).svalue == 129);
    hid_item_t item = read_one(d, sizeof(d), &pos);
    assert(item.value == 0x80000000u && item.svalue == INT32_MIN);
}

// A local Usage item: type 2, and a 4-byte usage keeps its page in bits 31..16.
static void test_local_extended_usage(void)
{
    const uint8_t d[] = {0x0B, 0x38, 0x02, 0x0C, 0x00};
    size_t pos = 0u;
    hid_item_t item = read_one(d, sizeof(d), &pos);
    assert(item.type == HID_ITEM_TYPE_LOCAL && item.tag == 0x0u && item.size == 4u);
    assert(item.value == 0x000C0238u);
}

// Long item: 0xFE, bDataSize, bLongItemTag, data. Skipped whole, flagged.
static void test_long_item_is_skipped_whole(void)
{
    const uint8_t d[] = {0xFE, 0x03, 0x77, 0x01, 0x02, 0x03, 0x05, 0x09};
    size_t pos = 0u;
    hid_item_t item = read_one(d, sizeof(d), &pos);
    assert(item.is_long);
    assert(item.tag == 0x77u);
    assert(pos == 6u);

    item = read_one(d, sizeof(d), &pos);
    assert(!item.is_long && item.value == 0x09u);
}

// Every truncation shape ends the walk with *pos at the end, so a loop that
// stops on anything but OK always terminates.
static void test_truncation_reports_and_parks_at_end(void)
{
    const uint8_t short_item[] = {0x26, 0xFF};           // wants 2 data bytes, has 1
    const uint8_t long_header[] = {0xFE, 0x03};          // header itself cut short
    const uint8_t long_data[] = {0xFE, 0x03, 0x01, 0x00};  // wants 3 data bytes, has 1
    const struct {
        const uint8_t *bytes;
        size_t length;
    } cases[] = {
        {short_item, sizeof(short_item)},
        {long_header, sizeof(long_header)},
        {long_data, sizeof(long_data)},
    };
    for (size_t i = 0u; i < sizeof(cases) / sizeof(cases[0]); ++i) {
        size_t pos = 0u;
        hid_item_t item;
        assert(hid_item_next(cases[i].bytes, cases[i].length, &pos, &item) ==
               HID_ITEM_TRUNCATED);
        assert(pos == cases[i].length);
    }
}

// Every prefix of a real descriptor must terminate cleanly: no out-of-bounds
// read, and a bounded number of items. (Under the sanitizer build this is the
// memory-safety check; plain, it is the termination check.)
static void test_every_prefix_terminates(void)
{
    const uint8_t mouse[] = {
        0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x09, 0x01, 0xA1, 0x00, 0x05, 0x09, 0x19,
        0x01, 0x29, 0x03, 0x15, 0x00, 0x25, 0x01, 0x95, 0x03, 0x75, 0x01, 0x81, 0x02,
        0x95, 0x01, 0x75, 0x05, 0x81, 0x01, 0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x15,
        0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x02, 0x81, 0x06, 0xC0, 0xC0,
    };
    for (size_t cut = 0u; cut <= sizeof(mouse); ++cut) {
        size_t pos = 0u;
        size_t items = 0u;
        hid_item_t item;
        hid_item_status_t status;
        while ((status = hid_item_next(mouse, cut, &pos, &item)) == HID_ITEM_OK) {
            items++;
            assert(items <= cut);
        }
        assert(status == HID_ITEM_END || status == HID_ITEM_TRUNCATED);
        assert(pos == cut);
    }
}

int main(void)
{
    test_size_codes_zero_one_two_and_four();
    test_sign_extension_follows_item_width();
    test_local_extended_usage();
    test_long_item_is_skipped_whole();
    test_truncation_reports_and_parks_at_end();
    test_every_prefix_terminates();
    printf("hid_item_test: ok\n");
    return 0;
}
