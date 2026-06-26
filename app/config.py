from __future__ import annotations
from pathlib import Path
import json
from pydantic import BaseModel, Field

DATA_DIR = Path('/app/data')
CONFIG_PATH = DATA_DIR / 'config.json'

class AppConfig(BaseModel):
    gateway_name: str = 'Daikin DIII Modbus Gateway'
    gateway_ip: str = '192.168.1.100'
    gateway_port: int = Field(default=502, ge=1, le=65535)
    slave_id: int = Field(default=1, ge=1, le=247)
    protocol: str = 'TCP'
    virtual_modbus_port: int = Field(default=1502, ge=1, le=65535)
    knx_gateway_name: str = 'KNX Main Gateway'
    knx_gateway_ip: str = '192.168.1.10'
    knx_gateway_port: int = Field(default=3671, ge=1, le=65535)
    knx_physical_address: str = '1.0.100'
    knx_protocol: str = 'TunnelUDP'
    knx_auto_connect: bool = False

def load_config() -> AppConfig:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        try:
            return AppConfig.model_validate_json(CONFIG_PATH.read_text())
        except Exception:
            pass
    cfg = AppConfig()
    save_config(cfg)
    return cfg

def save_config(cfg: AppConfig) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(cfg.model_dump_json(indent=2))
