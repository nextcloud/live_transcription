# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
ARG DEBIAN_VERSION=13
ARG CUDA_VERSION=12.4.1
ARG UBUNTU_VERSION=22.04
ARG SHERPA_ONNX_VERSION=1.13.8

ARG BASE_IMAGE=debian:${DEBIAN_VERSION}-slim
# The CUDA execution provider links against cuDNN, which the plain `devel` and
# `runtime` tags do not ship. Only the `cudnn-` flavours carry libcudnn.so.9,
# thus both the builder and the runtime must use them.
ARG CUDA_DEVEL_IMAGE=nvidia/cuda:${CUDA_VERSION}-cudnn-devel-ubuntu${UBUNTU_VERSION}
ARG CUDA_RUNTIME_IMAGE=nvidia/cuda:${CUDA_VERSION}-cudnn-runtime-ubuntu${UBUNTU_VERSION}

# ============================================================
# CUDA builder
#
# sherpa-onnx publishes CPU-only wheels on PyPI, and asking a CPU wheel for the
# CUDA provider falls back to the CPU silently. There is no official CUDA wheel
# for every Python version either, thus the CUDA image compiles one from the
# sdist with SHERPA_ONNX_ENABLE_GPU=ON.
#
# This stage needs the full CUDA toolkit and a compiler, which together weigh
# several GB. None of that belongs in the shipped image, so only the finished
# wheel is copied into the runtime stage.
# ============================================================
FROM ${CUDA_DEVEL_IMAGE} AS sherpa-builder-cuda
ARG SHERPA_ONNX_VERSION

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        software-properties-common gpg-agent \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        curl \
        git \
        libasound2-dev \
        python3.13 \
        python3.13-dev \
        python3.13-venv \
    && rm -rf /var/lib/apt/lists/*

# The build deps are installed up front, because the wheel is built without
# isolation. --system-site-packages is not used, so CMake is pointed at this
# venv's headers explicitly below.
RUN python3.13 -m venv /opt/buildenv \
    && /opt/buildenv/bin/python -m pip install --no-cache-dir --upgrade \
        pip setuptools wheel

# libcuda.so is provided by the host driver at runtime and is absent from the
# image, so the linker needs the toolkit's stub to resolve it at build time.
RUN ln -sf /usr/local/cuda/lib64/stubs/libcuda.so /usr/local/lib/libcuda.so \
    && ln -sf /usr/local/cuda/lib64/stubs/libcuda.so /usr/local/lib/libcuda.so.1
ENV LD_LIBRARY_PATH="/usr/local/lib:/usr/local/cuda/lib64:/usr/local/cuda/lib64/stubs:${LD_LIBRARY_PATH}"

# SHERPA_ONNX_ENABLE_GPU=ON is NVIDIA-only, which the upstream CMake says
# plainly. ENABLE_BINARY=OFF drops the command line tools, because the app only
# uses the Python API.
# Python_EXECUTABLE is set because CMake's FindPython looks at the interpreter
# on PATH, which is the system one, and then cannot find the headers for the
# venv the wheel is built in.
ENV SHERPA_ONNX_CMAKE_ARGS="-DSHERPA_ONNX_ENABLE_GPU=ON -DSHERPA_ONNX_ENABLE_BINARY=OFF -DPython_EXECUTABLE=/opt/buildenv/bin/python"

ENV SHERPA_ONNX_VERSION=${SHERPA_ONNX_VERSION}
COPY --chmod=775 dockerfile_scripts/build_sherpa_cuda.sh /tmp/build_sherpa_cuda.sh
RUN --mount=type=cache,target=/root/.cache/pip /tmp/build_sherpa_cuda.sh

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
        procps \
        python3.13 \
        python3.13-venv \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.13 1 \
    && rm -rf /var/lib/apt/lists/*

COPY --chmod=775 dockerfile_scripts/install_frpc.sh /tmp/install_frpc.sh
RUN /tmp/install_frpc.sh && rm /tmp/install_frpc.sh

RUN python3 -m venv /venv

# Install the GPU build from the builder stage, then the rest of the
# requirements with sherpa-onnx removed so pip cannot pull the CPU wheel over
# the top of it.
COPY --from=sherpa-builder-cuda /wheels /wheels
COPY requirements.txt /
RUN --mount=type=cache,target=/root/.cache/pip \
    /venv/bin/python3 -m pip install --root-user-action=ignore \
        --no-index --find-links=/wheels sherpa-onnx \
    && sed -i '/^sherpa-onnx$/d' requirements.txt \
    && /venv/bin/python3 -m pip install --root-user-action=ignore -r requirements.txt \
    && rm -rf /wheels requirements.txt

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
