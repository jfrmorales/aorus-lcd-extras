"""The AP->IAP step against a fake bus that behaves like the panel did."""
import pytest

from aorus_lcd import firmware
from aorus_lcd_fw_update import update

ACK = bytes.fromhex("015afea5")


class FakePanel:
    """Application at 0x61 until it receives FRAME *and* is read once; then
    only the bootloader at 0x22 answers (ACK to its status polls)."""

    def __init__(self, needs_read=True, reboots=True):
        self.mode = "app"
        self.pending = False
        self.needs_read, self.reboots = needs_read, reboots
        self.log = []

    def write(self, address, data):
        self.log.append(("write", address, bytes(data)))
        if self.mode == "app" and address == 0x61 and bytes(data) == update.FRAME:
            self.pending = True
            if not self.needs_read:
                self._reboot()

    def read(self, address, length):
        self.log.append(("read", address, length))
        if self.mode == "app" and address == 0x61:
            if self.pending:
                self._reboot()
                return ACK
            return bytes(length)
        if self.mode == "boot" and address == 0x22:
            return ACK
        raise OSError("NACK")

    def ping(self, address):
        self.log.append(("ping", address))
        return (self.mode == "app" and address == 0x61) or (self.mode == "boot" and address == 0x22)

    def _reboot(self):
        if self.reboots:
            self.mode = "boot"


class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s

    def now(self):
        return self.t


def test_frame_bytes():
    assert update.change_to_iap_frame().hex(" ") == "01 81 fe 7e 04 00 f1 ca"
    assert update.FRAME[4:6] == b"\x04\x00"
    assert firmware.crc16(b"\x04\x00\x00\x00") == 0xCAF1


def test_enter_bootloader_writes_frame_then_reads_app():
    bus, clock = FakePanel(), Clock()
    ack, address = update.enter_bootloader(bus, sleep=clock.sleep, clock=clock.now)
    assert (ack, address) == (ACK, 0x22)
    after = bus.log[bus.log.index(("write", 0x61, update.FRAME)) + 1]
    assert after == ("read", 0x61, 4)          # a read, not a ping, follows the frame


def test_never_reads_the_bootloader_without_a_write():
    bus, clock = FakePanel(), Clock()
    update.enter_bootloader(bus, sleep=clock.sleep, clock=clock.now)
    assert not any(op[0] == "read" and op[1] in (0x22, 0x23) for op in bus.log)


def test_no_bootloader_means_nothing_erased():
    bus, clock = FakePanel(reboots=False), Clock()
    with pytest.raises(update.UpdateError, match="nothing was erased"):
        update.enter_bootloader(bus, sleep=clock.sleep, clock=clock.now)
    assert all(op[0] != "write" or op[2] == update.FRAME for op in bus.log)


def test_refuses_when_app_is_not_running():
    bus, clock = FakePanel(), Clock()
    bus.mode = "boot"
    with pytest.raises(update.UpdateError):
        update.enter_bootloader(bus, sleep=clock.sleep, clock=clock.now)
    assert not any(op[0] == "write" for op in bus.log)
