# aorus-lcd-fw-update

[English](README.md) · **Español**

Actualiza desde Linux el firmware de la pantalla lateral **LCD Edge View** de una Gigabyte
**AORUS GeForce RTX 5090 MASTER**, con el panel **funcionando con normalidad**. No hace falta
Windows, ni Gigabyte Control Center, ni root.

```bash
aorus-lcd-fw-update extract GV-N5090AORUSM-32GD_LCD_F1.3.exe -o lcd-f1.3.bin
aorus-lcd-fw-update flash lcd-f1.3.bin          # solo comprueba, no escribe nada
aorus-lcd-fw-update flash lcd-f1.3.bin --yes    # ~2 minutos, no apagues
```

## Por qué existe

[c-drew/gigabyte-aorus-lcd-linux](https://github.com/c-drew/gigabyte-aorus-lcd-linux)
(`aorus-lcd`) ya controla el panel desde Linux y puede reflashearlo exactamente igual que el
actualizador de Gigabyte. Pero solo lo hace cuando el microcontrolador del panel **ya está
atascado en su bootloader IAP**. Un panel sano ejecuta su aplicación en la dirección I2C
`0x61`, y nadie había documentado cómo lo manda el actualizador de Gigabyte al bootloader.
Este proyecto añade justo ese paso y después cede el control al plan de flasheo verificado de
`aorus-lcd`. Es un complemento pequeño, no un fork.

## El paso que faltaba: `I2CAPChangeToIAP`

Sale de `GvLcdFwUpdate.dll`, que viene dentro del actualizador oficial
`GV-N5090AORUSM-32GD_LCD_F1.3.exe`:

1. `I2CAPChangeToIAP` (RVA `0x35B0`) arma `{cmd = 0x8101, byte de longitud = 4}` y llama al
   escritor de tramas `0x4060`. Ese escritor es el mismo de `I2CIAPChangeToAP` (`0x8102`), que
   `aorus-lcd` ya reproduce y tiene verificado en emulador. El escritor añade `~cmd` y un
   CRC-16/XMODEM del bloque `[len][crc]`, calculado con el campo del CRC a cero. En el bus,
   hacia `0x61` (`0xC2` en 8 bits), queda así:

   ```
   01 81 FE 7E 04 00 F1 CA
   ```

2. El escritor espera 100 ms y **lee 4 bytes de la dirección de la aplicación** (helper
   `0x3B30`: una lectura simple, sin escribir antes). Repite la lectura cada 1 ms, hasta 3000
   veces, hasta que la respuesta no sea cero. El panel contesta `01 5A FE A5` y se reinicia en
   su bootloader.
3. El flujo de Gigabyte espera 500 ms y empieza a hablar con el bootloader en `0x22` (o en
   `0x23` como alternativa). A partir de ahí se usa `firmware.plan_flash` de `aorus-lcd` tal
   cual.

**La lectura del paso 2 es imprescindible.** En el primer intento, la herramienta enviaba la
trama y después solo hacía un ping. El panel ignoró la petición, se quedó con su firmware y
no se borró nada. Con la lectura, pasó al bootloader en el acto. Encaja con el manejador del
propio firmware del panel
([análisis de EKYavsil](https://github.com/EKYavsil/aorus-lcd-panel-reverse-engineering)),
que espera un mensaje de 8 bytes que empiece por `0x8101 0x7EFE`.

Puedes comprobarlo con tu propia descarga, sin ejecutar nada de Gigabyte:

```bash
.venv/bin/pip install -e '.[verify]'
.venv/bin/python -I tools/verify_dll.py GV-N5090AORUSM-32GD_LCD_F1.3.exe
```

## Estado

Probado **una vez**, en una sola tarjeta: AORUS GeForce RTX 5090 MASTER 32G (subsistema PCI
`1458:416e`), LCD de la **1.1 a la 1.3**, driver NVIDIA 615.78.08, Bazzite (Fedora 44, kernel
7.2). Se agradecen pruebas con otras tarjetas.

Solo acepta imágenes que `aorus-lcd` reconoce por su SHA-256, y solo en la tarjeta a la que
pertenecen. Hoy eso significa la F1.3 para `1458:416e`, que es la más reciente que publica
Gigabyte para esta tarjeta. Las F1.4 y F1.5 que circulan son de los modelos MASTER ICE y
STEALTH ICE.

## Instalación

Necesita Python 3.11+ y el driver de NVIDIA (abierto o propietario). Accede por
`/dev/nvidiactl`, igual que las librerías de NVIDIA, así que no hacen falta root ni i2c-dev.

```bash
git clone https://github.com/jfrmorales/aorus-lcd-fw-update
cd aorus-lcd-fw-update
python3 -m venv .venv && .venv/bin/pip install -e .
```

Descarga el actualizador desde la página de soporte de Gigabyte para tu tarjeta (*Soporte →
Firmware → LCD Firmware*). `extract` solo analiza los recursos .NET del ejecutable: no ejecuta
nada de Gigabyte, y el repositorio no incluye ningún fichero suyo.

## Seguridad

- **Antes de borrar nada**, la herramienta comprueba el hash de la imagen, que corresponde a
  esta tarjeta y que la aplicación del panel responde. Sin `--yes`, se detiene ahí.
- Si el panel no entra en el bootloader, se detiene y no borra nada.
- La zona del bootloader (`0x0000-0x0FFF`) **no se borra nunca**. Si el flasheo se
  interrumpe, el panel queda en negro dentro de su bootloader y se recupera con
  `aorus-lcd firmware flash lcd-f1.3.bin --yes`.
- Nunca lee del bootloader sin enviarle antes una petición de estado. Una lectura en vacío
  bloquea el bus hasta un apagado completo.
- Mantiene el bloqueo de bus de `aorus-lcd` durante toda la operación, así que el servicio de
  `aorus-lcd` no puede colar escrituras en medio. Cierra antes GCC y OpenRGB.
- Después de actualizar, el panel puede volver al modo demo de Gigabyte ("Hi AORUS" y GIFs
  animados) e ignorar la primera subida. Se arregla con `aorus-lcd mode 5` o subiendo el
  contenido otra vez. Gigabyte recomienda un apagado completo después de actualizar.

Solo toca el microcontrolador del LCD, nunca la VBIOS de la GPU. Aun así, es ingeniería
inversa y no es oficial: úsalo bajo tu responsabilidad.

## Créditos y licencia

Los créditos están en el [README en inglés](README.md#credits). Licencia MIT: consulta
[LICENSE](LICENSE).
