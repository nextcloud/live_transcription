# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
ARG DEBIAN_VERSION=13
ARG CUDA_VERSION=12.8.1
ARG UBUNTU_VERSION=24.04
# Keep in step with the sherpa-onnx pin in requirements.txt.
ARG SHERPA_ONNX_VERSION=1.13.8

ARG BASE_IMAGE=debian:${DEBIAN_VERSION}-slim
# The CUDA execution provider links against cuDNN, which the plain `runtime`
# tags do not ship. Only the `cudnn-` flavours carry libcudnn.so.9.
#
# CUDA 12.8 is the oldest runtime that defines every libcudart symbol the
# 1.13.8 CUDA wheel imports. 12.4 to 12.6 lack cudaLibraryLoadData and three
# others, so the CUDA provider would fail to load and the server would fall
# back to the CPU.
ARG CUDA_RUNTIME_IMAGE=nvidia/cuda:${CUDA_VERSION}-cudnn-runtime-ubuntu${UBUNTU_VERSION}

# ============================================================
# CPU runtime
#
# Debian 13 ships Python 3.13 in its own repositories, thus this stage needs no
# third-party PPA. The sherpa-onnx wheel needs only glibc 2.16 and
# GLIBCXX 3.4.19, which Debian 13 (glibc 2.41) satisfies easily.
# ============================================================
FROM ${BASE_IMAGE} AS runtime-cpu

ARG DEBIAN_FRONTEND=noninteractive
ARG TZ=Etc/UTC

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        procps \
        python3 \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

COPY --chmod=775 dockerfile_scripts/install_frpc.sh /tmp/install_frpc.sh
RUN /tmp/install_frpc.sh && rm /tmp/install_frpc.sh

RUN python3 -m venv /venv

# git is needed only because requirements.txt pins supervisor to a git revision.
COPY requirements.txt /
RUN --mount=type=cache,target=/root/.cache/pip \
    /venv/bin/python3 -m pip install --root-user-action=ignore -r requirements.txt \
    && rm requirements.txt

ADD /ex_app/cs[s] /ex_app/css
ADD /ex_app/im[g] /ex_app/img
ADD /ex_app/j[s] /ex_app/js
ADD /ex_app/l10[n] /ex_app/l10n
ADD /ex_app/li[b] /ex_app/lib

COPY --chmod=775 healthcheck.sh /
COPY --chmod=775 start.sh /
COPY --chmod=775 logger_config.yaml /
COPY --chmod=644 supervisord.conf /etc/supervisor/supervisord.conf

WORKDIR /ex_app/lib
ENTRYPOINT ["/start.sh", "/venv/bin/supervisord", "-c", "/etc/supervisor/supervisord.conf"]
HEALTHCHECK --interval=20s --timeout=2s --retries=300 CMD /healthcheck.sh

# ============================================================
# CUDA runtime
#
# Ubuntu-based, because that is what the NVIDIA images provide. The cudnn
# runtime flavour carries libcudnn.so.9 and the CUDA math libraries that
# libonnxruntime_providers_cuda.so links against. The GPU itself is reached
# through the host driver, thus the container needs the NVIDIA container
# runtime at `docker run` time.
# ============================================================
FROM ${CUDA_RUNTIME_IMAGE} AS runtime-cuda
ARG SHERPA_ONNX_VERSION
# The onnxruntime build bundled in the CUDA wheel, which is part of its file
# name. It changes with the sherpa-onnx release, thus check
# https://k2-fsa.github.io/sherpa/onnx/cuda.html when bumping the version.
ARG SHERPA_ONNX_CUDA_ORT_VERSION=1.28.2
ARG SHERPA_ONNX_CUDA_WHEEL=https://huggingface.co/csukuangfj2/sherpa-onnx-wheels/resolve/main/cuda/${SHERPA_ONNX_VERSION}/sherpa_onnx-${SHERPA_ONNX_VERSION}+cuda12.cudnn9.onnxruntime${SHERPA_ONNX_CUDA_ORT_VERSION}-cp313-cp313-linux_x86_64.whl

ARG DEBIAN_FRONTEND=noninteractive
ARG TZ=Etc/UTC
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        software-properties-common gpg-agent \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        # The CUDA wheel's extension module links libasound.so.2 and, unlike
        # the PyPI wheel, does not bundle it.
        libasound2t64 \
        procps \
        python3.13 \
        python3.13-venv \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.13 1 \
    && rm -rf /var/lib/apt/lists/*

COPY --chmod=775 dockerfile_scripts/install_frpc.sh /tmp/install_frpc.sh
RUN /tmp/install_frpc.sh && rm /tmp/install_frpc.sh

RUN python3 -m venv /venv

# sherpa-onnx on PyPI is CPU-only, and a CPU wheel asked for the CUDA provider
# falls back to the CPU silently. Thus install the CUDA wheel that upstream
# publishes, then the rest of the requirements with sherpa-onnx removed so pip
# cannot pull the CPU wheel over the top of it.
COPY requirements.txt /
RUN --mount=type=cache,target=/root/.cache/pip \
    /venv/bin/python3 -m pip install --root-user-action=ignore "${SHERPA_ONNX_CUDA_WHEEL}" \
    && sed -i '/^sherpa-onnx==/d' requirements.txt \
    && /venv/bin/python3 -m pip install --root-user-action=ignore -r requirements.txt \
    && rm requirements.txt

ADD /ex_app/cs[s] /ex_app/css
ADD /ex_app/im[g] /ex_app/img
ADD /ex_app/j[s] /ex_app/js
ADD /ex_app/l10[n] /ex_app/l10n
ADD /ex_app/li[b] /ex_app/lib

COPY --chmod=775 healthcheck.sh /
COPY --chmod=775 start.sh /
COPY --chmod=775 logger_config.yaml /
COPY --chmod=644 supervisord.conf /etc/supervisor/supervisord.conf

WORKDIR /ex_app/lib
ENTRYPOINT ["/start.sh", "/venv/bin/supervisord", "-c", "/etc/supervisor/supervisord.conf"]
HEALTHCHECK --interval=20s --timeout=2s --retries=300 CMD /healthcheck.sh

# `docker build` with no --target produces the CPU image.
FROM runtime-cpu AS final
