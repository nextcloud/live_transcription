#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

MSG_RECEIVE_TIMEOUT = 10  # seconds
MAX_CONNECT_TRIES = 5  # maximum number of connection attempts
MAX_AUDIO_FRAMES = 20  # maximum number of audio frames to collect before sending to Vosk
MIN_TRANSCRIPT_SEND_INTERVAL = 0.3  # min seconds to wait before sending a partial transcript again
HPB_SHUTDOWN_TIMEOUT = 30  # seconds to wait for the ws connectino to shut down gracefully
CALL_LEAVE_TIMEOUT = 60  # seconds to wait before leaving the call if there are no targets
# wait VOSK_CONNECT_TIMEOUT seconds for the Vosk server handshake to complete,
# this includes the language load time in the Vosk server
VOSK_CONNECT_TIMEOUT = 60
