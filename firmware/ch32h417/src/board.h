#ifndef BOARD_H
#define BOARD_H

#include "ch32h417_port.h" /* device + StdPeriph (RCC/GPIO/DMA/SPI) + core_riscv */

#include "spi_frame.h" /* INJ_FRAME_SIZE */

/* Board-to-board SPI link pinout (roadmap Global Constraints).
 *
 * The FPGA (PMOD A) is the SPI master; this MCU is the mode-0 slave.
 *   PA4  NSS   (hardware chip-select input)   PMOD IO3 CS
 *   PA5  SCK   (clock input)                  PMOD IO0 SCK
 *   PA6  MISO  (data output to the FPGA)      PMOD IO2 MISO
 *   PA7  MOSI  (data input from the FPGA)     PMOD IO1 MOSI
 * All four are SPI1 alternate function 5. PA3 is MCU_READY, a plain GPIO output
 * held low until both DMA directions are armed (PMOD IO4). */
#define SPI_LINK_SPI              SPI1
#define SPI_LINK_GPIO             GPIOA
#define SPI_LINK_PIN_NSS          GPIO_Pin_4
#define SPI_LINK_PIN_SCK          GPIO_Pin_5
#define SPI_LINK_PIN_MISO         GPIO_Pin_6
#define SPI_LINK_PIN_MOSI         GPIO_Pin_7
#define SPI_LINK_PINSOURCE_NSS    GPIO_PinSource4
#define SPI_LINK_PINSOURCE_SCK    GPIO_PinSource5
#define SPI_LINK_PINSOURCE_MISO   GPIO_PinSource6
#define SPI_LINK_PINSOURCE_MOSI   GPIO_PinSource7
#define SPI_LINK_AF               GPIO_AF5
#define SPI_LINK_PIN_MCU_READY    GPIO_Pin_3

/* DMAMUX wiring. The CH32H417 routes DMA through a DMAMUX: a DMA1 channel
 * carries no peripheral traffic until DMA_MuxChannelConfig() binds its request
 * line. Request IDs are from CH32H417 Reference Manual V1.7 p.153 table 10-2
 * ("DMA multiplexer input-to-resource assignment"): SPI1_TX=63, SPI1_RX=64,
 * cross-checked against WCH EVT examples (SPI2=65/66, USART2=87/88). DMAMUX
 * channel N drives DMA1 channel N, so mux-channel 2/3 -> DMA1 channel 2/3.
 * SPI1 is on the HB2 bus (RCC_HB2Periph_SPI1); the DMA clock is
 * RCC_HBPeriph_DMA1. The SPI runs 8-bit data, so the DMA moves one byte per
 * unit, INJ_FRAME_SIZE units per slot. */
#define SPI_LINK_RX_DMA_CHANNEL      DMA1_Channel2
#define SPI_LINK_TX_DMA_CHANNEL      DMA1_Channel3
#define SPI_LINK_RX_DMA_MUX_CHANNEL  DMA_MuxChannel2
#define SPI_LINK_TX_DMA_MUX_CHANNEL  DMA_MuxChannel3
#define SPI_LINK_RX_DMA_REQUEST      64u /* SPI1_RX */
#define SPI_LINK_TX_DMA_REQUEST      63u /* SPI1_TX */
#define SPI_LINK_RX_DMA_IRQ          DMA1_Channel2_IRQn
#define SPI_LINK_RX_DMA_IT_TC        DMA1_IT_TC2

/* The DMA banks are exactly one wire slot each; the retirement path and the CRC
 * geometry both assume 32. */
_Static_assert(INJ_FRAME_SIZE == 32u, "SPI link DMA banks assume a 32-byte slot");

/* USB_SYNC: the FPGA drives one edge per USB frame onto PC6/TIM8_CH1 (AF3), the
 * MCU's window into the host's frame clock. PA2/TIM5 is not broken out on the
 * CH32H417QEU6 board; PC6 is the exposed TIM8 channel on header J5. TIM8 is an
 * advanced timer with split capture/compare and update vectors. */
#define USB_SYNC_GPIO             GPIOC
#define USB_SYNC_PIN              GPIO_Pin_6
#define USB_SYNC_PINSOURCE        GPIO_PinSource6
#define USB_SYNC_AF               GPIO_AF3
#define USB_SYNC_TIM              TIM8
#define USB_SYNC_TIM_CC_IRQ       TIM8_CC_IRQn
#define USB_SYNC_TIM_UP_IRQ       TIM8_UP_IRQn

#endif
