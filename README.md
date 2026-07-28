# 聽序 HearFlow Studio

聽序是 Windows 優先的本機字幕工作站。它把影音匯入、Qwen3-ASR 轉錄、
人工校訂、OpenAI-compatible 翻譯、字幕品質檢查、輸出與壓製集中在同一個
專案介面中。

核心 ASR 使用固定版本的
[`qwen3-asr-llama-cpp`](https://github.com/stevenke1981/qwen3-asr-llama-cpp)
Gateway 與 llama.cpp，不會用假資料代替轉錄結果。

## 主要功能

- 專案式 SQLite 保存；應用程式或引擎意外關閉後可恢復
- WAV、MP3、M4A、MP4、WebM、OGG、Opus、FLAC、AAC、MOV、MKV 批次佇列
- 本機 CPU、NVIDIA CUDA 12.4、Vulkan，或外部 Gateway
- 原文／譯文校訂、搜尋取代、片段拆分與合併
- OpenAI、Ollama、LM Studio 等 OpenAI-compatible 翻譯端點
- API key 交由系統憑證庫保存，不寫進專案或 runtime manifest
- TXT、HearFlow JSON、verbose JSON、SRT、WebVTT、ASS
- 字幕 QA、Markdown／JSON 報告與 FFmpeg ASS 壓製

## 最快開始

可攜版解壓後執行：

```text
HearFlowStudio.exe
```

正式可攜包預設已包含選定的本機引擎、Qwen3-ASR 0.6B Q8_0 模型與 FFmpeg。
若拿到的是 app-only 包，請在解壓目錄執行：

```powershell
.\scripts\install-engine.ps1 -Backend cpu
```

開發版：

```powershell
.\scripts\run.ps1
```

首次進入程式後，依下列步驟操作：

0. **快速安裝**：若使用 app-only 包，執行 `.\scripts\install-engine.ps1 -Backend cpu`（或 `cuda12.4`、`vulkan`）安裝本機引擎與模型。正式可攜包已內建，可跳過。
1. **啟動引擎**：在引擎區按「啟動引擎」並等待狀態成為「就緒」。
2. **建立專案**：建立專案並選擇專案資料夾。
3. **加入媒體**：加入影音檔。
4. **掃描與轉錄**：掃描媒體後開始轉錄。
5. **校訂結果**：在「文字校訂」與「字幕時間軸」檢查結果。
6. **翻譯**：視需要翻譯，或明確選擇「不翻譯」。
7. **QA 與輸出**：做全案 QA 後輸出字幕或壓製新的 MP4。

原始媒體及上游 raw JSON 不會被覆寫。

## 安裝本機引擎

安裝器固定使用：

- llama.cpp `b10155`
- Qwen3-ASR Gateway commit
  `363b60618e4029977d6492d4f41d852638740a43`
- `ggml-org/Qwen3-ASR-0.6B-GGUF` 的 Q8_0 主模型與 mmproj

選擇一種後端：

```powershell
.\scripts\install-engine.ps1 -Backend cpu
.\scripts\install-engine.ps1 -Backend cuda12.4
.\scripts\install-engine.ps1 -Backend vulkan
```

CPU 相容性最高；CUDA 12.4 適合相容的 NVIDIA 顯示卡；Vulkan 適合支援
Vulkan 的 Intel、AMD 或 NVIDIA 顯示卡。安裝約需 1 GiB 模型空間，另加所選
llama.cpp 後端及暫存下載空間。CUDA 版本會明顯較大。

安裝器會從 llama.cpp 套件尋找 FFmpeg；該發行套件若未附帶，則複製 PATH
上的 `ffmpeg.exe` 與 `ffprobe.exe`。找不到時會停止並提供明確錯誤，不會建立
看似成功但實際不可用的 runtime。

官方 release 資產與兩個模型檔都有固定的預期 SHA-256；下載快取不符時安裝
會停止，不會繼續執行未知 binary 或權重。

完整步驟、離線方式與疑難排解見
[`docs/INSTALLATION.md`](docs/INSTALLATION.md)。

## 建置可攜版

先安裝 runtime，再驗證與封裝：

```powershell
.\scripts\install-engine.ps1 -Backend cpu
.\scripts\verify.ps1
.\scripts\build.ps1
```

輸出：

```text
dist\HearFlowStudio\HearFlowStudio.exe
release\HearFlowStudio-0.1.0-win-x64-portable.zip
release\HearFlowStudio-0.1.0-win-x64-portable.zip.sha256
release\release-manifest.json
```

可攜包僅帶入 `runtime\engine`、`gateway`、`models`、`ffmpeg` 與
`manifest.json`，不會帶入下載壓縮檔、測試輸出或日誌。

若只需要不含本機模型的外部 Gateway 版本：

```powershell
.\scripts\build.ps1 -SkipEngineRuntime
```

安全預演（會核對 runtime 所有 SHA-256，但不產生 build 輸出）：

```powershell
.\scripts\build.ps1 -DryRun
```

## 驗證

```powershell
.\scripts\verify.ps1
```

上游 Gateway：

```powershell
Set-Location third_party\qwen3-asr-llama-cpp
.\scripts\validate-package.ps1
Set-Location gateway
cargo fmt --all -- --check
cargo check --all-targets --locked
cargo test --all-targets --locked
```

驗收證據與尚未宣稱的範圍見
[`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md)。

## 必須知道的邊界

- Gateway 是批次 ASR，不是即時串流；完整請求完成後才回傳。
- SRT／VTT 時間是 FFmpeg 分片級近似範圍，不是逐字 forced alignment。
- V1 沒有說話者分離。
- `prompt/context` 是實驗功能，不保證像 Whisper prompt 一樣穩定。
- 外部 Gateway 的取消只會停止本機等待；聽序不會擅自終止遠端工作。
- 翻譯需要使用者提供可用端點與憑證；沒有憑證時不會把假翻譯標成成功。

## 授權

HearFlow Studio 原始碼使用 MIT License。模型、Qt、FFmpeg、CUDA 及其他
第三方項目各有自己的授權與再散布條款；詳見
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
