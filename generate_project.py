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
        "pwm":   {"TIM2": {"CH1": "PA0", "CH2": "PA1", "CH3": "PA2", "CH4": "PA3"}},
    },
    "f1": {
        "usart": {"USART1": {"tx": "PA9", "rx": "PA10"}, "USART2": {"tx": "PA2", "rx": "PA3"}},
        "spi":   {"SPI1": {"sck": "PA5", "miso": "PA6", "mosi": "PA7"}},
        "i2c":   {"I2C1": {"scl": "PB6", "sda": "PB7"}},
        "can":   {"CAN1": {"rx": "PA11", "tx": "PA12"}},
        "pwm":   {"TIM2": {"CH1": "PA0", "CH2": "PA1", "CH3": "PA2", "CH4": "PA3"}},
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
            mods += ["hal_can"] if can_type == "bxcan" else ["hal_fdcan"]
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

    print(f"[fetch] cmsis-core ...")
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


def build_adc(cfg, fam, af_table):
    inst = cfg["instance"]
    channels = cfg.get("channels", ["IN0"])
    # ADC input pins on F4/F1: ADC1 IN0..IN7 = PA0..PA7 (very common convention)
    ch_pin_map = {"IN0": "PA0", "IN1": "PA1", "IN2": "PA2", "IN3": "PA3",
                  "IN4": "PA4", "IN5": "PA5", "IN6": "PA6", "IN7": "PA7"}
    var = f"h{inst.lower()}"
    rank_lines = []
    gpio = []
    for i, ch in enumerate(channels):
        pin = ch_pin_map.get(ch, "PA0")
        gpio.append({"port": pin[1], "num": int(pin[2:]), "mode": "ANALOG", "af": None, "pull": "NOPULL", "speed": None})
        rank_lines.append(f"""  sConfig.Channel = ADC_CHANNEL_{ch.replace('IN', '')};
  sConfig.Rank = {i + 1};
  sConfig.SamplingTime = ADC_SAMPLETIME_15CYCLES;
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


def write_main_files(mcu, entry, project_name, peripherals, out: Path):
    fam = entry["family"]
    decls = "\n".join(f"{p.handle_type} {p.var};" for _, p in peripherals)
    protos = "\n".join(f"static void {p.init_func}(void);" for _, p in peripherals)
    calls = "\n  ".join(f"{p.init_func}();" for _, p in peripherals)
    funcs = "\n\n".join(f"""static void {p.init_func}(void)
{{
{p.init_body}
}}""" for _, p in peripherals)

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

/* USER CODE BEGIN Includes */
/* USER CODE END Includes */

/* Private variables ---------------------------------------------------------*/
{decls}

/* Private function prototypes ------------------------------------------------*/
void SystemClock_Config(void);
static void MX_GPIO_Init(void);
{protos}

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

  /* USER CODE BEGIN WHILE */
  while (1)
  {{
  /* USER CODE END WHILE */

  /* USER CODE BEGIN 3 */
  }}
  /* USER CODE END 3 */
}}

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


def write_it_files(fam, peripherals, out: Path):
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

    it_c = f"""#include "main.h"
#include "stm32{fam}xx_it.h"

void NMI_Handler(void) {{ while (1) {{}} }}
void HardFault_Handler(void) {{ while (1) {{}} }}
void MemManage_Handler(void) {{ while (1) {{}} }}
void BusFault_Handler(void) {{ while (1) {{}} }}
void UsageFault_Handler(void) {{ while (1) {{}} }}
void SVC_Handler(void) {{}}
void DebugMon_Handler(void) {{}}
void PendSV_Handler(void) {{}}
void SysTick_Handler(void)
{{
  HAL_IncTick();
}}

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


def write_makefile(mcu, entry, project_name, startup_file, hal_src_files, system_c, out: Path):
    fam = entry["family"]
    fpu_flags = ""
    if entry.get("fpu"):
        fpu_flags = f"-mfpu={entry['fpu']} -mfloat-abi={entry.get('float_abi', 'hard')}"

    c_sources = ["Core/Src/main.c", f"Core/Src/stm32{fam}xx_it.c", f"Core/Src/stm32{fam}xx_hal_msp.c",
                 f"Core/Src/{system_c}"] + [f"Drivers/STM32{fam.upper()}xx_HAL_Driver/Src/{f}" for f in hal_src_files]
    c_sources_str = " \\\n\t".join(c_sources)

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
\t-IDrivers/CMSIS/Include

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
                print(f"[warn] CAN requested but can_type='{entry.get('can_type')}' "
                      f"is not yet implemented by this generator — skipping.")
            continue
        builder = BUILDERS.get(name)
        if builder:
            peripherals.append((name, builder(cfg_iface, fam, af_table)))

    write_main_files(args.mcu, entry, project_name, peripherals, out)
    write_it_files(fam, peripherals, out)
    write_hal_msp(fam, entry["gpio_model"], peripherals, out)
    write_linker_script(entry, project_name, out)
    write_makefile(args.mcu, entry, project_name, fetch_result["startup_file"],
                    fetch_result["hal_src_files"], fetch_result["system_c"], out)
    write_editor_files(editor, args.mcu, entry, project_name, out)

    print(f"\n[done] Generated project at {out}")
    print(f"        cd {out} && make   # requires gcc-arm-none-eabi")


if __name__ == "__main__":
    main()
