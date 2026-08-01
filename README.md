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

## If you only see the vendored `Drivers/` folder and no `main.c`

This repo (the generator) and a *generated project* (its output) are two
different things, and by default the output only exists as a **downloadable
run artifact**, not as files committed anywhere. If you've only ever run
the workflow and browsed the repo afterwards, you will not see `main.c` —
it was never pushed anywhere; it's sitting in a zip on that run's summary
page. This is very likely what's meant by "the script just copies the
libraries": the vendored `Drivers/`/`Core/Startup` files are the only part
that would ever appear directly if you're looking in the wrong place.

Two ways to actually see the generated `main.c`/HAL config/linker script:
1. **Actions tab → the completed run → Artifacts** at the bottom of the
   summary page → download and unzip.
2. Set `commit_to_repo: true` (now the default) when running the workflow —
   it opens a **pull request** with the whole generated project under
   `generated/<project_name>/`, so it shows up in the repo's file browser
   like any other commit.

Every combination below was actually compiled with `arm-none-eabi-gcc`
while building this, not just written and assumed to work:
- STM32F401RETx with USART + SPI + I2C + ADC + PWM all enabled together
- STM32F103C8Tx ("Blue Pill") with **every** supported interface enabled at
  once — USART + SPI + I2C + CAN + ADC + PWM
- STM32F411CEUx with no peripherals (minimal/blink-only project)
- STM32F401RETx and STM32F103C8Tx with **FreeRTOS** enabled (both CPU port
  variants — see below)

The "every interface at once" combination isn't just for show — it's what
actually caught a real bug: F1's `ADC_InitTypeDef` is an older, simpler
struct than F4's (no `ClockPrescaler`/`Resolution`/`EOCSelection`/
`DMAContinuousRequests` fields at all, and different `SamplingTime` macro
names), so code that built fine on F4 flat-out failed to compile on F1
until `build_adc()` was made aware of both ADC IP versions (`adc_style:
"standard"` vs `"legacy"` in the catalog). That combination is now a
permanent CI matrix entry specifically so this class of bug can't silently
come back.

A `ci-test-generator.yml` workflow now runs all 7 of these combinations
automatically on every push/PR to `scripts/**` or `mcu_catalog.json`, so a
future regression fails CI instead of shipping quietly. The user-facing
generation workflow also **always** compiles the result before it can
reach the artifact or the PR — there's no toggle to skip this — so a
combination that doesn't actually build can't be handed back as if it
were usable.

Two related guarantees enforced by `generate_project.py` itself: any
interface you enable that the target MCU genuinely can't support (e.g.
CAN on an MCU with no CAN peripheral wired up, or FreeRTOS on an MCU
missing a `freertos_port` entry) makes generation **fail outright**,
rather than silently generating a project missing the thing you asked
for.

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
`"afio_remap"` in the catalog). The same split exists for ADC
(`adc_style: "standard"` vs. `"legacy"` — F1's ADC IP is genuinely a
simpler/older struct, see "Where the code actually comes from" above).
Adding another F4/L4/G4-family part is low-risk (same models, mostly a
memory-size lookup); adding a different *family line* (H7's multi-region
RAM, L4/G4's split SRAM, U5, etc.) means verifying that family's
specifics first — see "Extending" below.

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

One deliberate choice: PWM's default instance/channels is TIM3 CH3/CH4
(→ PB0/PB1), not the more "obvious" TIM2 CH1/CH2 (→ PA0/PA1) — because
PA0/PA1 collide with ADC1's own default channels (IN0/IN1), and the two
would silently fight over the same pins if both were enabled with their
defaults. Found by actually building the "every interface enabled at once"
combination, not by inspection.

## Clock configuration

`SystemClock_Config()` is generated to run off the internal **HSI** with no
PLL — a deliberately conservative default that works on any board with no
assumptions about an external crystal being fitted or its frequency. It
runs correctly but not at the part's max speed. Once you've confirmed your
board's actual HSE frequency, switch it to HSE+PLL (this is the one piece
that's genuinely board-specific, not just part-specific, so it's out of
scope for auto-detection here).

## FreeRTOS

Set `"freertos": true` in the config JSON (or the `enable_freertos`
workflow checkbox) to get a FreeRTOS project using the **CMSIS-RTOS2**
wrapper (`osThreadNew`, `osKernelStart`, ...) — the same API CubeMX itself
generates against by default, not raw FreeRTOS calls. It vendors from:
- `github.com/STMicroelectronics/stm32-mw-freertos` — kernel (`tasks.c`,
  `queue.c`, ...), the GCC Cortex-M port (`ARM_CM4F` or `ARM_CM3` depending
  on the MCU), `heap_4.c`, and `cmsis_os2.c` (the CMSIS-RTOS2 wrapper).
- `cmsis_os2.h` (the RTOS2 API header itself) from the `STM32CubeF4`
  monorepo — it's tracked there directly (not a submodule) and is
  family-independent, so any Cube monorepo has the identical file.

A default task (`StartDefaultTask`) is created and the scheduler started
at the end of `main()`, matching CubeMX's own generated structure.

**Three integration details that only surfaced by actually linking this**
(none of them are things you'd get right guessing from the FreeRTOS docs
alone):
1. `configMAX_PRIORITIES` must be **exactly 56** — the CMSIS-RTOS2 wrapper
   maps its `osPriority_t` scale straight onto FreeRTOS priorities and
   `#error`s at compile time otherwise.
2. Several `INCLUDE_*` flags that look optional (`uxTaskGetStackHighWaterMark`,
   `xTimerPendFunctionCall`, `xSemaphoreGetMutexHolder`, ...) are hard
   requirements of the wrapper, not optional — same `#error`-at-compile-time
   discovery.
3. `cmsis_os2.c` ships its **own** `SysTick_Handler` by default, and that
   version never calls `HAL_IncTick()` — it only drives the RTOS tick. Left
   alone, every `HAL_Delay()`/HAL-internal timeout would hang forever, and
   it also collides at link time with this generator's own
   `SysTick_Handler`. Fixed by setting
   `USE_CUSTOM_SYSTICK_HANDLER_IMPLEMENTATION 1`, which makes `cmsis_os2.c`
   step aside; `stm32{family}xx_it.c`'s `SysTick_Handler` then calls both
   `HAL_IncTick()` and (once the scheduler's running) `xPortSysTickHandler()`.
   `SVC_Handler`/`PendSV_Handler` are provided directly by FreeRTOS's
   `port.c` via a `#define` remap in `FreeRTOSConfig.h`, so they're
   deliberately *not* defined a second time in `it.c` when FreeRTOS is on.

`configTOTAL_HEAP_SIZE` defaults to 8 KB, which fits even the smallest
catalog part (STM32F103C8's 20 KB RAM) alongside the rest of its BSS —
confirmed by an actual link, not just arithmetic. Lower it in
`write_freertos_config()` if you add a smaller-RAM part later.

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
  "freertos": false,
  "interfaces": {
    "usart": {"instance": "USART2", "baudrate": 115200},
    "spi": null,
    "i2c": {"instance": "I2C1"}
  }
}
```

Via GitHub Actions: Actions tab → "Generate STM32 Project" → Run workflow
→ pick MCU + peripherals → either download the artifact from the completed
run, or (default) find the opened PR under `generated/<project_name>/`.
The workflow always compiles the result first — there's no toggle to skip
this — so a combination that fails to build never reaches the artifact
or the PR; the run just shows red.

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

## Notes on the PR-based commit-back
Opening the PR uses the third-party `peter-evans/create-pull-request`
action, which needs `contents: write` and `pull-requests: write` on the
workflow's `GITHUB_TOKEN` (already set in `generate-stm32-project.yml`'s
job `permissions:` block). If your organization's default token
permissions are locked down repo-wide, you may need to allow this
explicitly in Settings → Actions → General → Workflow permissions.
