# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
ARG RT_IMAGE=ubuntu:22.04
FROM ${RT_IMAGE}

# The sherpa-onnx package on PyPI is CPU-only, and it falls back to the CPU
# silently if you ask for a GPU provider. Thus a CUDA image must install the
# separately published CUDA wheel, or COMPUTE_DEVICE=cuda is a no-op.
ARG SHERPA_ONNX_WHEEL=
ARG DEBIAN_FRONTEND=noninteractive
ARG TZ=Etc/UTC

RUN apt-get update \
    && apt-get install -y --no-install-recommends software-properties-common gpg-agent \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        procps \
        python3.12 \
        python3.12-dev \
        python3.12-venv \
        python3-pip \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1 \
    && rm -rf /var/lib/apt/lists/*

# Download and install FRP client into /usr/local/bin.
RUN set -ex; \
    ARCH=$(uname -m); \
    if [ "$ARCH" = "aarch64" ]; then \
      FRP_URL="https://raw.githubusercontent.com/nextcloud/HaRP/main/exapps_dev/frp_0.61.1_linux_arm64.tar.gz"; \
    else \
      FRP_URL="https://raw.githubusercontent.com/nextcloud/HaRP/main/exapps_dev/frp_0.61.1_linux_amd64.tar.gz"; \
    fi; \
    echo "Downloading FRP client from $FRP_URL"; \
    curl -L "$FRP_URL" -o /tmp/frp.tar.gz; \
    tar -C /tmp -xzf /tmp/frp.tar.gz; \
    mv /tmp/frp_0.61.1_linux_* /tmp/frp; \
    cp /tmp/frp/frpc /usr/local/bin/frpc; \
    chmod +x /usr/local/bin/frpc; \
    rm -rf /tmp/frp /tmp/frp.tar.gz

# Create a virtual environment for Python.
RUN python3 -m venv /venv

# Install the Python dependencies with a cache mount. sherpa-onnx ships prebuilt
# wheels, thus this image needs no compiler or build toolchain. git is here only
# because requirements.txt pins supervisor to a git revision.
COPY requirements.txt /
RUN if [ -n "$SHERPA_ONNX_WHEEL" ]; then sed -i '/^sherpa-onnx$/d' requirements.txt; fi
RUN --mount=type=cache,target=/root/.cache/pip \
    /venv/bin/python3 -m pip install --root-user-action=ignore -r requirements.txt \
    && if [ -n "$SHERPA_ONNX_WHEEL" ]; then \
         /venv/bin/python3 -m pip install --root-user-action=ignore "$SHERPA_ONNX_WHEEL"; \
       fi \
    && rm requirements.txt

# Add application files.
ADD /ex_app/cs[s] /ex_app/css
ADD /ex_app/im[g] /ex_app/img
ADD /ex_app/j[s] /ex_app/js
ADD /ex_app/l10[n] /ex_app/l10n
ADD /ex_app/li[b] /ex_app/lib

# Copy scripts with the proper permissions.
COPY --chmod=775 healthcheck.sh /
COPY --chmod=775 start.sh /
COPY --chmod=775 logger_config.yaml /
COPY --chmod=644 supervisord.conf /etc/supervisor/supervisord.conf

# Set working directory and define entrypoint/healthcheck.
WORKDIR /ex_app/lib
ENTRYPOINT ["/start.sh", "/venv/bin/supervisord", "-c", "/etc/supervisor/supervisord.conf"]
HEALTHCHECK --interval=20s --timeout=2s --retries=300 CMD /healthcheck.sh
