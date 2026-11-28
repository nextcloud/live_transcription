<!--
  - SPDX-FileCopyrightText: 2026 Nextcloud GmbH and Nextcloud contributors
  - SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Speech recordings of the stress test

The speakers of the stress test (`stress_call.py`) play these recordings. They are
reused under the licenses below, which `REUSE.toml` at the root of the repository
also declares for each file. `clips.json` lists for each file the language, the
source, the pinned download URL, the checksum of the source file, the checksum of
the file here and the reference transcript.

Every file was converted with `ffmpeg` to Opus, mono, 16 kHz, 32 kbit/s
(`-ac 1 -ar 16000 -c:a libopus -b:a 32k -application voip`). Apart from that
conversion and the excerpt of the LibriVox recording, the recordings are unchanged.

## FLEURS (CC BY 4.0)

`fleurs-<language>-<id>.opus` are single utterances of FLEURS, a dataset of read
Wikipedia sentences in 102 languages by Google:

- Alexis Conneau, Min Ma, Simran Khanuja, Yu Zhang, Vera Axelrod, Siddharth Dalmia,
  Jason Riesa, Clara Rivera and Ankur Bapna. "FLEURS: Few-shot Learning Evaluation
  of Universal Representations of Speech". 2022 IEEE Spoken Language Technology
  Workshop (SLT), 2023. https://arxiv.org/abs/2205.12446
- Dataset: https://huggingface.co/datasets/google/fleurs, revision
  `70bb2e84b976b7e960aa89f1c648e09c59f894dd`
- License: Creative Commons Attribution 4.0 International,
  https://creativecommons.org/licenses/by/4.0/

| File | Language | FLEURS split and id |
|---|---|---|
| `fleurs-en-1544.opus` | English | en_us, dev, 1544 |
| `fleurs-en-1646.opus` | English | en_us, dev, 1646 |
| `fleurs-de-1551.opus` | German | de_de, dev, 1551 |
| `fleurs-de-1585.opus` | German | de_de, dev, 1585 |
| `fleurs-es-1565.opus` | Spanish | es_419, dev, 1565 |
| `fleurs-es-1635.opus` | Spanish | es_419, dev, 1635 |
| `fleurs-fr-1646.opus` | French | fr_fr, dev, 1646 |
| `fleurs-it-1548.opus` | Italian | it_it, dev, 1548 |
| `fleurs-ru-1592.opus` | Russian | ru_ru, dev, 1592 |
| `fleurs-uk-1650.opus` | Ukrainian | uk_ua, dev, 1650 |
| `fleurs-hi-1559.opus` | Hindi | hi_in, dev, 1559 |
| `fleurs-hi-1648.opus` | Hindi | hi_in, dev, 1648 |
| `fleurs-hi-1784.opus` | Hindi | hi_in, test, 1784 |

## LibriVox (public domain)

`librivox-hi-himalay-ka-pathik.opus` is the part from 18 s to 80 s of the LibriVox
recording of "हिमालय का पथिक" (Himalay ka Pathik), a short story by Jaishankar
Prasad (1889–1937), read by Sonali Ekka, track 10 of "Multilingual Short Works
Collection 042 - Poetry & Prose". The part starts after the LibriVox introduction.

- Recording: https://librivox.org/multilingual-short-works-collection-042/,
  file https://archive.org/download/msw042_2609_librivox/msw042_10_himalaykapathik_prasad_sie_64kb.mp3
- License: LibriVox recordings are in the public domain, marked with the Creative
  Commons Public Domain Mark 1.0, https://creativecommons.org/publicdomain/mark/1.0/
- Text of the reference transcript: Hindi Wikisource, आकाश-दीप/हिमालय का पथिक,
  revision 391060, in the public domain
