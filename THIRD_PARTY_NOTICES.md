# Third-party notices

HearFlow Studio 原始碼套件不含模型權重。正式可攜版可由使用者執行
`scripts/install-engine.ps1` 下載並納入固定版本的本機 runtime；建置者對
再散布內容與授權負責。

| Component | 固定來源 | License / distribution note |
|---|---|---|
| Qwen3-ASR Gateway | `stevenke1981/qwen3-asr-llama-cpp` commit `363b60618e4029977d6492d4f41d852638740a43` | MIT；vendored source 保留原始 LICENSE |
| llama.cpp | `ggml-org/llama.cpp` release `b10155` | MIT；Windows binary 由官方 GitHub release 下載 |
| Qwen3-ASR model | `ggml-org/Qwen3-ASR-0.6B-GGUF`，Q8_0 主模型與 mmproj | 轉換 repo 的 model card 指向 `Qwen/Qwen3-ASR-0.6B`；基礎模型目前標示 Apache-2.0。再散布前仍需核對當下 model card 與使用限制 |
| Qt / PySide6 | Qt for Python | LGPLv3/GPLv3/commercial; the portable one-folder build keeps Qt DLLs dynamically replaceable |
| FFmpeg / FFprobe | llama.cpp 套件內工具，或安裝電腦 PATH 上的 build | 授權取決於實際 build configuration；安裝器找到工具不等於自動取得再散布許可 |
| httpx | Encode OSS Ltd. | BSD-3-Clause |
| keyring | Python Packaging Authority | MIT |
| PyInstaller | PyInstaller Development Team | GPL with bootloader exception |

正式套件必須：

1. 將實際納入依賴的完整授權文字複製到 `licenses/`。
2. 保留 `runtime/manifest.json` 的檔案 SHA-256。
3. 產生整包 ZIP 的 `.sha256`。
4. 依所選後端重新核對 CUDA、Vulkan、MSVC runtime 與 FFmpeg 的再散布條款。

安裝腳本與 runtime manifest 不寫入 API keys；使用者憑證由應用程式及作業
系統憑證庫處理。
