#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import asyncio
import threading

from livetypes import SupportedTranslationLanguages
from meta_translator import MetaTranslator


# todo: this is a hacky way to get the supported translation languages
#       we don't have access to the room token and language id outside of a call
#       and the OCP API requires a user id to authenticate, which may depend on the room owner
#       for now, "admin" is used as a placeholder
async def get_supported_translation_languages() -> SupportedTranslationLanguages:
	"""
	Raises
	------
		TranslateFatalException
		TranslateException
	"""  # noqa
	dummy_q1: asyncio.Queue = asyncio.Queue()
	dummy_q2: asyncio.Queue = asyncio.Queue()
	dummy_event = threading.Event()

	meta_translator = MetaTranslator(
		room_token="",
		room_lang_id="",
		translate_queue_input=dummy_q1,
		translate_queue_output=dummy_q2,
		should_translate=dummy_event,
	)
	return await meta_translator.get_translation_languages()
