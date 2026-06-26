from __future__ import annotations
from pathlib import Path
from typing import Any
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from .config import AppConfig, load_config, save_config
from .d3net_service import D3netRuntime
from .knx_service import KnxRuntime, KnxConfig, MonitorRequest
from .auth_service import verify_password, change_password, create_session, get_session_user, delete_session
from .mapping_service import AutoMapRequest, ProjectConfig, build_auto_targets, load_project, register_rows_for_targets, save_project

app = FastAPI(title='DATND Daikin D3net KNX Gateway')
templates = Jinja2Templates(directory=str(Path(__file__).parent / 'templates'))
runtime = D3netRuntime(); knx_runtime = KnxRuntime()

class PowerRequest(BaseModel): power: bool
class ModeRequest(BaseModel): mode: str
class SetpointRequest(BaseModel): setpoint: float
class FanDirectionRequest(BaseModel): direction: str
class D3netKnxLinkRequest(BaseModel): targets: list[dict[str, Any]] = Field(default_factory=list); force: bool = False
class LoginRequest(BaseModel): username: str; password: str
class ChangePasswordRequest(BaseModel): current_password: str; new_password: str; confirm_password: str

@app.on_event('startup')
async def startup_auto_reconnect_knx():
    knx_runtime.set_dpt_mapping_from_targets(load_project().devices)
    import asyncio
    app.state.bridge_task = asyncio.create_task(_background_d3net_knx_sync())
    cfg = load_config()
    if getattr(cfg, 'knx_auto_connect', False):
        knx_cfg = KnxConfig(gateway_name=cfg.knx_gateway_name, gateway_ip=cfg.knx_gateway_ip, gateway_port=cfg.knx_gateway_port, physical_address=cfg.knx_physical_address, protocol=cfg.knx_protocol)
        async def _auto_connect():
            try:
                await knx_runtime.connect(knx_cfg, persistent=True)
            except Exception as exc:
                knx_runtime.last_error = str(exc)
                knx_runtime.add_log('KNX gateway offline', knx_cfg.physical_address, '-', 'StartupReconnect', '', str(exc))
                # keep reconnect loop alive even if first validation/connect fails later due to temporary offline
                try:
                    knx_runtime._desired_connected = True
                    knx_runtime._ensure_reconnect_loop()
                except Exception:
                    pass
        asyncio.create_task(_auto_connect())

@app.on_event('shutdown')
async def shutdown_background_tasks():
    import asyncio
    task = getattr(app.state, 'bridge_task', None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

async def _background_d3net_knx_sync():
    import asyncio
    while True:
        await asyncio.sleep(3)
        try:
            project = load_project()
            if runtime.gateway and project.devices:
                await _sync_targets_to_knx(project.devices, force=False, refresh_units=False)
        except Exception as exc:
            knx_runtime.last_error = str(exc)
            knx_runtime.add_log('Bridge sync error', knx_runtime.config.physical_address, '-', 'D3netLink', '', str(exc))

def _as_knx_bool(value: Any) -> bool:
    if isinstance(value, bool): return value
    if isinstance(value, (int, float)): return int(value) != 0
    txt = str(value).strip().lower()
    if txt in {'1','true','on','yes'}: return True
    if txt in {'0','false','off','no'}: return False
    raise ValueError(f'Invalid KNX boolean value: {value!r}')

@app.post('/api/auth/login')
async def auth_login(body: LoginRequest):
    if not verify_password(body.username, body.password):
        raise HTTPException(status_code=401, detail='Invalid username or password')
    token = create_session(body.username)
    resp = JSONResponse({'ok': True, 'username': body.username})
    resp.set_cookie('datnd_session', token, httponly=True, samesite='lax', max_age=24*60*60)
    return resp

@app.get('/api/auth/status')
async def auth_status(request: Request):
    username = get_session_user(request.cookies.get('datnd_session'))
    return {'authenticated': bool(username), 'username': username or None}

@app.post('/api/auth/change-password')
async def auth_change_password(body: ChangePasswordRequest, request: Request):
    username = get_session_user(request.cookies.get('datnd_session'))
    if not username:
        raise HTTPException(status_code=401, detail='Not logged in')
    if body.new_password != body.confirm_password:
        raise HTTPException(status_code=400, detail='Confirm password does not match')
    ok, msg = change_password(username, body.current_password, body.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {'ok': True, 'message': msg}

@app.post('/api/auth/logout')
async def auth_logout(request: Request):
    delete_session(request.cookies.get('datnd_session'))
    resp = JSONResponse({'ok': True})
    resp.delete_cookie('datnd_session')
    return resp

@app.get('/', response_class=HTMLResponse)
async def index(request: Request): return templates.TemplateResponse('index.html', {'request':request})

@app.get('/api/config')
async def get_config(): return load_config().model_dump()
@app.post('/api/config')
async def post_config(cfg: AppConfig): save_config(cfg); return {'ok':True,'config':cfg.model_dump()}
@app.get('/api/project')
async def get_project():
    project = load_project()
    return {'ok': True, **project.model_dump(), 'register_map': register_rows_for_targets(project.devices)}
@app.post('/api/project')
async def post_project(project: ProjectConfig):
    saved = save_project(project)
    knx_runtime.set_dpt_mapping_from_targets(saved.devices)
    return {'ok': True, **saved.model_dump(), 'register_map': register_rows_for_targets(saved.devices)}
@app.post('/api/project/auto-map')
async def auto_map_project(body: AutoMapRequest):
    project = load_project()
    unit_rows = body.units
    if not unit_rows and runtime.gateway:
        unit_rows = await runtime.units_json_async()
    if not unit_rows:
        raise HTTPException(status_code=400, detail='No indoor units available. Connect / Scan Modbus first or provide units.')
    try:
        project.devices = build_auto_targets(unit_rows, rooms=project.rooms, existing=project.devices, base_address=body.base_address, overwrite_existing=body.overwrite_existing)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    saved = save_project(project)
    knx_runtime.set_dpt_mapping_from_targets(saved.devices)
    return {'ok': True, **saved.model_dump(), 'register_map': register_rows_for_targets(saved.devices)}
@app.get('/api/register-map')
async def get_register_map():
    project = load_project()
    return {'ok': True, 'rows': register_rows_for_targets(project.devices)}
@app.post('/api/start')
async def start(cfg: AppConfig):
    try: save_config(cfg); await runtime.start(cfg); return {'ok':True, **runtime.status_json()}
    except Exception as exc: raise HTTPException(status_code=400, detail=str(exc))
@app.post('/api/stop')
async def stop(): await runtime.stop(); return {'ok':True, **runtime.status_json()}
@app.get('/api/status')
async def status(): return runtime.status_json()
@app.get('/api/units')
async def units():
    try: return await runtime.units_json_async()
    except Exception as exc: raise HTTPException(status_code=500, detail=str(exc))
@app.post('/api/unit/{unit_id}/power')
async def set_power(unit_id: str, body: PowerRequest): await runtime.set_power(unit_id, body.power); return {'ok':True}
@app.post('/api/unit/{unit_id}/mode')
async def set_mode(unit_id: str, body: ModeRequest): await runtime.set_mode(unit_id, body.mode); return {'ok':True}
@app.post('/api/unit/{unit_id}/setpoint')
async def set_setpoint(unit_id: str, body: SetpointRequest): await runtime.set_setpoint(unit_id, body.setpoint); return {'ok':True}
@app.get('/api/debug/system')
async def debug_system():
    if not runtime.gateway: raise HTTPException(400,'Gateway not connected')
    sys = await runtime.gateway.async_read(__import__('app.d3net.encoding',fromlist=['SystemStatus']).SystemStatus,0)
    regs=list(getattr(sys,'_registers',[])); connected=[]
    for i,c in enumerate(sys.units_connected):
        if c: connected.append(f'{int(i/16)+1}-{i%16:02d}')
    return {'raw_30001_30009':regs,'connected_units_from_30002_30005':connected}
@app.get('/api/debug/unit/{unit_id}')
async def debug_unit(unit_id: str):
    u=runtime.get_unit_by_id(unit_id); return {'unit_id':u.unit_id,'index':u.index,'capability_address_zero_based':1000+u.index*3,'status_address_zero_based':2000+u.index*6,'error_address_zero_based':3600+u.index*2,'raw_310xx_capability':list(getattr(u.capabilities,'_registers',[])),'raw_320xx_status':list(getattr(u.status,'_registers',[]))}

@app.get('/api/knx/config')
async def knx_get_config():
    c=load_config(); return {'gateway_name':c.knx_gateway_name,'gateway_ip':c.knx_gateway_ip,'gateway_port':c.knx_gateway_port,'physical_address':c.knx_physical_address,'protocol':c.knx_protocol,'auto_connect':getattr(c,'knx_auto_connect',False)}
@app.post('/api/knx/config')
async def knx_set_config(cfg: KnxConfig):
    c=load_config(); c.knx_gateway_name=cfg.gateway_name; c.knx_gateway_ip=cfg.gateway_ip; c.knx_gateway_port=cfg.gateway_port; c.knx_physical_address=cfg.physical_address; c.knx_protocol=cfg.protocol; save_config(c); knx_runtime.set_config(cfg); return {'ok':True,'config':cfg.model_dump()}
@app.post('/api/knx/connect')
async def knx_connect(cfg: KnxConfig|None=None):
    try:
        if cfg is None:
            data=await knx_get_config(); cfg=KnxConfig(**{k:v for k,v in data.items() if k in {'gateway_name','gateway_ip','gateway_port','physical_address','protocol'}})
        await knx_set_config(cfg)
        c=load_config(); c.knx_auto_connect=True; save_config(c)
        await knx_runtime.connect(cfg, persistent=True); return {'ok':True, **knx_runtime.status_json()}
    except Exception as exc: raise HTTPException(status_code=400, detail=str(exc))
@app.post('/api/knx/disconnect')
async def knx_disconnect():
    c=load_config(); c.knx_auto_connect=False; save_config(c)
    await knx_runtime.disconnect(); return {'ok':True, **knx_runtime.status_json()}
@app.get('/api/knx/status')
async def knx_status(): return knx_runtime.status_json()
@app.post('/api/knx/monitor')
async def knx_monitor(body: MonitorRequest): knx_runtime.set_monitor(body.enabled); return {'ok':True, **knx_runtime.status_json()}
@app.get('/api/knx/logs')
async def knx_logs(limit:int=100, ga_filter:str|None=None): return knx_runtime.logs_json(limit, ga_filter)
@app.post('/api/knx/logs/clear')
async def knx_clear_logs(): knx_runtime.clear_logs(); return {'ok':True}

def _decode_dta_signed_x10(value:int)->float:
    value=int(value)&0xFFFF; sign=-1 if value&0x8000 else 1; return sign*(value&0x7FFF)/10.0

def _fan_raw_to_knx(value:int)->int|None:
    return {0:0,1:85,2:85,3:170,4:255,5:255}.get(int(value))

def _find_status(unit_rows, indoor):
    for u in unit_rows:
        if u.get('id')==indoor:
            st=(u.get('raw') or {}).get('status') or []
            if len(st)>=6: return [int(x) for x in st[:6]]
    return None

def _section(t, name):
    return ((t.get('mapping') or {}).get(name) or {})

@app.post('/api/knx/d3net-link/sync')
async def knx_d3net_sync(body: D3netKnxLinkRequest):
    targets = body.targets or load_project().devices
    return await _sync_targets_to_knx(targets, force=body.force, refresh_units=True)

async def _sync_targets_to_knx(targets: list[dict[str, Any]], force: bool = False, refresh_units: bool = True):
    knx_runtime.set_dpt_mapping_from_targets(targets)
    rows=await runtime.units_json_async() if runtime.gateway and refresh_units else runtime.units_json() if runtime.gateway else []
    mode_map={0:9,1:1,2:3,3:0,7:14}
    published=[]; skipped=[]
    for t in targets:
        indoor=str(t.get('indoor') or '').strip(); regs=_find_status(rows, indoor)
        if not indoor or regs is None:
            skipped.append({'target':t.get('target'),'indoor':indoor,'reason':'Indoor status unavailable'})
            continue
        r32001,r32002,r32003,_,r32005,_=regs
        vals=[]; sw=_section(t,'switch'); sp=_section(t,'setpoint'); amb=_section(t,'ambient'); mode=_section(t,'mode'); fan=_section(t,'fan')
        if sw.get('status'): vals.append(('On/off Status', sw['status'], 1 if r32001&1 else 0, '1.001'))
        if sp.get('status'): vals.append(('Setpoint Status', sp['status'], _decode_dta_signed_x10(r32003), '9.001'))
        if amb.get('status'): vals.append(('Status Ambient', amb['status'], _decode_dta_signed_x10(r32005), '9.001'))
        mr=r32002&0xF
        if mode.get('status') and mr in mode_map: vals.append(('Mode Status', mode['status'], mode_map[mr], '20.105'))
        fr=(r32001>>12)&7
        fv=_fan_raw_to_knx(fr)
        if fan.get('status') and fv is not None: vals.append(('Fan Status', fan['status'], fv, '5.001'))
        for field,ga,val,dpt in vals:
            if knx_runtime.publish_group_value(str(ga),val,dpt,force=force): published.append({'target':t.get('target'),'indoor':indoor,'field':field,'ga':ga,'dpt':dpt,'value':val})
    return {'ok':True,'published_count':len(published),'published':published,'skipped':skipped}

async def _handle_knx_control_event(event: dict[str,Any]):
    indoor=str(event.get('indoor') or '').strip(); field=str(event.get('field') or ''); val=event.get('value'); ga=event.get('ga',''); dpt=event.get('dpt','')
    try:
        if field=='On/off Control': await runtime.set_power(indoor, _as_knx_bool(val))
        elif field=='Setpoint Control': await runtime.set_setpoint(indoor, float(val))
        elif field=='Setpoint Control UpDown':
            current = await runtime.get_setpoint(indoor)
            step = 1 if _as_knx_bool(val) else -1
            new_value = current + step
            try:
                min_v = float(event.get('min')) if event.get('min') not in (None, '') else None
                max_v = float(event.get('max')) if event.get('max') not in (None, '') else None
                if min_v is not None: new_value = max(min_v, new_value)
                if max_v is not None: new_value = min(max_v, new_value)
            except Exception:
                pass
            await runtime.set_setpoint(indoor, new_value)
            target_ga = (event.get('setpoint_control_ga') or '').strip()
            if target_ga:
                knx_runtime.publish_group_value(target_ga, new_value, '9.001', force=True)
            val = f'{current} -> {new_value}'
        elif field=='Mode Control':
            mp={9:0,1:1,3:2,0:3,14:7}; v=int(round(float(val))); await runtime.set_mode_raw(indoor, mp[v])
        elif field=='Fan Control':
            v=int(round(float(val))); raw=0 if v<=42 else 1 if v<=127 else 3 if v<=212 else 5; await runtime.set_fan_speed_raw(indoor, raw)
        elif field=='Fan Control Step':
            current_raw, new_raw = await runtime.step_fan_speed_raw(indoor, _as_knx_bool(val))
            target_ga = (event.get('fan_control_ga') or '').strip()
            fan_value = _fan_raw_to_knx(new_raw)
            if target_ga and fan_value is not None:
                knx_runtime.publish_group_value(target_ga, fan_value, '5.001', force=True)
            val = f'{current_raw} -> {new_raw}'
        else: return
        knx_runtime.add_log('KNX -> D3net', event.get('source',''), ga, 'GroupValueWrite', dpt, f'{indoor} {field}: {val}')
    except Exception as exc:
        knx_runtime.last_error=str(exc); knx_runtime.add_log('D3net write error', event.get('source',''), ga, 'GroupValueWrite', dpt, str(exc))
knx_runtime.set_control_callback(_handle_knx_control_event)
