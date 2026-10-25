# Build and test image. Bookworm matches the oldest Raspberry Pi OS the
# package targets, so the tests run on its Python (3.11) and the .deb is
# built with its debhelper.
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        apt-utils \
        build-essential \
        debhelper \
        dpkg-dev \
        fakeroot \
        gnupg \
        lintian \
        python3 \
        python3-pytest \
        python3-venv \
        shellcheck \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/ruff \
    && /opt/ruff/bin/pip install --no-cache-dir ruff==0.8.4 \
    && ln -s /opt/ruff/bin/ruff /usr/local/bin/ruff

WORKDIR /src
