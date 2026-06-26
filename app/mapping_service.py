from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .register_map import unit_id_to_index

DATA_DIR = Path('/app/data')
PROJECT_PATH = DATA_DIR / 'project.json'

DEFAULT_ROOM = 'Default / Floor 1 / Room'
DEFAULT_GA_BASE = '1/1/0'
GA_STRIDE_PER_INDOOR = 11

GA_OFFSETS: dict[tuple[str, str], int] = {
    ('switch', 'control'): 0,
    ('switch', 'status'): 1,
    ('setpoint', 'control'): 2,
    ('setpoint', 'updown'): 3,
    ('setpoint', 'status'): 4,
    ('ambient', 'status'): 5,
    ('mode', 'control'): 6,
    ('mode', 'status'): 7,
    ('fan', 'control'): 8,
    ('fan', 'status'): 9,
    ('fan', 'step'): 10,
}


class ProjectConfig(BaseModel):
    rooms: list[dict[str, Any]] = Field(default_factory=list)
    devices: list[dict[str, Any]] = Field(default_factory=list)


class AutoMapRequest(BaseModel):
    base_address: str = DEFAULT_GA_BASE
    overwrite_existing: bool = False
    units: list[dict[str, Any] | str] = Field(default_factory=list)


def empty_mapping() -> dict[str, dict[str, Any]]:
    return {
        'switch': {'name': 'ACSwitch', 'control': '', 'status': ''},
        'setpoint': {
            'name': 'ACTempSetpoint',
            'control': '',
            'updown': '',
            'status': '',
            'min': 16,
            'max': 32,
        },
        'ambient': {'name': 'ACTempAmbient', 'status': ''},
        'mode': {'name': 'ACMode', 'control': '', 'status': ''},
        'fan': {'name': 'ACFan', 'control': '', 'status': '', 'step': ''},
    }


def normalize_mapping(mapping: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    merged = empty_mapping()
    for section, values in (mapping or {}).items():
        if section in merged and isinstance(values, dict):
            merged[section].update(values)
    return merged


def normalize_target(target: dict[str, Any]) -> dict[str, Any]:
    indoor = str(target.get('indoor') or '').strip()
    return {
        'target': str(target.get('target') or f'Indoor {indoor}' or 'Air Conditioner'),
        'type': str(target.get('type') or 'Air Condition'),
        'room': str(target.get('room') or DEFAULT_ROOM),
        'indoor': indoor,
        'mapping': normalize_mapping(target.get('mapping') if isinstance(target.get('mapping'), dict) else None),
    }


def load_project() -> ProjectConfig:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if PROJECT_PATH.exists():
        try:
            raw = json.loads(PROJECT_PATH.read_text())
            project = ProjectConfig.model_validate(raw)
            project.devices = [normalize_target(d) for d in project.devices]
            return project
        except Exception:
            pass
    project = ProjectConfig()
    save_project(project)
    return project


def save_project(project: ProjectConfig) -> ProjectConfig:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    project.devices = [normalize_target(d) for d in project.devices]
    PROJECT_PATH.write_text(project.model_dump_json(indent=2))
    return project


def group_address_to_int(address: str) -> int:
    parts = str(address or '').strip().split('/')
    if len(parts) != 3:
        raise ValueError('KNX group address must use 3-level format, e.g. 1/1/0')
    main, middle, sub = (int(p) for p in parts)
    if not (0 <= main <= 31 and 0 <= middle <= 7 and 0 <= sub <= 255):
        raise ValueError('KNX group address range is main 0..31 / middle 0..7 / sub 0..255')
    return (main * 8 + middle) * 256 + sub


def int_to_group_address(value: int) -> str:
    if not (0 <= value < 32 * 8 * 256):
        raise ValueError('KNX group address range exceeded')
    main_middle, sub = divmod(value, 256)
    main, middle = divmod(main_middle, 8)
    return f'{main}/{middle}/{sub}'


def first_room_label(rooms: list[dict[str, Any]]) -> str:
    if not rooms:
        return DEFAULT_ROOM
    room = rooms[0]
    return f"{room.get('area') or 'Default'} / {room.get('floor') or 'Floor 1'} / {room.get('room') or 'Room'}"


def _unit_id(unit: dict[str, Any] | str) -> str:
    if isinstance(unit, str):
        return unit.strip()
    return str(unit.get('id') or unit.get('indoor') or '').strip()


def _configured_group_addresses(targets: list[dict[str, Any]]) -> set[str]:
    addresses: set[str] = set()
    for target in targets:
        for section in normalize_mapping(target.get('mapping') if isinstance(target.get('mapping'), dict) else None).values():
            for value in section.values():
                text = str(value or '').strip()
                if text.count('/') == 2:
                    addresses.add(text)
    return addresses


def apply_auto_knx_addresses(target: dict[str, Any], base_address: str, overwrite: bool, used_addresses: set[str] | None = None) -> dict[str, Any]:
    target = normalize_target(target)
    indoor_index = unit_id_to_index(target['indoor'])
    base = group_address_to_int(base_address) + indoor_index * GA_STRIDE_PER_INDOOR
    for (section, field), offset in GA_OFFSETS.items():
        current = str(target['mapping'][section].get(field) or '').strip()
        if overwrite or not current:
            address_value = base + offset
            candidate = int_to_group_address(address_value)
            while used_addresses is not None and candidate in used_addresses:
                address_value += 1
                candidate = int_to_group_address(address_value)
            target['mapping'][section][field] = candidate
            if used_addresses is not None:
                used_addresses.add(candidate)
    return target


def build_auto_targets(
    units: list[dict[str, Any] | str],
    *,
    rooms: list[dict[str, Any]],
    existing: list[dict[str, Any]],
    base_address: str = DEFAULT_GA_BASE,
    overwrite_existing: bool = False,
) -> list[dict[str, Any]]:
    by_indoor = {str(t.get('indoor') or '').strip(): normalize_target(t) for t in existing if t.get('indoor')}
    room_label = first_room_label(rooms)
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    used_addresses = set() if overwrite_existing else _configured_group_addresses(existing)

    for unit in units:
        indoor = _unit_id(unit)
        if not indoor:
            continue
        target = by_indoor.get(indoor) or {
            'target': f'Indoor {indoor}',
            'type': 'Air Condition',
            'room': room_label,
            'indoor': indoor,
            'mapping': empty_mapping(),
        }
        ordered.append(apply_auto_knx_addresses(target, base_address, overwrite_existing, used_addresses))
        seen.add(indoor)

    for target in existing:
        indoor = str(target.get('indoor') or '').strip()
        if indoor and indoor not in seen:
            ordered.append(normalize_target(target))

    return ordered


def register_rows_for_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for target in targets:
        t = normalize_target(target)
        if not t['indoor']:
            continue
        try:
            index = unit_id_to_index(t['indoor'])
        except Exception as exc:
            rows.append(_row(t, 'Indoor', 'Address', 'Config', '', '', str(exc), '-'))
            continue
        input_base = 32001 + index * 6
        holding_base = 42001 + index * 3
        mapping = t['mapping']
        rows.extend([
            _row(t, 'ACSwitch', 'On/off Control', 'KNX -> DTA116A', mapping['switch'].get('control'), '1.001', '-', f'{holding_base} bit0'),
            _row(t, 'ACSwitch', 'On/off Status', 'DTA116A -> KNX', mapping['switch'].get('status'), '1.001', f'{input_base} bit0', '-'),
            _row(t, 'ACTempSetpoint', 'Setpoint Control', 'KNX -> DTA116A', mapping['setpoint'].get('control'), '9.001', '-', f'{holding_base + 2} signed x10'),
            _row(t, 'ACTempSetpoint', 'Setpoint Control UpDown', 'KNX -> DTA116A', mapping['setpoint'].get('updown'), '1.007', '-', f'{holding_base + 2} +/-1C'),
            _row(t, 'ACTempSetpoint', 'Setpoint Status', 'DTA116A -> KNX', mapping['setpoint'].get('status'), '9.001', f'{input_base + 2} signed x10', '-'),
            _row(t, 'ACTempAmbient', 'Status Ambient', 'DTA116A -> KNX', mapping['ambient'].get('status'), '9.001', f'{input_base + 4} signed x10', '-'),
            _row(t, 'ACMode', 'Mode Control', 'KNX -> DTA116A', mapping['mode'].get('control'), '20.105', '-', f'{holding_base + 1} bits0..3'),
            _row(t, 'ACMode', 'Mode Status', 'DTA116A -> KNX', mapping['mode'].get('status'), '20.105', f'{input_base + 1} bits0..3', '-'),
            _row(t, 'ACFan', 'Fan Control', 'KNX -> DTA116A', mapping['fan'].get('control'), '5.001', '-', f'{holding_base} bits12..14'),
            _row(t, 'ACFan', 'Fan Control Step', 'KNX -> DTA116A', mapping['fan'].get('step'), '1.007', '-', f'{holding_base} bits12..14 +/-1 step'),
            _row(t, 'ACFan', 'Fan Status', 'DTA116A -> KNX', mapping['fan'].get('status'), '5.001', f'{input_base} bits12..14', '-'),
        ])
    return rows


def _row(
    target: dict[str, Any],
    obj: str,
    field: str,
    direction: str,
    ga: Any,
    dpt: str,
    modbus_status: str,
    modbus_control: str,
) -> dict[str, Any]:
    return {
        'target': target['target'],
        'indoor': target['indoor'],
        'object': obj,
        'field': field,
        'direction': direction,
        'knx_group_address': str(ga or ''),
        'dpt': dpt,
        'modbus_status': modbus_status,
        'modbus_control': modbus_control,
    }
