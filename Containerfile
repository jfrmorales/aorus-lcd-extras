# The aorus-lcd service (web UI on 127.0.0.1:5090 + live GPU stats) in a container.
# The same image runs the single front page, panel UI + GIPHY (`--entrypoint aorus-lcd-giphy`, 127.0.0.1:5091).
# Flashing stays on the host: see "Run the panel service in a container" in README.md.
#
#   podman build -t localhost/aorus-lcd -f Containerfile .
#
# It needs, at run time (compose.yml and contrib/aorus-lcd.container already do it):
#   --network host                  aorus-lcd's bus lock is an abstract Unix socket, which is
#                                   per network namespace: without it, a flash from the host
#                                   would not see the service and both would write to the bus
#   --device nvidia.com/gpu=all     CDI: /dev/nvidiactl, /dev/nvidiaN and the host's
#                                   libnvidia-ml (NVML must match the running driver)
#   --security-opt label=disable    SELinux would otherwise deny the NVIDIA devices

FROM docker.io/library/python:3.12-slim-trixie AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md LICENSE /src/
COPY aorus_lcd_fw_update /src/aorus_lcd_fw_update
COPY aorus_lcd_giphy /src/aorus_lcd_giphy
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir /src

FROM docker.io/library/python:3.12-slim-trixie
# DejaVu: aorus-lcd's default text font. rsvg-convert: SVG logos.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core librsvg2-bin \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 990 --home-dir /var/lib/aorus-lcd --create-home lcd \
 && mkdir -p /etc/aorus-lcd
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    STATE_DIRECTORY=/var/lib/aorus-lcd \
    PYTHONUNBUFFERED=1
USER lcd
VOLUME /var/lib/aorus-lcd
# No HEALTHCHECK here: podman drops it from OCI images. compose.yml and the Quadlet unit set it.
ENTRYPOINT ["aorus-lcd"]
CMD ["daemon", "--config", "/etc/aorus-lcd/config.toml"]
