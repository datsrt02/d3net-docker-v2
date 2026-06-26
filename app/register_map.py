from __future__ import annotations

INPUT_SYSTEM_BASE = 0          # 30001
INPUT_CAP_BASE = 1000          # 31001
INPUT_CAP_STEP = 3
INPUT_STATUS_BASE = 2000       # 32001
INPUT_STATUS_STEP = 6
INPUT_ERROR_BASE = 3600        # 33601
INPUT_ERROR_STEP = 2

HOLDING_FORCED_OFF = 1000      # 41001
HOLDING_UNIT_BASE = 2000       # 42001
HOLDING_UNIT_STEP = 3


def unit_id_to_index(unit_id: str) -> int:
    upper_s, lower_s = unit_id.split("-", 1)
    upper = int(upper_s)
    lower = int(lower_s)
    if upper < 1 or upper > 4 or lower < 0 or lower > 15:
        raise ValueError("DIII address must be in range 1-00 to 4-15")
    return (upper - 1) * 16 + lower


def index_to_unit_id(index: int) -> str:
    return f"{index // 16 + 1}-{index % 16:02d}"


def set_bit(registers: list[int], reg_index: int, bit: int, value: bool) -> None:
    mask = 1 << bit
    if value:
        registers[reg_index] |= mask
    else:
        registers[reg_index] &= ~mask
