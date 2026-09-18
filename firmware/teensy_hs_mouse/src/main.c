// Synthetic High Speed HID mouse -- a test instrument for the hurra-cynthion
// relay, running bare metal on a SparkFun MicroMod Teensy (iMXRT1062).
//
// WHY THIS EXISTS
//
// Validating High Speed needs a device that (a) actually trains at 480 Mb/s and
// (b) emits reports at a rate Full Speed cannot reach. A real mouse fails both
// halves of the test: most are Full Speed, and a real one reports only when a
// hand moves it, so a gap in the relay's counters is indistinguishable from a
// hand holding still.
//
// The first attempt used Teensyduino's USB_HID type. It cannot work here, and
// the reason is worth recording: that type declares 5 interfaces on endpoints
// 2..6, and hurra-cynthion's enumerator is deliberately bounded at 4 interfaces
// and endpoint numbers <= MAX_RELAY_ENDPOINT_NUMBER (4). Teensyduino also
// silently adds a Seremu debug interface and a media-keys interface. No stock
// Teensyduino USB type has a mouse *and* fits, so the descriptors are built
// here by hand instead.
//
// WHAT IT PRESENTS
//
// One interface, one endpoint, nothing else:
//
//   bInterfaceClass    3   HID
//   bInterfaceSubClass 1   boot           -- required: the enumerator only
//   bInterfaceProtocol 2   mouse             commits a capture if it sees this
//   bEndpointAddress   0x81 (EP1 IN)      -- must be <= 4
//   wMaxPacketSize     8                  -- must be <= 64
//   bInterval          1
//
// bInterval is the whole point. At High Speed it is an exponent: the period is
// 2^(bInterval-1) microframes, so 1 means one 125 us microframe -- 8000 Hz. At
// Full Speed the same field is a direct count of 1 ms frames, so the ceiling
// there is 1000 Hz. If the relay's native_reports counter advances at ~8000/sec
// the TARGET link is running High Speed; that is a measurement, not an
// inference from a status bit.
//
// WHAT IT SENDS
//
// This build traces a slow circle for a legible injection demo: X and Y carry
// small per-report deltas and buttons/wheel stay zero. An earlier build put a
// free-running sequence counter in Y for drop/reorder detection, but using it
// as a raw delta moved the cursor at the microframe rate (~500k counts/s), so
// it was removed. Report-rate counting downstream still measures the negotiated
// link speed regardless of payload.

#include <stdint.h>
#include <stdbool.h>
#include <math.h>
#include <string.h>
#include "imxrt.h"
#include "desc_capture.h"
#include "usb_device.h"

extern uint32_t millis(void);
extern uint32_t micros(void);
extern void usb_host_shim_set_last_report(const uint8_t *data, uint16_t len);

// Report period in microseconds. 125 = 8000 Hz, reachable only at High Speed.
#ifndef REPORT_PERIOD_US
#define REPORT_PERIOD_US 125
#endif

// Transmit only every Nth generated report. 1 is normal operation; larger
// values are a diagnostic, and exist because the generation rate is otherwise
// invisible from the host side. The relay counts reports it *receives*, so a
// device generating 8000/sec and delivering 2000 looks exactly like one
// generating 2000 and delivering all of them -- and sweeping REPORT_PERIOD_US
// does not separate them, since both are min(generation, delivery) over the
// same pair of numbers. Transmitting one generation in N makes the observed
// rate generation/N, which does.
#ifndef DIAG_SEND_EVERY
#define DIAG_SEND_EVERY 1
#endif

#define MOUSE_EP        1
#define MOUSE_MAXPKT    8
#define REPORT_LEN      4

// --- Slow-circle motion (this build) ---------------------------------------
// The cursor traces a circle of CIRCLE_RADIUS counts, one revolution every
// CIRCLE_PERIOD_S seconds, so an injected drift is plainly visible fighting it.
// Deltas are computed from a float position and accumulated (see the loop), so
// at this radius/period they are 0 or +/-1 -- smooth and gentle, not the
// microframe-rate blur the earlier sequence-in-Y build produced.
#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif
#ifndef CIRCLE_RADIUS
#define CIRCLE_RADIUS 100
#endif
#ifndef CIRCLE_PERIOD_S
#define CIRCLE_PERIOD_S 6.0f
#endif
// Angle advanced per generated report: a full turn spread over
// (reports-per-second * period) reports, where reports-per-second is
// 1e6 / REPORT_PERIOD_US.
#define CIRCLE_DTHETA \
	(2.0f * (float)M_PI / (CIRCLE_PERIOD_S * (1000000.0f / (float)REPORT_PERIOD_US)))

// ---------------------------------------------------------------------------
// Static descriptors
// ---------------------------------------------------------------------------

// Boot-protocol mouse report descriptor: 3 buttons, X, Y, wheel. Byte layout
// is [buttons, dx, dy, wheel]; dx/dy/wheel are signed.
static const uint8_t hid_report_desc[] = {
	0x05, 0x01,        // Usage Page (Generic Desktop)
	0x09, 0x02,        // Usage (Mouse)
	0xA1, 0x01,        // Collection (Application)
	0x09, 0x01,        //   Usage (Pointer)
	0xA1, 0x00,        //   Collection (Physical)
	0x05, 0x09,        //     Usage Page (Button)
	0x19, 0x01,        //     Usage Minimum (1)
	0x29, 0x03,        //     Usage Maximum (3)
	0x15, 0x00,        //     Logical Minimum (0)
	0x25, 0x01,        //     Logical Maximum (1)
	0x95, 0x03,        //     Report Count (3)
	0x75, 0x01,        //     Report Size (1)
	0x81, 0x02,        //     Input (Data,Var,Abs)
	0x95, 0x01,        //     Report Count (1)
	0x75, 0x05,        //     Report Size (5)
	0x81, 0x03,        //     Input (Cnst,Var,Abs) -- padding
	0x05, 0x01,        //     Usage Page (Generic Desktop)
	0x09, 0x30,        //     Usage (X)
	0x09, 0x31,        //     Usage (Y)
	0x09, 0x38,        //     Usage (Wheel)
	0x15, 0x81,        //     Logical Minimum (-127)
	0x25, 0x7F,        //     Logical Maximum (127)
	0x75, 0x08,        //     Report Size (8)
	0x95, 0x03,        //     Report Count (3)
	0x81, 0x06,        //     Input (Data,Var,Rel)
	0xC0,              //   End Collection
	0xC0               // End Collection
};

static const uint8_t device_desc[18] = {
	18, 0x01,              // bLength, DEVICE
	0x00, 0x02,            // bcdUSB 2.00
	0x00, 0x00, 0x00,      // class/subclass/protocol: per-interface
	64,                    // bMaxPacketSize0 -- must be 64 at High Speed
	0xC0, 0x16,            // idVendor  0x16C0 (PJRC/OpenMoko shared space)
	0x8C, 0x04,            // idProduct 0x048C
	0x01, 0x00,            // bcdDevice 0.01
	1, 2, 0,               // iManufacturer, iProduct, iSerialNumber
	1                      // bNumConfigurations
};

#define CONFIG_DESC_LEN 34
static const uint8_t config_desc[CONFIG_DESC_LEN] = {
	// Configuration
	9, 0x02,
	CONFIG_DESC_LEN, 0x00, // wTotalLength
	1,                     // bNumInterfaces
	1,                     // bConfigurationValue
	0,                     // iConfiguration
	0xA0,                  // bmAttributes: bus powered, remote wakeup
	50,                    // bMaxPower: 100 mA
	// Interface 0: boot mouse
	9, 0x04,
	0,                     // bInterfaceNumber
	0,                     // bAlternateSetting
	1,                     // bNumEndpoints
	0x03,                  // bInterfaceClass: HID
	0x01,                  // bInterfaceSubClass: boot
	0x02,                  // bInterfaceProtocol: mouse
	0,                     // iInterface
	// HID descriptor
	9, 0x21,
	0x11, 0x01,            // bcdHID 1.11
	0x00,                  // bCountryCode
	1,                     // bNumDescriptors
	0x22,                  // bDescriptorType: Report
	sizeof(hid_report_desc), 0x00,
	// Endpoint 1 IN, interrupt
	7, 0x05,
	0x80 | MOUSE_EP,       // bEndpointAddress
	0x03,                  // bmAttributes: interrupt
	MOUSE_MAXPKT, 0x00,    // wMaxPacketSize
	1                      // bInterval -- 2^0 = 1 microframe = 125 us at HS
};

static captured_descriptors_t desc;

static void build_descriptors(void)
{
	memset(&desc, 0, sizeof(desc));

	memcpy(desc.device_desc, device_desc, sizeof(device_desc));
	desc.device_desc_len = sizeof(device_desc);
	memcpy(desc.config_desc, config_desc, sizeof(config_desc));
	desc.config_desc_len = sizeof(config_desc);

	desc.num_ifaces = 1;
	desc.ifaces[0].iface_num = 0;
	desc.ifaces[0].iface_class = 0x03;
	desc.ifaces[0].iface_subclass = 0x01;
	desc.ifaces[0].iface_protocol = 0x02;
	desc.ifaces[0].interrupt_in_ep = 0x80 | MOUSE_EP;
	desc.ifaces[0].interrupt_in_maxpkt = MOUSE_MAXPKT;
	desc.ifaces[0].interrupt_in_interval = 1;
	desc.ifaces[0].has_hid_desc = true;
	memcpy(desc.ifaces[0].hid_report_desc, hid_report_desc, sizeof(hid_report_desc));
	desc.ifaces[0].hid_report_desc_len = sizeof(hid_report_desc);

	// String 0 is the LANGID table; 1 and 2 are manufacturer and product.
	desc.langid_desc[0] = 4;
	desc.langid_desc[1] = 0x03;
	desc.langid_desc[2] = 0x09;
	desc.langid_desc[3] = 0x04;
	desc.langid_desc_len = 4;
	desc.langid = 0x0409;

	static const char *const strings[] = { "hurra", "Synthetic HS Mouse" };
	desc.num_strings = 2;
	for (int s = 0; s < 2; s++) {
		const char *str = strings[s];
		uint8_t n = (uint8_t)strlen(str);
		desc.string_index[s] = (uint8_t)(s + 1);
		desc.string_desc[s][0] = (uint8_t)(2 + n * 2);
		desc.string_desc[s][1] = 0x03;
		for (uint8_t i = 0; i < n; i++) {
			desc.string_desc[s][2 + i * 2] = (uint8_t)str[i];
			desc.string_desc[s][3 + i * 2] = 0;
		}
		desc.string_desc_len[s] = (uint8_t)(2 + n * 2);
	}

	desc.ep0_maxpkt = 64;
	desc.dev_addr = 0;
	desc.valid = true;
}

// ---------------------------------------------------------------------------
// Report generation
// ---------------------------------------------------------------------------

int main(void)
{
	build_descriptors();

	if (!usb_device_init(&desc)) {
		while (1) { }
	}

	uint32_t next_report = micros();
	uint8_t send_phase = 0;

	// Slow-circle state. theta steps a fixed amount per generated report;
	// emitted_x/y hold the integer position already sent, so each report emits
	// only the rounded delta and sub-count motion accumulates instead of being
	// truncated to zero (which at this radius it otherwise would be).
	float theta = 0.0f;
	int32_t emitted_x = (int32_t)CIRCLE_RADIUS;  // theta 0 -> (R cos0, R sin0) = (R, 0)
	int32_t emitted_y = 0;

	while (1) {
		usb_device_poll();

		uint32_t now = micros();
		// Signed comparison so the 32-bit micros() wrap (every ~71 minutes)
		// does not stall reporting for the rest of the wrap period.
		if ((int32_t)(now - next_report) < 0) continue;

		// Advance by exactly one period rather than resetting to `now`:
		// resetting discards the overshoot and makes the emitted rate drift
		// slower than requested, which downstream would look like relay drops
		// rather than a firmware artefact.
		next_report += REPORT_PERIOD_US;

		if (!usb_device_is_configured()) {
			next_report = micros() + REPORT_PERIOD_US;
			continue;
		}

		// Trace the circle: target = (R cos theta, R sin theta). Emit the
		// integer delta from the position already sent and clamp it to the
		// signed byte the boot report carries. emitted_x/y is the accumulator,
		// so fractional per-report motion is never lost.
		theta += CIRCLE_DTHETA;
		if (theta >= 2.0f * (float)M_PI) {
			theta -= 2.0f * (float)M_PI;
		}
		int32_t target_x = (int32_t)lroundf((float)CIRCLE_RADIUS * cosf(theta));
		int32_t target_y = (int32_t)lroundf((float)CIRCLE_RADIUS * sinf(theta));
		int32_t dx = target_x - emitted_x;
		int32_t dy = target_y - emitted_y;
		emitted_x = target_x;
		emitted_y = target_y;
		if (dx > 127) { dx = 127; } else if (dx < -127) { dx = -127; }
		if (dy > 127) { dy = 127; } else if (dy < -127) { dy = -127; }

		uint8_t report[REPORT_LEN];
		report[0] = 0;                     // buttons: none
		report[1] = (uint8_t)(int8_t)dx;   // X delta
		report[2] = (uint8_t)(int8_t)dy;   // Y delta
		report[3] = 0;                     // wheel

		if (++send_phase >= DIAG_SEND_EVERY) {
			send_phase = 0;
			if (usb_device_send_report(MOUSE_EP, report, REPORT_LEN)) {
				usb_host_shim_set_last_report(report, REPORT_LEN);
			}
		}
	}
}
