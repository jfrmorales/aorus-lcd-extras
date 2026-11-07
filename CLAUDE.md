# aorus-lcd-fw-update

Complemento de `aorus-lcd` (c-drew/gigabyte-aorus-lcd-linux, pineado por commit en
`pyproject.toml`) que actualiza el firmware del LCD Edge View con el panel funcionando.
README en inglés (principal) y `README.es.md`; código en inglés para la comunidad.

Trampas que no hay que deshacer:

- Tras la trama `01 81 FE 7E 04 00 F1 CA` hay que **leer** 4 bytes de 0x61, como la DLL.
  Con solo un ping el panel ignora la petición (probado el 07/11/2026).
- Nunca una lectura en vacío al bootloader (0x22/0x23): cuelga el bus hasta apagar la fuente.
  Para detectarlo, solo pings (`firmware.detect`).
- No se versiona nada de Gigabyte (`.exe`, `.dll`, `.bin` en `.gitignore`).
- Sin contenedor a propósito: habla con `/dev/nvidiactl` del host y es una CLI de un uso.
- Probado solo en `1458:416e`, LCD 1.1 → 1.3. No ampliar tarjetas sin hash verificado en
  `aorus_lcd.firmware.KNOWN_IMAGES`.
