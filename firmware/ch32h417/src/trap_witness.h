#pragma once

/* Clear random/stale shared-SRAM diagnostics before either core does work. */
void trap_witness_clear(void);
