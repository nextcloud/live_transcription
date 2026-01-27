#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from livetypes import SupportedTranslationLanguages
from meta_translator import MetaTranslator


async def get_supported_translation_languages() -> SupportedTranslationLanguages:
	"""
	Raises
	------
		TranslateFatalException
		TranslateException
	"""  # noqa
	return await MetaTranslator.get_translation_languages()
