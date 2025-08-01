from pydantic import BaseModel, Field

MODELS_LIST = {
	"br": "vosk-model-br-0.8",
	"ca": "vosk-model-small-ca-0.4",
	"cs": "vosk-model-small-cs-0.4-rhasspy",
	"cn": "vosk-model-small-cn-0.22",
	"de": "vosk-model-small-de-0.15",
	"en": "vosk-model-en-us-0.22",
	"eo": "vosk-model-small-eo-0.42",
	"es": "vosk-model-small-es-0.42",
	"fa": "vosk-model-small-fa-0.42",
	"fr": "vosk-model-small-fr-0.22",
	"hi": "vosk-model-small-hi-0.22",
	"it": "vosk-model-small-it-0.22",
	"ja": "vosk-model-small-ja-0.22",
	"ko": "vosk-model-small-ko-0.22",
	"kz": "vosk-model-small-kz-0.15",
	"nl": "vosk-model-small-nl-0.22",
	"pl": "vosk-model-small-pl-0.22",
	"pt": "vosk-model-small-pt-0.3",
	"ru": "vosk-model-small-ru-0.22",
	"te": "vosk-model-small-te-0.42",
	"tg": "vosk-model-small-tg-0.22",
	"tr": "vosk-model-small-tr-0.3",
	"uk": "vosk-model-small-uk-v3-nano",
	"uz": "vosk-model-small-uz-0.22",
	"vn": "vosk-model-small-vn-0.4",
}


class LanguageMetadata(BaseModel):
	separator: str = Field(" ", description="Separator used in the language")

class LanguageModel(BaseModel):
	name: str = Field(..., description="Name of the language")
	metadata: LanguageMetadata = Field(default_factory=LanguageMetadata, description="Metadata for the language")


LANGUAGE_MAP = {
	"br": LanguageModel(name="Breton (BR)"),
	"ca": LanguageModel(name="Catalan (CA)"),
	"cs": LanguageModel(name="Czech (CS)"),
	"cn": LanguageModel(
		name="Chinese (CN)",
		metadata=LanguageMetadata(
			separator="",
		),
	),
	"de": LanguageModel(name="German (DE)"),
	"en": LanguageModel(name="English (EN)"),
	"eo": LanguageModel(name="Esperanto (EO)"),
	"es": LanguageModel(name="Spanish (ES)"),
	"fa": LanguageModel(name="Persian (FA)"),
	"fr": LanguageModel(name="French (FR)"),
	"hi": LanguageModel(name="Hindi (HI)"),
	"it": LanguageModel(name="Italian (IT)"),
	"ja": LanguageModel(
		name="Japanese (JA)",
		metadata=LanguageMetadata(
			separator="",
		),
	),
	"ko": LanguageModel(name="Korean (KO)"),
	"kz": LanguageModel(name="Kazakh (KZ)"),
	"nl": LanguageModel(name="Dutch (NL)"),
	"pl": LanguageModel(name="Polish (PL)"),
	"pt": LanguageModel(name="Portuguese (PT)"),
	"ru": LanguageModel(name="Russian (RU)"),
	"te": LanguageModel(name="Telugu (TE)"),
	"tg": LanguageModel(name="Tajik (TG)"),
	"tr": LanguageModel(name="Turkish (TR)"),
	"uk": LanguageModel(name="Ukrainian (UK)"),
	"uz": LanguageModel(name="Uzbek (UZ)"),
	"vn": LanguageModel(name="Vietnamese (VN)"),
}
