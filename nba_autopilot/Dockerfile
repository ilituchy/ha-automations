# NBA Fantasy Autopilot -- Home Assistant app image.
#
# The base image is pinned LITERALLY and deliberately. Since Supervisor
# 2026.04.0 the `BUILD_FROM` build-arg is only passed when a build file
# (build.yaml / build.json) is present; this app has none, so the historical
# `ARG BUILD_FROM` / `FROM ${BUILD_FROM}` pattern would expand to an empty
# base image name and fail with:
#   "Default value for ARG $BUILD_FROM results in empty or invalid base image name"
#
# Multi-arch is handled by the registry manifest list: this tag is an OCI image
# index carrying linux/amd64 and linux/arm64, and `docker buildx build --platform`
# selects the right one. A per-arch Dockerfile is not needed.
FROM ghcr.io/home-assistant/base-python:3.13-alpine3.24-2026.06.1

# Supplied by the Supervisor on every build (these two are still injected even
# without a build file). Defaults keep plain `docker build` working locally.
ARG BUILD_VERSION="0.1.0"
ARG BUILD_ARCH="amd64"

# Alpine's Python is externally managed; the base image expects this.
ENV PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Install dependencies wheels-only. --only-binary=:all: makes the build fail
# loudly rather than silently falling back to an sdist that would need gcc.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --only-binary=:all: -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

# s6-overlay service definitions plus the Python application tree (/app/main.py).
COPY rootfs /

# The legacy builder used to inject io.hass.type/name/description/url and
# io.hass.version/arch. With no build file, the Supervisor passes only a subset,
# so declare them here explicitly.
LABEL \
    io.hass.type="app" \
    io.hass.name="NBA Fantasy Autopilot" \
    io.hass.description="Automated ESPN fantasy basketball lineup management for Home Assistant." \
    io.hass.url="https://github.com/ianlituchy/ha-nba-autopilot" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.arch="${BUILD_ARCH}" \
    org.opencontainers.image.title="NBA Fantasy Autopilot" \
    org.opencontainers.image.description="Automated ESPN fantasy basketball lineup management for Home Assistant." \
    org.opencontainers.image.source="https://github.com/ianlituchy/ha-nba-autopilot" \
    org.opencontainers.image.documentation="https://github.com/ianlituchy/ha-nba-autopilot/blob/main/nba_autopilot/DOCS.md" \
    org.opencontainers.image.authors="ian@vibrantpractice.com" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.version="${BUILD_VERSION}"

# Ingress-only; 8099 is bound inside the container and reached via the proxy.
EXPOSE 8099
