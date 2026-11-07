"""Check the I2CAPChangeToIAP frame against your own copy of Gigabyte's updater.

Parses (never runs) the updater .exe, pulls GvLcdFwUpdate.dll out of its .NET
resources, disassembles I2CAPChangeToIAP and confirms it still sends command
0x8101 with length byte 4 through the frame writer that also serves
I2CIAPChangeToAP (0x8102). Needs: pip install dnfile pefile capstone

    python -I tools/verify_dll.py GV-N5090AORUSM-32GD_LCD_F1.3.exe
"""
import re
import sys

import capstone
import dnfile
import pefile


def dll_from_updater(path):
    pe = dnfile.dnPE(path)
    for res in pe.net.resources or []:
        for entry in getattr(res.data, "entries", None) or []:
            if entry.name.lower() == "resource/gvlcdfwupdate.dll":
                data = bytes(entry.data)
                return data[4:]            # 4-byte length prefix
    raise SystemExit("no resource/gvlcdfwupdate.dll in this updater")


def disasm(pe, raw, name, limit=0x80):
    exports = {e.name.decode(): e.address for e in pe.DIRECTORY_ENTRY_EXPORT.symbols if e.name}
    rva = exports[name]
    off = pe.get_offset_from_rva(rva)
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    out = []
    for ins in md.disasm(raw[off:off + limit], pe.OPTIONAL_HEADER.ImageBase + rva):
        out.append(f"{ins.mnemonic} {ins.op_str}")
        if ins.mnemonic == "ret":
            break
    return out


def summary(lines):
    text = "\n".join(lines)
    cmd = re.search(r"mov eax, (0x[0-9a-f]+)\n.*mov word ptr \[rsp \+ 0x20\], ax", text)
    length = re.search(r"mov byte ptr \[rsp \+ 0x24\], (\w+)", text)
    calls = re.findall(r"call (0x[0-9a-f]+)", text)
    return (int(cmd.group(1), 16) if cmd else None,
            int(length.group(1), 0) if length else None,
            calls[0] if calls else None)


def main():
    raw = dll_from_updater(sys.argv[1])
    pe = pefile.PE(data=raw)
    to_iap = disasm(pe, raw, "I2CAPChangeToIAP")
    to_ap = disasm(pe, raw, "I2CIAPChangeToAP")
    cmd, length, writer = summary(to_iap)
    cmd_ap, length_ap, writer_ap = summary(to_ap)
    print("I2CAPChangeToIAP:\n  " + "\n  ".join(to_iap))
    print(f"\nI2CAPChangeToIAP: cmd {cmd and hex(cmd)}, length byte {length}, writer {writer}")
    print(f"I2CIAPChangeToAP: cmd {cmd_ap and hex(cmd_ap)}, length byte {length_ap}, writer {writer_ap}")
    ok = cmd == 0x8101 and length == 4 and writer is not None and writer == writer_ap and cmd_ap == 0x8102
    print("\nOK: matches aorus_lcd_fw_update.update.FRAME" if ok else "\nMISMATCH: do not flash")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
