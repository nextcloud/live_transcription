#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

MSG_RECEIVE_TIMEOUT = 10  # seconds
MAX_CONNECT_TRIES = 5  # maximum number of connection attempts
# Number of 20 ms audio frames to collect before sending to the ASR server. 16
# frames are 320 ms, which is the model's chunk, so every send completes exactly
# one decode instead of leaving audio waiting for the next send. Against 400 ms
# sends, p95 word latency fell from 984 ms to 735 ms at the same CPU cost.
MAX_AUDIO_FRAMES = 16
MIN_TRANSCRIPT_SEND_INTERVAL = 0.3  # min seconds to wait before sending a partial transcript again
HPB_SHUTDOWN_TIMEOUT = 30  # seconds to wait for the ws connectino to shut down gracefully
CALL_LEAVE_TIMEOUT = 60  # seconds to wait before leaving the call if there are no targets
# wait ASR_CONNECT_TIMEOUT seconds for the ASR server handshake to complete,
# this includes the model load time in the ASR server
ASR_CONNECT_TIMEOUT = 60
HPB_PING_TIMEOUT = 120  # seconds to wait for a ping response from HPB server
OCP_TASK_PROC_SCHED_RETRIES = 3
OCP_TASK_TIMEOUT = 30  # seconds to wait for a translation task to complete
SEND_TIMEOUT = 10  # timeout for sending transcripts and translations
# factor by which to increase the timeout on each timeout occurrence for transcripts and translations
TIMEOUT_INCREASE_FACTOR = 1.5
CACHE_TTL = 15 * 60  # cache values for 15 minutes
ICE_GATHERING_TIMEOUT = 30  # seconds

# todo
MAX_TRANSCRIPT_SEND_TIMEOUT = 30
MAX_TRANSLATION_SEND_TIMEOUT = 60
