from __future__ import annotations
import asyncio
from typing import Any

try:
    from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext
    try:
        from pymodbus.datastore import ModbusSlaveContext as _SlaveContext
    except Exception:
        from pymodbus.datastore import ModbusDeviceContext as _SlaveContext
    from pymodbus.server import StartAsyncTcpServer
except Exception:  # dependency absent during local lint
    ModbusSequentialDataBlock = None
    ModbusServerContext = None
    _SlaveContext = None
    StartAsyncTcpServer = None

class VirtualModbusServer:
    def __init__(self, port: int = 1502):
        self.port = port
        self.task: asyncio.Task | None = None
        self.context: Any | None = None

    async def start(self) -> None:
        if self.task or StartAsyncTcpServer is None:
            return
        store = _SlaveContext(
            di=ModbusSequentialDataBlock(0, [0]*10000),
            co=ModbusSequentialDataBlock(0, [0]*10000),
            hr=ModbusSequentialDataBlock(0, [0]*10000),
            ir=ModbusSequentialDataBlock(0, [0]*10000),
        )
        self.context = ModbusServerContext(slaves=store, single=True)
        self.task = asyncio.create_task(StartAsyncTcpServer(context=self.context, address=("0.0.0.0", self.port)))

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except Exception:
                pass
            self.task = None

    def set_input(self, address: int, value: int) -> None:
        if self.context:
            self.context[0].setValues(4, address, [int(value) & 0xFFFF])

    def set_holding(self, address: int, value: int) -> None:
        if self.context:
            self.context[0].setValues(3, address, [int(value) & 0xFFFF])
