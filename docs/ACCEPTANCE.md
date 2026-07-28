# HearFlow Studio 驗收紀錄

本文件區分「已取得的證據」與「尚未宣稱」，避免用編譯通過代替可用性。

## 已取得的上游與 ASR 證據（2026-07-28）

| 驗收項目 | 結果 | 證據 |
|---|---|---|
| 上游封裝驗證 | PASS | `scripts\validate-package.ps1` |
| Rust 格式 | PASS | `cargo fmt --all -- --check` |
| Rust 全 targets 編譯 | PASS | `cargo check --all-targets --locked` |
| Rust 測試 | PASS | 25 unit + 13 integration，共 38 tests |
| llama.cpp CUDA 模型載入 | PASS | llama.cpp `b10155`，Qwen3-ASR 0.6B Q8_0 |
| Gateway readiness | PASS | `/health/ready` 回傳 `ready`、HTTP 200 |
| 真實中文音訊轉錄 | PASS | 官方 `samples\asr_zh.wav`，HTTP 200 |
| 轉錄語句 | PASS | `甚至出现交易几乎停滞的情况。` |
| 時間精度標示 | PASS | 回應明示 `timestamp_accuracy=chunk` |

該次真實回應已保存於開發 runtime 的 `runtime\e2e`；此資料夾是驗收證據，
不會被帶進正式可攜包。

## 安裝與封裝腳本證據（2026-07-28）

| 驗收項目 | 結果 | 證據 |
|---|---|---|
| 四支 PowerShell 腳本語法 | PASS | PowerShell AST parser：`build`、`install-engine`、`run`、`verify` |
| CPU 安全預演 | PASS | 固定 CPU 資產 URL、模型 URL、FFmpeg fallback，無寫入 |
| CUDA 12.4 安全預演 | PASS | 主套件與 cudart 兩個固定資產，無寫入 |
| Vulkan 安全預演 | PASS | 固定 Vulkan 資產 URL，無寫入 |
| CUDA 12.4 實際 runtime 安裝 | PASS | llama-server、Gateway、模型、mmproj、FFmpeg、FFprobe 全部就位 |
| Windows PowerShell 5.1 安裝 | PASS | 實際完成 CUDA 12.4 安裝；不依賴 `Get-FileHash`、`Path.GetRelativePath` 或 `utf8NoBOM` |
| 下載完整性 | PASS | GitHub release 官方 digest 與 Hugging Face blob SHA-256 全相符 |
| runtime manifest | PASS | 六個必要檔案重新計算 SHA-256 全相符；`contains_credentials=false` |
| FFmpeg 啟動 | PASS | 封裝位置的 FFmpeg 8.1.1 與 FFprobe 8.1.1 實際執行 `-version` |
| 含 runtime build 預演 | PASS | 輸入、必要目錄與六筆 manifest SHA-256 全部通過 |
| app-only build 預演 | PASS | `-SkipEngineRuntime -DryRun` |

## 應用程式與真實工作流證據（2026-07-28）

| 驗收項目 | 結果 | 證據 |
|---|---|---|
| Python 自動測試 | PASS | 19 tests；資料庫/CAS、Gateway、翻譯、字幕、取消、工作流、UI smoke |
| Ruff 與 compileall | PASS | 格式、靜態檢查與 Python bytecode 編譯無錯誤 |
| 受管理引擎啟動 | PASS | llama-server 與 Gateway 由應用程式管理器啟動，live/ready/models 均成功 |
| 受管理引擎停止 | PASS | 兩個子程序退出，管理 manifest 移除 |
| 真實完整工作流 | PASS | 建立專案、匯入、Qwen3-ASR、清理、QA、五種字幕輸出與兩份報告 |
| 不翻譯的隱私邊界 | PASS | 工作狀態 `completed`、翻譯狀態 `skipped`，沒有呼叫外部翻譯 provider |
| SQLite 持久化 | PASS | `PRAGMA integrity_check=ok`；片段、七筆 artifact 與完成狀態可重開讀取 |
| ASS 壓製 MP4 | PASS | 新 MP4 含一條影像與一條音訊流；來源雜湊不變、無殘留暫存檔 |

真實工作流證據位於開發 runtime 的
`runtime\e2e\HearFlow 真實驗收 02 managed`。此資料夾不會帶入可攜包。

## 正式可攜包證據（2026-07-28）

| 驗收項目 | 結果 | 證據 |
|---|---|---|
| PyInstaller Windows EXE | PASS | `HearFlowStudio.exe` 啟動後有 PID、非零 HWND 與正確繁中標題 |
| 完整離線 ZIP | PASS | 內含 EXE、CUDA runtime、Gateway、兩個 GGUF、FFmpeg、FFprobe 與 manifest |
| 封裝 allowlist | PASS | ZIP 不含 downloads、logs、e2e、install-staging |
| ZIP 完整性 | PASS | 現算 SHA-256 與隨附 `.zip.sha256`、`release-manifest.json` 相同 |
| 全新繁中路徑解壓 | PASS | 解壓至 `runtime\e2e\正式可攜版 驗收` 後直接啟動 |
| 125% DPI 版面 | PASS | 1536×801 邏輯尺寸，水平與垂直捲動最大值皆為 0，三欄完整顯示 |
| 1280×720 版面 | PASS | 水平捲動最大值為 0；高度不足時使用垂直捲動，控制項仍可到達 |
| 已含 runtime 的快速安裝 | PASS | 不重複下載，直接啟動受管理引擎 |

## 安裝與封裝門檻

下列門檻應在每次正式版本重新執行，不能沿用舊版結果：

```powershell
.\scripts\install-engine.ps1 -Backend cpu -DryRun
.\scripts\verify.ps1
.\scripts\build.ps1 -Version 0.1.0
```

必須確認：

- PowerShell 腳本可由 AST parser 無錯誤解析。
- `runtime\manifest.json` 內六個必要檔案均存在，SHA-256 相符。
- ZIP 內含 engine、gateway、models、ffmpeg，且不含 downloads、logs、e2e。
- `.zip.sha256` 與現算 archive SHA-256 一致。
- 解壓至含空白及繁體中文的路徑仍能啟動。
- 打包 EXE 有 PID、非零 HWND、正確視窗標題及可讀截圖證據。
- 從打包 EXE 建立專案、匯入短音檔、轉錄、校訂、QA、輸出 SRT 可完成。
- 關閉並重開專案後，工作與校訂內容仍存在。

## 使用者驗收流程

1. 將可攜 ZIP 解壓到新的繁體中文路徑。
2. 核對 ZIP SHA-256。
3. 啟動 `HearFlowStudio.exe`，確認七個頁籤都可切換且控制項無重疊。
4. 建立新專案並加入一支含語音的短影音。
5. 啟動本機引擎，等待 ready，不接受只有程序存在但 health 未 ready。
6. 轉錄後核對原文、語言、片段時間和 raw JSON。
7. 編輯一段文字，關閉再開啟專案，確認編輯仍在。
8. 不設定翻譯 provider 時，狀態應為 `skipped`，不可顯示成功或虛構譯文。
9. 設定真實 provider 時，必須用真實端點完成翻譯；未提供憑證不得宣稱通過。
10. 輸出 SRT、VTT、ASS、JSON 與報告，確認 UTF-8、時間順序及檔案可讀。
11. 使用 ASS 壓製新 MP4，確認原始媒體未覆寫。
12. 轉錄中取消：受管理引擎可中止／重啟；外部 Gateway 只停止本機等待並明示。

## 誠實邊界

- 目前 ASR 回傳的是分片級時間，不能驗收為逐字對齊。
- V1 不做說話者分離。
- 轉錄是批次完成後回傳，不能驗收為即時串流。
- 翻譯 provider 必須另有有效服務與憑證；build/test 不代表 provider 已驗證。
- 外部 Gateway 的遠端取消能力不在本應用程式控制範圍。
- CUDA／Vulkan 可用性必須在交付目標電腦實測，不能只由開發機結果推論。

## 尚未宣稱

在沒有對應證據前，不宣稱下列項目已通過：

- 所有 GPU／驅動組合
- 離線翻譯模型品質
- 逐字級時間精度
- 說話者辨識
- 超過 Gateway 限制的單檔長度與大小
