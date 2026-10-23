# One image, every service (patch, login, game, observe). The compose file picks the module with
# `command:`, e.g. ["psu.game"]. Built by CI or by hand: docker build -f server/Dockerfile server
#
# Code only. The NBL archives, JSON tables and captured tables built from the discs are not in the
# image and never may be: the image is public. The host mounts them from $PSU_DATA/nbl,
# $PSU_DATA/gamedata and $PSU_DATA/captures.
#
# Standard library only, so there is no pip step.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PSU_LOG_DIR=/logs \
    PSU_STATE_DIR=/state \
    PSU_NBL_DIR=/nbl \
    PSU_CAPTURE_DIR=/captures \
    PSU_GAMEDATA=/gamedata

WORKDIR /app
COPY psu/ /app/psu/

# Non-root by default. Compose runs it as the owner of $PSU_DATA (PSU_UID/PSU_GID) so the
# bind-mounted logs and state stay writable; every game port is above 1024.
RUN useradd --system --no-create-home --uid 10110 psu \
    && mkdir -p /logs/sessions /state /nbl /captures /gamedata \
    && chown -R psu /logs /state
USER psu

ENTRYPOINT ["python", "-m"]
CMD ["psu.observe"]
