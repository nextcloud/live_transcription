<!--
  - SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
  - SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Live transcriptions in Nextcloud Talk

[![REUSE status](https://api.reuse.software/badge/github.com/nextcloud/live_transcription)](https://api.reuse.software/info/github.com/nextcloud/live_transcription)

Provides live transcriptions in Nextcloud Talk calls. High-performance backend (HPB) is required for this app to work.

The installation steps can be found here: https://docs.nextcloud.com/server/latest/admin_manual/ai/app_live_transcription.html#installation

> [!IMPORTANT]
> Environment variables "LT_HPB_URL" and "LT_INTERNAL_SECRET" are required to be set in "Deploy Options" near the "Deploy and Enable" button before the install.

### Speech recognition engine

Transcription runs on [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) with
NVIDIA's [nemotron-3.5-asr-streaming-0.6b](https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b),
a cache-aware FastConformer transducer that emits text while the speaker is still
talking. One 651 MB model covers all 28 supported languages and writes punctuation
and capital letters. The app downloads it on first start into its persistent storage,
together with a 628 kB [silero VAD](https://github.com/snakers4/silero-vad).

The model weights are licensed under [OpenMDW-1.1](https://openmdw.ai/license/1-1/),
which places no restrictions on the transcripts the model produces.

The supported languages are Arabic, Bulgarian, Chinese, Croatian, Czech, Danish, Dutch,
English, Estonian, Finnish, French, German, Hindi, Hungarian, Italian, Japanese,
Korean, Norwegian, Polish, Portuguese, Romanian, Russian, Slovak, Spanish, Swedish,
Turkish, Ukrainian, Vietnamese.

#### Sizing: how one example host performs

The numbers below are measured and not estimated. They are a worked example of what
to expect and not a guarantee. Your own figures will differ with another CPU, another
chunk size, or other languages.

**The example host**

| | |
|---|---|
| CPU | 4 cores, x86-64, **no GPU** |
| RAM | 12 GB |
| Model | `nemotron-3.5-asr-streaming-0.6b`, int8 ONNX, 320 ms chunk (the default) |
| `LT_MAX_WORKERS` | 4 (the default: one per core) |
| Voice activity detection | on (the default) |
| Audio | 48 kHz mono, 400 ms per chunk, as Talk delivers it |

Each participant holds one websocket connection. In the measurement every speaker
used a **different recording in a different language**, thus no two streams shared a
transcript or a language prompt.

**Memory**

Memory is dominated by the model, which is loaded once and shared by everyone. The
number of participants has little effect:

| Participants | Speaking | Resident memory |
|---|---|---|
| (idle, model loaded) | - | 1007 MiB |
| 2 | 1 | 1062 MiB |
| 4 | 2 | 1116 MiB |
| 16 | 2 | 1195 MiB |
| 16 | 4 | 1200 MiB |

A 16-person call costs about the same as a 2-person call. The first participants look
expensive at approximately 39 MiB each, and the later ones almost free at
approximately 1 MiB each. The cause is that the early cost is the inference engine's
per-thread memory arenas, which are allocated once and then reused.

**Budget approximately 1.2 GB for the steady state, and give the container 1.5 GB.**
Do not budget per participant.

**Speed is the real limit**

Memory is nearly identical whether one person speaks or four, but responsiveness is
not. The server must transcribe a 400 ms chunk in under 400 ms to keep pace:

| Participants | Speaking | Reply delay (p95) | Keeps pace |
|---|---|---|---|
| 16 | 1 | 0.22 s | yes |
| 16 | 2 | 0.28 s | yes |
| 8 | 4 | 0.44 s | **no** |
| 16 | 4 | 0.42 s | **no** |

On this host the ceiling is **four people speaking at the same time**, which is the
core count. One or two concurrent speakers, the normal shape of a meeting, leaves
comfortable headroom. Beyond the ceiling the delay grows without bound and the
captions fall progressively further behind.

Size by *simultaneous speakers* and not by participants. More speakers need more
cores or another server, and more memory does not help. `LT_MAX_WORKERS` bounds how
many chunks decode at once, and a value above the core count does not add capacity,
because it makes every speaker slower.

**Why idle participants are cheap**

The engine has no silence detection of its own. Without a voice activity detector it
spends 85 % to 97 % as much CPU on a silent stream as on a speaking one, and four
participants would exceed realtime even with one person talking. The VAD is what
makes listeners cost approximately 1 MiB and almost no CPU. Disable it with
`LT_ASR_DISABLE_VAD` only when debugging.

**Stability**

Memory settles rather than creeping. In a 20-minute soak with 8 connected
participants holding one connection each, 2 of them speaking continuously for
161 audio-minutes in total, resident memory rose from 1090 MiB to 1110 MiB and then
stopped. The last 8 minutes were flat at 1110 MiB, and the measured growth rate fell
from 0.0099 to 0.0022 MiB per audio-second.

A rate that decays shows one-off allocation being reused, whereas a leak holds the
rate constant. Thus treat approximately 1.2 GB as the steady state, and keep the
1.5 GB allowance for headroom.

#### Using an external server

Set `LT_DISABLE_INTERNAL_ASR=true` and point `LT_ASR_SERVER_URL` at your own server.
It must answer **exactly one** JSON message per binary audio frame, because the client
blocks on a reply after every frame:

```
C->S  {"config": {"language": "en", "sample_rate": 48000}}
S->C  {"success": true}
C->S  <binary s16le mono PCM>
S->C  {"partial": "..."}  or  {"text": "..."}     one, per frame
C->S  {"eof": 1}
S->C  {"text": "..."}
```

`{"text": ...}` marks the end of an utterance and triggers translation.
`{"partial": ...}` is an interim result for the same utterance.

### Environment variables

`example.env` contains all the environment variables needed to run the app. You can copy it to `.env` and change the values as needed.
The description of each variable other than AppAPI variables is in the `appinfo/info.xml` file.

### Image variants

The Dockerfile is multi-stage and builds two runtime images, selected with
`--target`:

| Target | Base | Use it for |
|---|---|---|
| `runtime-cpu` (default) | `debian:13-slim` | every CPU deployment, amd64 and arm64 |
| `runtime-cuda` | `nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04` | NVIDIA GPUs, amd64 only |

```bash
docker build -t live_transcription .                        # CPU
docker build --target runtime-cuda -t live_transcription:cuda .
```

The CPU image needs no GPU libraries at all, so it stays around 810 MB. The CUDA
image is larger, because the CUDA runtime and cuDNN alone are over a gigabyte.

**Why CUDA is compiled rather than installed.** sherpa-onnx on PyPI is CPU-only,
and asking a CPU wheel for the CUDA provider falls back to the CPU silently
rather than failing. The `sherpa-builder-cuda` stage therefore compiles the wheel
from the sdist with `SHERPA_ONNX_ENABLE_GPU=ON`, and only the finished wheel is
copied into the runtime image, which keeps the CUDA toolkit and the compiler out
of what you ship.

**Why the `cudnn-` tags.** The CUDA execution provider links against
`libcudnn.so.9`, which the plain `devel` and `runtime` CUDA images do not carry.
Only the `cudnn-` flavours ship it, thus both the builder and the runtime use
them. With a non-cudnn base the provider fails to load at runtime and the server
falls back to the CPU, which looks like a GPU that is simply slow.

Run the CUDA image with the NVIDIA container runtime, for example
`docker run --gpus all`, and set `COMPUTE_DEVICE=cuda`. The host needs only the
NVIDIA driver, because the CUDA toolkit is inside the image.

**AMD and Intel GPUs are not supported.** sherpa-onnx exposes exactly two GPU
build switches, `SHERPA_ONNX_ENABLE_GPU` for NVIDIA and
`SHERPA_ONNX_ENABLE_DIRECTML` for Windows. There is no ROCm, HIP or Vulkan
backend, so an AMD or Intel GPU runs the CPU image.

### Dev Docker Setup

1. `docker build -t ghcr.io/nextcloud-releases/live_transcription .`
2. Register a HaRP/Docker socket proxy deploy daemon:
   https://docs.nextcloud.com/server/latest/admin_manual/exapps_management/AppAPIAndExternalApps.html
   https://docs.nextcloud.com/server/latest/admin_manual/exapps_management/DeployConfigurations.html
3. Add a ghcr.io override to it: `occ app_api:daemon:registry:add --registry-from=ghcr.io --registry-to=local <deploy_daemon_name>`. Either use a new deploy daemon (with the same underlying DSP) or remember to remove this override later.
4. Register the app: `occ app_api:app:register live_transcription <deploy_daemon_name> --info-xml /path/to/live_transcription/appinfo/info.xml --wait-finish --env LT_HPB_URL=wss://nextcloud.local/standalone-signaling/spreed --env LT_INTERNAL_SECRET=7890`. Other environment variables can be added with `--env <name>=<value>`. Make sure to change the `LT_HPB_URL` to match your setup.

### Dev Bare-metal Setup

1. `cp example.env .env`, adjust the values in `.env` as needed.
2. `python -m venv .venv`
3. `. .venv/bin/activate`
4. `pip install -r requirements_dev.txt`
5. `python ex_app/lib/asr_server.py`

_in another terminal_

6. `. .venv/bin/activate`
7. `python ex_app/lib/main.py`

_in yet another terminal_

8. Register a manual deploy daemon: `occ app_api:daemon:register manual_install "Manual Install" "manual-install" "http" host.docker.internal "http://nextcloud.local"`. Change `host.docker.internal` to `localhost` if you're not using NC in docker.
9. Register the app: `occ app_api:app:register live_transcription manual_install --json-info "{\"id\":\"live_transcription\",\"name\":\"Live Transcription\",\"daemon_config_name\":\"manual_install\",\"version\":\"0.1.0\",\"secret\":\"12345\",\"port\":23000}" --wait-finish`

### HPB setup

1. The following `docker compose.yml` to set up the High Performance Backend (HPB) server. It is configured for Julius' docker dev setup.

```yaml
services:
  nc-talk:
    image: ghcr.io/nextcloud-releases/aio-talk:latest
    init: true
    ports:
      - 3478:3478/tcp
      - 3478:3478/udp
      - 8081:8081/tcp
    environment:
      SKIP_CERT_VERIFY: true
      NC_DOMAIN: "nextcloud${DOMAIN_SUFFIX}"
      TALK_PORT: 3478
      TURN_SECRET: "1234"
      SIGNALING_SECRET: "4567"
      INTERNAL_SECRET: "7890"
    restart: unless-stopped
```

2. Add location redirects in your nginx config:

```nginx
location /standalone-signaling/ {
  proxy_http_version 1.1;
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_pass http://nc-talk:8011/;
}

location /standalone-signaling/spreed {
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "Upgrade";
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_pass http://nc-talk:8011/spreed;
}
```

This should be added to the `location /` block of your Nextcloud server. Make sure to change the `proxy_pass` URL to match your setup.
For Julius' docker dev, this should go into `data/nginx/vhost.d/nextcloud.local_location`.

Small issue: The entire nginx stack fails to work if `spreedbackend` is not reachable/resolvable. Using a variable can fix this but I haven't got it to work yet. For now, make sure to have the HPB server running to avoid this issue, or use a static IP address.

6. In Talk, the HPB URL would be `https://nextcloud.local/standalone-signaling/` and the shared secret would be `4567`. Replace `nextcloud.local` here if you're not using the docker dev setup. `http` can also be used but microphone is not allowed in the browser for insecure connections.

TURN server setup would looks like:

|turn:only|localhost:3478|1234|UDP and TCP|
|-|-|-|-|

`localhost` here is the host at which the local PC running the browser should reach the TURN server.

And with that, the HPB should be working.
If it's not, debugging can be done through
1. the logs of the HPB's docker compose: `docker compose logs -f`
2. error messages in the HPB server settings in Talk
3. the browser console when opening a Talk room in the `Network` tab with `WS` filter
4. checking reachability of ports with `nc -zv -w 5 <host> <port>`
