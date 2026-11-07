"""Update the LCD Edge View firmware from a panel that is running normally.

aorus-lcd (c-drew/gigabyte-aorus-lcd-linux) can already flash the panel, but
only when the MCU is sitting in its IAP bootloader (0x22/0x23). The missing
piece is how Gigabyte's updater gets a *working* panel there:
GvLcdFwUpdate.dll!I2CAPChangeToIAP. Recovered from the F1.3 updater:

  I2CAPChangeToIAP (RVA 0x35B0) builds {cmd = 0x8101, len byte = 4} and calls
  the frame writer at 0x4060, which fills in ~cmd and a CRC-16/XMODEM over the
  4-byte [len][crc] block (crc field zeroed). On the wire, to 0x61 (0xC2):

      01 81 FE 7E 04 00 F1 CA

  The writer then waits 100 ms and polls the *application* address with plain
  4-byte reads (helper 0x3B30, no write first) every 1 ms, up to 3000 times,
  until the reply is non-zero. The panel answers 01 5A FE A5 and reboots into
  its bootloader. Without that read the panel ignores the request: an
  address-only ping right after the frame is not enough (tested).

  The vendor flow then sleeps 500 ms and talks to the bootloader, which is
  exactly where aorus-lcd's verified flash plan starts.

Byte 5 of the frame is left uninitialised on the DLL's stack; this tool sends
0x00. The panel firmware only checks the first two half-words (0x8101 0x7EFE)
of an 8-byte message.
"""
import time

from aorus_lcd import firmware
from aorus_lcd.transport import LCD_ADDRESS

CMD_AP_TO_IAP = 0x8101
FRAME = bytes.fromhex("0181fe7e0400f1ca")

POLL_DELAY = 0.1          # writer: Sleep(100) before the first poll
POLL_LIMIT = 3000         # writer: up to 0xBB8 polls ...
POLL_INTERVAL = 0.001     # ... with Sleep(1) between them
SETTLE = 0.5              # vendor flow: Sleep(500) after I2CAPChangeToIAP
BOOTLOADER_TIMEOUT = 5.0


class UpdateError(RuntimeError):
    pass


def change_to_iap_frame():
    """The I2CAPChangeToIAP frame, built the way the DLL's writer builds it."""
    head = CMD_AP_TO_IAP.to_bytes(2, "little") + (CMD_AP_TO_IAP ^ 0xFFFF).to_bytes(2, "little")
    block = bytes([4, 0])
    crc = firmware.crc16(block + b"\0\0")
    return head + block + crc.to_bytes(2, "little")


assert change_to_iap_frame() == FRAME


def poll_app(t, sleep=time.sleep):
    """Plain 4-byte reads of 0x61 until one is non-zero. Returns it, or None."""
    for _ in range(POLL_LIMIT):
        try:
            reply = t.read(LCD_ADDRESS, 4)
        except OSError:
            reply = None
        if reply and any(reply):
            return reply
        sleep(POLL_INTERVAL)
    return None


def wait_for_bootloader(t, timeout=BOOTLOADER_TIMEOUT, sleep=time.sleep, clock=time.monotonic):
    """Address-only pings (never an idle read) until a bootloader answers."""
    deadline = clock() + timeout
    while clock() < deadline:
        where = firmware.detect(t)
        if isinstance(where, tuple):
            return where[1]
        sleep(0.05)
    return None


def enter_bootloader(t, sleep=time.sleep, clock=time.monotonic):
    """Ask the running application to reboot into its IAP bootloader.
    Returns (ack bytes or None, bootloader address). Nothing is erased here."""
    if firmware.detect(t) != "app":
        raise UpdateError("the LCD application is not answering at 0x61")
    t.write(LCD_ADDRESS, FRAME)
    sleep(POLL_DELAY)
    ack = poll_app(t, sleep)
    sleep(SETTLE)
    address = wait_for_bootloader(t, sleep=sleep, clock=clock)
    if address is None:
        raise UpdateError(f"no bootloader appeared (ack {ack.hex(' ') if ack else 'none'}); "
                          "nothing was erased, the panel is unchanged")
    return ack, address


def flash(t, image, address, progress=None, sleep=time.sleep):
    """aorus-lcd's verified flash plan, then wait for the application."""
    reply = firmware.execute(t, address, firmware.plan_flash(image), sleep=sleep, progress=progress)
    if not firmware.wait_for_app(t, sleep=sleep):
        raise UpdateError("the application did not start after flashing; "
                          "the bootloader is intact: run `aorus-lcd firmware flash` with the same image")
    return reply
