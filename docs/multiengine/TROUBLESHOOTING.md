# 疑難排解

## `apply_upgrade.py` 回報 source anchor 不符

代表目前工作樹已經修改過同一段程式碼，升級器無法證明自動合併安全。先提交或備份現有變更，再查看錯誤指出的檔案。不要用文字編輯器盲目重複貼上；以升級包 payload 與 `docs/AUDIT.md` 做人工三方合併。

## ASR 顯示模型不存在

確認：

```text
runtime/models/asr/Qwen3-ASR-*.gguf
runtime/models/asr/*mmproj*.gguf
```

或重新執行：

```powershell
.\scripts\install-engine.ps1 -Backend cuda12.4 -InstallTranslationModel
```

舊版平面目錄仍可用，但檔名必須明確含 ASR。

## 本機翻譯 401

升級後不應再手動設定本機翻譯 API key。關閉舊程式與殘留 llama-server，重新啟動 HearFlow，再由 UI 啟動翻譯引擎。若仍有問題，查看 `runtime/logs/managed-translation.log`，但不要把可能含敏感內容的完整 log 公開貼出。

## CUDA 沒有使用 GPU

```powershell
nvidia-smi
nvcc --version
py -3.12 .\verify_upgrade.py . --strict-runtime --json
```

並確認設定後端是 CUDA、安裝器使用 `-Backend cuda12.4`。翻譯引擎的 GPU layers 應大於 0。

## `install-tts.ps1` 找不到 `nvcc.exe`

安裝 NVIDIA CUDA Toolkit，重新啟動 PowerShell，再確認 `nvcc --version`。只有執行既有 CUDA binary 時不必有 nvcc；從原始碼首次編譯 qwen3tts-rs 時需要。

## 遠端 API 401／403

- Credential 名稱必須與儲存 key 時相同。
- Provider ID 與 Credential ID 可以不同。
- Gemini native audio 使用 `x-goog-api-key`；文字 compatibility 使用 Bearer，adapter 會自動處理。
- Sakana endpoint 必須使用帳戶配發位址。

## OpenRouter TTS 拒絕 WAV

目前 profile 只允許 `mp3`、`pcm`。UI 切換 OpenRouter 時會自動改成支援格式。需要 WAV 時先輸出 PCM，再用 FFmpeg 轉換，或改用 OpenAI／Gemini。

## 遠端檔案太大

目前版本會明確拒絕超過設定上限的單檔，不會偷偷截斷。先以 FFmpeg 切段，或使用本機 Qwen3-ASR。自動切段與 resumable upload 列入後續版本。
