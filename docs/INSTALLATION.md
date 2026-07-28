# HearFlow Studio Windows 安裝指南

## 1. 系統需求

- Windows 10/11 x64
- 原始碼執行：Python 3.12 或 3.13
- 建置 Qwen Gateway：Rust 與 Cargo
- 下載階段可連線 GitHub 與 Hugging Face
- 約 2 GiB 以上可用空間（CPU）；CUDA 版本及封裝時需更多

顯示後端建議：

| 後端 | 適合情況 | 注意 |
|---|---|---|
| `cpu` | 最佳相容性、無獨立顯卡 | 速度通常最慢 |
| `cuda12.4` | 支援 CUDA 12.4 的 NVIDIA 顯卡 | 套件較大；驅動需相容 |
| `vulkan` | 支援 Vulkan 的 Intel／AMD／NVIDIA | 效能依驅動與顯卡而異 |

## 2. 安裝固定版本的本機引擎

從專案根目錄執行其中一條：

```powershell
.\scripts\install-engine.ps1 -Backend cpu
.\scripts\install-engine.ps1 -Backend cuda12.4
.\scripts\install-engine.ps1 -Backend vulkan
```

安裝器採固定版本，不會自行追逐最新版：

- llama.cpp `b10155`
- `stevenke1981/qwen3-asr-llama-cpp`
  `363b60618e4029977d6492d4f41d852638740a43`
- `ggml-org/Qwen3-ASR-0.6B-GGUF`
  - `Qwen3-ASR-0.6B-Q8_0.gguf`
  - `mmproj-Qwen3-ASR-0.6B-Q8_0.gguf`

安裝完成後的可攜 runtime：

```text
runtime\
├─ engine\<backend>\       llama.cpp 執行檔與 DLL
├─ gateway\                qwen3-asr-gateway.exe
├─ models\                 主模型與 mmproj
├─ ffmpeg\                 ffmpeg.exe、ffprobe.exe（及必要 DLL）
└─ manifest.json           版本、相對路徑、檔案大小、SHA-256
```

`runtime\downloads` 是可刪除的安裝快取，不會被放進正式可攜包。
`manifest.json` 不含 API key、存取權杖或其他真實秘密。

安裝器內建 GitHub release API 公布的四個 Windows 資產 SHA-256，以及
Hugging Face 兩個模型 blob 的 SHA-256。已存在的下載快取也會重新核對；若
不相符會停止並要求只移除該筆快取，避免執行被替換或損壞的檔案。

### 安裝器的 FFmpeg 規則

1. 若所選 llama.cpp 壓縮包內同時有 `ffmpeg.exe` 與 `ffprobe.exe`，使用該組。
2. 否則解析 PATH 上的兩個工具；WinGet symbolic link 會解析到真正檔案。
3. 複製後實際執行 `-version`，任一工具無法啟動即安裝失敗。

若找不到，先用可信任的 Windows 套件來源安裝 FFmpeg，確認：

```powershell
ffmpeg -version
ffprobe -version
```

再重跑引擎安裝器。FFmpeg 建置組態會影響授權；正式再散布前必須核對選用
build 的完整 license。

### 已有模型的離線／節省下載方式

把兩個檔案放進 `runtime\models`，再執行：

```powershell
.\scripts\install-engine.ps1 -Backend cpu -SkipModelDownload
```

只有兩檔都存在才會成功。安裝完成仍會計算 SHA-256。

已有 release Gateway binary、且不想重建時可使用：

```powershell
.\scripts\install-engine.ps1 -Backend cpu -SkipGatewayBuild
```

此選項仍會確認 vendored 原始碼正是固定 commit，且 binary 必須存在於上游
release 目錄。

### 安全預演

預演不下載、不解壓、不建置、不寫 manifest：

```powershell
.\scripts\install-engine.ps1 -Backend cpu -DryRun
```

## 3. 原始碼啟動

```powershell
.\scripts\run.ps1
```

腳本會建立 `.venv` 並安裝執行期需求。應用程式本身啟動引擎時，會在每次
執行產生或讀取安全保存的公開 Gateway key 與內部 llama-server key；兩者
必須不同且至少 32 bytes。安裝腳本不會預先寫死 key。

預設本機服務只應綁定 loopback：

```text
Gateway      http://127.0.0.1:8000
llama-server http://127.0.0.1:8080
```

若要開放區域網路，需另行處理防火牆、TLS、存取控制與密鑰輪替；這不是預設
安裝行為。

## 4. 使用外部 Gateway

外部模式不需要本機 runtime：

1. 在引擎設定選擇「外部 Gateway」。
2. 輸入 base URL。
3. 在偏好設定輸入本次連線 API key，或設定
   `HEARFLOW_GATEWAY_API_KEY` 環境變數；一般設定檔不會保存此 key。
4. 測試 `/health/ready`。

應用程式不會啟動、重啟或殺掉外部服務。取消外部請求僅停止本機等待，遠端
是否繼續計算由遠端服務決定。

## 5. 建置正式可攜包

含本機引擎：

```powershell
.\scripts\verify.ps1
.\scripts\build.ps1 -Version 0.1.0
```

app-only：

```powershell
.\scripts\build.ps1 -Version 0.1.0 -SkipEngineRuntime
```

含引擎建置若沒有 `runtime\manifest.json` 或缺少必要目錄，會直接失敗。
可先做不改動輸出的完整輸入與雜湊預演：

```powershell
.\scripts\build.ps1 -Version 0.1.0 -DryRun
```

成功後驗證雜湊：

```powershell
$zip = ".\release\HearFlowStudio-0.1.0-win-x64-portable.zip"
(Get-FileHash $zip -Algorithm SHA256).Hash
Get-Content "$zip.sha256"
```

兩者必須一致。可攜包刻意排除：

- `runtime\downloads`
- `runtime\logs`
- `runtime\e2e`
- `runtime\install-staging`
- 原始碼 `.env` 與開發憑證

## 6. 疑難排解

### CUDA 啟動失敗

先確認 NVIDIA 驅動支援 CUDA 12.4。若仍失敗，改用 `cpu` 驗證整體工作流，
再回頭處理 GPU 驅動，避免把顯卡問題誤判為字幕程式問題。

### Vulkan 找不到裝置

更新顯示驅動並確認系統具備 Vulkan runtime。多顯卡電腦的實際裝置選擇由
llama.cpp 與驅動決定。

### `/health/ready` 未就緒

依序確認：

1. `runtime\manifest.json` 中六個檔案都存在且 SHA-256 可重新計算。
2. `ffmpeg.exe -version` 與 `ffprobe.exe -version` 可執行。
3. 模型與 mmproj 檔名沒有交換。
4. 18000、18080 未被其他程式占用。
5. Gateway 與 llama-server 使用不同的 32-byte 以上 key。

### 大檔案轉錄很久

Gateway 是批次工作，不是串流。長影音會先由 FFmpeg 正規化並分片；完成前
不會逐字回傳。先用短音檔做端到端測試，再處理完整節目。

## 7. 移除

關閉 HearFlow 與其管理的兩個引擎程序後，只移除確定的應用程式資料夾即可。
專案資料夾與 Credential Manager 內的密鑰是獨立資料；是否保留應由使用者
決定，不應用寬廣的遞迴刪除命令自動清除。
