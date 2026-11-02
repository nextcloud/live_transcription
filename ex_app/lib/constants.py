#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import logging
import os


def _read_int_env(name: str, default: int, minimum: int) -> int:
	"""Read a whole number from the environment, falling back to the default for anything else."""
	raw = os.getenv(name)
	if raw is None or raw.strip() == "":
		return default
	try:
		value = int(raw)
	except ValueError:
		logging.getLogger("lt.constants").warning(
			"Ignoring %s=%r, it is not a whole number; using %d instead", name, raw, default,
		)
		return default
	if value < minimum:
		logging.getLogger("lt.constants").warning(
			"%s=%d is below the minimum of %d; using %d instead", name, value, minimum, minimum,
		)
		return minimum
	return value


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
# Total budget for one translation task. MetaTranslator enforces it with
# asyncio.wait_for(), so OCPTranslator.translate() has to fit its scheduling retries
# and its polling into the same budget. Raise it for slower on-premises translation
# providers, at the cost of transcripts lagging further behind the call.
OCP_TASK_TIMEOUT = _read_int_env("LT_OCP_TASK_TIMEOUT", default=30, minimum=5)
# OCPTranslator gives up this long before the caller cancels it, so that its own
# errors, which name the task status and the language pair, are actually raised.
OCP_TASK_GIVEUP_MARGIN = 2
SEND_TIMEOUT = 10  # timeout for sending transcripts and translations
# factor by which to increase the timeout on each timeout occurrence for transcripts and translations
TIMEOUT_INCREASE_FACTOR = 1.5
CACHE_TTL = 15 * 60  # cache values for 15 minutes
ICE_GATHERING_TIMEOUT = 30  # seconds

# todo
MAX_TRANSCRIPT_SEND_TIMEOUT = 30
MAX_TRANSLATION_SEND_TIMEOUT = 60
