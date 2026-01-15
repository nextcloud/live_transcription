#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from livetypes import SupportedTranslationLanguages
from ocp_translator import OCPTranslator


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
	ocp_translator = OCPTranslator(
		origin_language="en",
		target_language="en",
		room_token="languages-dummy",  # noqa: S106
	)
	# todo: use the staticmethod version when implemented from "meta_translator.py"
	return await ocp_translator.get_translation_languages()
