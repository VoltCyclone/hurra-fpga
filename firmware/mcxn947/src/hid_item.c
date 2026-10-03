// See hid_item.h.

#include "hid_item.h"

#define HID_ITEM_LONG_PREFIX 0xFEu

hid_item_status_t hid_item_next(const uint8_t *descriptor, size_t length, size_t *pos,
                                hid_item_t *item)
{
    if (*pos >= length) {
        *pos = length;
        return HID_ITEM_END;
    }

    const uint8_t prefix = descriptor[*pos];
    size_t cursor = *pos + 1u;

    if (prefix == HID_ITEM_LONG_PREFIX) {
        // bDataSize and bLongItemTag first, then the data. HID 1.11 defines no
        // long items, so the stack above only needs to step over them.
        if (length - cursor < 2u) {
            *pos = length;
            return HID_ITEM_TRUNCATED;
        }
        const size_t data_size = descriptor[cursor];
        const uint8_t long_tag = descriptor[cursor + 1u];
        cursor += 2u;
        if (data_size > length - cursor) {
            *pos = length;
            return HID_ITEM_TRUNCATED;
        }
        item->type = HID_ITEM_TYPE_RESERVED;
        item->tag = long_tag;
        item->size = 0u;
        item->is_long = true;
        item->value = 0u;
        item->svalue = 0;
        *pos = cursor + data_size;
        return HID_ITEM_OK;
    }

    // bSize 3 encodes FOUR data bytes, not three.
    const uint8_t size_code = (uint8_t)(prefix & 0x03u);
    const uint8_t size = size_code == 3u ? 4u : size_code;
    // Subtraction bounds the read without ever forming cursor + size.
    if (size > length - cursor) {
        *pos = length;
        return HID_ITEM_TRUNCATED;
    }

    uint32_t value = 0u;
    for (uint8_t i = 0u; i < size; ++i) {
        value |= (uint32_t)descriptor[cursor + i] << (uint8_t)(i * 8u);
    }

    int32_t svalue;
    if (size == 0u) {
        svalue = 0;
    } else if (size == 4u) {
        svalue = (int32_t)value;
    } else {
        const uint8_t bits = (uint8_t)(size * 8u);
        const uint32_t sign = UINT32_C(1) << (bits - 1u);
        svalue = (value & sign) != 0u ? (int32_t)value - (int32_t)(UINT32_C(1) << bits)
                                      : (int32_t)value;
    }

    item->type = (uint8_t)((prefix >> 2u) & 0x03u);
    item->tag = (uint8_t)(prefix >> 4u);
    item->size = size;
    item->is_long = false;
    item->value = value;
    item->svalue = svalue;
    *pos = cursor + size;
    return HID_ITEM_OK;
}
