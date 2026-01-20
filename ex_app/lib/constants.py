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
HPB_PING_TIMEOUT = 120  # seconds to wait for a ping response from HPB server
OCP_TASK_PROC_SCHED_RETRIES = 3
OCP_TASK_TIMEOUT = 30  # seconds to wait for a translation task to complete
SEND_TIMEOUT = 10  # timeout for sending transcripts and translations
# factor by which to increase the timeout on each timeout occurrence for transcripts and translations
TIMEOUT_INCREASE_FACTOR = 1.5
CACHE_TRANSLATION_LANGS_FOR = 15 * 60  # cache translation languages for 15 minutes
CACHE_TRANSLATION_TASK_TYPES_FOR = 15 * 60  # cache translation task types for 15 minutes

# todo
MAX_TRANSCRIPT_SEND_TIMEOUT = 30
MAX_TRANSLATION_SEND_TIMEOUT = 60
