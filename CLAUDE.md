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
- El flasheo, sin contenedor a propósito: es una CLI de un uso y un reinicio del contenedor no
  debe cortarlo. El servicio con panel web de `aorus-lcd` sí va en contenedor (`Containerfile`,
  `compose.yml`, `contrib/aorus-lcd.container`), y necesita **red del host**: el bloqueo de bus
  es un socket Unix abstracto, que depende del namespace de red. Sin ella, flash y servicio
  escribirían a la vez (probado el 07/11/2026). También `label=disable` y CDI
  `nvidia.com/gpu=all`.
- Esta sesión puede correr en el distrobox `Ubuntu`: podman está en el host
  (`distrobox-host-exec podman ...`), y el host no tiene proveedor de `podman compose`.
- Probado solo en `1458:416e`, LCD 1.1 → 1.3. No ampliar tarjetas sin hash verificado en
  `aorus_lcd.firmware.KNOWN_IMAGES`.
