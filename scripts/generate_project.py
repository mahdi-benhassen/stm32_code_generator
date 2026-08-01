#!/usr/bin/env python3
"""
generate_project.py

Builds a self-contained, buildable STM32 HAL project WITHOUT STM32CubeMX:
  1. Vendors CMSIS-Core, CMSIS-Device, and HAL driver source straight from
     STMicroelectronics' own GitHub repos (github.com/STMicroelectronics/...).
  2. Writes the application glue code itself (main.c, IRQ handlers, MSP,
     HAL config, linker script, Makefile) following the same structure/
     naming conventions STM32CubeMX generates (MX_<PERIPH>_Init(), USER
     CODE BEGIN/END markers, etc.).

Usage:
    python3 generate_project.py \
        --mcu STM32F401RETx \
        --catalog mcu_catalog.json \
        --config config.json \
        --output out/MyProject
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_BASE = "https://github.com/STMicroelectronics"

# ---------------------------------------------------------------------------
# Alternate-function / default-pin tables.
# Verified against the STM32F4 reference manual's AF mapping table for the
# most common instance of each peripheral. These are DEFAULTS for the most
# common instance/pins — override per-interface via "pins": {...} in the
# config JSON, and always double check against your exact package's
# datasheet alternate-function table before wiring real hardware.
# ---------------------------------------------------------------------------
AF_TABLE = {
    "f4": {"USART1": 7, "USART2": 7, "USART3": 7,
           "SPI1": 5, "SPI2": 5, "SPI3": 6,
           "I2C1": 4, "I2C2": 4, "I2C3": 4,
           "CAN1": 9, "CAN2": 9,
           "TIM2": 1, "TIM3": 2, "TIM4": 2},
}

DEFAULT_PINS = {
    "f4": {
        "usart": {"USART1": {"tx": "PA9", "rx": "PA10"}, "USART2": {"tx": "PA2", "rx": "PA3"}},
        "spi":   {"SPI1": {"sck": "PA5", "miso": "PA6", "mosi": "PA7"}},
        "i2c":   {"I2C1": {"scl": "PB6", "sda": "PB7"}},
        "can":   {"CAN1": {"rx": "PA11", "tx": "PA12"}},
        # TIM2 CH1-4 (PA0-3) is the "natural" PWM default but collides with
        # ADC1's default IN0/IN1 (also PA0/PA1) when both are enabled at
        # once. TIM3 CH3/CH4 -> PB0/PB1 is free against every other default
        # here (verified by actually building all interfaces together —
        # see the "all interfaces at once" CI matrix entry), so it's the
        # default `pwm_config` value in the workflow/README/CI. TIM2 is
        # still usable — just specify pins explicitly if combining it with
        # ADC on its default channels.
        "pwm":   {"TIM2": {"CH1": "PA0", "CH2": "PA1", "CH3": "PA2", "CH4": "PA3"},
                  "TIM3": {"CH3": "PB0", "CH4": "PB1"}},
    },
    "f1": {
        "usart": {"USART1": {"tx": "PA9", "rx": "PA10"}, "USART2": {"tx": "PA2", "rx": "PA3"}},
        "spi":   {"SPI1": {"sck": "PA5", "miso": "PA6", "mosi": "PA7"}},
        "i2c":   {"I2C1": {"scl": "PB6", "sda": "PB7"}},
        "can":   {"CAN1": {"rx": "PA11", "tx": "PA12"}},
        # Same TIM3 CH3/CH4 -> PB0/PB1 default as f4 above; on F1 these are
        # TIM3's fixed (non-remapped) pins, so no AFIO remap call is needed.
        "pwm":   {"TIM2": {"CH1": "PA0", "CH2": "PA1", "CH3": "PA2", "CH4": "PA3"},
                  "TIM3": {"CH3": "PB0", "CH4": "PB1"}},
    },
}

MANDATORY_MODULES = ["hal", "hal_cortex", "hal_rcc", "hal_rcc_ex", "hal_gpio",
                      "hal_dma", "hal_dma_ex", "hal_pwr", "hal_pwr_ex",
                      "hal_flash", "hal_flash_ex", "hal_exti"]

INTERFACE_MODULES = {
    "usart": ["hal_uart"],
    "spi": ["hal_spi"],
    "i2c": ["hal_i2c"],
    "adc": ["hal_adc", "hal_adc_ex"],
    "pwm": ["hal_tim", "hal_tim_ex"],
    # "can" resolved dynamically below based on can_type (bxcan/fdcan)
}


def sh(cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def sparse_clone(repo, dest: Path, paths):
    if dest.exists():
        shutil.rmtree(dest)
    sh(["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse",
        f"{REPO_BASE}/{repo}.git", str(dest)])
    sh(["git", "sparse-checkout", "init", "--cone"], cwd=dest)
    sh(["git", "sparse-checkout", "set", *paths], cwd=dest)


def modules_for(interfaces: dict, can_type: str):
    mods = list(MANDATORY_MODULES)
    for name, cfg in interfaces.items():
        if not cfg:
            continue
        if name == "can":
            if can_type == "bxcan":
                mods += ["hal_can"]
            elif can_type == "fdcan":
                mods += ["hal_fdcan"]
            # can_type == "none": nothing to fetch — main() rejects this
            # combination outright before it'd matter.
        else:
            mods += INTERFACE_MODULES.get(name, [])
    seen = set()
    return [m for m in mods if not (m in seen or seen.add(m))]


# ---------------------------------------------------------------------------
# Fetch step
# ---------------------------------------------------------------------------

def fetch_sources(mcu, entry, interfaces, out: Path, scratch: Path):
    fam = entry["family"]
    part = entry["part_suffix"]

    (out / "Core/Inc").mkdir(parents=True, exist_ok=True)
    (out / "Core/Src").mkdir(parents=True, exist_ok=True)
    (out / "Core/Startup").mkdir(parents=True, exist_ok=True)
    drv_inc = out / f"Drivers/STM32{fam.upper()}xx_HAL_Driver/Inc"
    drv_src = out / f"Drivers/STM32{fam.upper()}xx_HAL_Driver/Src"
    drv_inc.mkdir(parents=True, exist_ok=True)
    drv_src.mkdir(parents=True, exist_ok=True)
    cmsis_dev_inc = out / f"Drivers/CMSIS/Device/ST/STM32{fam.upper()}xx/Include"
    cmsis_dev_inc.mkdir(parents=True, exist_ok=True)
    cmsis_core_inc = out / "Drivers/CMSIS/Include"
    cmsis_core_inc.mkdir(parents=True, exist_ok=True)

    print("[fetch] cmsis-core ...")
    d = scratch / "cmsis-core"
    sparse_clone("cmsis-core", d, ["CMSIS/Core/Include"])
    shutil.copytree(d / "CMSIS/Core/Include", cmsis_core_inc, dirs_exist_ok=True)

    print(f"[fetch] cmsis_device_{fam} ...")
    d = scratch / f"cmsis_device_{fam}"
    sparse_clone(f"cmsis_device_{fam}", d, ["Include", "Source/Templates"])
    shutil.copy(d / f"Include/{part}.h", cmsis_dev_inc / f"{part}.h")
    shutil.copy(d / f"Include/stm32{fam}xx.h", cmsis_dev_inc / f"stm32{fam}xx.h")
    sysh = d / f"Include/system_stm32{fam}xx.h"
    if sysh.exists():
        shutil.copy(sysh, cmsis_dev_inc / sysh.name)
    shutil.copy(d / f"Source/Templates/system_stm32{fam}xx.c", out / "Core/Src" / f"system_stm32{fam}xx.c")
    startup_src = d / f"Source/Templates/gcc/startup_{part}.s"
    shutil.copy(startup_src, out / "Core/Startup" / startup_src.name)

    print(f"[fetch] stm32{fam}xx_hal_driver ...")
    d = scratch / f"stm32{fam}xx_hal_driver"
    sparse_clone(f"stm32{fam}xx_hal_driver", d, ["Inc", "Src"])
    shutil.copytree(d / "Inc", drv_inc, dirs_exist_ok=True)
    mods = modules_for(interfaces, entry.get("can_type", "none"))
    copied = []
    for mod in mods:
        src_c = d / "Src" / f"stm32{fam}xx_{mod}.c"
        if src_c.exists():
            shutil.copy(src_c, drv_src / src_c.name)
            copied.append(src_c.name)
        else:
            print(f"  (skip, not present in this family: stm32{fam}xx_{mod}.c)")

    conf_template = drv_inc / f"stm32{fam}xx_hal_conf_template.h"
    hal_conf = out / "Core/Inc" / f"stm32{fam}xx_hal_conf.h"
    shutil.copy(conf_template, hal_conf)

    return {
        "startup_file": startup_src.name,
        "hal_src_files": copied,
        "system_c": f"system_stm32{fam}xx.c",
    }


def fetch_freertos(entry, out: Path, scratch: Path):
    """Vendors FreeRTOS kernel + the GCC Cortex-M port + heap_4 + the
    CMSIS-RTOS v2 wrapper from STMicroelectronics/stm32-mw-freertos —
    the same repo ST's own STM32Cube packages use as a submodule."""
    port = entry["freertos_port"]
    base = out / "Middlewares/Third_Party/FreeRTOS/Source"
    (base / "include").mkdir(parents=True, exist_ok=True)
    (base / f"portable/GCC/{port}").mkdir(parents=True, exist_ok=True)
    (base / "portable/MemMang").mkdir(parents=True, exist_ok=True)
    (base / "CMSIS_RTOS_V2").mkdir(parents=True, exist_ok=True)

    print("[fetch] stm32-mw-freertos ...")
    d = scratch / "stm32-mw-freertos"
    sparse_clone("stm32-mw-freertos", d, ["Source"])

    for f in (d / "Source").glob("*.c"):
        shutil.copy(f, base / f.name)
    for f in (d / "Source/include").glob("*.h"):
        if f.name == "FreeRTOSConfig_template.h":
            continue
        shutil.copy(f, base / "include" / f.name)
    for name in ("port.c", "portmacro.h"):
        shutil.copy(d / f"Source/portable/GCC/{port}/{name}", base / f"portable/GCC/{port}/{name}")
    shutil.copy(d / "Source/portable/MemMang/heap_4.c", base / "portable/MemMang/heap_4.c")
    for name in ("cmsis_os2.c", "freertos_os2.h", "freertos_mpool.h"):
        shutil.copy(d / f"Source/CMSIS_RTOS_V2/{name}", base / "CMSIS_RTOS_V2" / name)

    # The CMSIS-RTOS2 API header (cmsis_os2.h) itself is not part of
    # stm32-mw-freertos — it's a generic, family-independent header ST
    # tracks directly (not as a submodule) inside each STM32CubeXX monorepo
    # at Drivers/CMSIS/RTOS2/Include/. Any family's monorepo has the same
    # file; STM32CubeF4 is used here purely as a stable, always-available
    # source for it.
    print("[fetch] CMSIS-RTOS2 API header (from STM32CubeF4) ...")
    d2 = scratch / "STM32CubeF4-rtos2"
    sparse_clone("STM32CubeF4", d2, ["Drivers/CMSIS/RTOS2/Include"])
    shutil.copy(d2 / "Drivers/CMSIS/RTOS2/Include/cmsis_os2.h", base / "CMSIS_RTOS_V2/cmsis_os2.h")

    c_files = [str(f.relative_to(out)) for f in base.glob("*.c")]
    c_files.append(str((base / f"portable/GCC/{port}/port.c").relative_to(out)))
    c_files.append(str((base / "portable/MemMang/heap_4.c").relative_to(out)))
    c_files.append(str((base / "CMSIS_RTOS_V2/cmsis_os2.c").relative_to(out)))
    include_dirs = [
        str((base / "include").relative_to(out)),
        str((base / f"portable/GCC/{port}").relative_to(out)),
        str((base / "CMSIS_RTOS_V2").relative_to(out)),
    ]
    return {"c_files": c_files, "include_dirs": include_dirs}


def write_freertos_config(entry, out: Path):
    """Compact, hand-verified FreeRTOSConfig.h. configMAX_PRIORITIES=56 and
    the specific INCLUDE_*/config* values below aren't arbitrary -- they are
    hard requirements enforced by #error checks inside the CMSIS-RTOS2
    wrapper (freertos_os2.h) that only surface at compile time, confirmed
    by actually compiling this against it."""
    hz = entry["hsi_hz"]
    fam = entry["family"]
    text = f'''#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

/* Required by the CMSIS-RTOS2 wrapper (freertos_os2.h) to pick its device
 * header -- must match the device header this MCU family uses. */
#define CMSIS_device_header "stm32{fam}xx.h"

/* configCPU_CLOCK_HZ matches SystemClock_Config()'s default: HSI, no PLL.
 * If you switch to HSE+PLL for a higher SYSCLK, update this to match, or
 * the RTOS tick period (and therefore every osDelay()/vTaskDelay()) will
 * be wrong. */
#define configCPU_CLOCK_HZ                      ( ( unsigned long ) {hz} )
#define configTICK_RATE_HZ                      ( ( TickType_t ) 1000 )
#define configUSE_PREEMPTION                    1
#define configUSE_PORT_OPTIMISED_TASK_SELECTION 0
#define configUSE_TICKLESS_IDLE                 0

/* configMAX_PRIORITIES must be exactly 56 -- the CMSIS-RTOS2 wrapper maps
 * its osPriority_t scale (1..56) directly onto FreeRTOS priorities and
 * #errors at compile time if this isn't 56. */
#define configMAX_PRIORITIES                    ( 56 )
#define configMINIMAL_STACK_SIZE                ( ( unsigned short ) 128 )
#define configTOTAL_HEAP_SIZE                   ( ( size_t ) ( 8 * 1024 ) )
#define configMAX_TASK_NAME_LEN                 ( 16 )
#define configUSE_16_BIT_TICKS                  0
#define configIDLE_SHOULD_YIELD                 1
#define configUSE_MUTEXES                       1
#define configUSE_RECURSIVE_MUTEXES             1
#define configUSE_COUNTING_SEMAPHORES           1
#define configQUEUE_REGISTRY_SIZE               8
#define configUSE_QUEUE_SETS                    0
#define configUSE_TIME_SLICING                  1
#define configSUPPORT_STATIC_ALLOCATION         0
#define configSUPPORT_DYNAMIC_ALLOCATION        1

/* Hooks */
#define configUSE_IDLE_HOOK                     0
#define configUSE_TICK_HOOK                     0
#define configUSE_MALLOC_FAILED_HOOK            1
#define configCHECK_FOR_STACK_OVERFLOW          2
#define configUSE_APPLICATION_TASK_TAG          0

/* configUSE_TRACE_FACILITY must be 1 -- required by the wrapper's
 * osThreadEnumerate() implementation. */
#define configGENERATE_RUN_TIME_STATS           0
#define configUSE_TRACE_FACILITY                1
#define configUSE_STATS_FORMATTING_FUNCTIONS    0

/* Software timers */
#define configUSE_TIMERS                        1
#define configTIMER_TASK_PRIORITY               ( configMAX_PRIORITIES - 1 )
#define configTIMER_QUEUE_LENGTH                10
#define configTIMER_TASK_STACK_DEPTH            configMINIMAL_STACK_SIZE

/* Co-routines -- unused */
#define configUSE_CO_ROUTINES                   0
#define configMAX_CO_ROUTINE_PRIORITIES         1

/* Optional API inclusions. uxTaskGetStackHighWaterMark, xTimerPendFunctionCall,
 * xTaskAbortDelay, xTaskGetHandle and xSemaphoreGetMutexHolder are required
 * (=1) by the CMSIS-RTOS2 wrapper, not optional here despite the name. */
#define INCLUDE_vTaskPrioritySet                  1
#define INCLUDE_uxTaskPriorityGet                 1
#define INCLUDE_vTaskDelete                       1
#define INCLUDE_vTaskSuspend                      1
#define INCLUDE_vTaskDelayUntil                   1
#define INCLUDE_vTaskDelay                        1
#define INCLUDE_xTaskGetSchedulerState             1
#define INCLUDE_xTaskGetCurrentTaskHandle          1
#define INCLUDE_uxTaskGetStackHighWaterMark        1
#define INCLUDE_xTaskGetIdleTaskHandle             0
#define INCLUDE_eTaskGetState                      1
#define INCLUDE_xTimerPendFunctionCall             1
#define INCLUDE_xTaskAbortDelay                    1
#define INCLUDE_xTaskGetHandle                     1
#define INCLUDE_xTaskResumeFromISR                 1
#define INCLUDE_xSemaphoreGetMutexHolder           1

/* Cortex-M interrupt priority configuration. All the STM32 parts in this
 * catalog implement 4 NVIC priority bits (16 levels) -- verify against your
 * exact part's reference manual if you add one that differs. */
#define configPRIO_BITS                          4
#define configLIBRARY_LOWEST_INTERRUPT_PRIORITY  15
#define configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY 5
#define configKERNEL_INTERRUPT_PRIORITY \\
    ( configLIBRARY_LOWEST_INTERRUPT_PRIORITY << (8 - configPRIO_BITS) )
#define configMAX_SYSCALL_INTERRUPT_PRIORITY \\
    ( configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY << (8 - configPRIO_BITS) )

#define configASSERT( x ) if ((x) == 0) {{ taskDISABLE_INTERRUPTS(); for(;;); }}

/* Let FreeRTOS's own port.c provide SVC_Handler/PendSV_Handler directly --
 * see stm32{fam}xx_it.c for how SysTick_Handler is shared between
 * HAL_IncTick() and the RTOS tick instead of being remapped here. */
#define vPortSVCHandler    SVC_Handler
#define xPortPendSVHandler PendSV_Handler

/* The CMSIS-RTOS2 wrapper (cmsis_os2.c) ships its own SysTick_Handler by
 * default (guarded by `#if USE_CUSTOM_SYSTICK_HANDLER_IMPLEMENTATION == 0`,
 * which is the case whenever this macro is left undefined, since an
 * undefined identifier in a preprocessor #if evaluates to 0). Its version
 * only calls xPortSysTickHandler() and never HAL_IncTick(), which would
 * silently hang every HAL_Delay()/HAL_GetTick()-based timeout forever.
 * Setting this to 1 makes cmsis_os2.c step aside so stm32{fam}xx_it.c's
 * SysTick_Handler (which calls both) is the only one that gets linked. */
#define USE_CUSTOM_SYSTICK_HANDLER_IMPLEMENTATION 1

#endif /* FREERTOS_CONFIG_H */
'''
    (out / "Core/Inc/FreeRTOSConfig.h").write_text(text)


# ---------------------------------------------------------------------------
# Application code generation
# ---------------------------------------------------------------------------

def pin_port_num(pin: str):
    """'PA9' -> ('A', 9)"""
    return pin[1], int(pin[2:])


class Peripheral:
    def __init__(self, instance, var, handle_type, init_func, init_body,
                 gpio_pins, clk_macro, irq_name=None):
        self.instance = instance
        self.var = var
        self.handle_type = handle_type
        self.init_func = init_func
        self.init_body = init_body
        self.gpio_pins = gpio_pins  # list of dicts: port,num,mode,af,pull,speed
        self.clk_macro = clk_macro
        self.irq_name = irq_name


def build_usart(cfg, fam, af_table):
    inst = cfg["instance"]
    pins = cfg.get("pins") or DEFAULT_PINS[fam]["usart"].get(inst, {"tx": "PA2", "rx": "PA3"})
    af = af_table.get(inst, 7)
    var = f"h{inst.lower()}"
    baud = cfg.get("baudrate", 115200)
    body = f"""  {var}.Instance = {inst};
  {var}.Init.BaudRate = {baud};
  {var}.Init.WordLength = UART_WORDLENGTH_{cfg.get('wordlength', 8)}B;
  {var}.Init.StopBits = UART_STOPBITS_{cfg.get('stopbits', 1)};
  {var}.Init.Parity = UART_PARITY_{cfg.get('parity', 'NONE')};
  {var}.Init.Mode = UART_MODE_{cfg.get('mode', 'TX_RX')};
  {var}.Init.HwFlowCtl = UART_HWCONTROL_NONE;
  {var}.Init.OverSampling = UART_OVERSAMPLING_16;
  if (HAL_UART_Init(&{var}) != HAL_OK)
  {{
    Error_Handler();
  }}"""
    gpio = [
        {"port": pins["tx"][1], "num": int(pins["tx"][2:]), "mode": "AF_PP", "af": af, "pull": "NOPULL", "speed": "HIGH"},
        {"port": pins["rx"][1], "num": int(pins["rx"][2:]), "mode": "AF_PP", "af": af, "pull": "NOPULL", "speed": "HIGH"},
    ]
    return Peripheral(inst, var, "UART_HandleTypeDef", f"MX_{inst}_UART_Init", body,
                       gpio, f"__HAL_RCC_{inst}_CLK_ENABLE", f"{inst}_IRQn")


def build_spi(cfg, fam, af_table):
    inst = cfg["instance"]
    pins = cfg.get("pins") or DEFAULT_PINS[fam]["spi"].get(inst, {"sck": "PA5", "miso": "PA6", "mosi": "PA7"})
    af = af_table.get(inst, 5)
    var = f"h{inst.lower()}"
    body = f"""  {var}.Instance = {inst};
  {var}.Init.Mode = SPI_MODE_{cfg.get('mode', 'MASTER')};
  {var}.Init.Direction = SPI_DIRECTION_{cfg.get('direction', '2LINES')};
  {var}.Init.DataSize = SPI_DATASIZE_{cfg.get('data_size', 8)}BIT;
  {var}.Init.CLKPolarity = SPI_POLARITY_{cfg.get('clk_polarity', 'LOW')};
  {var}.Init.CLKPhase = SPI_PHASE_{cfg.get('clk_phase', '1EDGE')};
  {var}.Init.NSS = SPI_NSS_{cfg.get('nss', 'SOFT')};
  {var}.Init.BaudRatePrescaler = SPI_BAUDRATEPRESCALER_{cfg.get('baudrate_prescaler', 16)};
  {var}.Init.FirstBit = SPI_FIRSTBIT_MSB;
  {var}.Init.TIMode = SPI_TIMODE_DISABLE;
  {var}.Init.CRCCalculation = SPI_CRCCALCULATION_DISABLE;
  {var}.Init.CRCPolynomial = 10;
  if (HAL_SPI_Init(&{var}) != HAL_OK)
  {{
    Error_Handler();
  }}"""
    gpio = [
        {"port": pins["sck"][1], "num": int(pins["sck"][2:]), "mode": "AF_PP", "af": af, "pull": "NOPULL", "speed": "HIGH"},
        {"port": pins["miso"][1], "num": int(pins["miso"][2:]), "mode": "AF_PP", "af": af, "pull": "NOPULL", "speed": "HIGH"},
        {"port": pins["mosi"][1], "num": int(pins["mosi"][2:]), "mode": "AF_PP", "af": af, "pull": "NOPULL", "speed": "HIGH"},
    ]
    return Peripheral(inst, var, "SPI_HandleTypeDef", f"MX_{inst}_Init", body,
                       gpio, f"__HAL_RCC_{inst}_CLK_ENABLE", f"{inst}_IRQn")


def build_i2c(cfg, fam, af_table):
    inst = cfg["instance"]
    pins = cfg.get("pins") or DEFAULT_PINS[fam]["i2c"].get(inst, {"scl": "PB6", "sda": "PB7"})
    af = af_table.get(inst, 4)
    var = f"h{inst.lower()}"
    body = f"""  {var}.Instance = {inst};
  {var}.Init.ClockSpeed = {cfg.get('clock_speed_hz', 100000)};
  {var}.Init.DutyCycle = I2C_DUTYCYCLE_2;
  {var}.Init.OwnAddress1 = {cfg.get('own_address1', 0)};
  {var}.Init.AddressingMode = I2C_ADDRESSINGMODE_7BIT;
  {var}.Init.DualAddressMode = I2C_DUALADDRESS_DISABLE;
  {var}.Init.OwnAddress2 = 0;
  {var}.Init.GeneralCallMode = I2C_GENERALCALL_DISABLE;
  {var}.Init.NoStretchMode = I2C_NOSTRETCH_DISABLE;
  if (HAL_I2C_Init(&{var}) != HAL_OK)
  {{
    Error_Handler();
  }}"""
    gpio = [
        {"port": pins["scl"][1], "num": int(pins["scl"][2:]), "mode": "AF_OD", "af": af, "pull": "PULLUP", "speed": "HIGH"},
        {"port": pins["sda"][1], "num": int(pins["sda"][2:]), "mode": "AF_OD", "af": af, "pull": "PULLUP", "speed": "HIGH"},
    ]
    return Peripheral(inst, var, "I2C_HandleTypeDef", f"MX_{inst}_Init", body,
                       gpio, f"__HAL_RCC_{inst}_CLK_ENABLE", f"{inst}_EV_IRQn")


def build_can_bxcan(cfg, fam, af_table):
    inst = cfg["instance"]
    pins = cfg.get("pins") or DEFAULT_PINS[fam]["can"].get(inst, {"rx": "PA11", "tx": "PA12"})
    af = af_table.get(inst, 9)
    var = f"h{inst.lower()}"
    body = f"""  {var}.Instance = {inst};
  {var}.Init.Prescaler = {cfg.get('prescaler', 4)};
  {var}.Init.Mode = CAN_MODE_{cfg.get('mode', 'NORMAL')};
  {var}.Init.SyncJumpWidth = CAN_SJW_1TQ;
  {var}.Init.TimeSeg1 = CAN_BS1_6TQ;
  {var}.Init.TimeSeg2 = CAN_BS2_1TQ;
  {var}.Init.TimeTriggeredMode = DISABLE;
  {var}.Init.AutoBusOff = DISABLE;
  {var}.Init.AutoWakeUp = DISABLE;
  {var}.Init.AutoRetransmission = ENABLE;
  {var}.Init.ReceiveFifoLocked = DISABLE;
  {var}.Init.TransmitFifoPriority = DISABLE;
  if (HAL_CAN_Init(&{var}) != HAL_OK)
  {{
    Error_Handler();
  }}"""
    gpio = [
        {"port": pins["rx"][1], "num": int(pins["rx"][2:]), "mode": "AF_PP", "af": af, "pull": "NOPULL", "speed": "HIGH"},
        {"port": pins["tx"][1], "num": int(pins["tx"][2:]), "mode": "AF_PP", "af": af, "pull": "NOPULL", "speed": "HIGH"},
    ]
    return Peripheral(inst, var, "CAN_HandleTypeDef", f"MX_{inst}_Init", body,
                       gpio, f"__HAL_RCC_{inst}_CLK_ENABLE", f"{inst}_RX0_IRQn")


def build_adc(cfg, fam, af_table, adc_style="standard"):
    inst = cfg["instance"]
    channels = cfg.get("channels", ["IN0"])
    # ADC input pins on F4/F1: ADC1 IN0..IN7 = PA0..PA7 (very common convention)
    ch_pin_map = {"IN0": "PA0", "IN1": "PA1", "IN2": "PA2", "IN3": "PA3",
                  "IN4": "PA4", "IN5": "PA5", "IN6": "PA6", "IN7": "PA7"}
    var = f"h{inst.lower()}"
    gpio = []
    rank_lines = []

    if adc_style == "legacy":
        # F1's ADC_InitTypeDef/ADC_ChannelConfTypeDef are the older, simpler
        # IP version: no ClockPrescaler/Resolution/EOCSelection/
        # DMAContinuousRequests fields, and SamplingTime uses a different
        # macro family (ADC_SAMPLETIME_1CYCLE_5, not ADC_SAMPLETIME_3CYCLES).
        # Confirmed by an actual F1 build failure — this is not a hypothetical.
        sample_time = "ADC_SAMPLETIME_1CYCLE_5"
        for i, ch in enumerate(channels):
            pin = ch_pin_map.get(ch, "PA0")
            gpio.append({"port": pin[1], "num": int(pin[2:]), "mode": "ANALOG", "af": None, "pull": "NOPULL", "speed": None})
            rank_lines.append(f"""  sConfig.Channel = ADC_CHANNEL_{ch.replace('IN', '')};
  sConfig.Rank = {i + 1};
  sConfig.SamplingTime = {sample_time};
  if (HAL_ADC_ConfigChannel(&{var}, &sConfig) != HAL_OK)
  {{
    Error_Handler();
  }}""")
        body = f"""  ADC_ChannelConfTypeDef sConfig = {{0}};

  {var}.Instance = {inst};
  {var}.Init.DataAlign = ADC_DATAALIGN_{cfg.get('alignment', 'RIGHT')};
  {var}.Init.ScanConvMode = {"ENABLE" if len(channels) > 1 else "DISABLE"};
  {var}.Init.ContinuousConvMode = {"ENABLE" if cfg.get('continuous_mode') else "DISABLE"};
  {var}.Init.NbrOfConversion = {len(channels)};
  {var}.Init.DiscontinuousConvMode = DISABLE;
  {var}.Init.NbrOfDiscConversion = 1;
  {var}.Init.ExternalTrigConv = ADC_SOFTWARE_START;
  if (HAL_ADC_Init(&{var}) != HAL_OK)
  {{
    Error_Handler();
  }}
""" + "\n".join(rank_lines)
    else:
        sample_time = "ADC_SAMPLETIME_15CYCLES"
        for i, ch in enumerate(channels):
            pin = ch_pin_map.get(ch, "PA0")
            gpio.append({"port": pin[1], "num": int(pin[2:]), "mode": "ANALOG", "af": None, "pull": "NOPULL", "speed": None})
            rank_lines.append(f"""  sConfig.Channel = ADC_CHANNEL_{ch.replace('IN', '')};
  sConfig.Rank = {i + 1};
  sConfig.SamplingTime = {sample_time};
  if (HAL_ADC_ConfigChannel(&{var}, &sConfig) != HAL_OK)
  {{
    Error_Handler();
  }}""")
        body = f"""  ADC_ChannelConfTypeDef sConfig = {{0}};

  {var}.Instance = {inst};
  {var}.Init.ClockPrescaler = ADC_CLOCK_SYNC_PCLK_DIV4;
  {var}.Init.Resolution = ADC_RESOLUTION_{cfg.get('resolution_bits', 12)}B;
  {var}.Init.ScanConvMode = {"ENABLE" if len(channels) > 1 else "DISABLE"};
  {var}.Init.ContinuousConvMode = {"ENABLE" if cfg.get('continuous_mode') else "DISABLE"};
  {var}.Init.DiscontinuousConvMode = DISABLE;
  {var}.Init.ExternalTrigConvEdge = ADC_EXTERNALTRIGCONVEDGE_NONE;
  {var}.Init.ExternalTrigConv = ADC_SOFTWARE_START;
  {var}.Init.DataAlign = ADC_DATAALIGN_{cfg.get('alignment', 'RIGHT')};
  {var}.Init.NbrOfConversion = {len(channels)};
  {var}.Init.DMAContinuousRequests = DISABLE;
  {var}.Init.EOCSelection = ADC_EOC_SINGLE_CONV;
  if (HAL_ADC_Init(&{var}) != HAL_OK)
  {{
    Error_Handler();
  }}
""" + "\n".join(rank_lines)

    return Peripheral(inst, var, "ADC_HandleTypeDef", f"MX_{inst}_Init", body,
                       gpio, f"__HAL_RCC_{inst}_CLK_ENABLE", f"{inst}_IRQn")


def build_pwm(cfg, fam, af_table):
    inst = cfg["instance"]
    channels = cfg.get("channels", ["CH1"])
    pins_default = DEFAULT_PINS[fam]["pwm"].get(inst, {})
    pins_cfg = cfg.get("pins", {})
    af = af_table.get(inst, 1)
    var = f"h{inst.lower()}"
    gpio = []
    ch_init_lines = []
    for ch in channels:
        pin = pins_cfg.get(ch) or pins_default.get(ch)
        if pin:
            gpio.append({"port": pin[1], "num": int(pin[2:]), "mode": "AF_PP", "af": af, "pull": "NOPULL", "speed": "HIGH"})
        ch_init_lines.append(f"""  sConfigOC.OCMode = TIM_OCMODE_PWM1;
  sConfigOC.Pulse = {cfg.get('period', 999) // 2};
  sConfigOC.OCPolarity = TIM_OCPOLARITY_HIGH;
  sConfigOC.OCFastMode = TIM_OCFAST_DISABLE;
  if (HAL_TIM_PWM_ConfigChannel(&{var}, &sConfigOC, TIM_CHANNEL_{ch[-1]}) != HAL_OK)
  {{
    Error_Handler();
  }}""")
    body = f"""  TIM_MasterConfigTypeDef sMasterConfig = {{0}};
  TIM_OC_InitTypeDef sConfigOC = {{0}};

  {var}.Instance = {inst};
  {var}.Init.Prescaler = {cfg.get('prescaler', 83)};
  {var}.Init.CounterMode = TIM_COUNTERMODE_UP;
  {var}.Init.Period = {cfg.get('period', 999)};
  {var}.Init.ClockDivision = TIM_CLOCKDIVISION_DIV1;
  {var}.Init.AutoReloadPreload = TIM_AUTORELOAD_PRELOAD_DISABLE;
  if (HAL_TIM_PWM_Init(&{var}) != HAL_OK)
  {{
    Error_Handler();
  }}
  sMasterConfig.MasterOutputTrigger = TIM_TRGO_RESET;
  sMasterConfig.MasterSlaveMode = TIM_MASTERSLAVEMODE_DISABLE;
  if (HAL_TIMEx_MasterConfigSynchronization(&{var}, &sMasterConfig) != HAL_OK)
  {{
    Error_Handler();
  }}
""" + "\n".join(ch_init_lines)
    return Peripheral(inst, var, "TIM_HandleTypeDef", f"MX_{inst}_Init", body,
                       gpio, f"__HAL_RCC_{inst}_CLK_ENABLE", f"{inst}_IRQn")


BUILDERS = {
    "usart": build_usart,
    "spi": build_spi,
    "i2c": build_i2c,
    "adc": build_adc,
    "pwm": build_pwm,
}

MSP_FUNC = {
    "usart": "HAL_UART_MspInit",
    "spi": "HAL_SPI_MspInit",
    "i2c": "HAL_I2C_MspInit",
    "can": "HAL_CAN_MspInit",
    "adc": "HAL_ADC_MspInit",
    "pwm": "HAL_TIM_PWM_MspInit",
}


def gpio_init_lines(p: Peripheral, gpio_model: str):
    """One HAL_GPIO_Init() call per pin group sharing mode/af (kept simple: one per pin)."""
    lines = []
    for g in p.gpio_pins:
        lines.append("  GPIO_InitStruct.Pin = GPIO_PIN_%d;" % g["num"])
        lines.append("  GPIO_InitStruct.Mode = GPIO_MODE_%s;" % g["mode"])
        lines.append("  GPIO_InitStruct.Pull = GPIO_%s;" % g["pull"])
        if g["speed"]:
            lines.append("  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_%s;" % g["speed"])
        if gpio_model == "af_number" and g["af"] is not None:
            lines.append("  GPIO_InitStruct.Alternate = GPIO_AF%d_%s;" % (g["af"], p.instance))
        lines.append("  HAL_GPIO_Init(GPIO%s, &GPIO_InitStruct);" % g["port"])
        lines.append("")
    return "\n".join(lines)


def write_hal_msp(fam, gpio_model, peripherals, out: Path):
    blocks = []
    for name, p in peripherals:
        msp_fn = MSP_FUNC[name]
        param_type = "TIM_HandleTypeDef" if name == "pwm" else p.handle_type
        param_name = "htim" if name == "pwm" else p.var
        clk_enables = "\n".join(f"    __HAL_RCC_GPIO{port}_CLK_ENABLE();"
                                 for port in sorted({g["port"] for g in p.gpio_pins}))
        gpio_lines = gpio_init_lines(p, gpio_model)
        blocks.append(f"""void {msp_fn}({param_type} *{param_name})
{{
  GPIO_InitTypeDef GPIO_InitStruct = {{0}};
  if ({param_name}->Instance == {p.instance})
  {{
    /* Peripheral clock enable */
    {p.clk_macro}();
{clk_enables}

    /* GPIO Configuration */
{gpio_lines}  }}
}}
""")
    body = "\n".join(blocks) if blocks else ""
    text = f"""/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file         stm32{fam}xx_hal_msp.c
  * @brief        HAL MSP module — peripheral-level (clock, GPIO, NVIC) init.
  *               Generated to match STM32CubeMX's HAL_<PERIPH>_MspInit()
  *               convention: each callback is invoked automatically by the
  *               corresponding HAL_<PERIPH>_Init() the first time it runs.
  ******************************************************************************
  */
/* USER CODE END Header */
#include "main.h"

/* USER CODE BEGIN 0 */
/* USER CODE END 0 */

void HAL_MspInit(void)
{{
  __HAL_RCC_SYSCFG_CLK_ENABLE();
  __HAL_RCC_PWR_CLK_ENABLE();
}}

{body}"""
    (out / "Core/Src" / f"stm32{fam}xx_hal_msp.c").write_text(text)


def write_main_files(mcu, entry, project_name, peripherals, out: Path, freertos=False):
    fam = entry["family"]
    decls = "\n".join(f"{p.handle_type} {p.var};" for _, p in peripherals)
    protos = "\n".join(f"static void {p.init_func}(void);" for _, p in peripherals)
    calls = "\n  ".join(f"{p.init_func}();" for _, p in peripherals)
    funcs = "\n\n".join(f"""static void {p.init_func}(void)
{{
{p.init_body}
}}""" for _, p in peripherals)

    if freertos:
        rtos_include = '#include "cmsis_os2.h"\n'
        rtos_decls = """
/* Definitions for defaultTask */
osThreadId_t defaultTaskHandle;
const osThreadAttr_t defaultTask_attributes = {
  .name = "defaultTask",
  .stack_size = 128 * 4,
  .priority = (osPriority_t) osPriorityNormal,
};"""
        rtos_proto = "void StartDefaultTask(void *argument);\n"
        loop_section = """  /* USER CODE BEGIN WHILE */

  /* Init scheduler */
  osKernelInitialize();

  /* Create the thread(s) */
  defaultTaskHandle = osThreadNew(StartDefaultTask, NULL, &defaultTask_attributes);

  /* Start scheduler */
  osKernelStart();

  /* We should never get here as control is now taken by the scheduler,
     but define a loop in case we do */
  while (1)
  {
  /* USER CODE END WHILE */

  /* USER CODE BEGIN 3 */
  }
  /* USER CODE END 3 */
}

/**
  * @brief  Function implementing the defaultTask thread.
  * @param  argument: Not used
  */
void StartDefaultTask(void *argument)
{
  /* USER CODE BEGIN StartDefaultTask */
  for (;;)
  {
    osDelay(1);
  }
  /* USER CODE END StartDefaultTask */
}"""
    else:
        rtos_include = ""
        rtos_decls = ""
        rtos_proto = ""
        loop_section = """  /* USER CODE BEGIN WHILE */
  while (1)
  {
  /* USER CODE END WHILE */

  /* USER CODE BEGIN 3 */
  }
  /* USER CODE END 3 */
}"""

    main_c = f"""/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.c
  * @brief          : Main program body
  * @note           : Generated from ST HAL/CMSIS sources fetched directly
  *                    from github.com/STMicroelectronics — no STM32CubeMX
  *                    involved. Target: {mcu} ({entry.get('board_note', '')}).
  ******************************************************************************
  */
/* USER CODE END Header */
#include "main.h"
{rtos_include}
/* USER CODE BEGIN Includes */
/* USER CODE END Includes */

/* Private variables ---------------------------------------------------------*/
{decls}
{rtos_decls}

/* Private function prototypes ------------------------------------------------*/
void SystemClock_Config(void);
static void MX_GPIO_Init(void);
{protos}{rtos_proto}

/* USER CODE BEGIN 0 */
/* USER CODE END 0 */

/**
  * @brief System Clock Configuration
  * @note  Safe portable default: runs off the internal HSI oscillator with
  *        no PLL, so it works out of the box on any board with no external
  *        crystal assumptions. Raise SYSCLK via HSE+PLL once you've
  *        confirmed your board's actual HSE frequency — see README.
  */
void SystemClock_Config(void)
{{
  RCC_OscInitTypeDef RCC_OscInitStruct = {{0}};
  RCC_ClkInitTypeDef RCC_ClkInitStruct = {{0}};

  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSI;
  RCC_OscInitStruct.HSIState = RCC_HSI_ON;
  RCC_OscInitStruct.HSICalibrationValue = RCC_HSICALIBRATION_DEFAULT;
  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_NONE;
  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
  {{
    Error_Handler();
  }}

  RCC_ClkInitStruct.ClockType = RCC_CLOCKTYPE_HCLK | RCC_CLOCKTYPE_SYSCLK
                               | RCC_CLOCKTYPE_PCLK1 | RCC_CLOCKTYPE_PCLK2;
  RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_HSI;
  RCC_ClkInitStruct.AHBCLKDivider = RCC_SYSCLK_DIV1;
  RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV1;
  RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV1;
  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_0) != HAL_OK)
  {{
    Error_Handler();
  }}
}}

int main(void)
{{
  HAL_Init();
  SystemClock_Config();

  MX_GPIO_Init();
  {calls}

{loop_section}

static void MX_GPIO_Init(void)
{{
  /* USER CODE BEGIN MX_GPIO_Init */
  /* Add plain GPIO (LEDs, buttons, etc.) configuration here.        */
  /* Peripheral alternate-function pins are configured automatically */
  /* by each HAL_<PERIPH>_MspInit() callback in stm32{fam}xx_hal_msp.c. */
  /* USER CODE END MX_GPIO_Init */
}}

{funcs}

/**
  * @brief  This function is executed in case of error occurrence.
  */
void Error_Handler(void)
{{
  __disable_irq();
  while (1)
  {{
  }}
}}

#ifdef USE_FULL_ASSERT
void assert_failed(uint8_t *file, uint32_t line)
{{
}}
#endif
"""
    (out / "Core/Src/main.c").write_text(main_c)

    main_h = f"""#ifndef __MAIN_H
#define __MAIN_H

#ifdef __cplusplus
extern "C" {{
#endif

#include "stm32{fam}xx_hal.h"

void Error_Handler(void);

#ifdef __cplusplus
}}
#endif

#endif /* __MAIN_H */
"""
    (out / "Core/Inc/main.h").write_text(main_h)


def write_it_files(fam, peripherals, out: Path, freertos=False):
    irqs = sorted({p.irq_name for _, p in peripherals if p.irq_name})
    handler_names = {irq: irq.replace("_IRQn", "_IRQHandler") for irq in irqs}
    handlers_h = "\n".join(f"void {h}(void);" for h in handler_names.values())
    handlers_c = "\n\n".join(f"""void {h}(void)
{{
  /* USER CODE BEGIN {h} */
  /* USER CODE END {h} */
}}""" for h in handler_names.values())

    it_h = f"""#ifndef __STM32{fam.upper()}XX_IT_H
#define __STM32{fam.upper()}XX_IT_H

#ifdef __cplusplus
extern "C" {{
#endif

void NMI_Handler(void);
void HardFault_Handler(void);
void MemManage_Handler(void);
void BusFault_Handler(void);
void UsageFault_Handler(void);
void SVC_Handler(void);
void DebugMon_Handler(void);
void PendSV_Handler(void);
void SysTick_Handler(void);
{handlers_h}

#ifdef __cplusplus
}}
#endif

#endif
"""
    (out / f"Core/Inc/stm32{fam}xx_it.h").write_text(it_h)

    if freertos:
        # port.c (FreeRTOS) supplies SVC_Handler/PendSV_Handler itself, via
        # the vPortSVCHandler/xPortPendSVHandler #define remap in
        # FreeRTOSConfig.h — defining them again here would be a duplicate
        # symbol at link time, so they're deliberately NOT defined below.
        rtos_includes = ('#include "FreeRTOS.h"\n#include "task.h"\n\n'
                          '/* xPortSysTickHandler is defined in FreeRTOS\'s port.c but only\n'
                          ' * forward-declared there (not exported via any FreeRTOS header),\n'
                          ' * so it needs an explicit extern here. */\n'
                          'extern void xPortSysTickHandler(void);\n')
        svc_pendsv_stubs = ""
        systick_body = """void SysTick_Handler(void)
{
  HAL_IncTick();
#if (INCLUDE_xTaskGetSchedulerState == 1)
  if (xTaskGetSchedulerState() != taskSCHEDULER_NOT_STARTED)
  {
    xPortSysTickHandler();
  }
#endif
}"""
    else:
        rtos_includes = ""
        svc_pendsv_stubs = "void SVC_Handler(void) {}\nvoid PendSV_Handler(void) {}\n"
        systick_body = """void SysTick_Handler(void)
{
  HAL_IncTick();
}"""

    it_c = f"""#include "main.h"
#include "stm32{fam}xx_it.h"
{rtos_includes}
void NMI_Handler(void) {{ while (1) {{}} }}
void HardFault_Handler(void) {{ while (1) {{}} }}
void MemManage_Handler(void) {{ while (1) {{}} }}
void BusFault_Handler(void) {{ while (1) {{}} }}
void UsageFault_Handler(void) {{ while (1) {{}} }}
{svc_pendsv_stubs}void DebugMon_Handler(void) {{}}
{systick_body}

{handlers_c}
"""
    (out / f"Core/Src/stm32{fam}xx_it.c").write_text(it_c)


def write_linker_script(entry, project_name, out: Path):
    ram_kb = entry["ram_size_kb"]
    flash_kb = entry["flash_size_kb"]
    text = f"""/* Auto-generated linker script for {project_name}
 * FLASH/RAM sizes come from mcu_catalog.json — double check them against
 * your exact part's datasheet before relying on this for production use.
 * Multi-bank/multi-region parts (e.g. many H7/L4/U5 devices have several
 * separate SRAM blocks) are simplified here to one contiguous RAM region.
 */
ENTRY(Reset_Handler)

_estack = ORIGIN(RAM) + LENGTH(RAM);
_Min_Heap_Size = 0x200;
_Min_Stack_Size = 0x400;

MEMORY
{{
  RAM (xrw)      : ORIGIN = {entry['ram_origin']},   LENGTH = {ram_kb}K
  FLASH (rx)     : ORIGIN = {entry['flash_origin']}, LENGTH = {flash_kb}K
}}

SECTIONS
{{
  .isr_vector :
  {{
    . = ALIGN(4);
    KEEP(*(.isr_vector))
    . = ALIGN(4);
  }} >FLASH

  .text :
  {{
    . = ALIGN(4);
    *(.text)
    *(.text*)
    *(.glue_7)
    *(.glue_7t)
    *(.eh_frame)
    KEEP (*(.init))
    KEEP (*(.fini))
    . = ALIGN(4);
    _etext = .;
  }} >FLASH

  .rodata :
  {{
    . = ALIGN(4);
    *(.rodata)
    *(.rodata*)
    . = ALIGN(4);
  }} >FLASH

  .ARM.extab : {{ *(.ARM.extab* .gnu.linkonce.armextab.*) }} >FLASH
  .ARM : {{ __exidx_start = .; *(.ARM.exidx*) __exidx_end = .; }} >FLASH

  _sidata = LOADADDR(.data);

  .data :
  {{
    . = ALIGN(4);
    _sdata = .;
    *(.data)
    *(.data*)
    . = ALIGN(4);
    _edata = .;
  }} >RAM AT> FLASH

  . = ALIGN(4);
  .bss :
  {{
    _sbss = .;
    __bss_start__ = _sbss;
    *(.bss)
    *(.bss*)
    *(COMMON)
    . = ALIGN(4);
    _ebss = .;
    __bss_end__ = _ebss;
  }} >RAM

  ._user_heap_stack :
  {{
    . = ALIGN(8);
    PROVIDE ( end = . );
    PROVIDE ( _end = . );
    . = . + _Min_Heap_Size;
    . = . + _Min_Stack_Size;
    . = ALIGN(8);
  }} >RAM

  /DISCARD/ :
  {{
    libc.a ( * )
    libm.a ( * )
    libgcc.a ( * )
  }}

  .ARM.attributes 0 : {{ *(.ARM.attributes) }}
}}
"""
    (out / f"{project_name}.ld").write_text(text)


def write_makefile(mcu, entry, project_name, startup_file, hal_src_files, system_c, out: Path,
                    extra_c_sources=None, extra_include_dirs=None):
    fam = entry["family"]
    fpu_flags = ""
    if entry.get("fpu"):
        fpu_flags = f"-mfpu={entry['fpu']} -mfloat-abi={entry.get('float_abi', 'hard')}"

    c_sources = ["Core/Src/main.c", f"Core/Src/stm32{fam}xx_it.c", f"Core/Src/stm32{fam}xx_hal_msp.c",
                 f"Core/Src/{system_c}"] + [f"Drivers/STM32{fam.upper()}xx_HAL_Driver/Src/{f}" for f in hal_src_files]
    c_sources += extra_c_sources or []
    c_sources_str = " \\\n\t".join(c_sources)

    extra_includes = "".join(f" \\\n\t-I{d}" for d in (extra_include_dirs or []))

    text = f"""# Auto-generated Makefile for {project_name} ({mcu})
# Works standalone (make / make flash) and can be imported into
# STM32CubeIDE as an existing Makefile project, or used from VS Code /
# CLion via the CMake wrapper in this repo (see README).

TARGET = {project_name}
MCU_CPU = -mcpu={entry['cpu']} -mthumb {fpu_flags}
DEFS = -D{entry['device_define']} -DUSE_HAL_DRIVER

C_INCLUDES = \\
\t-ICore/Inc \\
\t-IDrivers/STM32{fam.upper()}xx_HAL_Driver/Inc \\
\t-IDrivers/CMSIS/Device/ST/STM32{fam.upper()}xx/Include \\
\t-IDrivers/CMSIS/Include{extra_includes}

C_SOURCES = \\
\t{c_sources_str}

ASM_SOURCES = Core/Startup/{startup_file}

CC = arm-none-eabi-gcc
AS = arm-none-eabi-gcc -x assembler-with-cpp
CP = arm-none-eabi-objcopy
SZ = arm-none-eabi-size

CFLAGS = $(MCU_CPU) $(DEFS) $(C_INCLUDES) -Og -g3 -Wall -ffunction-sections -fdata-sections
LDFLAGS = $(MCU_CPU) -T{project_name}.ld -specs=nano.specs -specs=nosys.specs \\
\t-Wl,--gc-sections -Wl,-Map={project_name}.map -lc -lm -lnosys

BUILD_DIR = build
OBJECTS = $(addprefix $(BUILD_DIR)/,$(notdir $(C_SOURCES:.c=.o)))
OBJECTS += $(addprefix $(BUILD_DIR)/,$(notdir $(ASM_SOURCES:.s=.o)))
vpath %.c $(sort $(dir $(C_SOURCES)))
vpath %.s $(sort $(dir $(ASM_SOURCES)))

all: $(BUILD_DIR)/$(TARGET).elf $(BUILD_DIR)/$(TARGET).hex $(BUILD_DIR)/$(TARGET).bin

$(BUILD_DIR)/%.o: %.c Makefile | $(BUILD_DIR)
\t$(CC) -c $(CFLAGS) $< -o $@

$(BUILD_DIR)/%.o: %.s Makefile | $(BUILD_DIR)
\t$(AS) -c $(CFLAGS) $< -o $@

$(BUILD_DIR)/$(TARGET).elf: $(OBJECTS) Makefile
\t$(CC) $(OBJECTS) $(LDFLAGS) -o $@
\t$(SZ) $@

$(BUILD_DIR)/%.hex: $(BUILD_DIR)/%.elf
\t$(CP) -O ihex $< $@

$(BUILD_DIR)/%.bin: $(BUILD_DIR)/%.elf
\t$(CP) -O binary -S $< $@

$(BUILD_DIR):
\tmkdir -p $@

clean:
\trm -rf $(BUILD_DIR)

.PHONY: all clean
"""
    (out / "Makefile").write_text(text)


def write_editor_files(editor, mcu, entry, project_name, out: Path):
    fam = entry["family"]
    if editor == "vscode":
        vscode = out / ".vscode"
        vscode.mkdir(exist_ok=True)
        (vscode / "tasks.json").write_text(json.dumps({
            "version": "2.0.0",
            "tasks": [{
                "label": "Build", "type": "shell", "command": "make",
                "group": {"kind": "build", "isDefault": True},
                "problemMatcher": ["$gcc"]
            }, {
                "label": "Clean", "type": "shell", "command": "make clean"
            }]
        }, indent=2))
        (vscode / "c_cpp_properties.json").write_text(json.dumps({
            "configurations": [{
                "name": "STM32",
                "includePath": [
                    "${workspaceFolder}/Core/Inc",
                    f"${{workspaceFolder}}/Drivers/STM32{fam.upper()}xx_HAL_Driver/Inc",
                    f"${{workspaceFolder}}/Drivers/CMSIS/Device/ST/STM32{fam.upper()}xx/Include",
                    "${workspaceFolder}/Drivers/CMSIS/Include"
                ],
                "defines": [entry["device_define"], "USE_HAL_DRIVER"],
                "compilerPath": "/usr/bin/arm-none-eabi-gcc",
                "cStandard": "c11"
            }],
            "version": 4
        }, indent=2))
        (vscode / "launch.json").write_text(json.dumps({
            "version": "0.2.0",
            "configurations": [{
                "name": "Debug (Cortex-Debug + OpenOCD)",
                "cwd": "${workspaceFolder}",
                "executable": f"build/{project_name}.elf",
                "request": "launch",
                "type": "cortex-debug",
                "servertype": "openocd",
                "device": mcu,
                "configFiles": ["interface/stlink.cfg", "target/stm32f4x.cfg"]
            }]
        }, indent=2))
    elif editor == "cubeide":
        note = out / "STM32CubeIDE_IMPORT.md"
        note.write_text(
            "# Importing into STM32CubeIDE\n\n"
            "This project uses a plain Makefile rather than hand-generated Eclipse\n"
            "`.project`/`.cproject` XML (that XML is fragile to hand-write and easy\n"
            "to get subtly wrong). STM32CubeIDE imports Makefile projects natively:\n\n"
            "1. File -> Open Projects from File System... -> select this folder.\n"
            "2. Or File -> Import -> C/C++ -> Existing Code as Makefile Project.\n"
            "3. Build with the IDE's Makefile Targets view, or just `make` in a\n"
            "   terminal — both use the same Makefile.\n"
        )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mcu", required=True)
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--scratch", default=None)
    args = ap.parse_args()

    catalog = json.loads(Path(args.catalog).read_text())
    if args.mcu not in catalog:
        sys.exit(f"ERROR: {args.mcu} not in catalog. Known: {', '.join(catalog)}")
    entry = catalog[args.mcu]
    cfg = json.loads(Path(args.config).read_text())
    interfaces = cfg.get("interfaces", {})
    project_name = cfg.get("project_name", "GeneratedProject")
    editor = cfg.get("editor", "makefile")
    freertos = bool(cfg.get("freertos", False))
    if freertos and not entry.get("freertos_port"):
        sys.exit(f"ERROR: FreeRTOS was requested but {args.mcu} has no 'freertos_port' "
                 f"in the catalog. Add one before requesting FreeRTOS for this MCU — "
                 f"silently generating a bare-metal project instead of what was asked "
                 f"for is exactly the kind of surprise this generator should not produce.")

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    scratch = Path(args.scratch) if args.scratch else out.parent / ".fetch-scratch"
    scratch.mkdir(parents=True, exist_ok=True)

    fetch_result = fetch_sources(args.mcu, entry, interfaces, out, scratch)

    fam = entry["family"]
    af_table = AF_TABLE.get(fam, {})
    peripherals = []
    for name, cfg_iface in interfaces.items():
        if not cfg_iface:
            continue
        if name == "can":
            if entry.get("can_type") == "bxcan":
                peripherals.append((name, build_can_bxcan(cfg_iface, fam, af_table)))
            else:
                sys.exit(f"ERROR: CAN was requested but can_type="
                          f"'{entry.get('can_type')}' for {args.mcu} is not supported "
                          f"by this generator (only 'bxcan' is implemented). Either "
                          f"pick a different MCU, disable CAN, or implement FDCAN "
                          f"support in build_can_fdcan() first — a project that "
                          f"silently omits an interface you asked for is worse than "
                          f"one that refuses to generate.")
            continue
        if name == "adc":
            peripherals.append((name, build_adc(cfg_iface, fam, af_table,
                                                 adc_style=entry.get("adc_style", "standard"))))
            continue
        builder = BUILDERS.get(name)
        if builder:
            peripherals.append((name, builder(cfg_iface, fam, af_table)))
        else:
            sys.exit(f"ERROR: unknown interface '{name}' in config — no builder "
                      f"registered for it in BUILDERS.")

    extra_c_sources, extra_include_dirs = [], []
    if freertos:
        rtos_result = fetch_freertos(entry, out, scratch)
        write_freertos_config(entry, out)
        extra_c_sources = rtos_result["c_files"]
        extra_include_dirs = rtos_result["include_dirs"]

    write_main_files(args.mcu, entry, project_name, peripherals, out, freertos=freertos)
    write_it_files(fam, peripherals, out, freertos=freertos)
    write_hal_msp(fam, entry["gpio_model"], peripherals, out)
    write_linker_script(entry, project_name, out)
    write_makefile(args.mcu, entry, project_name, fetch_result["startup_file"],
                    fetch_result["hal_src_files"], fetch_result["system_c"], out,
                    extra_c_sources=extra_c_sources, extra_include_dirs=extra_include_dirs)
    write_editor_files(editor, args.mcu, entry, project_name, out)

    print(f"\n[done] Generated project at {out}")
    print(f"        cd {out} && make   # requires gcc-arm-none-eabi")


if __name__ == "__main__":
    main()
