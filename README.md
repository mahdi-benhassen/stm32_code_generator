# STM32 project generator — no STM32CubeMX

Pick an MCU and peripherals from a GitHub Actions form and get back a
buildable STM32 HAL project. No STM32CubeMX involved anywhere: the
CMSIS/HAL source is vendored straight from STMicroelectronics' own GitHub
repos, and a Python script writes the application glue code itself
(`main.c`, MSP callbacks, HAL config, linker script, `Makefile`) following
the same structure and naming conventions STM32CubeMX generates.

Because nothing here needs the CubeMX installer (which is gated behind a
myST login) or a GUI, **this runs on a normal GitHub-hosted `ubuntu-latest`
runner** — no self-hosted runner required.

Every combination below was actually compiled with `arm-none-eabi-gcc`
while building this, not just written and assumed to work:
- STM32F401RETx with USART + SPI + I2C + ADC + PWM all enabled together
- STM32F103C8Tx ("Blue Pill") with USART + SPI + I2C + CAN
- STM32F411CEUx with no peripherals (minimal/blink-only project)

## Where the code actually comes from

| Source | Repo | What's vendored |
|---|---|---|
| CMSIS-Core | `github.com/STMicroelectronics/cmsis-core` | Cortex-M core headers (device-independent) |
| CMSIS-Device | `github.com/STMicroelectronics/cmsis_device_{family}` | Register definitions, startup `.s`, `system_stm32{family}xx.c` |
| HAL driver | `github.com/STMicroelectronics/stm32{family}xx_hal_driver` | `HAL_*_Init()` implementations |

These are the same standalone repos STM32CubeMX/CubeIDE themselves pull as
git submodules — verified by inspecting `STM32CubeF4`'s `.gitmodules`.
`scripts/generate_project.py` shallow+sparse-clones just the needed paths
from each, so a run only transfers a few MB, not full monorepos.

**What this script writes itself** (not vendored): `main.c`/`main.h`,
`stm32{family}xx_it.c/h` (IRQ handler stubs), `stm32{family}xx_hal_msp.c`
(the `HAL_<PERIPH>_MspInit()` clock/GPIO/pin-mux callbacks — this is the
part that's genuinely equivalent to what CubeMX's code generator does),
the linker script, and the `Makefile`.

## MCU catalog — intentionally small, and why

`mcu_catalog.json` ships 3 MCUs, not a huge list. Each one requires
getting several real hardware facts right — flash/RAM size, CPU/FPU
variant, and (biggest source of subtle bugs) which **GPIO alternate-
function model** the family uses:
- **F4/L4/G4-style**: `GPIO_InitTypeDef` has an `Alternate` field, you pick
  an AF number (0–15) that routes a pin to a peripheral.
- **F1-style**: no `Alternate` field at all — F1 uses the older AFIO
  remap-register model instead. Code that's correct for F4 will not even
  compile on F1 (confirmed while building this: F1's `GPIO_InitTypeDef`
  genuinely has no `Alternate` member).

Both models are implemented and tested (`gpio_model: "af_number"` vs.
`"afio_remap"` in the catalog). Adding another F4/L4/G4-family part is
low-risk (same model, mostly a memory-size lookup); adding a different
*family line* (H7's multi-region RAM, L4/G4's split SRAM, U5, etc.) means
verifying that family's specifics first — see "Extending" below.

## Default pins / alternate-function numbers

`AF_TABLE` and `DEFAULT_PINS` in `generate_project.py` give each peripheral
a common default (e.g. USART2 → PA2/PA3, AF7 — the standard Nucleo-64
virtual-COM-port pins). These are verified against the real vendored
`stm32f4xx_hal_gpio_ex.h` (grepped the actual header for exact macro names
like `GPIO_AF7_USART2` while building this), but they're still just
*defaults* for the most common instance. Override them per interface via
`"pins": {"tx": "PA9", "rx": "PA10"}` in that interface's config JSON, and
verify against your exact package's alternate-function table for anything
beyond the defaults.

## Clock configuration

`SystemClock_Config()` is generated to run off the internal **HSI** with no
PLL — a deliberately conservative default that works on any board with no
assumptions about an external crystal being fitted or its frequency. It
runs correctly but not at the part's max speed. Once you've confirmed your
board's actual HSE frequency, switch it to HSE+PLL (this is the one piece
that's genuinely board-specific, not just part-specific, so it's out of
scope for auto-detection here).

## Usage

Locally:
```
python3 scripts/generate_project.py \
    --mcu STM32F401RETx \
    --catalog mcu_catalog.json \
    --config my_config.json \
    --output out/MyProject
cd out/MyProject && make
```
where `my_config.json` looks like:
```json
{
  "project_name": "MyProject",
  "editor": "vscode",
  "interfaces": {
    "usart": {"instance": "USART2", "baudrate": 115200},
    "spi": null,
    "i2c": {"instance": "I2C1"}
  }
}
```

Via GitHub Actions: Actions tab → "Generate STM32 Project" → Run workflow
→ pick MCU + peripherals → download the artifact from the completed run.
`build_firmware: true` (default) also compiles it in CI, so a broken
combination fails the workflow run instead of silently shipping.

## Editor / IDE options
- **makefile** (default): just the `Makefile` — works from any editor/CLI.
- **vscode**: adds `.vscode/{tasks,launch,c_cpp_properties}.json` (build
  via `make`, debug via Cortex-Debug + OpenOCD — install that extension
  and an OpenOCD build separately).
- **cubeide**: adds a short note on importing as a Makefile project.
  (Deliberately not hand-generating STM32CubeIDE's `.project`/`.cproject`
  Eclipse XML — that format is intricate and easy to get subtly wrong;
  importing the Makefile is the robust path.)

## Extending
- **New MCU, same family** (e.g. another F4 part): add an entry to
  `mcu_catalog.json` with correct `part_suffix` (matches the header name in
  `cmsis_device_{family}`'s `Include/`), flash/RAM size, and device define.
- **New family** (L4, G4, H7, ...): first confirm that family's repo names
  follow the same pattern (`stm32{fam}xx_hal_driver`, `cmsis_device_{fam}`
  — true for every family checked while building this: f1, f4, g4, l4, h7,
  u5, c0), then add an `AF_TABLE`/`DEFAULT_PINS` entry for that family and
  work out its RAM layout (single region vs. split SRAM/CCM/DTCM).
- **New interface type**: add a `build_<x>()` function following the
  existing ones' shape (returns a `Peripheral`), register it in `BUILDERS`
  and `MSP_FUNC`, add `enable_<x>`/`<x>_config` workflow inputs.
- **USB**: not included — it needs the separate
  `stm32_mw_usb_device` middleware repo on top of HAL, which is a bigger
  addition than the other peripherals here.
