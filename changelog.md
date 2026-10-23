<!--
  - SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
  - SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Change Log
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/)
and this project adheres to [Semantic Versioning](http://semver.org/).

## 3.0.0 - 2026-10-23

### Added
- replace Vosk with sherpa-onnx and the multilingual nemotron-3.5-asr-streaming-0.6b model, one model for every language, with punctuation and capitalisation @kyteinsky
- automatic language detection, the new default, also used for any language the model has no prompt for @kyteinsky
- regional languages en_GB, es_US, fr_CA and pt_BR, and bg, da, et, fi, hr, hu, no, ro, sk and sv @kyteinsky
- voice activity detection, so silent participants cost almost no CPU @kyteinsky
- batched decoding of concurrent speakers, one lane per core @kyteinsky
- final text every 10 s during long speech, so translation does not wait for a pause @kyteinsky
- CUDA image built on the upstream sherpa-onnx CUDA wheel, and an arm64 CPU image @kyteinsky
- LT_ASR_THREADS and LT_ASR_DISABLE_VAD settings @kyteinsky

### Changed
- BREAKING: LT_VOSK_SERVER_URL and LT_DISABLE_INTERNAL_VOSK are renamed to LT_ASR_SERVER_URL and LT_DISABLE_INTERNAL_ASR. The old names still work when the new ones are unset, with a deprecation warning, and will be removed in the next major release @kyteinsky
- BREAKING: ar_TN, br, ca, eo, fa, kk, te, tg and uz are no longer offered, because the model does not transcribe them. A room set to one of them now uses automatic detection @kyteinsky
- LT_MAX_WORKERS defaults to the CPU core count, and a higher value logs a warning @kyteinsky
- the app sends 320 ms of audio per message, the model's chunk size @kyteinsky
- the healthcheck checks that the ASR server process is alive @kyteinsky

### Fixed
- replies from the ASR server arrived one audio chunk late, because the config reply was never read @kyteinsky
- a partial transcript containing the word "success" failed a language switch @kyteinsky
- wss:// ASR server URLs always failed to connect @kyteinsky
- the server waited forever for a mistyped LT_ASR_VAD_MODEL path @kyteinsky


## 2.1.3 - 2026-08-19

### Changed
- add NC 35 support ([#86](https://github.com/nextcloud/live_transcription/pull/86)) @kyteinsky
- minor fixes ([#90](https://github.com/nextcloud/live_transcription/pull/90)) @kyteinsky

### Fixed
- remove redundant lines ([#87](https://github.com/nextcloud/live_transcription/pull/87)) @kyteinsky


## 2.1.2 - 2026-05-07

### Changed
- improve internal secret description ([#81](https://github.com/nextcloud/live_transcription/pull/81)) @kyteinsky

### Fixed
- handle exc_info type errors ([#80](https://github.com/nextcloud/live_transcription/pull/80)) @kyteinsky
- improve logging at important places ([#82](https://github.com/nextcloud/live_transcription/pull/82)) @kyteinsky


## 2.1.1 - 2026-04-17

### Fixed
- get HPB settings in a lazy way to not crash on startup ([#75](https://github.com/nextcloud/live_transcription/pull/75)) @kyteinsky


## 2.1.0 - 2026-03-03

### Added
- add support for NC 34

### Changed
- perf(OCPTranslator): Speedup translations ([#61](https://github.com/nextcloud/live_transcription/pull/61)) @marcelklehr
- switch to staticmethod for task types for no room requirement ([#66](https://github.com/nextcloud/live_transcription/pull/66)) @kyteinsky

### Fixed
- Make is_language_pair_supported actually return True ([#60](https://github.com/nextcloud/live_transcription/pull/60)) @marcelklehr
- use the larger LANGUAGE_MAP and fallback to lang name ([#66](https://github.com/nextcloud/live_transcription/pull/66)) @kyteinsky
- fix ocp_translator detect_language detection ([#66](https://github.com/nextcloud/live_transcription/pull/66)) @kyteinsky
- memory leak improvements ([#67](https://github.com/nextcloud/live_transcription/pull/67)) @kyteinsky


## 2.0.0 - 2026-01-20

### Added
- feat: Live translation

### Breaking changes
- Dropped support for NC < 33


## 1.3.0 - 2025-12-02

### Added
- add /capabilities endpoint ([#47](https://github.com/nextcloud/live_transcription/pull/47)) @kyteinsky

### Fixed
- clear reconnection task in the HPB close() ([#48](https://github.com/nextcloud/live_transcription/pull/48)) @kyteinsky
- remove redundant keep_peers arg in HPB close() ([#48](https://github.com/nextcloud/live_transcription/pull/48)) @kyteinsky
- delay 2 secs between reconnection tries ([#48](https://github.com/nextcloud/live_transcription/pull/48)) @kyteinsky
- conditional reconnection ([#48](https://github.com/nextcloud/live_transcription/pull/48)) @kyteinsky
- catch HPB server connection exceptions ([#48](https://github.com/nextcloud/live_transcription/pull/48)) @kyteinsky
- join the call after short resume ([#48](https://github.com/nextcloud/live_transcription/pull/48)) @kyteinsky
- continue with normal ops if short resume succeeds ([#48](https://github.com/nextcloud/live_transcription/pull/48)) @kyteinsky


## 1.2.3 - 2025-11-25

### Fixed
- pure asyncio locks ([#44](https://github.com/nextcloud/live_transcription/pull/44)) @kyteinsky
- prevent race condition of start when a client is shutting down ([#44](https://github.com/nextcloud/live_transcription/pull/44)) @kyteinsky
- increase HPB timeout and add reconnection logic ([#44](https://github.com/nextcloud/live_transcription/pull/44)) @kyteinsky
- use HPB session id to remove targets from signaling messages ([#44](https://github.com/nextcloud/live_transcription/pull/44)) @kyteinsky
- ensure peer connections are healthy on reconnect ([#44](https://github.com/nextcloud/live_transcription/pull/44)) @kyteinsky
- pause sending transcript until the HPB connection re-establishes ([#44](https://github.com/nextcloud/live_transcription/pull/44)) @kyteinsky


## 1.2.2 - 2025-11-07

### Changed
- allow https url scheme and add /spreed in hpb url ([#36](https://github.com/nextcloud/live_transcription/pull/36)) @kyteinsky
- improve install docs and LT_HPB_URL description ([#35](https://github.com/nextcloud/live_transcription/pull/35)) @kyteinsky

### Fixed
- pip's behaviour change with installing vosk's deps ([#37](https://github.com/nextcloud/live_transcription/pull/37)) @kyteinsky


## 1.2.1 - 2025-11-05

### Changed
- bump max NC version to 33

### Fixed
- return gracefully on vosk connection error ([#29](https://github.com/nextcloud/live_transcription/pull/29)) @kyteinsky


## 1.2.0 - 2025-09-11

### Added
- support sending partial transcript chunks ([#22](https://github.com/nextcloud/live_transcription/pull/22)) @kyteinsky

### Fixed
- better HPB error message handling ([#26](https://github.com/nextcloud/live_transcription/pull/26)) @kyteinsky
- slow down transcript sent to 300ms ([#26](https://github.com/nextcloud/live_transcription/pull/26)) @kyteinsky
- better shutdown process ([#26](https://github.com/nextcloud/live_transcription/pull/26)) @kyteinsky
- drop the "the", it's cleaner ([#26](https://github.com/nextcloud/live_transcription/pull/26)) @kyteinsky


## 1.1.2 - 2025-08-28

### Fixed
- use LT_DISABLE_INTERNAL_VOSK in vosk server ([#21](https://github.com/nextcloud/live_transcription/pull/21)) @kyteinsky
- override nc_py_api's NPA_NC_CERT based on SKIP_CERT_VERIFY ([#23](https://github.com/nextcloud/live_transcription/pull/23)) @kyteinsky


## 1.1.1 - 2025-08-28

### Fixed
- pin nc_py_api to 0.20.2 ([#19](https://github.com/nextcloud/live_transcription/pull/19)) @kyteinsky


## 1.1.0 - 2025-08-28

### Added
- Add reuse status badge ([#6](https://github.com/nextcloud/live_transcription/pull/6)) @AndyScherzinger
- add RTL support in language metadata ([#7](https://github.com/nextcloud/live_transcription/pull/7)) @kyteinsky
- add Arabic and Arabic Tunisian languages ([#8](https://github.com/nextcloud/live_transcription/pull/8)) @kyteinsky
- add langId in sent transcript signaling messages ([#9](https://github.com/nextcloud/live_transcription/pull/9)) @kyteinsky
- add app version to file logs ([#13](https://github.com/nextcloud/live_transcription/pull/13)) @kyteinsky

### Changed
- switch to Nextcloud session id for endpoints ([#10](https://github.com/nextcloud/live_transcription/pull/10)) @kyteinsky
- further clarify the env vars purpose in info.xml ([#16](https://github.com/nextcloud/live_transcription/pull/16)) @kyteinsky
- refactor out different classes and general utils into separate files ([#17](https://github.com/nextcloud/live_transcription/pull/17)) @kyteinsky

### Fixed
- streamline error response in set-language call ([#7](https://github.com/nextcloud/live_transcription/pull/7)) @kyteinsky
- use ISO 639 language codes ([#7](https://github.com/nextcloud/live_transcription/pull/7)) @kyteinsky
- better error handling ([#7](https://github.com/nextcloud/live_transcription/pull/7)) @kyteinsky
- increase timeout for the model in vosk to load ([#7](https://github.com/nextcloud/live_transcription/pull/7)) @kyteinsky
- streamline error response in set-language call ([#7](https://github.com/nextcloud/live_transcription/pull/7)) @kyteinsky
- use the given language, not the global one in language switch ([#14](https://github.com/nextcloud/live_transcription/pull/14)) @kyteinsky
- use a stash list for unseen targets to be added ([#13](https://github.com/nextcloud/live_transcription/pull/13)) @kyteinsky
- load dotenv in the logger ([#17](https://github.com/nextcloud/live_transcription/pull/17)) @kyteinsky


## 1.0.1 - 2025-08-07

### Fixed
- fix: info.xml fix and add info.xml linter workflow ([#3](https://github.com/nextcloud/live_transcription/pull/3)) @kyteinsky


## 1.0.0 - 2025-08-06

### Added
- Initial release of the app.
