# DATND Daikin DIII-Net / KNX UI v24

Modernized UI while keeping the existing functions and menu structure:

- Overview Dashboard
- Modbus Gateway Config
- KNX Gateway Config
- Indoor Mapping Address
- Area / Room
- Register Map
- Logs

Run:

```bash
docker compose down
docker compose build --no-cache
docker compose up -d
```

Open: `http://SERVER_IP:8080`

Default login: `admin / admin`

## v26
- Fixed login button issue caused by DOM id/function name collision (`login`).

## v27
- Replaced top-right D logo with user icon menu.
- Added Admin user dropdown with Change Password and Logout.
- Added backend password storage in /app/data/users.json using PBKDF2-SHA256.
- Updated Dockerfile pip install step to use Aliyun mirror with cache mount.


## v28
- Added ACTempSetpoint -> Setpoint Control UpDown KNX address.
- DPT: 1.007. Value 1 increases setpoint by 1°C, value 0 decreases setpoint by 1°C.
- The server reads the current D3net setpoint, writes the adjusted value to D3net holding register 42003, and also publishes the new value to the configured KNX Setpoint Control GA (DPT 9.001) for consistency.

## v29
- Fixed DPT 1.xxx incoming telegram decode for Setpoint Control UpDown.
- Fixed KNX -> D3net UpDown handler so values 0/1 no longer become an empty string.

## v30
- Validate KNX Gateway IP/port before connect.
- KNX status reports Online only when xknx has a real tunnel connection.
- Persist KNX auto-connect setting after Connect.
- Auto reconnect after Docker/container restart when previously connected.
- Keep reconnect loop active and report gateway offline when KNX/IP gateway is unreachable; reconnects automatically when gateway returns.

## v31
- Added persistent project mapping in `/app/data/project.json` for rooms, indoor targets, and KNX group addresses.
- Added auto-create KNX group addresses from a base GA, using 10 addresses per DIII indoor unit.
- Added dynamic DTA116A register map per indoor target.
- Backend now keeps the D3net -> KNX status bridge running in the background after Modbus scan, without requiring the browser UI to stay open.
