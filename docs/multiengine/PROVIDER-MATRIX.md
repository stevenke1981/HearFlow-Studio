# 遠端供應商能力矩陣

預設值為 2026-07-29 建立升級包時的設定，全部可在 UI 修改。供應商可能新增、改名或淘汰模型，因此正式部署前應以各供應商官方文件與帳戶可用模型為準。

| Provider ID | STT | Translation | TTS | 認證 | 備註 |
|---|---:|---:|---:|---|---|
| `openai` | ✓ | ✓ | ✓ | Bearer | multipart `/audio/transcriptions`、chat completions、`/audio/speech` |
| `xai` | ✓ | ✓ | ✓ | Bearer | STT/TTS 使用 xAI 原生 `/stt`、`/tts`；TTS 語言轉為 BCP-47 |
| `openrouter` | ✓ | ✓ | ✓ | Bearer | STT 使用 base64 JSON；TTS 目前限制 `mp3` 或 `pcm`；支援 HTTP-Referer/X-Title |
| `gemini` | ✓ | ✓ | ✓ | Bearer / x-goog-api-key | 翻譯走 Gemini OpenAI compatibility；STT/TTS 走 native generateContent |
| `sakana` | — | ✓ | — | Bearer | Fugu 為文字 API；endpoint 由 Sakana 配發，必須手動填入 |
| `custom-openai` | 依服務 | 依服務 | 依服務 | Bearer 或 loopback 無 key | 自訂 OpenAI 相容 URL、模型與能力 |
| `local-llama` | — | ✓ | — | session key | 只作為本機翻譯 profile，不顯示成遠端 STT/TTS |

## 內建可編輯預設模型

| Provider | STT | Translation | TTS |
|---|---|---|---|
| OpenAI | `gpt-4o-mini-transcribe` | `gpt-5-mini` | `gpt-4o-mini-tts` |
| xAI | endpoint 決定 | `grok-4.5` | endpoint 決定 |
| OpenRouter | `openai/gpt-4o-mini-transcribe` | `openai/gpt-5-mini` | `openai/gpt-4o-mini-tts-2025-12-15` |
| Gemini | `gemini-3.6-flash` | `gemini-3.6-flash` | `gemini-3.1-flash-tts-preview` |
| Sakana | — | `fugu` | — |

## 官方參考

- OpenAI Audio: `https://platform.openai.com/docs/guides/speech-to-text`、`https://platform.openai.com/docs/guides/text-to-speech`
- xAI Voice: `https://docs.x.ai/docs/guides/voice`
- OpenRouter Audio: `https://openrouter.ai/docs/features/multimodal/audio`
- Gemini Audio / TTS: `https://ai.google.dev/gemini-api/docs/audio`、`https://ai.google.dev/gemini-api/docs/speech-generation`
- Sakana Fugu: `https://sakana.ai/fugu/`
- llama.cpp server: `https://github.com/ggml-org/llama.cpp/tree/master/tools/server`
