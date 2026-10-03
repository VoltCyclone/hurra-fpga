// See inj_map_build.h.

#include "inj_map_build.h"

#include <string.h>

#include "inj_command.h"

static uint8_t entry_flags(const hid_mouse_field_t *field)
{
    if (field->kind == HID_MOUSE_BUTTONS) {
        return (uint8_t)INJ_MAP_ENTRY_FLAG_BUTTON;
    }
    uint8_t flags = (uint8_t)INJ_MAP_ENTRY_FLAG_RELATIVE;
    if (field->logical_minimum < 0) {
        flags |= (uint8_t)INJ_MAP_ENTRY_FLAG_SIGNED;
    }
    switch (field->kind) {
    case HID_MOUSE_X:
        flags |= (uint8_t)INJ_MAP_ENTRY_FLAG_X;
        break;
    case HID_MOUSE_Y:
        flags |= (uint8_t)INJ_MAP_ENTRY_FLAG_Y;
        break;
    case HID_MOUSE_WHEEL:
        flags |= (uint8_t)INJ_MAP_ENTRY_FLAG_WHEEL;
        break;
    case HID_MOUSE_PAN:
        flags |= (uint8_t)INJ_MAP_ENTRY_FLAG_PAN;
        break;
    default:
        break;  // not produced by hid_mouse_layout.c; the FPGA would reject it
    }
    return flags;
}

uint8_t inj_map_build_entries(const hid_mouse_layout_t *layout, const inj_map_target_t *target,
                              inj_map_entry_payload_t out[HID_MOUSE_MAX_FIELDS])
{
    memset(out, 0, HID_MOUSE_MAX_FIELDS * sizeof(out[0]));
    const uint8_t count =
        layout->field_count < HID_MOUSE_MAX_FIELDS ? layout->field_count : HID_MOUSE_MAX_FIELDS;
    for (uint8_t i = 0u; i < count; ++i) {
        const hid_mouse_field_t *field = &layout->fields[i];
        inj_map_entry_payload_t *e = &out[i];
        e->descriptor_generation = target->descriptor_generation;
        e->map_generation = target->map_generation;
        e->entry_index = i;
        e->interface_number = target->interface_number;
        e->endpoint_number = target->endpoint_number;
        e->report_id = layout->report_id;
        e->usage_page = field->usage_page;
        e->usage = field->usage;
        e->bit_offset = field->bit_offset;
        e->bit_width = field->bit_width;
        e->flags = entry_flags(field);
        e->logical_minimum = field->logical_minimum;
        e->logical_maximum = field->logical_maximum;
        e->report_length = target->report_length;
    }
    return count;
}

uint32_t inj_map_entries_crc32(const inj_map_entry_payload_t *entries, uint8_t count)
{
    // The payload structs are packed and asserted to be exactly one payload
    // (INJ_FRAME_PAYLOAD_SIZE) wide, so the array IS the concatenated bytes.
    return inj_crc32((const uint8_t *)entries, (uint32_t)count * INJ_FRAME_PAYLOAD_SIZE);
}
