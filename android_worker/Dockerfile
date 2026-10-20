FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# A separate x86_64 Linux build host for GeckoView/Fenix. Mozilla bootstrap
# downloads the SDK/NDK/JDK required by the selected *Firefox release*.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl wget git mercurial python3 python3-venv python3-pip \
    build-essential clang cmake ninja-build make autoconf automake pkg-config sudo \
    openjdk-21-jdk-headless zip unzip xz-utils bzip2 tar zstd file \
    libstdc++6 libssl-dev libxml2-dev libasound2-dev libdbus-1-dev \
    libx11-dev libx11-xcb-dev libxt-dev libgtk-3-dev libpulse-dev \
    nodejs npm libncurses6 procps && \
    rm -rf /var/lib/apt/lists/*

# Ubuntu 24.04 base images may already contain `ubuntu` with UID/GID 1000.
# Reuse that identity rather than failing on `useradd --uid 1000`.
# The worker deliberately drops privileges to 1000:1000 when running mach.
RUN set -eu; \
    if ! getent group 1000 >/dev/null; then groupadd --gid 1000 builder; fi; \
    if getent passwd 1000 >/dev/null; then \
        existing_user="$(getent passwd 1000 | cut -d: -f1)"; \
        if [ "$existing_user" != "builder" ]; then \
            usermod --login builder --home /home/builder --move-home "$existing_user"; \
        fi; \
    else \
        useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash builder; \
    fi; \
    usermod --gid 1000 --shell /bin/bash builder; \
    echo "builder ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/mozilla-builder; \
    chmod 0440 /etc/sudoers.d/mozilla-builder; \
    test "$(id -u builder)" = 1000; \
    test "$(id -g builder)" = 1000
WORKDIR /app
COPY worker.py launcher.py /app/
ENV SHARED_DIR=/data/shared \
    BUILD_DIR=/work
CMD ["python3", "-u", "/app/launcher.py"]
