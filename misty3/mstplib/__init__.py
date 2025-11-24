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

import argparse
from typing import Any, Callable
from bacpypes3.debugging import ModuleLogger
from bacpypes3.debugging import bacpypes_debugging
from bacpypes3.argparse import SimpleArgumentParser

# some debugging
_debug = 0
_log = ModuleLogger(globals())

@bacpypes_debugging
class MSTPArgumentParser(SimpleArgumentParser):

    """
    MSTPArgumentParser extends SimpleArgumentParser with MSTP-specific options.
    """

    _debug: Callable[..., None]

    def __init__(self, **kwargs: Any) -> None:
        if _debug:
            MSTPArgumentParser._debug("__init__ %r", kwargs)

        # let the base class add the standard BACpypes options
        super().__init__(**kwargs)

        # --- MSTP options ---

        # required: serial interface, e.g. /dev/ttyS0 or /dev/ttyUSB0
        self.add_argument(
            "--interface",
            type=str,
            required=True,
            help="MSTP serial interface (e.g. /dev/ttyS0, /dev/ttyUSB0)",
        )

        # required: serial interface, e.g. /dev/ttyS0 or /dev/ttyUSB0
        self.add_argument(
            "--mstpaddress",
            type=int,
            required=True,
            help="MSTP mac address (e.g. 25)",
        )

        # directory for MSTP runtime files (token logs, traces, etc.)
        self.add_argument(
            "--mstpdir",
            type=str,
            default="/var/tmp",
            help="directory for MSTP runtime files (default: /var/tmp)",
        )

        # Max-Masters (1..127, default=127)
        self.add_argument(
            "--maxmasters",
            type=int,
            default=127,
            help="MSTP Max-Masters value (1–127, default: 127)",
        )

        # baud rate, default 38400
        self.add_argument(
            "--baudrate",
            type=int,
            default=38400,
            help="MSTP serial baud rate (default: 38400)",
        )

        # Max_Info_Frames, default 1
        self.add_argument(
            "--maxinfo",
            type=int,
            default=1,
            help="MSTP Max-Info-Frames (default: 1)",
        )

    def expand_args(self, result_args: argparse.Namespace) -> None:
        if _debug:
            MSTPArgumentParser._debug("expand_args %r", result_args)

        # --- basic validation for MSTP bits ---

        # Max-Masters must be in 1..127
        if not (1 <= result_args.maxmasters < 128):
            raise ValueError("max_masters must be between 1 and 127")

        # baud_rate must be positive
        if result_args.baudrate <= 0:
            raise ValueError("baud_rate must be a positive integer")

        if result_args.maxinfo <= 0:
            raise ValueError("maxinfo must be a positive integer")

        if not result_args.interface:
            # should be guaranteed by required=True, but double check
            raise ValueError("interface is required")

        if not os.path.exists(result_args.interface):
            raise ValueError(f"interface path does not exist: {result_args.interface}")

        result_args.interface_name = os.path.basename(
            result_args.interface.rstrip("/")
        )

        if not result_args.mstpaddress:
            # should be guaranteed by required=True, but double check
            raise ValueError("address is required")

        if result_args.mstpaddress <= 0 or result_args.mstpaddress >= 128:
            raise ValueError("mstpaddress must be between 1 to 127")

        # --- validate mstpdir ---
        if not os.path.exists(result_args.mstpdir):
            raise ValueError(f"mstpdir does not exist: {result_args.mstpdir}")

        if not os.path.isdir(result_args.mstpdir):
            raise ValueError(f"mstpdir is not a directory: {result_args.mstpdir}")

        # writable check (permission + effective uid)
        if not os.access(result_args.mstpdir, os.W_OK):
            raise ValueError(f"mstpdir is not writable: {result_args.mstpdir}")

        # now let the base class do its checks and env expansion
        super().expand_args(result_args)


class MSTPApplication(Application):
    _mstp_global_inited = False

    @classmethod
    def from_args(cls, args):
        app = super().from_args(args)
        app._post_init_mstp(args)
        return app

    def _post_init_mstp(self, args):

        cls = type(self)
        if not cls._mstp_global_inited:
            self._global_mstp_init(args)
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

        # print("OUT:", raw.hex(sep=' '))

        # call application MSTP TX
        await self._app.send_mstp(raw, npdu)

        # forward to original
        # return await self._app._orig_out(npdu)

    async def _inbound_hook(self, pdu):
        raw = bytes(pdu.pduData)

        # print("IN:", raw.hex(sep=' '))

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

            # print(f"TX MSTP: dst_mac={dst_mac}, bytes={payload.hex(' ')}")

            sent = self.socket.send(payload)
            # print("SENT", sent)

        except Exception as e:
            print("MSTP send error:", e)

    def _global_mstp_init(self, args):
        """
        Called once per process, before any adapter is hooked.
        """

        '''
        args Namespace(loggers=False, debug=None, 
        color=None, route_aware=None, name='Excelsior', 
        instance=999, network=0, address=None, vendoridentifier=999, 
        foreign=None, ttl=30, bbmd=None, interface='/var/tmp/ttyp0', 
        mstpaddress=25, mstpdir='/var/tmp', maxmasters=127, 
        baudrate=38400, maxinfo=1, low_limit=1, high_limit=6000)
        '''

        interface_devname = args.interface
        interface_filename = os.path.basename(interface_devname)

        # interface_filename='ttyp0'
        # interface_devname='/var/tmp/ttyp0'

        # mstp_dir = '/var/tmp'
        mstp_dir = args.mstpdir
        mstp_dir = tempfile.mkdtemp(prefix="ma_",dir=mstp_dir)

        my_addr = '{}/mstp{}'.format(mstp_dir, interface_filename)
        try:
            os.remove(my_addr)
        except:
            pass

        # Call the library to init the mstp_agent
        # dirname="/Users/rags/misty/misty/mstplib"

        dirname=os.path.dirname(__file__)
        libname = "libmstp_agent.so"
        libmstp_path=os.path.join(dirname, libname)
        mstp_lib = ctypes.cdll.LoadLibrary(libmstp_path)

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

        self.address = args.mstpaddress
        mac = self.address
        max_masters = args.maxmasters
        baud_rate = args.baudrate
        maxinfo = args.maxinfo

        buf = struct.pack('iiii', mac, max_masters, baud_rate, maxinfo);

        print(
            f"mstpaddress={self.address} max_masters={max_masters} "
            f"baud_rate={baud_rate} maxinfo={maxinfo} "
            f"mstpdir={mstp_dir} interface={interface_devname}"
        )

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

        # print(f"MSTP parsed src_mac={src_mac}, npdu={raw_npdu.hex(' ')}")

        # 1) Create an empty PDU
        pdu = PDU()

        # 2) Fill in raw data and source
        pdu.pduData = bytearray(raw_npdu)   # IMPORTANT: bytearray, not bytes
        pdu.pduSource = Address(src_mac)    # same style as old MSTPDirector

        # 3) Inject into the original inbound path (same as UDP)
        await self._orig_in(pdu)
