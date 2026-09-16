/* Generated report-injection wire contract; do not edit by hand. */
#ifndef INJECTION_WIRE_H
#define INJECTION_WIRE_H

#include <stddef.h>
#include <stdint.h>

#define INJ_FRAME_SOF 0x68u
#define INJ_FRAME_SIZE 32u
#define INJ_FRAME_HEADER_SIZE 4u
#define INJ_FRAME_PAYLOAD_SIZE 26u
#define INJ_CRC16_INIT 0xFFFFu
#define INJ_CRC16_POLY 0x1021u
#define INJ_CRC32_INIT 0xFFFFFFFFu
#define INJ_CRC32_POLY 0xEDB88320u
#define INJ_CRC32_XOROUT 0xFFFFFFFFu

#define INJ_MAX_DESCRIPTOR_BYTES_PER_INTERFACE 2048u
#define INJ_MAX_FIELDS 64u
#define INJ_MAX_INTERFACES 4u
#define INJ_MAX_LAYOUTS 16u
#define INJ_MAX_REPORT_BYTES 64u

#define INJ_TYPE_IDLE 0x00u
#define INJ_TYPE_LINK_STATUS 0x01u
#define INJ_TYPE_DESCRIPTOR_FRAGMENT 0x02u
#define INJ_TYPE_REPORT_FRAGMENT 0x03u
#define INJ_TYPE_MAP_STATUS 0x04u
#define INJ_TYPE_COMMAND_ACK 0x05u
#define INJ_TYPE_COUNTERS 0x06u
#define INJ_TYPE_MAP_BEGIN 0x81u
#define INJ_TYPE_MAP_ENTRY 0x82u
#define INJ_TYPE_MAP_COMMIT 0x83u
#define INJ_TYPE_RELATIVE 0x84u
#define INJ_TYPE_BUTTON_STATE 0x85u
#define INJ_TYPE_PHYSICAL_MASK 0x86u
#define INJ_TYPE_CLEAR 0x87u
#define INJ_TYPE_TELEMETRY_CONFIG 0x88u
#define INJ_CLEAR_FLAG_MOTION 1u
#define INJ_CLEAR_FLAG_BUTTONS 2u
#define INJ_CLEAR_FLAG_PHYSICAL_MASKS 4u
#define INJ_CLEAR_FLAG_QUEUED_TIMED 8u
#define INJ_CLEAR_FLAG_ALL 15u
#define INJ_COMMAND_ACK_FLAG_LATE 1u
#define INJ_COMMAND_ACK_FLAG_SYNTHESIZED 2u
#define INJ_COMMAND_ACK_RESULT_SUCCESS 0u
#define INJ_COMMAND_ACK_RESULT_STALE_LEASE 1u
#define INJ_COMMAND_ACK_RESULT_STALE_MAP 2u
#define INJ_COMMAND_ACK_RESULT_BUSY 3u
#define INJ_COMMAND_ACK_RESULT_OVERFLOW 4u
#define INJ_COMMAND_ACK_RESULT_UNSUPPORTED_TARGET 5u
#define INJ_COMMAND_ACK_RESULT_CLEARED 6u
#define INJ_COMMAND_ACK_STAGE_ADMITTED 0u
#define INJ_COMMAND_ACK_STAGE_COMPLETED 1u
#define INJ_COMMAND_ACK_STAGE_REJECTED 2u
#define INJ_COUNTERS_PAGE_LINK 0u
#define INJ_COUNTERS_PAGE_COMMAND 1u
#define INJ_COUNTERS_PAGE_REPORT_MAP 2u
#define INJ_LINK_STATUS_FLAG_ENUMERATED 1u
#define INJ_LINK_STATUS_FLAG_MAP_ACTIVE 2u
#define INJ_LINK_STATUS_FLAG_INJECTION_ENABLED 4u
#define INJ_LINK_STATUS_FLAG_RELAY_READY 8u
#define INJ_MAP_ENTRY_FLAG_SIGNED 1u
#define INJ_MAP_ENTRY_FLAG_RELATIVE 2u
#define INJ_MAP_ENTRY_FLAG_BUTTON 4u
#define INJ_MAP_ENTRY_FLAG_X 8u
#define INJ_MAP_ENTRY_FLAG_Y 16u
#define INJ_MAP_ENTRY_FLAG_WHEEL 32u
#define INJ_MAP_ENTRY_FLAG_PAN 64u
#define INJ_MAP_STATUS_ERROR_NONE 0u
#define INJ_MAP_STATUS_ERROR_DESCRIPTOR_GENERATION 1u
#define INJ_MAP_STATUS_ERROR_MAP_GENERATION 2u
#define INJ_MAP_STATUS_ERROR_ENTRY_COUNT 3u
#define INJ_MAP_STATUS_ERROR_LAYOUT_COUNT 4u
#define INJ_MAP_STATUS_ERROR_CRC 5u
#define INJ_MAP_STATUS_ERROR_INTERFACE 6u
#define INJ_MAP_STATUS_ERROR_ENDPOINT 7u
#define INJ_MAP_STATUS_ERROR_REPORT_LENGTH 8u
#define INJ_MAP_STATUS_ERROR_FIELD_WIDTH 9u
#define INJ_MAP_STATUS_ERROR_BIT_OFFSET 10u
#define INJ_MAP_STATUS_ERROR_REPORT_ID_PREFIX 11u
#define INJ_MAP_STATUS_ERROR_OVERLAP 12u
#define INJ_MAP_STATUS_ERROR_UNSUPPORTED_FIELD 13u
#define INJ_MAP_STATUS_ERROR_DUPLICATE_ENTRY 14u
#define INJ_MAP_STATUS_ERROR_INTERNAL 15u
#define INJ_MAP_STATUS_STATUS_CANDIDATE_ACCEPTED 0u
#define INJ_MAP_STATUS_STATUS_COMMIT_ACCEPTED 1u
#define INJ_MAP_STATUS_STATUS_REJECTED 2u
#define INJ_MAP_STATUS_STATUS_ABORTED 3u
#define INJ_RELATIVE_FLAG_X 1u
#define INJ_RELATIVE_FLAG_Y 2u
#define INJ_RELATIVE_FLAG_WHEEL 4u
#define INJ_RELATIVE_FLAG_PAN 8u
#define INJ_COUNTER_COMMAND_ACCEPTED 0u
#define INJ_COUNTER_COMMAND_REJECTED 1u
#define INJ_COUNTER_COMMAND_LATE 2u
#define INJ_COUNTER_COMMAND_QUEUE_FULL 3u
#define INJ_COUNTER_COMMAND_COMPLETED 4u
#define INJ_COUNTER_COMMAND_OVERFLOW 5u
#define INJ_COUNTER_LINK_BAD_SOF 0u
#define INJ_COUNTER_LINK_BAD_CRC 1u
#define INJ_COUNTER_LINK_BAD_LENGTH 2u
#define INJ_COUNTER_LINK_BAD_TYPE 3u
#define INJ_COUNTER_LINK_SEQUENCE_GAP 4u
#define INJ_COUNTER_LINK_DUPLICATE 5u
#define INJ_COUNTER_REPORT_MAP_NATIVE 0u
#define INJ_COUNTER_REPORT_MAP_MUTATED 1u
#define INJ_COUNTER_REPORT_MAP_SYNTHESIZED 2u
#define INJ_COUNTER_REPORT_MAP_MONITORING_DROP 3u
#define INJ_COUNTER_REPORT_MAP_MAP_COMMIT 4u
#define INJ_COUNTER_REPORT_MAP_MAP_REJECT 5u

#define INJ_BUTTON_STATE_LEASE_GENERATION_OFFSET 0u
#define INJ_BUTTON_STATE_MAP_GENERATION_OFFSET 2u
#define INJ_BUTTON_STATE_COMMAND_SEQUENCE_OFFSET 4u
#define INJ_BUTTON_STATE_TARGET_FRAME_OFFSET 6u
#define INJ_BUTTON_STATE_INTERFACE_NUMBER_OFFSET 8u
#define INJ_BUTTON_STATE_ENDPOINT_NUMBER_OFFSET 9u
#define INJ_BUTTON_STATE_REPORT_ID_OFFSET 10u
#define INJ_BUTTON_STATE_FLAGS_OFFSET 11u
#define INJ_BUTTON_STATE_BUTTONS_OFFSET 12u
#define INJ_BUTTON_STATE_HOLD_REPORTS_OFFSET 20u
#define INJ_CLEAR_LEASE_GENERATION_OFFSET 0u
#define INJ_CLEAR_MAP_GENERATION_OFFSET 2u
#define INJ_CLEAR_COMMAND_SEQUENCE_OFFSET 4u
#define INJ_CLEAR_TARGET_FRAME_OFFSET 6u
#define INJ_CLEAR_CLEAR_FLAGS_OFFSET 8u
#define INJ_CLEAR_REASON_OFFSET 10u
#define INJ_COMMAND_ACK_LEASE_GENERATION_OFFSET 0u
#define INJ_COMMAND_ACK_MAP_GENERATION_OFFSET 2u
#define INJ_COMMAND_ACK_COMMAND_SEQUENCE_OFFSET 4u
#define INJ_COMMAND_ACK_TARGET_FRAME_OFFSET 6u
#define INJ_COMMAND_ACK_ACCEPTED_FRAME_OFFSET 8u
#define INJ_COMMAND_ACK_COMPLETED_FRAME_OFFSET 10u
#define INJ_COMMAND_ACK_ACK_STAGE_OFFSET 12u
#define INJ_COMMAND_ACK_RESULT_OFFSET 13u
#define INJ_COMMAND_ACK_COMMAND_TYPE_OFFSET 14u
#define INJ_COMMAND_ACK_FLAGS_OFFSET 15u
#define INJ_COMMAND_ACK_INTERFACE_NUMBER_OFFSET 16u
#define INJ_COMMAND_ACK_ENDPOINT_NUMBER_OFFSET 17u
#define INJ_COMMAND_ACK_REPORT_ID_OFFSET 18u
#define INJ_COUNTERS_PAGE_OFFSET 0u
#define INJ_COUNTERS_FLAGS_OFFSET 1u
#define INJ_COUNTERS_COUNTER0_OFFSET 2u
#define INJ_COUNTERS_COUNTER1_OFFSET 6u
#define INJ_COUNTERS_COUNTER2_OFFSET 10u
#define INJ_COUNTERS_COUNTER3_OFFSET 14u
#define INJ_COUNTERS_COUNTER4_OFFSET 18u
#define INJ_COUNTERS_COUNTER5_OFFSET 22u
#define INJ_DESCRIPTOR_FRAGMENT_DESCRIPTOR_GENERATION_OFFSET 0u
#define INJ_DESCRIPTOR_FRAGMENT_INTERFACE_NUMBER_OFFSET 2u
#define INJ_DESCRIPTOR_FRAGMENT_OFFSET_OFFSET 4u
#define INJ_DESCRIPTOR_FRAGMENT_TOTAL_OFFSET 6u
#define INJ_DESCRIPTOR_FRAGMENT_DATA_OFFSET 8u
#define INJ_LINK_STATUS_USB_FRAME_OFFSET 0u
#define INJ_LINK_STATUS_USB_SUBFRAME_OFFSET 2u
#define INJ_LINK_STATUS_LINK_FLAGS_OFFSET 3u
#define INJ_LINK_STATUS_DESCRIPTOR_GENERATION_OFFSET 4u
#define INJ_LINK_STATUS_ACTIVE_MAP_GENERATION_OFFSET 6u
#define INJ_LINK_STATUS_SLOT_COUNTER_OFFSET 8u
#define INJ_LINK_STATUS_NATIVE_REPORT_COUNT_OFFSET 12u
#define INJ_LINK_STATUS_FAULT_FLAGS_OFFSET 16u
#define INJ_LINK_STATUS_LAST_RX_SEQUENCE_OFFSET 20u
#define INJ_MAP_BEGIN_DESCRIPTOR_GENERATION_OFFSET 0u
#define INJ_MAP_BEGIN_MAP_GENERATION_OFFSET 2u
#define INJ_MAP_BEGIN_ENTRY_COUNT_OFFSET 4u
#define INJ_MAP_BEGIN_LAYOUT_COUNT_OFFSET 5u
#define INJ_MAP_BEGIN_FLAGS_OFFSET 6u
#define INJ_MAP_BEGIN_ENTRIES_CRC32_OFFSET 8u
#define INJ_MAP_COMMIT_DESCRIPTOR_GENERATION_OFFSET 0u
#define INJ_MAP_COMMIT_MAP_GENERATION_OFFSET 2u
#define INJ_MAP_COMMIT_ENTRY_COUNT_OFFSET 4u
#define INJ_MAP_COMMIT_LAYOUT_COUNT_OFFSET 5u
#define INJ_MAP_COMMIT_FLAGS_OFFSET 6u
#define INJ_MAP_COMMIT_ENTRIES_CRC32_OFFSET 8u
#define INJ_MAP_ENTRY_DESCRIPTOR_GENERATION_OFFSET 0u
#define INJ_MAP_ENTRY_MAP_GENERATION_OFFSET 2u
#define INJ_MAP_ENTRY_ENTRY_INDEX_OFFSET 4u
#define INJ_MAP_ENTRY_INTERFACE_NUMBER_OFFSET 5u
#define INJ_MAP_ENTRY_ENDPOINT_NUMBER_OFFSET 6u
#define INJ_MAP_ENTRY_REPORT_ID_OFFSET 7u
#define INJ_MAP_ENTRY_USAGE_PAGE_OFFSET 8u
#define INJ_MAP_ENTRY_USAGE_OFFSET 10u
#define INJ_MAP_ENTRY_BIT_OFFSET_OFFSET 12u
#define INJ_MAP_ENTRY_BIT_WIDTH_OFFSET 14u
#define INJ_MAP_ENTRY_FLAGS_OFFSET 15u
#define INJ_MAP_ENTRY_LOGICAL_MINIMUM_OFFSET 16u
#define INJ_MAP_ENTRY_LOGICAL_MAXIMUM_OFFSET 20u
#define INJ_MAP_ENTRY_REPORT_LENGTH_OFFSET 24u
#define INJ_MAP_STATUS_DESCRIPTOR_GENERATION_OFFSET 0u
#define INJ_MAP_STATUS_MAP_GENERATION_OFFSET 2u
#define INJ_MAP_STATUS_ACTIVE_MAP_GENERATION_OFFSET 4u
#define INJ_MAP_STATUS_ENTRY_INDEX_OFFSET 6u
#define INJ_MAP_STATUS_STATUS_OFFSET 7u
#define INJ_MAP_STATUS_ERROR_OFFSET 8u
#define INJ_MAP_STATUS_FLAGS_OFFSET 9u
#define INJ_MAP_STATUS_ENTRIES_CRC32_OFFSET 10u
#define INJ_PHYSICAL_MASK_LEASE_GENERATION_OFFSET 0u
#define INJ_PHYSICAL_MASK_MAP_GENERATION_OFFSET 2u
#define INJ_PHYSICAL_MASK_COMMAND_SEQUENCE_OFFSET 4u
#define INJ_PHYSICAL_MASK_TARGET_FRAME_OFFSET 6u
#define INJ_PHYSICAL_MASK_INTERFACE_NUMBER_OFFSET 8u
#define INJ_PHYSICAL_MASK_ENDPOINT_NUMBER_OFFSET 9u
#define INJ_PHYSICAL_MASK_REPORT_ID_OFFSET 10u
#define INJ_PHYSICAL_MASK_FLAGS_OFFSET 11u
#define INJ_PHYSICAL_MASK_BUTTON_MASK_OFFSET 12u
#define INJ_RELATIVE_LEASE_GENERATION_OFFSET 0u
#define INJ_RELATIVE_MAP_GENERATION_OFFSET 2u
#define INJ_RELATIVE_COMMAND_SEQUENCE_OFFSET 4u
#define INJ_RELATIVE_TARGET_FRAME_OFFSET 6u
#define INJ_RELATIVE_INTERFACE_NUMBER_OFFSET 8u
#define INJ_RELATIVE_ENDPOINT_NUMBER_OFFSET 9u
#define INJ_RELATIVE_REPORT_ID_OFFSET 10u
#define INJ_RELATIVE_FLAGS_OFFSET 11u
#define INJ_RELATIVE_X_OFFSET 12u
#define INJ_RELATIVE_Y_OFFSET 14u
#define INJ_RELATIVE_WHEEL_OFFSET 16u
#define INJ_RELATIVE_PAN_OFFSET 18u
#define INJ_RELATIVE_HOLD_REPORTS_OFFSET 20u
#define INJ_REPORT_FRAGMENT_DESCRIPTOR_GENERATION_OFFSET 0u
#define INJ_REPORT_FRAGMENT_INTERFACE_NUMBER_OFFSET 2u
#define INJ_REPORT_FRAGMENT_ENDPOINT_NUMBER_OFFSET 3u
#define INJ_REPORT_FRAGMENT_REPORT_ID_OFFSET 4u
#define INJ_REPORT_FRAGMENT_OFFSET_OFFSET 5u
#define INJ_REPORT_FRAGMENT_TOTAL_OFFSET 7u
#define INJ_REPORT_FRAGMENT_DATA_OFFSET 9u
#define INJ_TELEMETRY_CONFIG_LEASE_GENERATION_OFFSET 0u
#define INJ_TELEMETRY_CONFIG_COMMAND_SEQUENCE_OFFSET 2u
#define INJ_TELEMETRY_CONFIG_TELEMETRY_MASK_OFFSET 4u
#define INJ_TELEMETRY_CONFIG_INTERVAL_SLOTS_OFFSET 8u
#define INJ_TELEMETRY_CONFIG_REPORT_SAMPLE_DIVISOR_OFFSET 10u
#define INJ_TELEMETRY_CONFIG_COUNTER_PAGE_OFFSET 12u
#define INJ_TELEMETRY_CONFIG_FLAGS_OFFSET 13u

static inline uint16_t inj_load_u16_le(const uint8_t *data) {
    return (uint16_t)data[0] | ((uint16_t)data[1] << 8);
}

static inline uint32_t inj_load_u32_le(const uint8_t *data) {
    return (uint32_t)data[0] | ((uint32_t)data[1] << 8) |
           ((uint32_t)data[2] << 16) | ((uint32_t)data[3] << 24);
}

static inline uint64_t inj_load_u64_le(const uint8_t *data) {
    return (uint64_t)inj_load_u32_le(data) | ((uint64_t)inj_load_u32_le(data + 4) << 32);
}

static inline void inj_store_u16_le(uint8_t *data, uint16_t value) {
    data[0] = (uint8_t)value;
    data[1] = (uint8_t)(value >> 8);
}

static inline void inj_store_u32_le(uint8_t *data, uint32_t value) {
    data[0] = (uint8_t)value;
    data[1] = (uint8_t)(value >> 8);
    data[2] = (uint8_t)(value >> 16);
    data[3] = (uint8_t)(value >> 24);
}

static inline void inj_store_u64_le(uint8_t *data, uint64_t value) {
    inj_store_u32_le(data, (uint32_t)value);
    inj_store_u32_le(data + 4, (uint32_t)(value >> 32));
}

typedef struct __attribute__((packed)) {
    uint16_t lease_generation;
    uint16_t map_generation;
    uint16_t command_sequence;
    uint16_t target_frame;
    uint8_t interface_number;
    uint8_t endpoint_number;
    uint8_t report_id;
    uint8_t flags;
    uint64_t buttons;
    uint16_t hold_reports;
    uint32_t reserved;
} inj_button_state_payload_t;
_Static_assert(sizeof(inj_button_state_payload_t) == INJ_FRAME_PAYLOAD_SIZE,
               "button_state payload must fill one slot payload");

typedef struct __attribute__((packed)) {
    uint16_t lease_generation;
    uint16_t map_generation;
    uint16_t command_sequence;
    uint16_t target_frame;
    uint16_t clear_flags;
    uint8_t reason;
    uint8_t reserved[15];
} inj_clear_payload_t;
_Static_assert(sizeof(inj_clear_payload_t) == INJ_FRAME_PAYLOAD_SIZE,
               "clear payload must fill one slot payload");

typedef struct __attribute__((packed)) {
    uint16_t lease_generation;
    uint16_t map_generation;
    uint16_t command_sequence;
    uint16_t target_frame;
    uint16_t accepted_frame;
    uint16_t completed_frame;
    uint8_t ack_stage;
    uint8_t result;
    uint8_t command_type;
    uint8_t flags;
    uint8_t interface_number;
    uint8_t endpoint_number;
    uint8_t report_id;
    uint8_t reserved[7];
} inj_command_ack_payload_t;
_Static_assert(sizeof(inj_command_ack_payload_t) == INJ_FRAME_PAYLOAD_SIZE,
               "command_ack payload must fill one slot payload");

typedef struct __attribute__((packed)) {
    uint8_t page;
    uint8_t flags;
    uint32_t counter0;
    uint32_t counter1;
    uint32_t counter2;
    uint32_t counter3;
    uint32_t counter4;
    uint32_t counter5;
} inj_counters_payload_t;
_Static_assert(sizeof(inj_counters_payload_t) == INJ_FRAME_PAYLOAD_SIZE,
               "counters payload must fill one slot payload");

typedef struct __attribute__((packed)) {
    uint16_t descriptor_generation;
    uint8_t interface_number;
    uint8_t reserved;
    uint16_t offset;
    uint16_t total;
    uint8_t data[18];
} inj_descriptor_fragment_payload_t;
_Static_assert(sizeof(inj_descriptor_fragment_payload_t) == INJ_FRAME_PAYLOAD_SIZE,
               "descriptor_fragment payload must fill one slot payload");

typedef struct __attribute__((packed)) {
    uint8_t reserved[26];
} inj_idle_payload_t;
_Static_assert(sizeof(inj_idle_payload_t) == INJ_FRAME_PAYLOAD_SIZE,
               "idle payload must fill one slot payload");

typedef struct __attribute__((packed)) {
    uint16_t usb_frame;
    uint8_t usb_subframe;
    uint8_t link_flags;
    uint16_t descriptor_generation;
    uint16_t active_map_generation;
    uint32_t slot_counter;
    uint32_t native_report_count;
    uint32_t fault_flags;
    uint8_t last_rx_sequence;
    uint8_t reserved[5];
} inj_link_status_payload_t;
_Static_assert(sizeof(inj_link_status_payload_t) == INJ_FRAME_PAYLOAD_SIZE,
               "link_status payload must fill one slot payload");

typedef struct __attribute__((packed)) {
    uint16_t descriptor_generation;
    uint16_t map_generation;
    uint8_t entry_count;
    uint8_t layout_count;
    uint16_t flags;
    uint32_t entries_crc32;
    uint8_t reserved[14];
} inj_map_begin_payload_t;
_Static_assert(sizeof(inj_map_begin_payload_t) == INJ_FRAME_PAYLOAD_SIZE,
               "map_begin payload must fill one slot payload");

typedef struct __attribute__((packed)) {
    uint16_t descriptor_generation;
    uint16_t map_generation;
    uint8_t entry_count;
    uint8_t layout_count;
    uint16_t flags;
    uint32_t entries_crc32;
    uint8_t reserved[14];
} inj_map_commit_payload_t;
_Static_assert(sizeof(inj_map_commit_payload_t) == INJ_FRAME_PAYLOAD_SIZE,
               "map_commit payload must fill one slot payload");

typedef struct __attribute__((packed)) {
    uint16_t descriptor_generation;
    uint16_t map_generation;
    uint8_t entry_index;
    uint8_t interface_number;
    uint8_t endpoint_number;
    uint8_t report_id;
    uint16_t usage_page;
    uint16_t usage;
    uint16_t bit_offset;
    uint8_t bit_width;
    uint8_t flags;
    int32_t logical_minimum;
    int32_t logical_maximum;
    uint8_t report_length;
    uint8_t reserved;
} inj_map_entry_payload_t;
_Static_assert(sizeof(inj_map_entry_payload_t) == INJ_FRAME_PAYLOAD_SIZE,
               "map_entry payload must fill one slot payload");

typedef struct __attribute__((packed)) {
    uint16_t descriptor_generation;
    uint16_t map_generation;
    uint16_t active_map_generation;
    uint8_t entry_index;
    uint8_t status;
    uint8_t error;
    uint8_t flags;
    uint32_t entries_crc32;
    uint8_t reserved[12];
} inj_map_status_payload_t;
_Static_assert(sizeof(inj_map_status_payload_t) == INJ_FRAME_PAYLOAD_SIZE,
               "map_status payload must fill one slot payload");

typedef struct __attribute__((packed)) {
    uint16_t lease_generation;
    uint16_t map_generation;
    uint16_t command_sequence;
    uint16_t target_frame;
    uint8_t interface_number;
    uint8_t endpoint_number;
    uint8_t report_id;
    uint8_t flags;
    uint64_t button_mask;
    uint8_t reserved[6];
} inj_physical_mask_payload_t;
_Static_assert(sizeof(inj_physical_mask_payload_t) == INJ_FRAME_PAYLOAD_SIZE,
               "physical_mask payload must fill one slot payload");

typedef struct __attribute__((packed)) {
    uint16_t lease_generation;
    uint16_t map_generation;
    uint16_t command_sequence;
    uint16_t target_frame;
    uint8_t interface_number;
    uint8_t endpoint_number;
    uint8_t report_id;
    uint8_t flags;
    int16_t x;
    int16_t y;
    int16_t wheel;
    int16_t pan;
    uint16_t hold_reports;
    uint32_t reserved;
} inj_relative_payload_t;
_Static_assert(sizeof(inj_relative_payload_t) == INJ_FRAME_PAYLOAD_SIZE,
               "relative payload must fill one slot payload");

typedef struct __attribute__((packed)) {
    uint16_t descriptor_generation;
    uint8_t interface_number;
    uint8_t endpoint_number;
    uint8_t report_id;
    uint16_t offset;
    uint16_t total;
    uint8_t data[17];
} inj_report_fragment_payload_t;
_Static_assert(sizeof(inj_report_fragment_payload_t) == INJ_FRAME_PAYLOAD_SIZE,
               "report_fragment payload must fill one slot payload");

typedef struct __attribute__((packed)) {
    uint16_t lease_generation;
    uint16_t command_sequence;
    uint32_t telemetry_mask;
    uint16_t interval_slots;
    uint16_t report_sample_divisor;
    uint8_t counter_page;
    uint8_t flags;
    uint8_t reserved[12];
} inj_telemetry_config_payload_t;
_Static_assert(sizeof(inj_telemetry_config_payload_t) == INJ_FRAME_PAYLOAD_SIZE,
               "telemetry_config payload must fill one slot payload");

_Static_assert(offsetof(inj_button_state_payload_t, lease_generation) == INJ_BUTTON_STATE_LEASE_GENERATION_OFFSET,
               "inj_button_state_payload_t.lease_generation must sit at its wire offset");
_Static_assert(offsetof(inj_button_state_payload_t, map_generation) == INJ_BUTTON_STATE_MAP_GENERATION_OFFSET,
               "inj_button_state_payload_t.map_generation must sit at its wire offset");
_Static_assert(offsetof(inj_button_state_payload_t, command_sequence) == INJ_BUTTON_STATE_COMMAND_SEQUENCE_OFFSET,
               "inj_button_state_payload_t.command_sequence must sit at its wire offset");
_Static_assert(offsetof(inj_button_state_payload_t, target_frame) == INJ_BUTTON_STATE_TARGET_FRAME_OFFSET,
               "inj_button_state_payload_t.target_frame must sit at its wire offset");
_Static_assert(offsetof(inj_button_state_payload_t, interface_number) == INJ_BUTTON_STATE_INTERFACE_NUMBER_OFFSET,
               "inj_button_state_payload_t.interface_number must sit at its wire offset");
_Static_assert(offsetof(inj_button_state_payload_t, endpoint_number) == INJ_BUTTON_STATE_ENDPOINT_NUMBER_OFFSET,
               "inj_button_state_payload_t.endpoint_number must sit at its wire offset");
_Static_assert(offsetof(inj_button_state_payload_t, report_id) == INJ_BUTTON_STATE_REPORT_ID_OFFSET,
               "inj_button_state_payload_t.report_id must sit at its wire offset");
_Static_assert(offsetof(inj_button_state_payload_t, flags) == INJ_BUTTON_STATE_FLAGS_OFFSET,
               "inj_button_state_payload_t.flags must sit at its wire offset");
_Static_assert(offsetof(inj_button_state_payload_t, buttons) == INJ_BUTTON_STATE_BUTTONS_OFFSET,
               "inj_button_state_payload_t.buttons must sit at its wire offset");
_Static_assert(offsetof(inj_button_state_payload_t, hold_reports) == INJ_BUTTON_STATE_HOLD_REPORTS_OFFSET,
               "inj_button_state_payload_t.hold_reports must sit at its wire offset");
_Static_assert(offsetof(inj_clear_payload_t, lease_generation) == INJ_CLEAR_LEASE_GENERATION_OFFSET,
               "inj_clear_payload_t.lease_generation must sit at its wire offset");
_Static_assert(offsetof(inj_clear_payload_t, map_generation) == INJ_CLEAR_MAP_GENERATION_OFFSET,
               "inj_clear_payload_t.map_generation must sit at its wire offset");
_Static_assert(offsetof(inj_clear_payload_t, command_sequence) == INJ_CLEAR_COMMAND_SEQUENCE_OFFSET,
               "inj_clear_payload_t.command_sequence must sit at its wire offset");
_Static_assert(offsetof(inj_clear_payload_t, target_frame) == INJ_CLEAR_TARGET_FRAME_OFFSET,
               "inj_clear_payload_t.target_frame must sit at its wire offset");
_Static_assert(offsetof(inj_clear_payload_t, clear_flags) == INJ_CLEAR_CLEAR_FLAGS_OFFSET,
               "inj_clear_payload_t.clear_flags must sit at its wire offset");
_Static_assert(offsetof(inj_clear_payload_t, reason) == INJ_CLEAR_REASON_OFFSET,
               "inj_clear_payload_t.reason must sit at its wire offset");
_Static_assert(offsetof(inj_command_ack_payload_t, lease_generation) == INJ_COMMAND_ACK_LEASE_GENERATION_OFFSET,
               "inj_command_ack_payload_t.lease_generation must sit at its wire offset");
_Static_assert(offsetof(inj_command_ack_payload_t, map_generation) == INJ_COMMAND_ACK_MAP_GENERATION_OFFSET,
               "inj_command_ack_payload_t.map_generation must sit at its wire offset");
_Static_assert(offsetof(inj_command_ack_payload_t, command_sequence) == INJ_COMMAND_ACK_COMMAND_SEQUENCE_OFFSET,
               "inj_command_ack_payload_t.command_sequence must sit at its wire offset");
_Static_assert(offsetof(inj_command_ack_payload_t, target_frame) == INJ_COMMAND_ACK_TARGET_FRAME_OFFSET,
               "inj_command_ack_payload_t.target_frame must sit at its wire offset");
_Static_assert(offsetof(inj_command_ack_payload_t, accepted_frame) == INJ_COMMAND_ACK_ACCEPTED_FRAME_OFFSET,
               "inj_command_ack_payload_t.accepted_frame must sit at its wire offset");
_Static_assert(offsetof(inj_command_ack_payload_t, completed_frame) == INJ_COMMAND_ACK_COMPLETED_FRAME_OFFSET,
               "inj_command_ack_payload_t.completed_frame must sit at its wire offset");
_Static_assert(offsetof(inj_command_ack_payload_t, ack_stage) == INJ_COMMAND_ACK_ACK_STAGE_OFFSET,
               "inj_command_ack_payload_t.ack_stage must sit at its wire offset");
_Static_assert(offsetof(inj_command_ack_payload_t, result) == INJ_COMMAND_ACK_RESULT_OFFSET,
               "inj_command_ack_payload_t.result must sit at its wire offset");
_Static_assert(offsetof(inj_command_ack_payload_t, command_type) == INJ_COMMAND_ACK_COMMAND_TYPE_OFFSET,
               "inj_command_ack_payload_t.command_type must sit at its wire offset");
_Static_assert(offsetof(inj_command_ack_payload_t, flags) == INJ_COMMAND_ACK_FLAGS_OFFSET,
               "inj_command_ack_payload_t.flags must sit at its wire offset");
_Static_assert(offsetof(inj_command_ack_payload_t, interface_number) == INJ_COMMAND_ACK_INTERFACE_NUMBER_OFFSET,
               "inj_command_ack_payload_t.interface_number must sit at its wire offset");
_Static_assert(offsetof(inj_command_ack_payload_t, endpoint_number) == INJ_COMMAND_ACK_ENDPOINT_NUMBER_OFFSET,
               "inj_command_ack_payload_t.endpoint_number must sit at its wire offset");
_Static_assert(offsetof(inj_command_ack_payload_t, report_id) == INJ_COMMAND_ACK_REPORT_ID_OFFSET,
               "inj_command_ack_payload_t.report_id must sit at its wire offset");
_Static_assert(offsetof(inj_counters_payload_t, page) == INJ_COUNTERS_PAGE_OFFSET,
               "inj_counters_payload_t.page must sit at its wire offset");
_Static_assert(offsetof(inj_counters_payload_t, flags) == INJ_COUNTERS_FLAGS_OFFSET,
               "inj_counters_payload_t.flags must sit at its wire offset");
_Static_assert(offsetof(inj_counters_payload_t, counter0) == INJ_COUNTERS_COUNTER0_OFFSET,
               "inj_counters_payload_t.counter0 must sit at its wire offset");
_Static_assert(offsetof(inj_counters_payload_t, counter1) == INJ_COUNTERS_COUNTER1_OFFSET,
               "inj_counters_payload_t.counter1 must sit at its wire offset");
_Static_assert(offsetof(inj_counters_payload_t, counter2) == INJ_COUNTERS_COUNTER2_OFFSET,
               "inj_counters_payload_t.counter2 must sit at its wire offset");
_Static_assert(offsetof(inj_counters_payload_t, counter3) == INJ_COUNTERS_COUNTER3_OFFSET,
               "inj_counters_payload_t.counter3 must sit at its wire offset");
_Static_assert(offsetof(inj_counters_payload_t, counter4) == INJ_COUNTERS_COUNTER4_OFFSET,
               "inj_counters_payload_t.counter4 must sit at its wire offset");
_Static_assert(offsetof(inj_counters_payload_t, counter5) == INJ_COUNTERS_COUNTER5_OFFSET,
               "inj_counters_payload_t.counter5 must sit at its wire offset");
_Static_assert(offsetof(inj_descriptor_fragment_payload_t, descriptor_generation) == INJ_DESCRIPTOR_FRAGMENT_DESCRIPTOR_GENERATION_OFFSET,
               "inj_descriptor_fragment_payload_t.descriptor_generation must sit at its wire offset");
_Static_assert(offsetof(inj_descriptor_fragment_payload_t, interface_number) == INJ_DESCRIPTOR_FRAGMENT_INTERFACE_NUMBER_OFFSET,
               "inj_descriptor_fragment_payload_t.interface_number must sit at its wire offset");
_Static_assert(offsetof(inj_descriptor_fragment_payload_t, offset) == INJ_DESCRIPTOR_FRAGMENT_OFFSET_OFFSET,
               "inj_descriptor_fragment_payload_t.offset must sit at its wire offset");
_Static_assert(offsetof(inj_descriptor_fragment_payload_t, total) == INJ_DESCRIPTOR_FRAGMENT_TOTAL_OFFSET,
               "inj_descriptor_fragment_payload_t.total must sit at its wire offset");
_Static_assert(offsetof(inj_descriptor_fragment_payload_t, data) == INJ_DESCRIPTOR_FRAGMENT_DATA_OFFSET,
               "inj_descriptor_fragment_payload_t.data must sit at its wire offset");
_Static_assert(offsetof(inj_link_status_payload_t, usb_frame) == INJ_LINK_STATUS_USB_FRAME_OFFSET,
               "inj_link_status_payload_t.usb_frame must sit at its wire offset");
_Static_assert(offsetof(inj_link_status_payload_t, usb_subframe) == INJ_LINK_STATUS_USB_SUBFRAME_OFFSET,
               "inj_link_status_payload_t.usb_subframe must sit at its wire offset");
_Static_assert(offsetof(inj_link_status_payload_t, link_flags) == INJ_LINK_STATUS_LINK_FLAGS_OFFSET,
               "inj_link_status_payload_t.link_flags must sit at its wire offset");
_Static_assert(offsetof(inj_link_status_payload_t, descriptor_generation) == INJ_LINK_STATUS_DESCRIPTOR_GENERATION_OFFSET,
               "inj_link_status_payload_t.descriptor_generation must sit at its wire offset");
_Static_assert(offsetof(inj_link_status_payload_t, active_map_generation) == INJ_LINK_STATUS_ACTIVE_MAP_GENERATION_OFFSET,
               "inj_link_status_payload_t.active_map_generation must sit at its wire offset");
_Static_assert(offsetof(inj_link_status_payload_t, slot_counter) == INJ_LINK_STATUS_SLOT_COUNTER_OFFSET,
               "inj_link_status_payload_t.slot_counter must sit at its wire offset");
_Static_assert(offsetof(inj_link_status_payload_t, native_report_count) == INJ_LINK_STATUS_NATIVE_REPORT_COUNT_OFFSET,
               "inj_link_status_payload_t.native_report_count must sit at its wire offset");
_Static_assert(offsetof(inj_link_status_payload_t, fault_flags) == INJ_LINK_STATUS_FAULT_FLAGS_OFFSET,
               "inj_link_status_payload_t.fault_flags must sit at its wire offset");
_Static_assert(offsetof(inj_link_status_payload_t, last_rx_sequence) == INJ_LINK_STATUS_LAST_RX_SEQUENCE_OFFSET,
               "inj_link_status_payload_t.last_rx_sequence must sit at its wire offset");
_Static_assert(offsetof(inj_map_begin_payload_t, descriptor_generation) == INJ_MAP_BEGIN_DESCRIPTOR_GENERATION_OFFSET,
               "inj_map_begin_payload_t.descriptor_generation must sit at its wire offset");
_Static_assert(offsetof(inj_map_begin_payload_t, map_generation) == INJ_MAP_BEGIN_MAP_GENERATION_OFFSET,
               "inj_map_begin_payload_t.map_generation must sit at its wire offset");
_Static_assert(offsetof(inj_map_begin_payload_t, entry_count) == INJ_MAP_BEGIN_ENTRY_COUNT_OFFSET,
               "inj_map_begin_payload_t.entry_count must sit at its wire offset");
_Static_assert(offsetof(inj_map_begin_payload_t, layout_count) == INJ_MAP_BEGIN_LAYOUT_COUNT_OFFSET,
               "inj_map_begin_payload_t.layout_count must sit at its wire offset");
_Static_assert(offsetof(inj_map_begin_payload_t, flags) == INJ_MAP_BEGIN_FLAGS_OFFSET,
               "inj_map_begin_payload_t.flags must sit at its wire offset");
_Static_assert(offsetof(inj_map_begin_payload_t, entries_crc32) == INJ_MAP_BEGIN_ENTRIES_CRC32_OFFSET,
               "inj_map_begin_payload_t.entries_crc32 must sit at its wire offset");
_Static_assert(offsetof(inj_map_commit_payload_t, descriptor_generation) == INJ_MAP_COMMIT_DESCRIPTOR_GENERATION_OFFSET,
               "inj_map_commit_payload_t.descriptor_generation must sit at its wire offset");
_Static_assert(offsetof(inj_map_commit_payload_t, map_generation) == INJ_MAP_COMMIT_MAP_GENERATION_OFFSET,
               "inj_map_commit_payload_t.map_generation must sit at its wire offset");
_Static_assert(offsetof(inj_map_commit_payload_t, entry_count) == INJ_MAP_COMMIT_ENTRY_COUNT_OFFSET,
               "inj_map_commit_payload_t.entry_count must sit at its wire offset");
_Static_assert(offsetof(inj_map_commit_payload_t, layout_count) == INJ_MAP_COMMIT_LAYOUT_COUNT_OFFSET,
               "inj_map_commit_payload_t.layout_count must sit at its wire offset");
_Static_assert(offsetof(inj_map_commit_payload_t, flags) == INJ_MAP_COMMIT_FLAGS_OFFSET,
               "inj_map_commit_payload_t.flags must sit at its wire offset");
_Static_assert(offsetof(inj_map_commit_payload_t, entries_crc32) == INJ_MAP_COMMIT_ENTRIES_CRC32_OFFSET,
               "inj_map_commit_payload_t.entries_crc32 must sit at its wire offset");
_Static_assert(offsetof(inj_map_entry_payload_t, descriptor_generation) == INJ_MAP_ENTRY_DESCRIPTOR_GENERATION_OFFSET,
               "inj_map_entry_payload_t.descriptor_generation must sit at its wire offset");
_Static_assert(offsetof(inj_map_entry_payload_t, map_generation) == INJ_MAP_ENTRY_MAP_GENERATION_OFFSET,
               "inj_map_entry_payload_t.map_generation must sit at its wire offset");
_Static_assert(offsetof(inj_map_entry_payload_t, entry_index) == INJ_MAP_ENTRY_ENTRY_INDEX_OFFSET,
               "inj_map_entry_payload_t.entry_index must sit at its wire offset");
_Static_assert(offsetof(inj_map_entry_payload_t, interface_number) == INJ_MAP_ENTRY_INTERFACE_NUMBER_OFFSET,
               "inj_map_entry_payload_t.interface_number must sit at its wire offset");
_Static_assert(offsetof(inj_map_entry_payload_t, endpoint_number) == INJ_MAP_ENTRY_ENDPOINT_NUMBER_OFFSET,
               "inj_map_entry_payload_t.endpoint_number must sit at its wire offset");
_Static_assert(offsetof(inj_map_entry_payload_t, report_id) == INJ_MAP_ENTRY_REPORT_ID_OFFSET,
               "inj_map_entry_payload_t.report_id must sit at its wire offset");
_Static_assert(offsetof(inj_map_entry_payload_t, usage_page) == INJ_MAP_ENTRY_USAGE_PAGE_OFFSET,
               "inj_map_entry_payload_t.usage_page must sit at its wire offset");
_Static_assert(offsetof(inj_map_entry_payload_t, usage) == INJ_MAP_ENTRY_USAGE_OFFSET,
               "inj_map_entry_payload_t.usage must sit at its wire offset");
_Static_assert(offsetof(inj_map_entry_payload_t, bit_offset) == INJ_MAP_ENTRY_BIT_OFFSET_OFFSET,
               "inj_map_entry_payload_t.bit_offset must sit at its wire offset");
_Static_assert(offsetof(inj_map_entry_payload_t, bit_width) == INJ_MAP_ENTRY_BIT_WIDTH_OFFSET,
               "inj_map_entry_payload_t.bit_width must sit at its wire offset");
_Static_assert(offsetof(inj_map_entry_payload_t, flags) == INJ_MAP_ENTRY_FLAGS_OFFSET,
               "inj_map_entry_payload_t.flags must sit at its wire offset");
_Static_assert(offsetof(inj_map_entry_payload_t, logical_minimum) == INJ_MAP_ENTRY_LOGICAL_MINIMUM_OFFSET,
               "inj_map_entry_payload_t.logical_minimum must sit at its wire offset");
_Static_assert(offsetof(inj_map_entry_payload_t, logical_maximum) == INJ_MAP_ENTRY_LOGICAL_MAXIMUM_OFFSET,
               "inj_map_entry_payload_t.logical_maximum must sit at its wire offset");
_Static_assert(offsetof(inj_map_entry_payload_t, report_length) == INJ_MAP_ENTRY_REPORT_LENGTH_OFFSET,
               "inj_map_entry_payload_t.report_length must sit at its wire offset");
_Static_assert(offsetof(inj_map_status_payload_t, descriptor_generation) == INJ_MAP_STATUS_DESCRIPTOR_GENERATION_OFFSET,
               "inj_map_status_payload_t.descriptor_generation must sit at its wire offset");
_Static_assert(offsetof(inj_map_status_payload_t, map_generation) == INJ_MAP_STATUS_MAP_GENERATION_OFFSET,
               "inj_map_status_payload_t.map_generation must sit at its wire offset");
_Static_assert(offsetof(inj_map_status_payload_t, active_map_generation) == INJ_MAP_STATUS_ACTIVE_MAP_GENERATION_OFFSET,
               "inj_map_status_payload_t.active_map_generation must sit at its wire offset");
_Static_assert(offsetof(inj_map_status_payload_t, entry_index) == INJ_MAP_STATUS_ENTRY_INDEX_OFFSET,
               "inj_map_status_payload_t.entry_index must sit at its wire offset");
_Static_assert(offsetof(inj_map_status_payload_t, status) == INJ_MAP_STATUS_STATUS_OFFSET,
               "inj_map_status_payload_t.status must sit at its wire offset");
_Static_assert(offsetof(inj_map_status_payload_t, error) == INJ_MAP_STATUS_ERROR_OFFSET,
               "inj_map_status_payload_t.error must sit at its wire offset");
_Static_assert(offsetof(inj_map_status_payload_t, flags) == INJ_MAP_STATUS_FLAGS_OFFSET,
               "inj_map_status_payload_t.flags must sit at its wire offset");
_Static_assert(offsetof(inj_map_status_payload_t, entries_crc32) == INJ_MAP_STATUS_ENTRIES_CRC32_OFFSET,
               "inj_map_status_payload_t.entries_crc32 must sit at its wire offset");
_Static_assert(offsetof(inj_physical_mask_payload_t, lease_generation) == INJ_PHYSICAL_MASK_LEASE_GENERATION_OFFSET,
               "inj_physical_mask_payload_t.lease_generation must sit at its wire offset");
_Static_assert(offsetof(inj_physical_mask_payload_t, map_generation) == INJ_PHYSICAL_MASK_MAP_GENERATION_OFFSET,
               "inj_physical_mask_payload_t.map_generation must sit at its wire offset");
_Static_assert(offsetof(inj_physical_mask_payload_t, command_sequence) == INJ_PHYSICAL_MASK_COMMAND_SEQUENCE_OFFSET,
               "inj_physical_mask_payload_t.command_sequence must sit at its wire offset");
_Static_assert(offsetof(inj_physical_mask_payload_t, target_frame) == INJ_PHYSICAL_MASK_TARGET_FRAME_OFFSET,
               "inj_physical_mask_payload_t.target_frame must sit at its wire offset");
_Static_assert(offsetof(inj_physical_mask_payload_t, interface_number) == INJ_PHYSICAL_MASK_INTERFACE_NUMBER_OFFSET,
               "inj_physical_mask_payload_t.interface_number must sit at its wire offset");
_Static_assert(offsetof(inj_physical_mask_payload_t, endpoint_number) == INJ_PHYSICAL_MASK_ENDPOINT_NUMBER_OFFSET,
               "inj_physical_mask_payload_t.endpoint_number must sit at its wire offset");
_Static_assert(offsetof(inj_physical_mask_payload_t, report_id) == INJ_PHYSICAL_MASK_REPORT_ID_OFFSET,
               "inj_physical_mask_payload_t.report_id must sit at its wire offset");
_Static_assert(offsetof(inj_physical_mask_payload_t, flags) == INJ_PHYSICAL_MASK_FLAGS_OFFSET,
               "inj_physical_mask_payload_t.flags must sit at its wire offset");
_Static_assert(offsetof(inj_physical_mask_payload_t, button_mask) == INJ_PHYSICAL_MASK_BUTTON_MASK_OFFSET,
               "inj_physical_mask_payload_t.button_mask must sit at its wire offset");
_Static_assert(offsetof(inj_relative_payload_t, lease_generation) == INJ_RELATIVE_LEASE_GENERATION_OFFSET,
               "inj_relative_payload_t.lease_generation must sit at its wire offset");
_Static_assert(offsetof(inj_relative_payload_t, map_generation) == INJ_RELATIVE_MAP_GENERATION_OFFSET,
               "inj_relative_payload_t.map_generation must sit at its wire offset");
_Static_assert(offsetof(inj_relative_payload_t, command_sequence) == INJ_RELATIVE_COMMAND_SEQUENCE_OFFSET,
               "inj_relative_payload_t.command_sequence must sit at its wire offset");
_Static_assert(offsetof(inj_relative_payload_t, target_frame) == INJ_RELATIVE_TARGET_FRAME_OFFSET,
               "inj_relative_payload_t.target_frame must sit at its wire offset");
_Static_assert(offsetof(inj_relative_payload_t, interface_number) == INJ_RELATIVE_INTERFACE_NUMBER_OFFSET,
               "inj_relative_payload_t.interface_number must sit at its wire offset");
_Static_assert(offsetof(inj_relative_payload_t, endpoint_number) == INJ_RELATIVE_ENDPOINT_NUMBER_OFFSET,
               "inj_relative_payload_t.endpoint_number must sit at its wire offset");
_Static_assert(offsetof(inj_relative_payload_t, report_id) == INJ_RELATIVE_REPORT_ID_OFFSET,
               "inj_relative_payload_t.report_id must sit at its wire offset");
_Static_assert(offsetof(inj_relative_payload_t, flags) == INJ_RELATIVE_FLAGS_OFFSET,
               "inj_relative_payload_t.flags must sit at its wire offset");
_Static_assert(offsetof(inj_relative_payload_t, x) == INJ_RELATIVE_X_OFFSET,
               "inj_relative_payload_t.x must sit at its wire offset");
_Static_assert(offsetof(inj_relative_payload_t, y) == INJ_RELATIVE_Y_OFFSET,
               "inj_relative_payload_t.y must sit at its wire offset");
_Static_assert(offsetof(inj_relative_payload_t, wheel) == INJ_RELATIVE_WHEEL_OFFSET,
               "inj_relative_payload_t.wheel must sit at its wire offset");
_Static_assert(offsetof(inj_relative_payload_t, pan) == INJ_RELATIVE_PAN_OFFSET,
               "inj_relative_payload_t.pan must sit at its wire offset");
_Static_assert(offsetof(inj_relative_payload_t, hold_reports) == INJ_RELATIVE_HOLD_REPORTS_OFFSET,
               "inj_relative_payload_t.hold_reports must sit at its wire offset");
_Static_assert(offsetof(inj_report_fragment_payload_t, descriptor_generation) == INJ_REPORT_FRAGMENT_DESCRIPTOR_GENERATION_OFFSET,
               "inj_report_fragment_payload_t.descriptor_generation must sit at its wire offset");
_Static_assert(offsetof(inj_report_fragment_payload_t, interface_number) == INJ_REPORT_FRAGMENT_INTERFACE_NUMBER_OFFSET,
               "inj_report_fragment_payload_t.interface_number must sit at its wire offset");
_Static_assert(offsetof(inj_report_fragment_payload_t, endpoint_number) == INJ_REPORT_FRAGMENT_ENDPOINT_NUMBER_OFFSET,
               "inj_report_fragment_payload_t.endpoint_number must sit at its wire offset");
_Static_assert(offsetof(inj_report_fragment_payload_t, report_id) == INJ_REPORT_FRAGMENT_REPORT_ID_OFFSET,
               "inj_report_fragment_payload_t.report_id must sit at its wire offset");
_Static_assert(offsetof(inj_report_fragment_payload_t, offset) == INJ_REPORT_FRAGMENT_OFFSET_OFFSET,
               "inj_report_fragment_payload_t.offset must sit at its wire offset");
_Static_assert(offsetof(inj_report_fragment_payload_t, total) == INJ_REPORT_FRAGMENT_TOTAL_OFFSET,
               "inj_report_fragment_payload_t.total must sit at its wire offset");
_Static_assert(offsetof(inj_report_fragment_payload_t, data) == INJ_REPORT_FRAGMENT_DATA_OFFSET,
               "inj_report_fragment_payload_t.data must sit at its wire offset");
_Static_assert(offsetof(inj_telemetry_config_payload_t, lease_generation) == INJ_TELEMETRY_CONFIG_LEASE_GENERATION_OFFSET,
               "inj_telemetry_config_payload_t.lease_generation must sit at its wire offset");
_Static_assert(offsetof(inj_telemetry_config_payload_t, command_sequence) == INJ_TELEMETRY_CONFIG_COMMAND_SEQUENCE_OFFSET,
               "inj_telemetry_config_payload_t.command_sequence must sit at its wire offset");
_Static_assert(offsetof(inj_telemetry_config_payload_t, telemetry_mask) == INJ_TELEMETRY_CONFIG_TELEMETRY_MASK_OFFSET,
               "inj_telemetry_config_payload_t.telemetry_mask must sit at its wire offset");
_Static_assert(offsetof(inj_telemetry_config_payload_t, interval_slots) == INJ_TELEMETRY_CONFIG_INTERVAL_SLOTS_OFFSET,
               "inj_telemetry_config_payload_t.interval_slots must sit at its wire offset");
_Static_assert(offsetof(inj_telemetry_config_payload_t, report_sample_divisor) == INJ_TELEMETRY_CONFIG_REPORT_SAMPLE_DIVISOR_OFFSET,
               "inj_telemetry_config_payload_t.report_sample_divisor must sit at its wire offset");
_Static_assert(offsetof(inj_telemetry_config_payload_t, counter_page) == INJ_TELEMETRY_CONFIG_COUNTER_PAGE_OFFSET,
               "inj_telemetry_config_payload_t.counter_page must sit at its wire offset");
_Static_assert(offsetof(inj_telemetry_config_payload_t, flags) == INJ_TELEMETRY_CONFIG_FLAGS_OFFSET,
               "inj_telemetry_config_payload_t.flags must sit at its wire offset");

/* Every payload struct is packed and aliases wire bytes directly. */
#if defined(__BYTE_ORDER__) && defined(__ORDER_LITTLE_ENDIAN__)
_Static_assert(__BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__,
               "packed payload structs assume a little-endian host");
#endif

/* Membership in the contract's message-type set. Generated because that
 * set is literal JSON data; the per-type payload *length* is not, and is
 * hand-written in spi_frame.c. */
static inline int inj_type_is_known(uint8_t type) {
    switch (type) {
    case INJ_TYPE_IDLE:
    case INJ_TYPE_LINK_STATUS:
    case INJ_TYPE_DESCRIPTOR_FRAGMENT:
    case INJ_TYPE_REPORT_FRAGMENT:
    case INJ_TYPE_MAP_STATUS:
    case INJ_TYPE_COMMAND_ACK:
    case INJ_TYPE_COUNTERS:
    case INJ_TYPE_MAP_BEGIN:
    case INJ_TYPE_MAP_ENTRY:
    case INJ_TYPE_MAP_COMMIT:
    case INJ_TYPE_RELATIVE:
    case INJ_TYPE_BUTTON_STATE:
    case INJ_TYPE_PHYSICAL_MASK:
    case INJ_TYPE_CLEAR:
    case INJ_TYPE_TELEMETRY_CONFIG:
        return 1;
    default:
        return 0;
    }
}

static const uint8_t inj_golden_relative_payload[INJ_FRAME_PAYLOAD_SIZE] = {
    0x22u, 0x11u, 0x44u, 0x33u, 0x66u, 0x55u, 0x88u, 0x77u, 0x09u, 0x0Au, 0x0Bu, 0x0Fu, 0xFEu, 0xFFu, 0x34u, 0x12u, 0xCCu, 0xEDu, 0xFFu, 0x7Fu, 0xBCu, 0x9Au, 0x00u, 0x00u, 0x00u, 0x00u,
};

static const uint8_t inj_golden_map_entry_payload[INJ_FRAME_PAYLOAD_SIZE] = {
    0x34u, 0x12u, 0x78u, 0x56u, 0x9Au, 0x02u, 0x03u, 0x01u, 0x01u, 0x00u, 0x30u, 0x00u, 0x08u, 0x00u, 0x0Cu, 0x1Bu, 0x00u, 0xF8u, 0xFFu, 0xFFu, 0xFFu, 0x07u, 0x00u, 0x00u, 0x04u, 0x00u,
};

#endif /* INJECTION_WIRE_H */
