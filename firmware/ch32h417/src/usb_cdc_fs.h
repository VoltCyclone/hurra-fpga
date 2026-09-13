#ifndef USB_CDC_FS_H
#define USB_CDC_FS_H
// usb_cdc_fs.h -- CDC-ACM virtual COM port on the CH32H417 USBFS controller.
//
// In hurra-cynthion the Cynthion FPGA owns the USB device clone and report
// injection; the CH32H417 is the platform-link MCU, exchanging the frozen wire
// format with the FPGA over SPI1 (ch32_link.c). Its USBFS controller (PA11/PA12,
// USB-C port) is otherwise unused, so this driver brings it up as a USB CDC-ACM
// device -- a virtual serial port the control machine opens with no driver
// install -- to carry the in-band command/response channel (kmbox-style
// injection text).
//
// This is the ONLY USBFS_IRQHandler in the V5F image: hurra-cynthion has no
// on-MCU device-clone driver (that role moved to the FPGA), so there is no
// competing USBFS ISR to reconcile. The handler is force-kept via a
// `--undefined=USBFS_IRQHandler` root in the Makefile because the vector table
// references it weakly.
//
// Ported from Hurra-v3 src/usb_cdc_fs.c. Conventions preserved verbatim: the
// UEPn_DMA registers hold the raw buffer address while CPU access uses the same
// address through a +0x20000000 alias; DMA buffers live in the .usbdma section;
// the shared USBHS 480 MHz PLL is brought up idempotently and never torn down
// (the USBFS 48 MHz clock is /10 of it); the V5F IRQ is routed with
// NVIC_SetAllocateIRQ(USBFS_IRQn, Core_ID_V5F) before enabling.
//
// EP layout (CDC-ACM, fixed -- no cloned device):
//   EP0      control, MPS 64 (enumeration + CDC class requests)
//   EP1 IN   interrupt notification (required by CDC-ACM; unused, never armed)
//   EP2 OUT  bulk, host->device command bytes -> rx ring
//   EP3 IN   bulk, device->host response bytes <- tx ring

#include <stdint.h>
#include <stdbool.h>

// Bring up RCC (idempotent shared-PLL reuse), the USBFS controller, EP0/EP2/EP3,
// route + enable the V5F IRQ. Safe to call once at startup. The controller drives
// PA11/PA12 directly (no GPIO AF config needed).
void cdc_fs_init(void);

// Foreground EP servicing: re-arm bulk OUT (EP2) after the ring has been drained,
// and flush any queued bulk IN (EP3) when the endpoint is idle. Call from the
// main loop. The ISR does the wire-level work; this just moves the rings.
void cdc_fs_poll(void);

// Pull up to `max` received command bytes out of the rx ring into `buf`.
// Returns the number of bytes copied (0 if none pending).
uint16_t cdc_fs_rx_read(uint8_t *buf, uint16_t max);

// Queue up to `len` response bytes into the tx ring for the host to read on
// EP3 IN. Returns the number of bytes accepted (may be < len if the ring fills).
uint16_t cdc_fs_tx_write(const uint8_t *buf, uint16_t len);

// True once the host has issued SET_CONFIGURATION (the COM port is open-able).
bool cdc_fs_is_configured(void);

// The single V5F-image USBFS interrupt service routine. Must match the vector
// name in core/startup_v5f.S.
void USBFS_IRQHandler(void);

#endif /* USB_CDC_FS_H */
