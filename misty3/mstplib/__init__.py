"""
"""
import sys
import asyncio
import tempfile
import os
import ctypes
import socket
import struct
import time
from bacpypes3.netservice import PDU
from bacpypes3.pdu import Address
from bacpypes3.app import Application

class MSTPApplication(Application):
    _mstp_global_inited = False
    _mstp_lib = None

    @classmethod
    def from_args(cls, args):
        print(args)
        app = super().from_args(args)
        app._post_init_mstp()
        return app

    def _post_init_mstp(self):

        cls = type(self)
        if not cls._mstp_global_inited:
            self._global_mstp_init()
            cls._mstp_global_inited = True


        # the adapter being patched
        self._adapter = self.nsap.adapters[None]

        # save reference so adapter can call back into us
        self._adapter._app = self

        # save original methods
        self._orig_out = self._adapter.process_npdu
        self._orig_in = self._adapter.confirmation

        # patch them
        self._adapter.process_npdu = self._outbound_hook.__get__(self._adapter)
        self._adapter.confirmation = self._inbound_hook.__get__(self._adapter)

    async def _outbound_hook(self, npdu):
        # self = adapter, but application available via self._app
        pdu = npdu.encode()
        raw = bytes(pdu.pduData)

        print("OUT:", raw.hex(sep=' '))

        # call application MSTP TX
        await self._app.send_mstp(raw, npdu)

        # forward to original
        # return await self._app._orig_out(npdu)

    async def _inbound_hook(self, pdu):
        raw = bytes(pdu.pduData)

        print("IN:", raw.hex(sep=' '))

        # forward to the application for possible processing later
        return await self._app._orig_in(pdu)

    async def send_mstp(self, raw: bytes, npdu):
        try:
            dest = getattr(npdu, "pduDestination", None)
            dst_mac = 255  # default = broadcast

            if isinstance(dest, Address):
                # unicast case
                if dest.addrType == Address.localStationAddr and dest.addrAddr:
                    # addrAddr is a tuple/list, first element = MAC
                    dst_mac = dest.addrAddr[0]

                # broadcast case (local broadcast)
                elif dest.addrType == Address.localBroadcastAddr:
                    dst_mac = 255  # already default

            # build MSS/TP payload: [destination MAC] + NPDU bytes
            payload = bytes([dst_mac]) + raw

            print(f"TX MSTP: dst_mac={dst_mac}, bytes={payload.hex(' ')}")

            sent = self.socket.send(payload)
            print("SENT", sent)

        except Exception as e:
            print("MSTP send error:", e)

    def _global_mstp_init(self):
        """
        Called once per process, before any adapter is hooked.
        """
        print("One time INIT")

        interface_filename='ttyp0'
        interface_devname='/var/tmp/ttyp0'

        mstp_dir = '/var/tmp'
        mstp_dir = tempfile.mkdtemp(prefix="ma_",dir=mstp_dir)

        my_addr = '{}/mstp{}'.format(mstp_dir, interface_filename)
        try:
            os.remove(my_addr)
        except:
            pass

        # Call the library to init the mstp_agent
        dirname="/Users/rags/misty/misty/mstplib"
        libname = "libmstp_agent.so"
        libmstp_path=os.path.join(dirname, libname)
        mstp_lib = ctypes.cdll.LoadLibrary(libmstp_path)
        MSTPApplication.mstp_lib = mstp_lib

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.setblocking(False)

        self.socket = sock
        self._fileno = sock.fileno()

        # try to re-use a server port if possible
        try:
            self.socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_REUSEADDR,
                self.socket.getsockopt(socket.SOL_SOCKET,
                                       socket.SO_REUSEADDR) | 1
                )
        except OSError:
            pass
        

        self.addr = my_addr
        self.socket.bind(my_addr)

        # allow it to send broadcasts
        self.socket.setsockopt( socket.SOL_SOCKET, socket.SO_BROADCAST, 1 )

        self.address = 25
        mac = 25
        max_masters = 127
        baud_rate = 38400
        maxinfo = 1
        buf = struct.pack('iiii', mac, max_masters, baud_rate, maxinfo);

        mstp_lib.init(buf, interface_devname.encode(), mstp_dir.encode())

        # to ensure that the server is ready
        time.sleep(0.5)

        # server to send the MSTP PDU's
        self.server_address = '{}/mstp_server'.format(mstp_dir)
        self.socket.connect(self.server_address)

        # Safe: register a callback, don’t spin your own loop
        loop = asyncio.get_running_loop()
        loop.add_reader(self.socket.fileno(), self._mstp_rx_ready)

    def _mstp_rx_ready(self):
        while True:
            try:
                data = self.socket.recv(2048)
            except BlockingIOError:
                break  # queue drained → return to event loop

            if not data:
                continue

            asyncio.create_task(self._handle_mstp_frame(data))

    async def _handle_mstp_frame(self, data: bytes) -> None:
        """
        data format from libmstp_agent: [src_mac] + NPDU bytes
        """
        if len(data) < 2:
            return

        src_mac = data[0]
        raw_npdu = data[1:]

        print(f"MSTP parsed src_mac={src_mac}, npdu={raw_npdu.hex(' ')}")

        # 1) Create an empty PDU
        pdu = PDU()

        # 2) Fill in raw data and source
        pdu.pduData = bytearray(raw_npdu)   # IMPORTANT: bytearray, not bytes
        pdu.pduSource = Address(src_mac)    # same style as old MSTPDirector

        # 3) Inject into the original inbound path (same as UDP)
        await self._orig_in(pdu)
