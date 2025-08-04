ARG RT_IMAGE=nvidia/cuda:12.6.3-devel-ubuntu24.04
FROM ${RT_IMAGE}

ARG HAVE_CUDA
ARG KALDI_MKL
ARG DEBIAN_FRONTEND=noninteractive
ARG TZ=Etc/UTC

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        procps \
        supervisor \
        wget \
        bzip2 \
        unzip \
        xz-utils \
        g++ \
        make \
        cmake \
        git \
        python3 \
        python3-dev \
        python3-pip \
        python3-venv \
        zlib1g-dev \
        automake \
        autoconf \
        libtool \
        pkg-config \
        ca-certificates \
        curl \
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

# Copy requirements and install Python dependencies using a cache mount.
COPY requirements.txt /
RUN python3 -m venv /venv
# TODO
#RUN --mount=type=cache,target=/root/.cache/pip \
RUN \
    /venv/bin/python3 -m pip install --root-user-action=ignore -r requirements.txt && rm requirements.txt

RUN \
    COMMIT=bc5baf14231660bd50b7d05788865b4ac6c34481 \
	&& git clone -c remote.origin.fetch=+${COMMIT}:refs/remotes/origin/$COMMIT --no-checkout --progress --depth 1 https://github.com/alphacep/kaldi /opt/kaldi \
	&& cd /opt/kaldi \
	&& git checkout $COMMIT \
    && curl -o /opt/kaldi/tools/extras/install_mkl.sh https://raw.githubusercontent.com/kaldi-asr/kaldi/aef1d98603b68e6cf3a973e9dcd71915e2a175fe/tools/extras/install_mkl.sh \
    && cd /opt/kaldi/tools \
    && sed -i 's:status=0:exit 0:g' extras/check_dependencies.sh \
    && sed -i 's:--enable-ngram-fsts:--enable-ngram-fsts --disable-bin:g' Makefile \
	&& sed -i 's: -msse -msse2 : -msse -msse2 -mavx -mavx2 :' /opt/kaldi/src/makefiles/linux_x86_64_mkl.mk \
	&& sed -i 's: -msse -msse2 : -msse -msse2 -mavx -mavx2 :' /opt/kaldi/src/makefiles/linux_openblas.mk \
	&& sed -i 's: -msse -msse2: -msse -msse2 -mavx -mavx2:' /opt/kaldi/tools/Makefile \
    && make -j 8 openfst cub \
    && if [ "x$KALDI_MKL" != "x1" ] ; then \
          extras/install_openblas_clapack.sh; \
       else \
          extras/install_mkl.sh; \
       fi

RUN cd /opt/kaldi/src \
    && HAVE_CUDA_YN=$(if [ "x$HAVE_CUDA" = "x1" ]; then echo "yes"; else echo "no"; fi) \
    && if [ "x$KALDI_MKL" != "x1" ] ; then \
          ./configure --mathlib=OPENBLAS_CLAPACK --shared --use-cuda=$HAVE_CUDA_YN; \
       else \
          ./configure --mathlib=MKL --shared --use-cuda=$HAVE_CUDA_YN; \
       fi \
    && sed -i 's:-msse -msse2:-msse -msse2 -mavx -mavx2:g' kaldi.mk \
    && sed -i 's: -O1 : -O3 :g' kaldi.mk \
    && if [ "x$HAVE_CUDA" != "x1" ]; then \
          make -j 8 online2 lm rnnlm; \
       else \
          make -j 8 online2 lm rnnlm cudafeat cudadecoder; \
       fi

RUN /venv/bin/python -m pip install --upgrade websockets cffi setuptools \
    \
    && COMMIT=0f364e3a4407fbc837f37423223dff9c7b3e8557 \
    && git clone -c remote.origin.fetch=+${COMMIT}:refs/remotes/origin/$COMMIT --no-checkout --progress --depth 1 https://github.com/alphacep/vosk-api /opt/vosk-api \
    && cd /opt/vosk-api \
    && git checkout $COMMIT \
    && cd /opt/vosk-api/src \
    && HAVE_OPENBLAS=$(if [ "x$KALDI_MKL" = "x1" ]; then echo "0"; else echo "1"; fi) \
    && HAVE_CUDA=$HAVE_CUDA HAVE_MKL=$KALDI_MKL HAVE_OPENBLAS_CLAPACK=$(HAVE_OPENBLAS) KALDI_ROOT=/opt/kaldi make -j \
    && cd /opt/vosk-api/python \
    && /venv/bin/python3 ./setup.py install \
    \
    && rm -rf /opt/vosk-api/src/*.o \
    && rm -rf /opt/kaldi \
    && rm -rf /root/.cache \
    && rm -rf /var/lib/apt/lists/*

# Add application files.
ADD /ex_app/cs[s] /ex_app/css
ADD /ex_app/im[g] /ex_app/img
ADD /ex_app/j[s] /ex_app/js
ADD /ex_app/l10[n] /ex_app/l10n
ADD /ex_app/li[b] /ex_app/lib

# Copy scripts with the proper permissions.
COPY --chmod=775 healthcheck.sh /
COPY --chmod=775 start.sh /
COPY --chmod=644 supervisord.conf /etc/supervisor/supervisord.conf

# Set working directory and define entrypoint/healthcheck.
WORKDIR /ex_app/lib
ENTRYPOINT ["/start.sh", "supervisord", "-c", "/etc/supervisor/supervisord.conf"]
HEALTHCHECK --interval=20s --timeout=2s --retries=300 CMD /healthcheck.sh
