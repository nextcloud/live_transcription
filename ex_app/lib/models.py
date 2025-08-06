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
	separator: str = Field(default=" ", description="Separator used in the language")

class LanguageModel(BaseModel):
	name: str = Field(..., description="Name of the language")
	metadata: LanguageMetadata = Field(default_factory=LanguageMetadata, description="Metadata for the language")


LANGUAGE_MAP = {
	"br": LanguageModel(name="Breton"),
	"ca": LanguageModel(name="Català"),
	"cs": LanguageModel(name="Čeština"),
	"cn": LanguageModel(
		name="中国",
		metadata=LanguageMetadata(
			separator="",
		),
	),
	"de": LanguageModel(name="Deutsch"),
	"en": LanguageModel(name="English"),
	"eo": LanguageModel(name="Esperanto"),
	"es": LanguageModel(name="Español"),
	"fa": LanguageModel(name="فارسی"),
	"fr": LanguageModel(name="Français"),
	"hi": LanguageModel(name="हिंदी"),
	"it": LanguageModel(name="Italiano"),
	"ja": LanguageModel(
		name="日本語",
		metadata=LanguageMetadata(
			separator="",
		),
	),
	"ko": LanguageModel(name="한국인"),
	"kz": LanguageModel(name="қазақ"),
	"nl": LanguageModel(name="Nederlands"),
	"pl": LanguageModel(name="Polski"),
	"pt": LanguageModel(name="Português"),
	"ru": LanguageModel(name="Русский"),
	"te": LanguageModel(name="తెలుగు"),
	"tg": LanguageModel(name="тоҷикӣ"),
	"tr": LanguageModel(name="Türkçe"),
	"uk": LanguageModel(name="українська"),
	"uz": LanguageModel(name="o'zbek"),
	"vn": LanguageModel(name="Tiếng Việt"),
}
