# Dynasty Advisor -- Home Assistant app image.
#
# Base pinned literally, same as nba_autopilot (see its Dockerfile): since
# Supervisor 2026.04.0 BUILD_FROM is only passed when a build file exists.
FROM ghcr.io/home-assistant/base-python:3.13-alpine3.24-2026.06.1

ARG BUILD_VERSION="0.6.1"
ARG BUILD_ARCH="amd64"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Standard library only, except the Anthropic SDK for the optional AI review
# (advisor/review.py imports it lazily; everything else runs without it).
RUN pip install --no-cache-dir "anthropic>=0.125,<2"

COPY advisor /app/advisor
COPY rootfs /

LABEL \
    io.hass.type="app" \
    io.hass.name="Dynasty Advisor" \
    io.hass.description="Trade and add/drop recommendations for a Sleeper dynasty league." \
    io.hass.url="https://github.com/ilituchy/ha-automations/tree/main/dynasty_advisor" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.arch="${BUILD_ARCH}" \
    org.opencontainers.image.title="Dynasty Advisor" \
    org.opencontainers.image.source="https://github.com/ilituchy/ha-automations" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.version="${BUILD_VERSION}"

EXPOSE 8099
