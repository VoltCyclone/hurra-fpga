// USB descriptors for the CPU0 CDC console -- migration step 4.
//
// Design doc section 7 predicted that the CH32's usb_cdc_fs.c (644 lines, 627
// of them guarded) would collapse to "~17 lines of TinyUSB glue plus
// descriptors". This file and usb_console.c are that collapse. The CH32 file
// hand-wrote the endpoint state machine, the DMA buffer aliasing and every
// chapter-9 response; none of that survives, because TinyUSB owns it.
//
// There is no MMIO here, so no guard -- but this file is not host-tested
// either: it is a table whose only consumer is the vendored stack, and its
// correctness is established by a host enumerating the device, which is
// step 4's first gate.
//
// --- Device identity -------------------------------------------------------
//
// 0x1209:0x0001 is pid.codes' own test/prototype allocation. It is deliberate
// and it is not a placeholder to be "improved" by picking something that
// looks more official: an invented VID squats on a real vendor's assigned ID,
// so the host's driver database can bind the wrong driver to this device and
// -- worse -- bind this VID/PID's driver to theirs. pid.codes exists to make
// that unnecessary. Change it only to another pid.codes allocation, or to an
// ID this project actually owns.
//
// --- Serial number ---------------------------------------------------------
//
// A fixed placeholder, and section 10 of the design doc says why nothing else
// is available yet: SYSCON->DIEID is a *revision and die* number, identical
// across every board of the same revision, so using it would advertise a
// unique-looking serial that is not unique -- strictly worse than a constant,
// because a host would cache per-serial state against it. Real identity on
// this part comes from the PUF, and a PUF transaction must never sit on the
// boot path between mcu_ready and the foreground loop (section 10 again), so
// it cannot simply be read here. A constant is honest: two boards on one host
// present as two instances of the same serial, which is visible rather than
// subtly wrong.

#include "tusb.h"

#define USB_VID 0x1209  // pid.codes
#define USB_PID 0x0001  // pid.codes test/prototype PID

enum {
    STRING_LANGID = 0,
    STRING_MANUFACTURER,
    STRING_PRODUCT,
    STRING_SERIAL,
    STRING_CDC_INTERFACE,
    STRING_COUNT,
};

static const tusb_desc_device_t s_device_descriptor = {
    .bLength = sizeof(tusb_desc_device_t),
    .bDescriptorType = TUSB_DESC_DEVICE,
    .bcdUSB = 0x0200,

    // CDC-ACM puts the class on the interface association, not the device, so
    // the device triple is the miscellaneous/common/IAD encoding. Getting this
    // wrong is the classic "enumerates but the CDC data interface never
    // binds" failure on Windows.
    .bDeviceClass = TUSB_CLASS_MISC,
    .bDeviceSubClass = MISC_SUBCLASS_COMMON,
    .bDeviceProtocol = MISC_PROTOCOL_IAD,

    .bMaxPacketSize0 = CFG_TUD_ENDPOINT0_SIZE,

    .idVendor = USB_VID,
    .idProduct = USB_PID,
    .bcdDevice = 0x0100,

    .iManufacturer = STRING_MANUFACTURER,
    .iProduct = STRING_PRODUCT,
    .iSerialNumber = STRING_SERIAL,

    .bNumConfigurations = 1,
};

const uint8_t *tud_descriptor_device_cb(void)
{
    return (const uint8_t *)&s_device_descriptor;
}

// --- Configuration ---------------------------------------------------------

enum {
    ITF_NUM_CDC = 0,
    ITF_NUM_CDC_DATA,
    ITF_NUM_TOTAL,
};

#define EPNUM_CDC_NOTIF 0x81
#define EPNUM_CDC_OUT 0x02
#define EPNUM_CDC_IN 0x82

#define CONFIG_TOTAL_LEN (TUD_CONFIG_DESC_LEN + TUD_CDC_DESC_LEN)

// The bulk max packet size is the one field that differs between the speeds,
// so the two configurations are generated from one macro with it substituted.
// A High Speed bulk endpoint MUST declare 512; a Full Speed one MUST declare
// 64, and the other-speed descriptor below is what a host reads to learn what
// it would get after a speed change.
#define CDC_CONFIG_DESC(bulk_mps)                                                            \
    TUD_CONFIG_DESCRIPTOR(1, ITF_NUM_TOTAL, 0, CONFIG_TOTAL_LEN, 0x00, 100),                 \
        TUD_CDC_DESCRIPTOR(ITF_NUM_CDC, STRING_CDC_INTERFACE, EPNUM_CDC_NOTIF, 8,            \
                           EPNUM_CDC_OUT, EPNUM_CDC_IN, (bulk_mps))

static const uint8_t s_config_high_speed[] = {CDC_CONFIG_DESC(512)};
static const uint8_t s_config_full_speed[] = {CDC_CONFIG_DESC(64)};

const uint8_t *tud_descriptor_configuration_cb(uint8_t index)
{
    (void)index;
    return (tud_speed_get() == TUSB_SPEED_HIGH) ? s_config_high_speed : s_config_full_speed;
}

// GET_DESCRIPTOR(OTHER_SPEED_CONFIGURATION) returns what the *other* speed
// would look like, with the descriptor type rewritten. TinyUSB's weak default
// returns NULL, which stalls -- legal-ish but it makes a High Speed device
// look broken to a host that asks, so answer properly.
const uint8_t *tud_descriptor_other_speed_configuration_cb(uint8_t index)
{
    (void)index;

    static uint8_t other_speed[CONFIG_TOTAL_LEN];
    const uint8_t *source =
        (tud_speed_get() == TUSB_SPEED_HIGH) ? s_config_full_speed : s_config_high_speed;

    for (uint32_t i = 0u; i < CONFIG_TOTAL_LEN; ++i) {
        other_speed[i] = source[i];
    }
    other_speed[1] = TUSB_DESC_OTHER_SPEED_CONFIG;
    return other_speed;
}

// Required of every High Speed-capable device. Same identity, no
// configurations enumerated here (the other-speed descriptor carries those).
static const tusb_desc_device_qualifier_t s_device_qualifier = {
    .bLength = sizeof(tusb_desc_device_qualifier_t),
    .bDescriptorType = TUSB_DESC_DEVICE_QUALIFIER,
    .bcdUSB = 0x0200,
    .bDeviceClass = TUSB_CLASS_MISC,
    .bDeviceSubClass = MISC_SUBCLASS_COMMON,
    .bDeviceProtocol = MISC_PROTOCOL_IAD,
    .bMaxPacketSize0 = CFG_TUD_ENDPOINT0_SIZE,
    .bNumConfigurations = 1,
    .bReserved = 0,
};

const uint8_t *tud_descriptor_device_qualifier_cb(void)
{
    return (const uint8_t *)&s_device_qualifier;
}

// --- Strings ---------------------------------------------------------------

static const char *const s_strings[STRING_COUNT] = {
    [STRING_LANGID] = NULL,  // handled separately; see below
    [STRING_MANUFACTURER] = "hurra",
    [STRING_PRODUCT] = "hurra-adapter",
    // Fixed placeholder. See the header comment: DIEID is revision/die and
    // the PUF may not be touched on the boot path.
    [STRING_SERIAL] = "000000000001",
    [STRING_CDC_INTERFACE] = "hurra-adapter console",
};

const uint16_t *tud_descriptor_string_cb(uint8_t index, uint16_t langid)
{
    (void)langid;

    // UTF-16LE, built in place. The +1 is the length/type header word; the
    // cap is the 255-byte bLength field, so 126 characters plus the header.
    static uint16_t buffer[127];
    uint8_t count = 0u;

    if (index == STRING_LANGID) {
        buffer[1] = 0x0409;  // English (United States)
        count = 1u;
    } else {
        if (index >= STRING_COUNT) {
            return NULL;
        }
        const char *text = s_strings[index];
        if (text == NULL) {
            return NULL;
        }
        // ASCII only, so the widening is a cast rather than a conversion. Any
        // non-ASCII byte here would silently produce a wrong code point, which
        // is why the table above is plain ASCII by construction.
        while (text[count] != '\0' && count < (sizeof(buffer) / sizeof(buffer[0])) - 1u) {
            buffer[count + 1u] = (uint16_t)(uint8_t)text[count];
            count++;
        }
    }

    buffer[0] = (uint16_t)((uint16_t)(TUSB_DESC_STRING << 8) | (uint16_t)(2u * count + 2u));
    return buffer;
}
