#include "hid_report_decoder.h"

#include <stdint.h>

typedef struct {
    char text[HID_REPORT_TEXT_LINE_CHARS + 1u];
    size_t length;
} line_builder_t;

static void line_init(line_builder_t *line)
{
    line->length = 0u;
    line->text[0] = '\0';
}

static void line_char(line_builder_t *line, char value)
{
    if (line->length >= HID_REPORT_TEXT_LINE_CHARS) {
        return;
    }
    line->text[line->length] = value;
    line->length++;
    line->text[line->length] = '\0';
}

static void line_text(line_builder_t *line, const char *text)
{
    while (*text != '\0') {
        line_char(line, *text);
        text++;
    }
}

static char hex_digit(uint8_t value)
{
    return value < 10u ? (char)('0' + value)
                       : (char)('A' + (uint8_t)(value - 10u));
}

static void line_hex(line_builder_t *line, uint32_t value, uint8_t digits)
{
    line_text(line, "0x");
    for (uint8_t i = 0u; i < digits; ++i) {
        const uint8_t shift = (uint8_t)((digits - i - 1u) * 4u);
        line_char(line, hex_digit((uint8_t)((value >> shift) & 0x0fu)));
    }
}

static void line_unsigned(line_builder_t *line, uint64_t value)
{
    char reverse[20];
    size_t count = 0u;
    do {
        reverse[count] = (char)('0' + (value % 10u));
        value /= 10u;
        count++;
    } while (value != 0u && count < sizeof(reverse));

    while (count > 0u) {
        count--;
        line_char(line, reverse[count]);
    }
}

static void line_signed(line_builder_t *line, int64_t value)
{
    uint64_t magnitude;
    if (value < 0) {
        line_char(line, '-');
        magnitude = (uint64_t)(-(value + 1)) + UINT64_C(1);
    } else {
        magnitude = (uint64_t)value;
    }
    line_unsigned(line, magnitude);
}

static void emit_line(hid_report_decode_result_t *result,
                      hid_report_line_fn emit, void *context,
                      const line_builder_t *line)
{
    result->lines++;
    if (emit != NULL) {
        emit(context, line->text);
    }
}

static uint32_t unsigned_value(const uint8_t *data, size_t size)
{
    uint32_t value = 0u;
    for (size_t i = 0u; i < size; ++i) {
        value |= (uint32_t)data[i] << (uint8_t)(i * 8u);
    }
    return value;
}

static int64_t signed_value(uint32_t value, size_t size)
{
    if (size == 0u) {
        return 0;
    }
    const uint8_t bits = (uint8_t)(size * 8u);
    const uint64_t wide = value;
    if ((wide & (UINT64_C(1) << (bits - 1u))) == 0u) {
        return (int64_t)wide;
    }
    return (int64_t)(wide - (UINT64_C(1) << bits));
}

static const char *usage_page_name(uint16_t page)
{
    switch (page) {
    case 0x01u:
        return "Generic Desktop";
    case 0x09u:
        return "Button";
    case 0x0cu:
        return "Consumer";
    default:
        return NULL;
    }
}

static const char *generic_desktop_usage(uint16_t usage)
{
    switch (usage) {
    case 0x01u:
        return "Pointer";
    case 0x02u:
        return "Mouse";
    case 0x04u:
        return "Joystick";
    case 0x05u:
        return "Game Pad";
    case 0x06u:
        return "Keyboard";
    case 0x07u:
        return "Keypad";
    case 0x08u:
        return "Multi-axis Controller";
    case 0x30u:
        return "X";
    case 0x31u:
        return "Y";
    case 0x32u:
        return "Z";
    case 0x33u:
        return "Rx";
    case 0x34u:
        return "Ry";
    case 0x35u:
        return "Rz";
    case 0x36u:
        return "Slider";
    case 0x37u:
        return "Dial";
    case 0x38u:
        return "Wheel";
    case 0x39u:
        return "Hat Switch";
    case 0x48u:
        return "Resolution Multiplier";
    default:
        return NULL;
    }
}

static const char *consumer_usage(uint16_t usage)
{
    switch (usage) {
    case 0x0001u:
        return "Consumer Control";
    case 0x00b0u:
        return "Play";
    case 0x00b1u:
        return "Pause";
    case 0x00b5u:
        return "Next Track";
    case 0x00b6u:
        return "Previous Track";
    case 0x00b7u:
        return "Stop";
    case 0x00cdu:
        return "Play/Pause";
    case 0x00e2u:
        return "Mute";
    case 0x00e9u:
        return "Volume Up";
    case 0x00eau:
        return "Volume Down";
    case 0x0238u:
        return "AC Pan";
    default:
        return NULL;
    }
}

static const char *usage_name(uint16_t page, uint16_t usage)
{
    if (page == 0x01u) {
        return generic_desktop_usage(usage);
    }
    if (page == 0x0cu) {
        return consumer_usage(usage);
    }
    return NULL;
}

static void emit_usage(hid_report_decode_result_t *result,
                       hid_report_line_fn emit, void *context,
                       const char *label, uint16_t page, uint16_t usage)
{
    line_builder_t line;
    line_init(&line);
    line_text(&line, label);
    line_text(&line, ": ");

    const char *const name = usage_name(page, usage);
    if (name != NULL) {
        line_text(&line, name);
    } else if (page == 0x09u) {
        line_text(&line, "Button ");
        line_unsigned(&line, usage);
    } else {
        line_hex(&line, page, 4u);
        line_char(&line, ':');
        line_hex(&line, usage, 4u);
    }
    emit_line(result, emit, context, &line);
}

static const char *collection_name(uint32_t value)
{
    switch (value) {
    case 0u:
        return "Physical";
    case 1u:
        return "Application";
    case 2u:
        return "Logical";
    case 3u:
        return "Report";
    case 4u:
        return "Named Array";
    case 5u:
        return "Usage Switch";
    case 6u:
        return "Usage Modifier";
    default:
        return NULL;
    }
}

static void emit_main_flags(hid_report_decode_result_t *result,
                            hid_report_line_fn emit, void *context,
                            const char *label, uint32_t flags, bool has_storage)
{
    line_builder_t line;
    line_init(&line);
    line_text(&line, label);
    line_text(&line, ": ");
    line_text(&line, (flags & 0x01u) != 0u ? "Const" : "Data");
    line_char(&line, ' ');
    line_text(&line, (flags & 0x02u) != 0u ? "Var" : "Array");
    line_char(&line, ' ');
    line_text(&line, (flags & 0x04u) != 0u ? "Relative" : "Absolute");
    emit_line(result, emit, context, &line);

    line_init(&line);
    line_text(&line, "  ");
    line_text(&line, (flags & 0x08u) != 0u ? "Wrap" : "NoWrap");
    line_char(&line, ' ');
    line_text(&line, (flags & 0x10u) != 0u ? "NonLinear" : "Linear");
    line_char(&line, ' ');
    line_text(&line, (flags & 0x20u) != 0u ? "NoPreferred" : "Preferred");
    emit_line(result, emit, context, &line);

    line_init(&line);
    line_text(&line, "  ");
    line_text(&line, (flags & 0x40u) != 0u ? "Null" : "NoNull");
    if (has_storage) {
        line_char(&line, ' ');
        line_text(&line, (flags & 0x80u) != 0u ? "Volatile" : "NonVolatile");
        emit_line(result, emit, context, &line);

        line_init(&line);
        line_text(&line, "  ");
        line_text(&line, (flags & 0x100u) != 0u ? "BufferedBytes" : "BitField");
    } else {
        line_char(&line, ' ');
        line_text(&line, (flags & 0x100u) != 0u ? "BufferedBytes" : "BitField");
    }
    emit_line(result, emit, context, &line);
}

static void emit_unknown(hid_report_decode_result_t *result,
                         hid_report_line_fn emit, void *context,
                         const char *type, uint8_t tag, uint32_t value,
                         size_t size)
{
    line_builder_t line;
    line_init(&line);
    line_text(&line, type);
    line_text(&line, " 0x");
    line_char(&line, hex_digit(tag));
    line_text(&line, ": ");
    line_hex(&line, value, size == 0u ? 1u : (uint8_t)(size * 2u));
    emit_line(result, emit, context, &line);
}

static void emit_truncated(hid_report_decode_result_t *result,
                           hid_report_line_fn emit, void *context,
                           const char *kind, size_t offset)
{
    line_builder_t line;
    line_init(&line);
    line_text(&line, "Truncated ");
    line_text(&line, kind);
    line_text(&line, " @");
    line_hex(&line, (uint32_t)offset, offset <= 0xffu ? 2u : 4u);
    emit_line(result, emit, context, &line);
}

static void emit_long_item(hid_report_decode_result_t *result,
                           hid_report_line_fn emit, void *context, uint8_t tag,
                           const uint8_t *data, size_t size)
{
    line_builder_t line;
    line_init(&line);
    line_text(&line, "Long ");
    line_hex(&line, tag, 2u);
    line_char(&line, ':');

    size_t i = 0u;
    while (i < size && line.length + 3u <= HID_REPORT_TEXT_LINE_CHARS) {
        line_char(&line, ' ');
        line_char(&line, hex_digit((uint8_t)(data[i] >> 4u)));
        line_char(&line, hex_digit((uint8_t)(data[i] & 0x0fu)));
        i++;
    }
    if (i < size) {
        while (line.length > HID_REPORT_TEXT_LINE_CHARS - 4u) {
            line.length--;
        }
        line.text[line.length] = '\0';
        line_text(&line, " ...");
    }
    emit_line(result, emit, context, &line);
}

hid_report_decode_result_t hid_report_decode(const uint8_t *descriptor,
                                             size_t length,
                                             hid_report_line_fn emit,
                                             void *context)
{
    hid_report_decode_result_t result = {0};
    uint16_t usage_page = 0u;
    bool logical_minimum_negative = false;
    size_t collection_depth = 0u;
    size_t offset = 0u;

    if (descriptor == NULL && length != 0u) {
        line_builder_t line;
        line_init(&line);
        line_text(&line, "Invalid descriptor pointer");
        emit_line(&result, emit, context, &line);
        result.malformed = true;
        return result;
    }

    while (offset < length) {
        const size_t item_offset = offset;
        const uint8_t prefix = descriptor[offset];
        offset++;

        if (prefix == 0xfeu) {
            if (length - offset < 2u) {
                result.malformed = true;
                emit_truncated(&result, emit, context, "long item", item_offset);
                break;
            }
            const size_t long_size = descriptor[offset];
            const uint8_t long_tag = descriptor[offset + 1u];
            offset += 2u;
            if (long_size > length - offset) {
                result.malformed = true;
                emit_truncated(&result, emit, context, "long item", item_offset);
                break;
            }
            emit_long_item(&result, emit, context, long_tag,
                           &descriptor[offset], long_size);
            result.items++;
            offset += long_size;
            continue;
        }

        const uint8_t size_code = (uint8_t)(prefix & 0x03u);
        const size_t item_size = size_code == 3u ? 4u : size_code;
        const uint8_t type = (uint8_t)((prefix >> 2u) & 0x03u);
        const uint8_t tag = (uint8_t)(prefix >> 4u);
        if (item_size > length - offset) {
            result.malformed = true;
            emit_truncated(&result, emit, context, "item", item_offset);
            break;
        }

        const uint32_t value = unsigned_value(&descriptor[offset], item_size);
        const int64_t signed_item = signed_value(value, item_size);
        offset += item_size;
        result.items++;

        if (type == 0u) {
            if (tag == 8u) {
                emit_main_flags(&result, emit, context, "Input", value, false);
            } else if (tag == 9u) {
                emit_main_flags(&result, emit, context, "Output", value, true);
            } else if (tag == 10u) {
                line_builder_t line;
                line_init(&line);
                line_text(&line, "Collection: ");
                const char *const name = collection_name(value);
                if (name != NULL) {
                    line_text(&line, name);
                } else {
                    line_hex(&line, value, item_size == 0u ? 1u : (uint8_t)(item_size * 2u));
                }
                emit_line(&result, emit, context, &line);
                collection_depth++;
            } else if (tag == 11u) {
                emit_main_flags(&result, emit, context, "Feature", value, true);
            } else if (tag == 12u) {
                line_builder_t line;
                line_init(&line);
                if (collection_depth == 0u) {
                    line_text(&line, "End Collection (unmatched)");
                    result.malformed = true;
                } else {
                    collection_depth--;
                    line_text(&line, "End Collection");
                }
                emit_line(&result, emit, context, &line);
            } else {
                emit_unknown(&result, emit, context, "Main", tag, value, item_size);
            }
        } else if (type == 1u) {
            line_builder_t line;
            line_init(&line);
            if (tag == 0u) {
                usage_page = (uint16_t)value;
                line_text(&line, "Usage Page: ");
                const char *const name = usage_page_name(usage_page);
                if (name != NULL) {
                    line_text(&line, name);
                } else {
                    line_hex(&line, value, item_size == 0u ? 1u : (uint8_t)(item_size * 2u));
                }
            } else if (tag == 1u) {
                logical_minimum_negative = signed_item < 0;
                line_text(&line, "Logical Min: ");
                line_signed(&line, signed_item);
            } else if (tag == 2u) {
                line_text(&line, "Logical Max: ");
                if (logical_minimum_negative) {
                    line_signed(&line, signed_item);
                } else {
                    line_unsigned(&line, value);
                }
            } else if (tag == 7u) {
                line_text(&line, "Report Size: ");
                line_unsigned(&line, value);
            } else if (tag == 8u) {
                line_text(&line, "Report ID: ");
                line_unsigned(&line, value);
            } else if (tag == 9u) {
                line_text(&line, "Report Count: ");
                line_unsigned(&line, value);
            } else {
                emit_unknown(&result, emit, context, "Global", tag, value, item_size);
                continue;
            }
            emit_line(&result, emit, context, &line);
        } else if (type == 2u && tag <= 2u) {
            const uint16_t item_page = item_size == 4u
                                           ? (uint16_t)(value >> 16u)
                                           : usage_page;
            const uint16_t usage = (uint16_t)value;
            const char *const label =
                tag == 0u ? "Usage" : (tag == 1u ? "Usage Min" : "Usage Max");
            emit_usage(&result, emit, context, label, item_page, usage);
        } else if (type == 2u) {
            emit_unknown(&result, emit, context, "Local", tag, value, item_size);
        } else {
            emit_unknown(&result, emit, context, "Reserved", tag, value, item_size);
        }
    }

    result.unclosed_collections = collection_depth;
    if (collection_depth != 0u) {
        line_builder_t line;
        line_init(&line);
        line_text(&line, "Unclosed Collections: ");
        line_unsigned(&line, collection_depth);
        emit_line(&result, emit, context, &line);
        result.malformed = true;
    }
    return result;
}
