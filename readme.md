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
talking. One 651 MB model covers all 28 supported languages, four of them in two
regions, and writes punctuation
and capital letters. The app downloads it on first start into its persistent storage,
together with a 1.3 MB [silero VAD](https://github.com/snakers4/silero-vad) v6.

The model weights are licensed under [OpenMDW-1.1](https://openmdw.ai/license/1-1/),
which places no restrictions on the transcripts the model produces.

The supported languages are Arabic, Bulgarian, Chinese, Croatian, Czech, Danish, Dutch,
English, Estonian, Finnish, French, German, Hindi, Hungarian, Italian, Japanese,
Korean, Norwegian, Polish, Portuguese, Romanian, Russian, Slovak, Spanish, Swedish,
Turkish, Ukrainian, Vietnamese.
English, French, Portuguese and Spanish also come in a second region: `en_GB`, `fr_CA`,
`pt_BR` and `es_US`. The bare ids keep their previous meaning, which is US English,
France French, European Portuguese and Spain Spanish. Translation sends a regional id
to the translation provider as it is when the provider lists it, and as its bare
language otherwise.

The default language is **automatic detection**, which recognises the spoken
language by itself and follows a speaker who changes language mid-call. It is also
what a room gets when its language is not in the list above, instead of an error.
Naming the language is still worth it when you know it: a wrong explicit language
produces little or no text, but a right one is never worse than detection. A room
with automatic detection is translated with the translation provider's own
language detection.

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
| `LT_ASR_THREADS` | 1 (the default) |
| Voice activity detection | on (the default) |
| Audio | 48 kHz mono, 320 ms per chunk, as the app sends it |

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
not. Each speaker sent a different recording paced in real time for 20 s, and
waited for each reply as the app does. "Behind" is how far the last reply trailed
the end of the audio:

| Speaking | Chunk | Reply delay (p95) | Behind | Keeps pace |
|---|---|---|---|---|
| 4 | 320 ms | 0.12 s | 0.00 s | yes |
| 16 | 320 ms | 0.37 s | 0.26 s | yes |
| 2 | 400 ms | 0.19 s | 0.18 s | yes |
| 4 | 400 ms | 0.20 s | 0.22 s | yes |
| 8 | 400 ms | 0.37 s | 0.37 s | yes |
| 12 | 400 ms | 0.47 s | 0.50 s | yes |
| 16 | 400 ms | 0.63 s | 0.68 s | yes |

The app sends 320 ms chunks because that is the model's own chunk, thus every send
completes one decode and no audio waits for the next send.

Up to one speaker per core, each one is decoded on its own lane. Past that, a free
lane decodes all the speakers waiting for it in one batch, which shares the cost
of the encoder. With 16 speakers that used 76 CPU seconds, against 101 seconds and
a 7.4 s lag for the earlier server that gave each speaker its own turn. Batching
costs some accuracy: scored against the slower 1120 ms model, the word error rate
of 8 batched speakers went from 7.7 % to 8.6 %.

Size by *simultaneous speakers* and not by participants. More speakers need more
cores or another server, and more memory does not help. `LT_MAX_WORKERS` sets the
number of decode lanes, and the default is one per core. Leave it there: a lane
beyond the cores rarely finds more than one speaker waiting, so it takes turns on
the cores instead of batching. With 16 speakers, 8 lanes fell 6.7 s behind where
4 lanes kept pace.

Nothing is refused. Every speaker who connects is transcribed, and past what the
host can carry they all fall behind together rather than some being turned away.

`LT_ASR_THREADS` is the other half of that budget, and it is the one worth leaving
alone. It sets how many threads decode a *single* chunk, so the two multiply: the
old default of one thread per core gave 16 threads per decode on a 16-thread host,
and 16 of those in flight. A 320 ms chunk is too small to divide profitably, and
the threads spend the difference spinning rather than working. Measured with four
speakers on this host, raising it from 1 to 4 cost 28.1 CPU seconds instead of 12.8
and pushed p95 from 210 ms to 384 ms, so it is slower *and* dearer in both
directions.

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
The server must answer **exactly one** JSON message per binary audio frame, because the client
blocks on a reply after every frame:

```
C->S  {"config": {"language": "en", "sample_rate": 48000}}
S->C  {"success": true}
C->S  <binary s16le mono PCM>
S->C  {"partial": "..."}  or  {"text": "..."}     one, per frame
C->S  {"eof": 1}
S->C  {"text": "..."}
```

`{"text": ...}` is final text, which triggers translation. It is sent at a pause, and
also after 10 s of speech without one, in which case it carries the words decided so
far and the next partials continue from there.
`{"partial": ...}` is interim text that follows the last final.

The pre-3.0 names `LT_DISABLE_INTERNAL_VOSK` and `LT_VOSK_SERVER_URL` still work when
the new ones are unset, with a deprecation warning in the log. They will be removed in
the next major release.

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

**Why the CUDA image installs a different wheel.** sherpa-onnx on PyPI is
CPU-only, and asking a CPU wheel for the CUDA provider falls back to the CPU
silently rather than failing. The CUDA image therefore installs the CUDA wheel
that upstream publishes at <https://k2-fsa.github.io/sherpa/onnx/cuda.html>.
`SHERPA_ONNX_VERSION` selects the release. When you change it, also change the
pin in `requirements.txt`, and set `SHERPA_ONNX_CUDA_ORT_VERSION` to the
onnxruntime version in the new wheel's file name.

**Why the `cudnn-` tags.** The CUDA execution provider links against
`libcudnn.so.9`, which the plain `devel` and `runtime` CUDA images do not carry.
Only the `cudnn-` flavours ship it. The base is CUDA 12.8, because 12.4 to 12.6
lack symbols that the 1.13.8 wheel imports from `libcudart`. With a base that is
too old or lacks cuDNN, the provider fails to load at runtime and the server
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
