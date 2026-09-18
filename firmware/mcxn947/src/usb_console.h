// The CDC console's hardware half: USB HS clock and PHY bring-up, the TinyUSB
// device stack, and the transport between it and the portable console.
//
// Migration step 4 of docs/MCXN947_CONTROLLER.md section 9. Declarations here
// are portable C; every MMIO access and every TinyUSB call is inside the
// single `#if defined(MCXN947)` block in usb_console.c.
//
// --- Where this sits in the boot ladder ------------------------------------
//
// Section 4(a) is explicit, and the ordering is the safety invariant rather
// than a style:
//
//     4. raise mcu_ready        <-- THE LINK IS LIVE HERE
//     6. USB PHY bring-up + tud_init()
//
// link_init() raises `mcu_ready` as its last act, and main() calls
// usb_console_init() after it. So the link is already running before the USB
// PHY is touched, and there is no execution order in which a USB failure --
// an unplugged J11, a PHY PLL that never locks, a host that never enumerates
// -- can prevent or delay it. Moving this call above link_init() would
// silently invert that, and nothing in the build would object.
//
// The same reasoning forbids putting anything slow here that the link's
// health depends on. Section 10's note about the PUF is the live example: a
// PUF transaction for a real serial number must not go between `mcu_ready`
// and the foreground loop, which is why the serial is a constant in
// usb_descriptors.c.

#ifndef HURRA_MCXN947_USB_CONSOLE_H
#define HURRA_MCXN947_USB_CONSOLE_H

// SPC/LDO, the 24 MHz crystal, the USB HS PHY PLL, the controller clock gates,
// the PHY itself, then the TinyUSB device stack and the portable console.
// Call AFTER link_init(). Never blocks on a host.
void usb_console_init(void);

// Pump TinyUSB, move received bytes into the console, and step the bulk load
// generator. Call from the foreground loop as fast as it turns.
void usb_console_poll(void);

#endif  // HURRA_MCXN947_USB_CONSOLE_H
