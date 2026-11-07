"""aorus-lcd-fw-update: update the AORUS LCD Edge View firmware from Linux."""
import argparse
import sys
import time

from aorus_lcd import firmware
from aorus_lcd.panel import BusLock, Panel
from aorus_lcd.transport import find_gpu, open_transport

from . import __version__, update


def _load(path):
    image = open(path, "rb").read()
    info = firmware.identify(image)
    if not info:
        raise SystemExit(f"refusing: {path} is not an LCD firmware image aorus-lcd knows "
                         "(see aorus_lcd.firmware.KNOWN_IMAGES)")
    return image, info


def _gpu_for(info, pci):
    gpu = find_gpu(pci)
    if (gpu.subvendor, gpu.subdevice) != info["subsystem"]:
        raise SystemExit(f"refusing: the image is for {info['package']}, this card is {gpu.describe()}")
    return gpu


def cmd_extract(args):
    image = firmware.extract(args.updater)
    info = firmware.identify(image)
    with open(args.output, "wb") as f:
        f.write(image)
    print(f"wrote {args.output} ({len(image)} bytes): "
          + (f"LCD firmware {info['version']} for {info['package']}" if info else
             "UNKNOWN image, `flash` will refuse it"))


def cmd_flash(args):
    image, info = _load(args.image)
    gpu = _gpu_for(info, args.gpu)
    writes = sum(op[0] == "write" for op in firmware.plan_flash(image))
    print(f"GPU:    {gpu.describe()}")
    print(f"image:  LCD firmware {info['version']}, {len(image)} bytes, {writes} bus writes")
    print(f"AP->IAP frame: {update.FRAME.hex(' ')}")

    lock = BusLock(timeout=2.0)
    with lock, open_transport("nvrm", gpu) as t:
        if firmware.detect(t) != "app":
            raise SystemExit("the LCD application is not running; if the panel is black and stuck "
                             "in its bootloader, use `aorus-lcd firmware flash` instead")
        current = Panel(t, lock).status()["firmware"]
        print(f"panel:  firmware {current}")
        if not args.yes:
            print("checks passed, nothing written. Re-run with --yes to flash.")
            return

        print("asking the panel to reboot into its bootloader ...", flush=True)
        try:
            ack, address = update.enter_bootloader(t)
        except update.UpdateError as e:
            raise SystemExit(str(e)) from None
        print(f"panel acked {ack.hex(' ') if ack else '(nothing)'}; bootloader at {address:#04x}")

        print("flashing, 1-3 minutes. Do not power off.", flush=True)
        last = [0.0]

        def progress(done, total):
            now = time.monotonic()
            if done == total or now - last[0] >= 5:
                last[0] = now
                print(f"  {done}/{total}", flush=True)

        try:
            update.flash(t, image, address, progress=progress)
        except update.UpdateError as e:
            raise SystemExit(str(e)) from None
        time.sleep(1)
        print(f"done: panel firmware {Panel(t, lock).status()['firmware']}")
        print("The panel may come back in Gigabyte's demo mode; `aorus-lcd mode 5` (or any "
              "upload) takes it out. A full power-off is what Gigabyte recommends after an update.")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="aorus-lcd-fw-update", description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--gpu", metavar="PCI", help="GPU PCI address (default: the Gigabyte card)")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("extract", help="pull the LCD firmware out of Gigabyte's updater .exe "
                       "(not executed, only parsed)")
    p.add_argument("updater")
    p.add_argument("-o", "--output", default="lcd-firmware.bin")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("flash", help="update a working panel (checks only, unless --yes)")
    p.add_argument("image")
    p.add_argument("--yes", action="store_true", help="actually flash")
    p.set_defaults(func=cmd_flash)

    args = ap.parse_args(argv)
    try:
        args.func(args)
    except (LookupError, OSError, RuntimeError, ValueError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
