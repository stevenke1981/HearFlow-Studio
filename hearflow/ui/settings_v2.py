"""Provider-aware HearFlow settings dialog.

Secrets are intentionally collected separately from the dataclass settings and
are written only through ``SecretStore`` by the main window.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from hearflow.services.provider_catalog import (
    PROVIDER_PROFILES,
    ProviderCapability,
    get_provider_profile,
    profiles_for,
)
from hearflow.services.settings import (
    AppSettings,
    EngineSettings,
    RemoteTranscriptionSettings,
    SpeechSettings,
    TranslationEngineSettings,
    TranslationSettings,
)


class SettingsDialog(QDialog):
    """Edit local CUDA engines and capability-aware remote providers."""

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._original = settings
        self.setWindowTitle("偏好設定：本機 CUDA 與遠端 API")
        self.resize(820, 760)
        self.setMinimumSize(720, 620)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_asr_tab(settings), "語音轉錄")
        self.tabs.addTab(self._build_translation_tab(settings), "字幕翻譯")
        self.tabs.addTab(self._build_speech_tab(settings), "TTS 語音合成")

        warning = QLabel(
            "API 金鑰不會寫入 settings、專案資料庫或日誌；只會存入系統金鑰圈。"
            "Windows 預設使用 Credential Manager。"
        )
        warning.setWordWrap(True)
        warning.setProperty("warning", True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(warning)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_asr_tab(self, settings: AppSettings) -> QWidget:
        page, layout = _scroll_page()

        self.asr_mode = QComboBox()
        self.asr_mode.addItem("本機 Qwen3-ASR（llama.cpp，由聽序管理）", "managed")
        self.asr_mode.addItem("外部 Qwen3-ASR Gateway", "external")
        _select_data(self.asr_mode, settings.engine.mode)

        self.asr_backend = _backend_combo(settings.engine.backend)
        self.gateway_url = QLineEdit(settings.engine.gateway_url)
        self.gateway_credential = QLineEdit(settings.engine.credential_id)
        self.gateway_key = _secret_edit("外部 Gateway 才需要；留空不改變")
        self.asr_model = QLineEdit(settings.engine.model_id)
        self.asr_threads = _spin(1, 128, settings.engine.threads)
        self.asr_context = _spin(512, 262_144, settings.engine.context_size)

        local_form = QFormLayout()
        local_form.addRow("模式", self.asr_mode)
        local_form.addRow("GPU/運算後端", self.asr_backend)
        local_form.addRow("Gateway 位址", self.gateway_url)
        local_form.addRow("Gateway Credential 名稱", self.gateway_credential)
        local_form.addRow("Gateway 金鑰", self.gateway_key)
        local_form.addRow("Qwen3-ASR 模型別名", self.asr_model)
        local_form.addRow("CPU 執行緒", self.asr_threads)
        local_form.addRow("Context size", self.asr_context)
        local_group = QGroupBox("本機或自管 Qwen3-ASR")
        local_group.setLayout(local_form)
        layout.addWidget(local_group)

        remote = settings.remote_transcription
        self.remote_stt_enabled = QCheckBox("改用遠端語音轉錄 API")
        self.remote_stt_enabled.setChecked(remote.enabled)
        self.stt_provider = _provider_combo(
            ProviderCapability.TRANSCRIPTION,
            remote.provider_id,
        )
        self.stt_credential = QLineEdit(remote.credential_id)
        self.stt_api_key = _secret_edit("留空保留既有金鑰")
        self.stt_base_url = QLineEdit(remote.base_url)
        self.stt_base_url.setPlaceholderText("留空使用供應商預設位址")
        self.stt_model = QLineEdit(remote.model)
        self.stt_model.setPlaceholderText("留空使用供應商目前預設模型")
        self.stt_language = QLineEdit(remote.language)
        self.stt_prompt = QLineEdit(remote.prompt)
        self.stt_response_format = QComboBox()
        for value in ("json", "verbose_json", "diarized_json"):
            self.stt_response_format.addItem(value, value)
        _select_data(self.stt_response_format, remote.response_format)
        self.stt_temperature = _double_spin(0.0, 1.0, remote.temperature, 0.05)
        self.stt_max_mib = _spin(
            1,
            2048,
            max(1, round(remote.max_file_bytes / 1024 / 1024)),
        )
        self.stt_http_referer = QLineEdit(remote.http_referer)
        self.stt_app_title = QLineEdit(remote.app_title)

        remote_form = QFormLayout()
        remote_form.addRow("", self.remote_stt_enabled)
        remote_form.addRow("供應商", self.stt_provider)
        remote_form.addRow("Credential 名稱", self.stt_credential)
        remote_form.addRow("API 金鑰", self.stt_api_key)
        remote_form.addRow("API base URL", self.stt_base_url)
        remote_form.addRow("模型", self.stt_model)
        remote_form.addRow("語言提示", self.stt_language)
        remote_form.addRow("專有名詞／提示", self.stt_prompt)
        remote_form.addRow("回應格式", self.stt_response_format)
        remote_form.addRow("Temperature", self.stt_temperature)
        remote_form.addRow("單檔上限（MiB）", self.stt_max_mib)
        remote_form.addRow("OpenRouter HTTP-Referer", self.stt_http_referer)
        remote_form.addRow("OpenRouter X-Title", self.stt_app_title)
        remote_group = QGroupBox("遠端 STT")
        remote_group.setLayout(remote_form)
        layout.addWidget(remote_group)

        self.stt_provider.currentIndexChanged.connect(self._fill_stt_defaults)
        self.remote_stt_enabled.toggled.connect(self._update_asr_enabled_state)
        self._update_asr_enabled_state()
        layout.addStretch(1)
        return page

    def _build_translation_tab(self, settings: AppSettings) -> QWidget:
        page, layout = _scroll_page()

        self.translation_mode = QComboBox()
        self.translation_mode.addItem("不翻譯", "disabled")
        self.translation_mode.addItem("本機 llama.cpp（CUDA 優先）", "local")
        self.translation_mode.addItem("遠端 API", "remote")
        mode = (
            "local"
            if settings.translation_engine.enabled
            else "remote"
            if settings.translation.enabled
            else "disabled"
        )
        _select_data(self.translation_mode, mode)

        local = settings.translation_engine
        self.translation_backend = _backend_combo(local.backend)
        self.translation_local_model = QLineEdit(local.model_id)
        self.translation_repository = QLineEdit(local.model_repository)
        self.translation_quant = QLineEdit(local.model_quant)
        self.translation_port = _spin(1, 65_535, local.port)
        self.translation_threads = _spin(1, 128, local.threads)
        self.translation_context = _spin(512, 262_144, local.context_size)
        self.translation_gpu_layers = _spin(0, 999, local.gpu_layers)

        mode_form = QFormLayout()
        mode_form.addRow("翻譯模式", self.translation_mode)
        mode_group = QGroupBox("功能選擇")
        mode_group.setLayout(mode_form)
        layout.addWidget(mode_group)

        local_form = QFormLayout()
        local_form.addRow("GPU/運算後端", self.translation_backend)
        local_form.addRow("模型別名", self.translation_local_model)
        local_form.addRow("Hugging Face repository", self.translation_repository)
        local_form.addRow("GGUF quant", self.translation_quant)
        local_form.addRow("llama-server port", self.translation_port)
        local_form.addRow("CPU 執行緒", self.translation_threads)
        local_form.addRow("Context size", self.translation_context)
        local_form.addRow("GPU layers", self.translation_gpu_layers)
        self.translation_local_group = QGroupBox("本機翻譯引擎")
        self.translation_local_group.setLayout(local_form)
        layout.addWidget(self.translation_local_group)

        remote = settings.translation
        self.translation_provider = _provider_combo(
            ProviderCapability.TRANSLATION,
            remote.provider_id,
            exclude={"local-llama"},
        )
        self.translation_credential = QLineEdit(remote.credential_id)
        self.translation_api_key = _secret_edit("留空保留既有金鑰")
        self.translation_url = QLineEdit(remote.base_url)
        self.translation_url.setPlaceholderText("留空使用供應商預設位址")
        self.translation_model = QLineEdit(remote.model)
        self.translation_model.setPlaceholderText("留空使用供應商目前預設模型")
        self.source_language = QLineEdit(remote.source_language)
        self.target_language = QLineEdit(remote.target_language)
        self.translation_style = QLineEdit(remote.style)
        self.translation_system_prompt = QLineEdit(remote.system_prompt)
        self.translation_batch_size = _spin(1, 200, remote.batch_size)
        self.translation_temperature = _double_spin(
            0.0,
            2.0,
            remote.temperature,
            0.05,
        )
        self.translation_retries = _spin(0, 10, remote.max_retries)
        self.translation_response_format = QCheckBox("要求 JSON response_format")
        self.translation_response_format.setChecked(remote.use_response_format)
        self.translation_http_referer = QLineEdit(remote.http_referer)
        self.translation_app_title = QLineEdit(remote.app_title)

        remote_form = QFormLayout()
        remote_form.addRow("供應商", self.translation_provider)
        remote_form.addRow("Credential 名稱", self.translation_credential)
        remote_form.addRow("API 金鑰", self.translation_api_key)
        remote_form.addRow("API base URL", self.translation_url)
        remote_form.addRow("模型", self.translation_model)
        remote_form.addRow("來源語言", self.source_language)
        remote_form.addRow("目標語言", self.target_language)
        remote_form.addRow("翻譯風格", self.translation_style)
        remote_form.addRow("附加 system prompt", self.translation_system_prompt)
        remote_form.addRow("每批字幕數", self.translation_batch_size)
        remote_form.addRow("Temperature", self.translation_temperature)
        remote_form.addRow("最大重試", self.translation_retries)
        remote_form.addRow("", self.translation_response_format)
        remote_form.addRow("OpenRouter HTTP-Referer", self.translation_http_referer)
        remote_form.addRow("OpenRouter X-Title", self.translation_app_title)
        self.translation_remote_group = QGroupBox("遠端翻譯 API")
        self.translation_remote_group.setLayout(remote_form)
        layout.addWidget(self.translation_remote_group)

        self.translation_mode.currentIndexChanged.connect(self._update_translation_state)
        self.translation_provider.currentIndexChanged.connect(self._fill_translation_defaults)
        self._update_translation_state()
        layout.addStretch(1)
        return page

    def _build_speech_tab(self, settings: AppSettings) -> QWidget:
        page, layout = _scroll_page()
        speech = settings.speech

        self.speech_enabled = QCheckBox("啟用 TTS 語音合成")
        self.speech_enabled.setChecked(speech.enabled)
        self.speech_mode = QComboBox()
        self.speech_mode.addItem("本機 Qwen3-TTS（qwen3tts-rs / CUDA）", "local")
        self.speech_mode.addItem("遠端 API", "remote")
        _select_data(self.speech_mode, speech.mode)

        self.speech_provider = _provider_combo(
            ProviderCapability.SPEECH,
            speech.provider_id,
        )
        self.speech_credential = QLineEdit(speech.credential_id)
        self.speech_api_key = _secret_edit("留空保留既有金鑰")
        self.speech_url = QLineEdit(speech.base_url)
        self.speech_url.setPlaceholderText("留空使用供應商預設位址")
        self.speech_model = QLineEdit(speech.model)
        self.speech_voice = QLineEdit(speech.voice)
        self.speech_language = QLineEdit(speech.language)
        self.speech_format = QComboBox()
        for value in ("wav", "mp3", "pcm", "aac", "flac", "opus"):
            self.speech_format.addItem(value, value)
        _select_data(self.speech_format, speech.output_format)
        self.speech_speed = _double_spin(0.25, 4.0, speech.speed, 0.05)
        self.speech_instructions = QLineEdit(speech.instructions)
        self.speech_max_chars = _spin(100, 10_000, speech.max_chars_per_request)
        self.speech_http_referer = QLineEdit(speech.http_referer)
        self.speech_app_title = QLineEdit(speech.app_title)

        common_form = QFormLayout()
        common_form.addRow("", self.speech_enabled)
        common_form.addRow("模式", self.speech_mode)
        common_form.addRow("模型", self.speech_model)
        common_form.addRow("Voice / Speaker", self.speech_voice)
        common_form.addRow("語言", self.speech_language)
        common_form.addRow("輸出格式", self.speech_format)
        common_form.addRow("語速", self.speech_speed)
        common_form.addRow("聲線指令", self.speech_instructions)
        common_form.addRow("單次最大字元", self.speech_max_chars)
        common_group = QGroupBox("共同設定")
        common_group.setLayout(common_form)
        layout.addWidget(common_group)

        self.local_tts_executable = QLineEdit(speech.local_executable)
        self.local_tts_executable.setPlaceholderText("留空自動搜尋 runtime/tts 或 PATH")
        self.local_tts_model_dir = QLineEdit(speech.local_model_dir)
        self.local_tts_model_dir.setPlaceholderText("留空自動搜尋 runtime/models/tts")
        self.local_tts_backend = QComboBox()
        self.local_tts_backend.addItem("Candle", "candle")
        self.local_tts_backend.addItem("Python fallback", "python")
        _select_data(self.local_tts_backend, speech.local_backend)
        self.local_talker_backend = QComboBox()
        self.local_talker_backend.addItem("SafeTensors", "safetensors")
        self.local_talker_backend.addItem("GGUF", "gguf")
        _select_data(self.local_talker_backend, speech.local_talker_backend)
        self.local_talker_gguf = QLineEdit(speech.local_talker_gguf)

        local_form = QFormLayout()
        local_form.addRow("qwen3tts-rs 執行檔", self.local_tts_executable)
        local_form.addRow("模型目錄", self.local_tts_model_dir)
        local_form.addRow("執行後端", self.local_tts_backend)
        local_form.addRow("Talker 權重後端", self.local_talker_backend)
        local_form.addRow("Talker GGUF", self.local_talker_gguf)
        self.speech_local_group = QGroupBox("本機 Qwen3-TTS")
        self.speech_local_group.setLayout(local_form)
        layout.addWidget(self.speech_local_group)

        remote_form = QFormLayout()
        remote_form.addRow("供應商", self.speech_provider)
        remote_form.addRow("Credential 名稱", self.speech_credential)
        remote_form.addRow("API 金鑰", self.speech_api_key)
        remote_form.addRow("API base URL", self.speech_url)
        remote_form.addRow("OpenRouter HTTP-Referer", self.speech_http_referer)
        remote_form.addRow("OpenRouter X-Title", self.speech_app_title)
        self.speech_remote_group = QGroupBox("遠端 TTS")
        self.speech_remote_group.setLayout(remote_form)
        layout.addWidget(self.speech_remote_group)

        note = QLabel(
            "本機 ASR 與翻譯由 llama.cpp 使用 CUDA；Qwen3-TTS 由 qwen3tts-rs/Candle 使用 CUDA，"
            "不會把 TTS 誤標為 llama.cpp 功能。"
        )
        note.setWordWrap(True)
        note.setProperty("muted", True)
        layout.addWidget(note)

        self.speech_enabled.toggled.connect(self._update_speech_state)
        self.speech_mode.currentIndexChanged.connect(self._update_speech_state)
        self.speech_provider.currentIndexChanged.connect(self._fill_speech_defaults)
        self._update_speech_state()
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------------
    # Dynamic controls
    # ------------------------------------------------------------------

    def _update_asr_enabled_state(self, _checked: bool | None = None) -> None:
        remote = self.remote_stt_enabled.isChecked()
        for widget in (
            self.stt_provider,
            self.stt_credential,
            self.stt_api_key,
            self.stt_base_url,
            self.stt_model,
            self.stt_language,
            self.stt_prompt,
            self.stt_response_format,
            self.stt_temperature,
            self.stt_max_mib,
            self.stt_http_referer,
            self.stt_app_title,
        ):
            widget.setEnabled(remote)

    def _update_translation_state(self, _index: int | None = None) -> None:
        mode = str(self.translation_mode.currentData())
        self.translation_local_group.setEnabled(mode == "local")
        self.translation_remote_group.setEnabled(mode == "remote")

    def _update_speech_state(self, _value: Any = None) -> None:
        enabled = self.speech_enabled.isChecked()
        local = str(self.speech_mode.currentData()) == "local"
        self.speech_mode.setEnabled(enabled)
        self.speech_local_group.setEnabled(enabled and local)
        self.speech_remote_group.setEnabled(enabled and not local)
        for widget in (
            self.speech_model,
            self.speech_voice,
            self.speech_language,
            self.speech_format,
            self.speech_speed,
            self.speech_instructions,
            self.speech_max_chars,
        ):
            widget.setEnabled(enabled)

    def _fill_stt_defaults(self, _index: int) -> None:
        profile = get_provider_profile(str(self.stt_provider.currentData()))
        self.stt_base_url.setText(profile.audio_base_url or profile.base_url)
        self.stt_model.setText(profile.default_stt_model)
        self.stt_credential.setText(profile.id)

    def _fill_translation_defaults(self, _index: int) -> None:
        profile = get_provider_profile(str(self.translation_provider.currentData()))
        self.translation_url.setText(profile.translation_base_url or profile.base_url)
        self.translation_model.setText(profile.default_translation_model)
        self.translation_credential.setText(profile.id)
        self.translation_response_format.setChecked(profile.translation_response_format)

    def _fill_speech_defaults(self, _index: int) -> None:
        profile = get_provider_profile(str(self.speech_provider.currentData()))
        self.speech_url.setText(profile.audio_base_url or profile.base_url)
        self.speech_model.setText(profile.default_tts_model)
        self.speech_credential.setText(profile.id)
        self.speech_voice.setText(profile.default_voice)
        self.speech_language.setText(profile.default_language)
        supported = profile.supported_tts_formats
        if supported and str(self.speech_format.currentData()) not in supported:
            _select_data(self.speech_format, supported[0])

    # ------------------------------------------------------------------
    # Values
    # ------------------------------------------------------------------

    def build_settings(self) -> AppSettings:
        """Build validated non-secret settings; constructors perform validation."""

        engine: EngineSettings = replace(
            self._original.engine,
            mode=str(self.asr_mode.currentData()),
            backend=str(self.asr_backend.currentData()),
            gateway_url=self.gateway_url.text().strip(),
            credential_id=self.gateway_credential.text().strip(),
            model_id=self.asr_model.text().strip(),
            threads=self.asr_threads.value(),
            context_size=self.asr_context.value(),
        )
        remote_stt: RemoteTranscriptionSettings = replace(
            self._original.remote_transcription,
            enabled=self.remote_stt_enabled.isChecked(),
            provider_id=str(self.stt_provider.currentData()),
            credential_id=self.stt_credential.text().strip(),
            base_url=self.stt_base_url.text().strip(),
            model=self.stt_model.text().strip(),
            language=self.stt_language.text().strip(),
            prompt=self.stt_prompt.text().strip(),
            response_format=str(self.stt_response_format.currentData()),
            temperature=self.stt_temperature.value(),
            max_file_bytes=self.stt_max_mib.value() * 1024 * 1024,
            http_referer=self.stt_http_referer.text().strip(),
            app_title=self.stt_app_title.text().strip(),
        )

        translation_mode = str(self.translation_mode.currentData())
        translation: TranslationSettings = replace(
            self._original.translation,
            enabled=translation_mode == "remote",
            provider_id=str(self.translation_provider.currentData()),
            credential_id=self.translation_credential.text().strip(),
            base_url=self.translation_url.text().strip(),
            model=self.translation_model.text().strip(),
            source_language=self.source_language.text().strip(),
            target_language=self.target_language.text().strip(),
            style=self.translation_style.text().strip(),
            system_prompt=self.translation_system_prompt.text().strip(),
            batch_size=self.translation_batch_size.value(),
            temperature=self.translation_temperature.value(),
            max_retries=self.translation_retries.value(),
            use_response_format=self.translation_response_format.isChecked(),
            http_referer=self.translation_http_referer.text().strip(),
            app_title=self.translation_app_title.text().strip(),
        )
        translation_engine: TranslationEngineSettings = replace(
            self._original.translation_engine,
            enabled=translation_mode == "local",
            backend=str(self.translation_backend.currentData()),
            model_id=self.translation_local_model.text().strip(),
            model_repository=self.translation_repository.text().strip(),
            model_quant=self.translation_quant.text().strip(),
            port=self.translation_port.value(),
            threads=self.translation_threads.value(),
            context_size=self.translation_context.value(),
            gpu_layers=self.translation_gpu_layers.value(),
        )

        speech_mode = str(self.speech_mode.currentData())
        speech_provider_id = str(self.speech_provider.currentData())
        speech_profile = get_provider_profile(speech_provider_id)
        speech_model = self.speech_model.text().strip()
        speech_voice = self.speech_voice.text().strip()
        speech_language = self.speech_language.text().strip()
        speech_format = str(self.speech_format.currentData())
        if speech_mode == "remote":
            speech_model = speech_model or speech_profile.default_tts_model
            speech_voice = speech_voice or speech_profile.default_voice
            speech_language = speech_language or speech_profile.default_language
            if (
                speech_profile.supported_tts_formats
                and speech_format not in speech_profile.supported_tts_formats
            ):
                speech_format = speech_profile.supported_tts_formats[0]
        else:
            speech_format = "wav"

        speech: SpeechSettings = replace(
            self._original.speech,
            enabled=self.speech_enabled.isChecked(),
            mode=speech_mode,
            provider_id=speech_provider_id,
            credential_id=self.speech_credential.text().strip(),
            base_url=self.speech_url.text().strip(),
            model=speech_model,
            voice=speech_voice,
            language=speech_language,
            output_format=speech_format,
            speed=self.speech_speed.value(),
            instructions=self.speech_instructions.text().strip(),
            max_chars_per_request=self.speech_max_chars.value(),
            local_executable=self.local_tts_executable.text().strip(),
            local_model_dir=self.local_tts_model_dir.text().strip(),
            local_backend=str(self.local_tts_backend.currentData()),
            local_talker_backend=str(self.local_talker_backend.currentData()),
            local_talker_gguf=self.local_talker_gguf.text().strip(),
            http_referer=self.speech_http_referer.text().strip(),
            app_title=self.speech_app_title.text().strip(),
        )
        return replace(
            self._original,
            engine=engine,
            remote_transcription=remote_stt,
            translation=translation,
            translation_engine=translation_engine,
            speech=speech,
        )

    def secret_values(self) -> dict[str, str]:
        """Return only newly entered secrets, keyed by credential id."""

        result: dict[str, str] = {}
        for credential, edit in (
            (self.gateway_credential.text().strip(), self.gateway_key),
            (self.stt_credential.text().strip(), self.stt_api_key),
            (self.translation_credential.text().strip(), self.translation_api_key),
            (self.speech_credential.text().strip(), self.speech_api_key),
        ):
            value = edit.text()
            if credential and value:
                result[credential] = value
        return result


def _scroll_page() -> tuple[QWidget, QVBoxLayout]:
    outer = QWidget()
    outer_layout = QVBoxLayout(outer)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    content = QWidget()
    layout = QVBoxLayout(content)
    layout.setAlignment(Qt.AlignmentFlag.AlignTop)
    scroll.setWidget(content)
    outer_layout.addWidget(scroll)
    return outer, layout


def _backend_combo(value: str) -> QComboBox:
    combo = QComboBox()
    combo.addItem("NVIDIA CUDA", "cuda")
    combo.addItem("Vulkan", "vulkan")
    combo.addItem("CPU", "cpu")
    _select_data(combo, value)
    return combo


def _provider_combo(
    capability: ProviderCapability,
    value: str,
    *,
    exclude: set[str] | None = None,
) -> QComboBox:
    combo = QComboBox()
    excluded = exclude or set()
    for profile in profiles_for(capability):
        if profile.id not in excluded:
            combo.addItem(profile.label, profile.id)
    if combo.findData(value) < 0 and value in PROVIDER_PROFILES:
        profile = PROVIDER_PROFILES[value]
        if profile.supports(capability) and profile.id not in excluded:
            combo.addItem(profile.label, profile.id)
    _select_data(combo, value)
    return combo


def _spin(minimum: int, maximum: int, value: int) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(minimum, maximum)
    spin.setValue(max(minimum, min(maximum, value)))
    return spin


def _double_spin(
    minimum: float,
    maximum: float,
    value: float,
    step: float,
) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(minimum, maximum)
    spin.setDecimals(2)
    spin.setSingleStep(step)
    spin.setValue(max(minimum, min(maximum, value)))
    return spin


def _secret_edit(placeholder: str) -> QLineEdit:
    edit = QLineEdit()
    edit.setEchoMode(QLineEdit.EchoMode.Password)
    edit.setPlaceholderText(placeholder)
    return edit


def _select_data(combo: QComboBox, value: Any) -> None:
    index = combo.findData(value)
    combo.setCurrentIndex(index if index >= 0 else 0)


def _fill_if_empty(edit: QLineEdit, value: str) -> None:
    if not edit.text().strip() and value:
        edit.setText(value)
