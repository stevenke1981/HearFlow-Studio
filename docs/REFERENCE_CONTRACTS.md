# 既有參照展開

此文件是 `on_reference_detected_hook` 的輸出。產品程式只能依此處已展開且可測試的合約
呼叫外部元件；不直接複製第三方內部實作。

## 1. qwen3-asr-llama-cpp Gateway

- Repository: <https://github.com/stevenke1981/qwen3-asr-llama-cpp>
- 固定整合 commit: `363b60618e4029977d6492d4f41d852638740a43`
- License: MIT（模型、llama.cpp、FFmpeg 另有各自授權）
- 執行邊界：App → Rust Axum Gateway `127.0.0.1:8000` → llama-server
  `127.0.0.1:8080`
- 上游限制：非串流、分片級時間戳、無字詞 forced alignment。

本機已以目前 stable Rust 通過 `validate-package.ps1`、Cargo fmt/check 及 38 項測試。
上游宣告 MSRV 1.85，但 dev dependency `wiremock 0.6.5` 已讓 Rust 1.85 CI 失敗；
HearFlow 的開發／建置環境因此明確要求 Rust 1.88+，正式使用者不需要 Rust。

llama.cpp Windows runtime 候選固定 `b10155`，只有在 CPU/CUDA 中文樣本 E2E 通過後才
寫入 release runtime manifest；不可在執行時無條件追 latest。

### 1.1 健康端點

```http
GET /health/live
GET /health/ready
```

- `live=200`：Gateway 行程存在。
- `ready=200`：Gateway 亦可連到 llama-server。
- `ready=503`：llama-server 未啟動、載入中或不可達。

### 1.2 模型與語言

```http
GET /v1/models
Authorization: Bearer {API_KEY}

GET /v1/audio/languages
Authorization: Bearer {API_KEY}
```

App 不硬編造 loaded model；顯示值來自此端點與本機 managed engine 設定。

### 1.3 轉錄

```http
POST /v1/audio/transcriptions
Authorization: Bearer {API_KEY}
Content-Type: multipart/form-data
```

完整欄位：

| 欄位 | 型別 | 必填 | 驗證 |
|---|---|---:|---|
| `file` | binary | 是 | 上游允許且 FFprobe 可讀的影音 |
| `model` | string | 否 | `qwen3-asr`、`whisper-1` 相容別名或 `MODEL_ID` |
| `language` | string | 否 | 預設 `auto` |
| `prompt` | string | 否 | 實驗性，UTF-8 最多 4096 bytes |
| `response_format` | enum | 否 | `json`, `verbose_json`, `text`, `srt`, `vtt` |
| `temperature` | float | 否 | 0.0 到 1.0 |

HearFlow 固定要求 `verbose_json`，再由自己的 canonical segments 產生所有格式，避免
不同格式各呼叫一次模型。

完整 `verbose_json` shape：

```json
{
  "task": "transcribe",
  "language": "zh",
  "duration": 12.345,
  "text": "完整文字",
  "segments": [
    {
      "id": 0,
      "seek": 0,
      "start": 0.0,
      "end": 12.345,
      "text": "完整文字"
    }
  ],
  "timestamp_accuracy": "chunk"
}
```

回應標頭：

```text
X-ASR-Model: qwen3-asr-0.6b
X-ASR-Chunks: 1
X-ASR-Timestamp-Accuracy: chunk
```

Python 使用範例：

```python
from pathlib import Path

import httpx


def transcribe_example(
    base_url: str,
    api_key: str,
    media_path: Path,
) -> dict[str, object]:
    """Call the upstream Gateway once and return verbose transcription JSON."""
    with media_path.open("rb") as stream:
        response = httpx.post(
            f"{base_url.rstrip('/')}/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (media_path.name, stream, "application/octet-stream")},
            data={
                "model": "qwen3-asr",
                "language": "auto",
                "response_format": "verbose_json",
                "temperature": "0",
            },
            timeout=httpx.Timeout(connect=10, read=3600, write=300, pool=10),
        )
    response.raise_for_status()
    payload: dict[str, object] = response.json()
    return payload
```

建議合約測試：

1. `200`、`text` 非空、segments 時間遞增、`timestamp_accuracy=chunk`。
2. 無/錯 key 為 `401`；App 日誌不得含 key。
3. ready 503、轉錄 502、400、408、413、429 都轉成可操作錯誤。
4. `X-ASR-Chunks > 1` 時 segment 數與時間範圍合理。
5. 無效 JSON 或缺少必要欄位不得寫入 canonical SQLite。

## 2. 上游 managed engine 行程

### 2.0 模型 manifest

| 模型 | Hugging Face revision | 主 GGUF | mmproj | 合計 |
|---|---|---:|---:|---:|
| 0.6B Q8_0 | `928ab958…` | 804,749,248 B | 214,392,480 B | 1,019,141,728 B |
| 1.7B Q8_0 | `36a67868…` | 2,165,034,944 B | 355,709,344 B | 2,520,744,288 B |

正式下載器必須使用 `.partial`、續傳、大小與 SHA-256 驗證、原子改名；檔案存在但大小
或雜湊不符時不得標記已安裝。

### 2.1 llama-server

上游 PowerShell 啟動參數語意：

```powershell
llama-server `
  --host 127.0.0.1 `
  --port 8080 `
  --alias qwen3-asr-0.6b `
  --ctx-size 8192 `
  --batch-size 2048 `
  --ubatch-size 512 `
  --threads 4 `
  --threads-batch 4 `
  --parallel 1 `
  --n-gpu-layers 999 `
  --timeout 3600 `
  --jinja --metrics --no-webui `
  -hf ggml-org/Qwen3-ASR-0.6B-GGUF:Q8_0 `
  --flash-attn on `
  --api-key $InternalKey
```

本機 managed 模式強制 host 為 `127.0.0.1`。GPU 不可用時改為
`--n-gpu-layers 0 --no-mmproj-offload --flash-attn off`。

### 2.2 Gateway

必要 `.env` 語意：

```dotenv
API_KEY=<至少 32 bytes>
BIND_ADDR=127.0.0.1:8000
LLAMA_URL=http://127.0.0.1:8080
LLAMA_API_KEY=<另一組至少 32 bytes>
MODEL_ID=qwen3-asr-0.6b
MAX_CONCURRENT_JOBS=1
CHUNK_SECONDS=90
FFMPEG_BIN=<完整 ffmpeg 路徑>
FFPROBE_BIN=<完整 ffprobe 路徑>
```

兩組 key 必須不同。`.env` 位於 runtime，不放入原始碼、zip 診斷或專案。

### 2.3 真取消

managed engine 的 llama-server 與 Gateway 必須被放在 App 專屬行程樹／Windows Job
Object 中。強制取消目前 generation 時：

1. 將 SQLite active attempt 標記為 `cancelling`。
2. 終止該 generation 的整個 managed 行程樹。
3. generation 加一。
4. 舊 HTTP 回應即使晚到，也因 attempt/generation CAS 被拒絕。
5. 重新啟動 managed engine；ready 後才允許下一份工作。

外部 Gateway 沒有可用取消合約，因此只取消 App 等待與後續排隊。

## 3. FFprobe / FFmpeg

### 3.1 探測

```powershell
ffprobe -v error -print_format json -show_format -show_streams -- "<media>"
```

程式只使用 argv list，不經 shell 字串拼接。驗證：

- 程序 exit code 0。
- JSON 可解析。
- 至少一個 `codec_type=audio` stream。
- duration 為有限且大於 0 的數字。

### 3.2 ASS 壓製

```powershell
ffmpeg -hide_banner -y -i "<input>" -vf "ass=<escaped-ass-path>" `
  -map 0:v:0 -map 0:a:0? -c:v libx264 -crf 18 -preset medium `
  -c:a copy -movflags +faststart "<new-output.mp4>"
```

- 輸出永不等於輸入。
- Windows filter path 需依 FFmpeg filter 規則 escape，不能只做 shell quote。
- 壓製後以 FFprobe 確認 video stream、duration 與非零檔案。
- 輸入前後 SHA-256 必須相同。

## 4. OpenAI-compatible Chat Completions

```http
POST {base_url}/chat/completions
Authorization: Bearer {provider_key}
Content-Type: application/json
```

完整 request shape：

```json
{
  "model": "provider-model",
  "temperature": 0.2,
  "response_format": {"type": "json_object"},
  "messages": [
    {
      "role": "system",
      "content": "翻譯規則與詞彙表"
    },
    {
      "role": "user",
      "content": "{\"segments\":[{\"id\":\"seg-1\",\"text\":\"Hello\"}]}"
    }
  ]
}
```

接受 response：

```json
{
  "choices": [
    {
      "message": {
        "content": "{\"translations\":[{\"id\":\"seg-1\",\"text\":\"哈囉\"}]}"
      }
    }
  ]
}
```

provider 相容性規則：

- `base_url` 可為 `https://api.openai.com/v1`、Ollama/LM Studio 相容 `/v1`。
- localhost provider 可選擇空 key；遠端 provider 必須有 Credential Manager key。
- 不支援 `response_format` 的 provider 可關閉該欄，但仍須做嚴格 JSON 解析。
- 容許內容外層有單一 Markdown JSON code fence；不容許缺 id、重複 id、多餘 id。
- 429/5xx 以 bounded exponential backoff 重試；401 不重試。
- 任一批失敗時，成功批次保留；該批狀態為 failed，不標成翻譯完成。

建議測試：

1. 正常 id 對映。
2. Ollama localhost 無 key。
3. code fence JSON。
4. 缺/重複/未知 id 拒絕。
5. 429 後成功、401 立即失敗、5xx 超過上限。
6. 日誌、SQLite、報告與 exception message 都不含 key。

## 5. Windows Credential Manager

由 Python `keyring` 使用 Windows backend：

```python
import keyring


SERVICE_NAME = "HearFlow Studio"


def save_provider_key(provider_id: str, secret: str) -> None:
    """Store one provider key in Windows Credential Manager."""
    if not secret:
        raise ValueError("secret must not be empty")
    keyring.set_password(SERVICE_NAME, provider_id, secret)


def load_provider_key(provider_id: str) -> str | None:
    """Return a provider key without logging or persisting it elsewhere."""
    return keyring.get_password(SERVICE_NAME, provider_id)
```

測試使用記憶體 fake backend，不碰使用者真實 Credential Manager。

## 6. Qt 背景執行

- 任何 HTTP、FFprobe、FFmpeg、下載、Cargo、檔案雜湊都不得在 GUI thread 執行。
- worker 只透過 signal 傳送 immutable event。
- UI 關閉時先要求 controller 停止接受新工作，再依 managed/external 語意處理 active job。
- 不以裝飾性 0–100 模擬上游推論；HTTP 等待顯示 indeterminate。
