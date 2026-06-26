from __future__ import annotations
import asyncio, logging
from typing import Any
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.framer import FramerType as ModbusFramer
from .config import AppConfig
from .modbus_server import VirtualModbusServer
from .d3net.gateway import D3netGateway, D3netUnit
from .d3net.encoding import SystemStatus, UnitCapability, UnitStatus, UnitError
from .d3net.const import D3netOperationMode, D3netFanSpeed, D3netFanDirection

_LOGGER = logging.getLogger(__name__)

def _mode_name(raw: int) -> str:
    return {0:'FAN',1:'HEAT',2:'COOL',3:'AUTO',4:'VENT',5:'UNDEFINED',6:'SLAVE',7:'DRY'}.get(raw, f'RAW_{raw}')

def _safe(fn, default=None):
    try: return fn()
    except Exception: return default

class D3netRuntime:
    def __init__(self) -> None:
        self.config: AppConfig | None = None
        self.gateway: D3netGateway | None = None
        self.running = False
        self.connected = False
        self.last_error: str | None = None
        self.poll_task: asyncio.Task | None = None
        self.virtual_server: VirtualModbusServer | None = None

    async def start(self, config: AppConfig) -> None:
        await self.stop()
        self.config = config
        client = AsyncModbusTcpClient(host=config.gateway_ip, port=config.gateway_port, timeout=10) if config.protocol != 'RTU over TCP' else AsyncModbusTcpClient(host=config.gateway_ip, port=config.gateway_port, timeout=10, framer=ModbusFramer.RTU)
        self.gateway = D3netGateway(client, config.slave_id)
        try:
            await self.gateway.async_setup()
            if not self.gateway.units:
                await self.rediscover_units_from_system()
            self.connected = True
            self.running = True
            self.last_error = None
        except Exception as exc:
            self.connected = False
            self.running = False
            self.last_error = str(exc)
            raise
        self.virtual_server = VirtualModbusServer(config.virtual_modbus_port)
        try: await self.virtual_server.start()
        except Exception as exc: _LOGGER.warning('virtual modbus server failed: %s', exc)
        self.poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self.running = False
        if self.poll_task:
            self.poll_task.cancel()
            try: await self.poll_task
            except Exception: pass
            self.poll_task = None
        if self.gateway:
            try: await self.gateway.async_close()
            except Exception: pass
        if self.virtual_server:
            try: await self.virtual_server.stop()
            except Exception: pass
        self.gateway = None
        self.connected = False

    async def _poll_loop(self) -> None:
        while self.running:
            try: await self.poll_once()
            except Exception as exc: self.last_error = str(exc)
            await asyncio.sleep(3)

    async def poll_once(self) -> None:
        if not self.gateway: return
        await self.rediscover_units_from_system()
        for unit in self.gateway.units or []:
            await unit.async_update_status()
        self.connected = True
        await self.sync_all_to_virtual_modbus()

    async def rediscover_units_from_system(self) -> None:
        if not self.gateway: return
        system: SystemStatus = await self.gateway.async_read(SystemStatus, 0)
        units: list[D3netUnit] = []
        for index, connected in enumerate(system.units_connected):
            if connected and not system.units_error[index]:
                cap = await self.gateway.async_read(UnitCapability, index)
                st = await self.gateway.async_read(UnitStatus, index)
                units.append(D3netUnit(self.gateway, index, cap, st))
        self.gateway._units = units

    async def units_json_async(self) -> list[dict[str, Any]]:
        if not self.gateway: return []
        await self.rediscover_units_from_system()
        return self.units_json()

    def units_json(self) -> list[dict[str, Any]]:
        if not self.gateway: return []
        rows=[]
        for u in self.gateway.units or []:
            st, cap = u.status, u.capabilities
            mode = _safe(lambda: st.operating_mode)
            cur = _safe(lambda: st.operating_current)
            fs = _safe(lambda: st.fan_speed)
            fd = _safe(lambda: st.fan_direct)
            rows.append({
                'id': u.unit_id, 'index': u.index,
                'power': _safe(lambda: st.power, False),
                'mode': mode.name if mode else 'UNKNOWN', 'mode_value': mode.value if mode else None,
                'running': cur.name if cur else 'UNKNOWN',
                'fan': _safe(lambda: st.fan, False),
                'fan_speed': fs.name if fs else 'UNKNOWN', 'fan_direction': fd.name if fd else 'UNKNOWN',
                'setpoint': _safe(lambda: st.temp_setpoint), 'current_temperature': _safe(lambda: st.temp_current),
                'filter_warning': _safe(lambda: st.filter_warning, False),
                'capabilities': {
                    'fan': _safe(lambda: cap.fan_mode_capable, False), 'cool': _safe(lambda: cap.cool_mode_capable, False),
                    'heat': _safe(lambda: cap.heat_mode_capable, False), 'auto': _safe(lambda: cap.auto_mode_capable, False),
                    'dry': _safe(lambda: cap.dry_mode_capable, False), 'fan_speed': _safe(lambda: cap.fan_speed_capable, False),
                    'fan_direction': _safe(lambda: cap.fan_direct_capable, False),
                    'cool_min': _safe(lambda: cap.cool_setpoint_lowerlimit), 'cool_max': _safe(lambda: cap.cool_setpoint_upperlimit),
                    'heat_min': _safe(lambda: cap.heat_setpoint_lowerlimit), 'heat_max': _safe(lambda: cap.heat_setpoint_upperlimit),
                },
                'raw': {'capability': list(getattr(cap,'_registers',[])), 'status': list(getattr(st,'_registers',[]))}
            })
        return rows

    def status_json(self) -> dict[str, Any]:
        return {'running': self.running, 'connected': self.connected, 'last_error': self.last_error, 'unit_count': len(self.gateway.units or []) if self.gateway else 0}

    def get_unit_by_id(self, unit_id: str) -> D3netUnit:
        if not self.gateway: raise RuntimeError('Gateway not connected')
        for u in self.gateway.units or []:
            if u.unit_id == unit_id: return u
        raise KeyError(f'Unit {unit_id} not found')

    async def set_power(self, unit_id: str, power: bool) -> None:
        u=self.get_unit_by_id(unit_id); await u.async_write_prepare(); u.status.power=power; await u.async_write_commit(); await u.async_update_status(); await self.sync_all_to_virtual_modbus()
    async def get_setpoint(self, unit_id: str) -> float:
        u = self.get_unit_by_id(unit_id)
        value = _safe(lambda: u.status.temp_setpoint)
        if value is None:
            await u.async_update_status()
            value = _safe(lambda: u.status.temp_setpoint)
        if value is None:
            raise RuntimeError(f'Cannot read current setpoint for {unit_id}')
        return float(value)

    async def set_setpoint(self, unit_id: str, value: float) -> None:
        u=self.get_unit_by_id(unit_id); await u.async_write_prepare(); u.status.temp_setpoint=value; await u.async_write_commit(); await u.async_update_status(); await self.sync_all_to_virtual_modbus()
    async def set_mode_raw(self, unit_id: str, raw_mode: int) -> None:
        u=self.get_unit_by_id(unit_id); await u.async_write_prepare(); u.status.operating_mode=D3netOperationMode(int(raw_mode)); u.status.power=True; await u.async_write_commit(); await u.async_update_status(); await self.sync_all_to_virtual_modbus()
    async def set_mode(self, unit_id: str, mode: str) -> None:
        await self.set_mode_raw(unit_id, D3netOperationMode[mode.upper()].value)
    async def set_fan_speed_raw(self, unit_id: str, raw_speed: int) -> None:
        u=self.get_unit_by_id(unit_id); await u.async_write_prepare(); u.status.fan_speed=D3netFanSpeed(int(raw_speed)); await u.async_write_commit(); await u.async_update_status(); await self.sync_all_to_virtual_modbus()
    async def set_fan_direction(self, unit_id: str, direction: str) -> None:
        u=self.get_unit_by_id(unit_id); await u.async_write_prepare(); u.status.fan_direct=D3netFanDirection[direction]; await u.async_write_commit(); await u.async_update_status(); await self.sync_all_to_virtual_modbus()

    async def sync_all_to_virtual_modbus(self) -> None:
        if not self.virtual_server or not self.gateway: return
        try:
            sys = await self.gateway.async_read(SystemStatus,0)
            for i,v in enumerate(getattr(sys,'_registers',[])[:9]): self.virtual_server.set_input(i,v)
            for u in self.gateway.units or []:
                for i,v in enumerate(getattr(u.capabilities,'_registers',[])[:3]): self.virtual_server.set_input(1000+u.index*3+i, v)
                for i,v in enumerate(getattr(u.status,'_registers',[])[:6]): self.virtual_server.set_input(2000+u.index*6+i, v)
        except Exception: pass
