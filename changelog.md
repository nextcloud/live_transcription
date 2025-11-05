<!--
  - SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
  - SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Change Log
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/)
and this project adheres to [Semantic Versioning](http://semver.org/).

## Unreleased


## 1.2.1 - 2025-11-05

### Fixed
- return gracefully on vosk connection error (#29) @kyteinsky

### Changed
- bump max NC version to 33


## 1.2.0 - 2025-09-11

### Added
- support sending partial transcript chunks (#22) @kyteinsky

### Fixed
- better HPB error message handling (#26) @kyteinsky
- slow down transcript sent to 300ms (#26) @kyteinsky
- better shutdown process (#26) @kyteinsky
- drop the "the", it's cleaner (#26) @kyteinsky


## 1.1.2 - 2025-08-28

### Fixed
- use LT_DISABLE_INTERNAL_VOSK in vosk server (#21) @kyteinsky
- override nc_py_api's NPA_NC_CERT based on SKIP_CERT_VERIFY (#23) @kyteinsky


## 1.1.1 - 2025-08-28

### Fixed
- pin nc_py_api to 0.20.2 (#19) @kyteinsky


## 1.1.0 - 2025-08-28

### Added
- Add reuse status badge (#6) @AndyScherzinger
- add RTL support in language metadata (#7) @kyteinsky
- add Arabic and Arabic Tunisian languages (#8) @kyteinsky
- add langId in sent transcript signaling messages (#9) @kyteinsky
- add app version to file logs (#13) @kyteinsky

### Fixed
- streamline error response in set-language call (#7) @kyteinsky
- use ISO 639 language codes (#7) @kyteinsky
- better error handling (#7) @kyteinsky
- increase timeout for the model in vosk to load (#7) @kyteinsky
- streamline error response in set-language call (#7) @kyteinsky
- use the given language, not the global one in language switch (#14) @kyteinsky
- use a stash list for unseen targets to be added (#13) @kyteinsky
- load dotenv in the logger (#17) @kyteinsky

### Changed
- switch to Nextcloud session id for endpoints (#10) @kyteinsky
- further clarify the env vars purpose in info.xml (#16) @kyteinsky
- refactor out different classes and general utils into separate files (#17) @kyteinsky


## 1.0.1 - 2025-08-07

### Fixed
- fix: info.xml fix and add info.xml linter workflow (#3) @kyteinsky


## 1.0.0 - 2025-08-06

### Added
- Initial release of the app.
