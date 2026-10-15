# PICSimLab QEMU source

This runtime is built from the public PICSimLab ESP32 QEMU fork:

- Repository: <https://github.com/lcgamboa/qemu>
- Branch: `picsimlab-esp32`
- Revision: `e39508f79cb0681bbe15cfcebd40a3f45cdc3c48`
- QEMU version reported by the source: 9.2.2
- License: GNU GPL version 2; see `COPYING`

The corresponding local changes are recorded in:

- `patches/qemu-picsimlab/0001-classic-esp32-safe-host-io.patch`
- `patches/qemu-picsimlab/0002-windows-xtensa-dll.patch`

Run `scripts/build-qemu-picsimlab.ps1` from the repository root to recreate the source checkout, apply the patches, build `bin/libqemu-xtensa.dll`, strip debug symbols, and copy the required classic ESP32 ROM files.

The packaged library is isolated in one worker process per active simulated board because this QEMU machine uses process-global state. The application currently exposes firmware-backed UART0 and digital GPIO only. It does not claim connected ADC, I2C, SPI, RMT, LEDC, or networking support.
