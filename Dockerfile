# syntax=docker/dockerfile:1
# PPK processor: RTKLIB-EX (rtklibexplorer/RTKLIB) + Python tooling
# for DJI Matrice 4E flights against ESTPOS (Leica Spider) base RINEX files.

ARG RTKLIB_REF=v2.5.1

# ---------------------------------------------------------------- builder ----
FROM debian:bookworm-slim AS builder
ARG RTKLIB_REF
RUN apt-get update && apt-get install -y --no-install-recommends \
        git cmake build-essential ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src

# RTKLIB-EX (https://github.com/rtklibexplorer/RTKLIB). RTKLIB_REF may be a tag (v2.5.1) or a branch (main).
RUN git clone --depth 1 --branch "${RTKLIB_REF}" https://github.com/rtklibexplorer/RTKLIB.git rtklib \
    && git -C rtklib log -1 --format='%H %cd %s' > /src/rtklib_commit.txt
# Number of carrier frequency slots compiled into RTKLIB (upstream default 3 = L1+L2+L5). 4 adds the
# fourth slot (Galileo E6, BeiDou B3I) so pos1-frequency=l1+l2+l5+l6 works instead of being clamped to 3.
ARG RTKLIB_NFREQ=4
RUN sed -i "s/-DNFREQ=3/-DNFREQ=${RTKLIB_NFREQ}/" rtklib/CMakeLists.txt \
    && grep -q "DNFREQ=${RTKLIB_NFREQ}" rtklib/CMakeLists.txt \
    && cmake -S rtklib -B rtklib/build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build rtklib/build --target rnx2rtkp convbin pos2kml -j"$(nproc)" \
    && ls -la rtklib/bin

# Hatanaka decompressor (crx2rnx) for compact RINEX base files. Best effort: a stub is
# installed if the source download fails so the image still builds.
RUN set -e; mkdir -p /out/bin; \
    if curl -fsSL https://terras.gsi.go.jp/ja/crx2rnx/RNXCMP_4.2.0_src.tar.gz -o rnxcmp.tgz \
       && mkdir rnxcmp && tar xzf rnxcmp.tgz -C rnxcmp --strip-components=1 \
       && gcc -O2 -o /out/bin/crx2rnx "$(find rnxcmp -name 'crx2rnx.c' | head -1)"; then \
         echo "crx2rnx built"; \
    else \
         printf '#!/bin/sh\necho "crx2rnx not available in this image" >&2\nexit 1\n' > /out/bin/crx2rnx; \
         chmod +x /out/bin/crx2rnx; echo "crx2rnx stub installed"; \
    fi

# IGS ANTEX for the base antenna PCV (virtual RINEX header says LEIAR25.R4 LEIT).
RUN mkdir -p /out/antex && curl -fsSL https://files.igs.org/pub/station/general/igs20.atx -o /out/antex/igs20.atx \
    && grep -c 'LEIAR25.R4      LEIT' /out/antex/igs20.atx

# ---------------------------------------------------------------- runtime ----
FROM python:3.12-slim
ARG RTKLIB_REF
LABEL org.opencontainers.image.title="ppk-processor" \
      org.opencontainers.image.description="DJI PPK processing with RTKLIB-EX ${RTKLIB_REF}"
RUN apt-get update && apt-get install -y --no-install-recommends gzip bzip2 unzip tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /src/rtklib/bin/rnx2rtkp /src/rtklib/bin/convbin /src/rtklib/bin/pos2kml /usr/local/bin/
COPY --from=builder /out/bin/crx2rnx /usr/local/bin/crx2rnx
COPY --from=builder /src/rtklib/lib/librtklib.so /usr/local/lib/
RUN ldconfig && rnx2rtkp --version
COPY --from=builder /out/antex /app/antex
COPY --from=builder /src/rtklib_commit.txt /app/rtklib_commit.txt
COPY config /app/config
COPY ppk /app/ppk
RUN pip install --no-cache-dir /app/ppk pytest
ENV PPK_CONF=/app/config/dji_m4e.conf \
    PPK_ANTEX=/app/antex/igs20.atx \
    PPK_RTKLIB_REF=${RTKLIB_REF} \
    PYTHONUNBUFFERED=1
WORKDIR /data
ENTRYPOINT ["ppk"]
CMD ["--help"]
