// Stubs for the three usb_host_* symbols usb_device.c links against.
//
// usb_device.c is a *passthrough* device: when the PC issues a HID class
// request it forwards it upstream to the real mouse and replays the answer.
// This firmware has no upstream -- it IS the mouse -- so those calls are
// answered locally instead.
//
// The split matters, and it falls out of how usb_device.c routes by direction:
//
//   * Class OUT requests (SET_IDLE, SET_PROTOCOL, SET_REPORT) go through the
//     deferred "fire" path. usb_device.c has already ACKed the status stage by
//     the time it fires, so a no-op here is exactly right: the request is
//     accepted and discarded, which is correct for a device with no state to
//     set. Stalling them instead would be wrong -- some hosts abandon a mouse
//     that stalls SET_IDLE.
//
//   * Class IN requests (GET_REPORT, GET_IDLE, GET_PROTOCOL) go through the
//     synchronous path and expect data back. Returning < 0 makes usb_device.c
//     stall, which is a legal answer for a boot mouse and is what hosts get
//     from plenty of real ones. GET_REPORT is answered properly anyway, since
//     it is the one a host may actually rely on.

#include <string.h>
#include <stdbool.h>
#include <stdint.h>
#include "usb_host.h"

#define HID_REQ_GET_REPORT   0x01
#define HID_REQ_GET_IDLE     0x02
#define HID_REQ_GET_PROTOCOL 0x03

// The last report handed to the USB stack, so a GET_REPORT answers with the
// device's actual current state rather than zeros.
static uint8_t last_report[8];
static uint16_t last_report_len;

void usb_host_shim_set_last_report(const uint8_t *data, uint16_t len)
{
	if (len > sizeof(last_report)) len = sizeof(last_report);
	memcpy(last_report, data, len);
	last_report_len = len;
}

// Never busy: there is no upstream transfer to be in flight.
bool usb_host_control_async_busy(void)
{
	return false;
}

// Class OUT requests land here. Accept and discard.
void usb_host_control_transfer_fire(uint8_t addr, uint8_t maxpkt,
	const usb_setup_t *setup, uint8_t *data)
{
	(void)addr; (void)maxpkt; (void)setup; (void)data;
}

// Class IN requests land here.
int usb_host_control_transfer(uint8_t addr, uint8_t maxpkt,
	const usb_setup_t *setup, uint8_t *data, uint32_t timeout_ms)
{
	(void)addr; (void)maxpkt; (void)timeout_ms;

	if ((setup->bmRequestType & 0x60) != 0x20) return -1;  // not class
	if (data == NULL) return -1;

	switch (setup->bRequest) {
	case HID_REQ_GET_REPORT: {
		uint16_t n = last_report_len;
		if (n == 0) return -1;
		if (n > setup->wLength) n = setup->wLength;
		memcpy(data, last_report, n);
		return (int)n;
	}
	case HID_REQ_GET_IDLE:
		if (setup->wLength < 1) return -1;
		data[0] = 0;              // "indefinite" -- report only on change
		return 1;
	case HID_REQ_GET_PROTOCOL:
		if (setup->wLength < 1) return -1;
		data[0] = 1;              // report protocol
		return 1;
	default:
		return -1;
	}
}
