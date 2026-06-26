from __future__ import annotations
import asyncio, re, ipaddress
from collections import deque
from datetime import datetime
from typing import Any, Awaitable, Callable
from pydantic import BaseModel, Field

try:
    from xknx import XKNX
    from xknx.io import ConnectionConfig, ConnectionType
    from xknx.telegram import Telegram
    from xknx.telegram.address import GroupAddress, IndividualAddress
    from xknx.telegram.apci import GroupValueWrite
    from xknx.dpt import DPTBinary, DPT2ByteFloat, DPTValue1ByteUnsigned, DPTArray
except Exception:
    XKNX = None

class KnxConfig(BaseModel):
    gateway_name: str = 'KNX Main Gateway'
    gateway_ip: str = '192.168.1.10'
    gateway_port: int = Field(default=3671, ge=1, le=65535)
    physical_address: str = '1.0.100'
    protocol: str = 'TunnelUDP'
class MonitorRequest(BaseModel): enabled: bool

def _boolish(v: Any) -> bool:
    if isinstance(v,bool): return v
    if isinstance(v,(int,float)): return v != 0
    return str(v).lower() in {'1','true','on','yes'}

class KnxRuntime:
    def __init__(self) -> None:
        self.config = KnxConfig(); self.connected=False; self.monitor_enabled=False; self.last_error=None
        self.logs: deque[dict[str,Any]] = deque(maxlen=500); self._xknx=None; self._task=None; self._reconnect_task=None; self._desired_connected=False; self._last_published={}
        self.ga_dpt_map: dict[str,str] = {}; self.ga_control_map: dict[str,dict[str,Any]] = {}; self._control_callback=None

    def add_log(self, service, source, destination, typ, dpt, value):
        self.logs.appendleft({'time': datetime.now().strftime('%d-%b-%y %H:%M:%S.%f')[:-3], 'service':service, 'flags':'', 'prio':'Low', 'source_address':source, 'source_name':'', 'destination_address':destination, 'destination_route':'6' if destination!='-' else '', 'type':typ, 'dpt':dpt, 'value':value})
    def logs_json(self, limit=100, ga_filter=None):
        rows=list(self.logs)[:max(1,min(limit,500))]
        if ga_filter:
            rows=[r for r in rows if str(r.get('destination_address','')).startswith(ga_filter[:-1] if ga_filter.endswith('*') else ga_filter)]
        return rows
    def clear_logs(self): self.logs.clear()
    def set_monitor(self, enabled: bool): self.monitor_enabled=enabled; self.add_log('local',self.config.physical_address,'-','Monitor','', 'ON' if enabled else 'OFF')
    def set_config(self, cfg: KnxConfig): self.config=cfg; self.add_log('local',cfg.physical_address,'-','GatewayConfig','',f'Saved {cfg.gateway_name}')
    def _real_connected(self) -> bool:
        try:
            return bool(self._xknx is not None and self._xknx.connection_manager.connected)
        except Exception:
            return False

    def status_json(self):
        self.connected = self._real_connected()
        return {'connected':self.connected,'gateway_online':self.connected,'monitor_enabled':self.monitor_enabled,'last_error':self.last_error,'real_knx_enabled':self._xknx is not None,'xknx_installed':XKNX is not None,'xknx_task_running':bool(self._task and not self._task.done()),'auto_reconnect_enabled':self._desired_connected,'reconnect_task_running':bool(self._reconnect_task and not self._reconnect_task.done()),'config':self.config.model_dump(),'log_count':len(self.logs),'dpt_map_count':len(self.ga_dpt_map),'dpt_map':self.ga_dpt_map,'control_map_count':len(self.ga_control_map),'control_map':self.ga_control_map}

    def _validate_gateway_config(self, cfg: KnxConfig) -> None:
        try:
            ipaddress.ip_address(str(cfg.gateway_ip).strip())
        except Exception as exc:
            raise ValueError(f"Invalid KNX Gateway IP: {cfg.gateway_ip}") from exc
        if not (1 <= int(cfg.gateway_port) <= 65535):
            raise ValueError(f"Invalid KNX Gateway port: {cfg.gateway_port}")
        if cfg.protocol not in {'TunnelUDP','TunnelTCP','Multicast'}:
            raise ValueError(f"Invalid KNX protocol: {cfg.protocol}")

    def _conn_type(self):
        if self.config.protocol == 'TunnelTCP': return ConnectionType.TUNNELING_TCP
        if self.config.protocol == 'Multicast': return ConnectionType.ROUTING
        return ConnectionType.TUNNELING

    async def connect(self, cfg: KnxConfig|None=None, *, persistent: bool=True):
        if cfg: self.config=cfg
        self._validate_gateway_config(self.config)
        self._desired_connected = persistent
        await self._connect_once(wait_timeout=5.0)
        self._ensure_reconnect_loop()

    async def _connect_once(self, wait_timeout: float=5.0):
        await self._stop_xknx(silent=True)
        if XKNX is None:
            self.connected=False; self.last_error='xknx not installed'; raise RuntimeError(self.last_error)
        cc=ConnectionConfig(connection_type=self._conn_type(), gateway_ip=self.config.gateway_ip if self.config.protocol!='Multicast' else None, gateway_port=self.config.gateway_port, individual_address=self.config.physical_address, multicast_port=self.config.gateway_port, auto_reconnect=True)
        self._xknx=XKNX(connection_config=cc, telegram_received_cb=self._telegram_received, daemon_mode=True)
        self._task=asyncio.create_task(self._run())
        deadline=asyncio.get_running_loop().time()+wait_timeout
        while asyncio.get_running_loop().time()<deadline:
            if self._real_connected():
                self.connected=True; self.last_error=None; self.add_log('local',self.config.physical_address,'-','Connect','',f'KNX Gateway online {self.config.gateway_ip}:{self.config.gateway_port}')
                return
            if self._task.done():
                exc=self._task.exception()
                if exc:
                    self.connected=False; self.last_error=str(exc); self.add_log('KNX gateway offline',self.config.physical_address,'-','Connect','',self.last_error); return
            await asyncio.sleep(.2)
        self.connected=False; self.last_error=f'KNX Gateway offline or no tunnel response: {self.config.gateway_ip}:{self.config.gateway_port}'
        self.add_log('KNX gateway offline',self.config.physical_address,'-','Connect','',self.last_error)

    async def _run(self):
        try:
            await self._xknx.start()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.connected=False; self.last_error=str(exc); self.add_log('KNX gateway offline',self.config.physical_address,'-','Connection','',str(exc))

    def _ensure_reconnect_loop(self):
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task=asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self):
        while self._desired_connected:
            try:
                if not self._real_connected():
                    self.connected=False
                    await self._connect_once(wait_timeout=5.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected=False; self.last_error=str(exc); self.add_log('KNX gateway offline',self.config.physical_address,'-','Reconnect','',str(exc))
            await asyncio.sleep(10)

    async def _stop_xknx(self, silent=False):
        if self._xknx:
            try: await self._xknx.stop()
            except Exception: pass
        self._xknx=None
        if self._task and not self._task.done():
            self._task.cancel()
            try: await self._task
            except Exception: pass
        self._task=None; self.connected=False
        if not silent: self.add_log('local',self.config.physical_address,'-','Disconnect','','Disconnected')

    async def disconnect(self, silent=False):
        self._desired_connected=False
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try: await self._reconnect_task
            except Exception: pass
        self._reconnect_task=None
        await self._stop_xknx(silent=silent)

    def _extract_payload_bytes(self, payload: Any) -> list[int]:
        s=str(payload)
        return [int(x,16) for x in re.findall(r'0x([0-9a-fA-F]+)', s)]

    def _extract_dpt1_value(self, payload: Any) -> int | None:
        """Decode 1-bit KNX values from xknx payload objects or their string form.

        DPT 1.001/1.007 telegrams are often represented by xknx as DPTBinary
        without hex bytes, so the old hex-only parser returned an empty string.
        """
        try:
            v = getattr(payload, 'value', None)
            if hasattr(v, 'value'):
                vv = getattr(v, 'value')
                if isinstance(vv, bool): return 1 if vv else 0
                if isinstance(vv, (int, float)): return 1 if int(vv) else 0
                txt = str(vv).strip().lower()
                if txt in {'1','true','on','yes'}: return 1
                if txt in {'0','false','off','no'}: return 0
        except Exception:
            pass
        s = str(payload).strip().lower()
        m = re.search(r'dptbinary[^>]*value=\"([^\"]+)\"', s) or re.search(r'value=\"(true|false|0|1)\"', s)
        if m:
            txt = m.group(1).strip().lower()
            if txt in {'1','true','on','yes'}: return 1
            if txt in {'0','false','off','no'}: return 0
        return None

    def _decode_dpt9(self, data):
        raw=((data[0]&255)<<8)|(data[1]&255); sign=-1 if raw&0x8000 else 1; exp=(raw>>11)&0x0F; mant=raw&0x07FF; return round(sign*.01*mant*(2**exp),2)
    def _decode_value(self, dpt, data, payload=None):
        if dpt.startswith('1.'):
            direct = self._extract_dpt1_value(payload) if payload is not None else None
            if direct is not None: return direct
            if data: return 1 if data[-1]&1 else 0
            return ''
        if not data: return ''
        if dpt.startswith('9.') and len(data)>=2: return self._decode_dpt9(data[-2:])
        if dpt.startswith('5.') or dpt.startswith('20.'): return data[-1]&255
        return '['+','.join(f'0x{x:02X}' for x in data)+']'
    def _decode_bus_value(self, dest, payload):
        data=self._extract_payload_bytes(payload); dpt=self.ga_dpt_map.get(str(dest).strip(),'raw'); return dpt, self._decode_value(dpt,data,payload)
    def _telegram_received(self, telegram):
        try:
            src=str(getattr(telegram,'source_address','')); dest=str(getattr(telegram,'destination_address','')); payload=getattr(telegram,'payload',None); typ=type(payload).__name__ if payload else 'Telegram'; dpt,val=self._decode_bus_value(dest,payload)
            if self.monitor_enabled: self.add_log('from bus',src,dest,typ,dpt,val)
            control=self.ga_control_map.get(dest)
            if control and typ=='GroupValueWrite' and self._control_callback and src != str(self.config.physical_address):
                event=dict(control); event.update({'source':src,'value':val,'raw_type':typ}); asyncio.create_task(self._control_callback(event))
        except Exception as exc: self.add_log('KNX monitor error','', '-', 'Decode','',str(exc))
    def set_control_callback(self, cb): self._control_callback=cb
    def set_dpt_mapping_from_targets(self, targets: list[dict[str,Any]]):
        dpt_map={}; control={}
        def add(ga,dpt):
            if ga: dpt_map[str(ga).strip()]=dpt
        def addc(ga,dpt,target,field,extra=None):
            if ga:
                g=str(ga).strip(); dpt_map[g]=dpt; control[g]={'target':target.get('target',''),'indoor':target.get('indoor',''),'field':field,'ga':g,'dpt':dpt}
                if extra: control[g].update(extra)
        for t in targets or []:
            m=t.get('mapping') or {}; sw=m.get('switch') or {}; sp=m.get('setpoint') or {}; amb=m.get('ambient') or {}; mode=m.get('mode') or {}; fan=m.get('fan') or {}
            addc(sw.get('control'),'1.001',t,'On/off Control'); add(sw.get('status'),'1.001')
            addc(sp.get('control'),'9.001',t,'Setpoint Control'); add(sp.get('status'),'9.001'); add(amb.get('status'),'9.001')
            addc(sp.get('updown'),'1.007',t,'Setpoint Control UpDown', {'setpoint_control_ga': sp.get('control'), 'min': sp.get('min'), 'max': sp.get('max')})
            addc(mode.get('control'),'20.105',t,'Mode Control'); add(mode.get('status'),'20.105')
            addc(fan.get('control'),'5.001',t,'Fan Control'); add(fan.get('status'),'5.001')
        self.ga_dpt_map.update(dpt_map); self.ga_control_map=control
    def _validate_group_address(self, address: str) -> str:
        m=re.fullmatch(r'(\d{1,2})/(\d{1,2})/(\d{1,3})', (address or '').strip())
        if not m: raise ValueError(f'Invalid KNX group address {address}')
        main,mid,sub=map(int,m.groups())
        if not (0<=main<=31 and 0<=mid<=7 and 0<=sub<=255): raise ValueError(f'Invalid KNX group address {address}: 3-level range is main 0..31 / middle 0..7 / sub 0..255')
        return address
    def _payload_for_dpt(self,dpt,value):
        if dpt.startswith('1.'): return DPTBinary(1 if _boolish(value) else 0)
        if dpt.startswith('9.'): return DPT2ByteFloat.to_knx(float(value))
        if dpt.startswith('5.') or dpt.startswith('20.'): return DPTValue1ByteUnsigned.to_knx(int(round(float(value))))
        return DPTArray([int(value)&255])
    async def _send_group_value(self,dest,value,dpt):
        if not self.connected or not self._xknx: raise RuntimeError('KNX is not connected')
        dest=self._validate_group_address(dest); payload=self._payload_for_dpt(dpt,value)
        telegram=Telegram(destination_address=GroupAddress(dest), payload=GroupValueWrite(payload), source_address=IndividualAddress(self.config.physical_address))
        await self._xknx.telegrams.put(telegram)
    def publish_group_value(self,dest,value,dpt,source=None,force=False,label='D3netLink'):
        dest=(dest or '').strip()
        if not dest: return False
        try: dest=self._validate_group_address(dest)
        except Exception as exc: self.last_error=str(exc); self.add_log('KNX address error',source or self.config.physical_address,dest,'GroupValueWrite',dpt,str(exc)); return False
        if not self._real_connected():
            self.connected=False; self.last_error='KNX is not connected'
            self.add_log('KNX error',source or self.config.physical_address,dest,'GroupValueWrite',dpt,self.last_error)
            return False
        key=f'{dest}|{dpt}'
        if not force and self._last_published.get(key)==value: return False
        self._last_published[key]=value; self.add_log('D3net -> KNX',source or self.config.physical_address,dest,'GroupValueWrite',dpt,value)
        asyncio.create_task(self._send_guarded(dest,value,dpt)); return True
    async def _send_guarded(self,dest,value,dpt):
        try: await self._send_group_value(dest,value,dpt); self.add_log('to bus',self.config.physical_address,dest,'GroupValueWrite',dpt,value)
        except Exception as exc: self.last_error=str(exc); self.add_log('KNX error',self.config.physical_address,dest,'GroupValueWrite',dpt,str(exc))
