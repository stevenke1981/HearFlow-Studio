# 安全設計

## 秘密管理

- API key 不屬於 `AppSettings` dataclass。
- UI 的秘密欄位由 `SettingsDialog.secret_values()` 分離回傳。
- `SecretStore` 使用 keyring；Windows 預設後端為 Credential Manager。
- `SettingsRepository.save()` 遞迴拒絕 `api_key`、`secret`、`password`、`token` 等欄位。
- 專案 event log 與 artifact metadata 不寫入 key。
- 本機 llama-server 的 key 由 process manager 每個 session 隨機產生。

## 本機服務

- 管理型 Gateway 與 llama-server 綁定 `127.0.0.1`。
- ASR public key、ASR internal llama key、翻譯 internal llama key彼此分離。
- 管理器只停止自己建立且能驗證 identity 的子行程。
- 遠端 URL 禁止內嵌 username/password、query 與 fragment。
- HTTP redirect 關閉，避免 Authorization header 被不透明轉送。

## 升級器

- 只接受既定 repository anchors。
- 新 Python 內容在寫入前先做 AST parse。
- 既有檔案先完整備份。
- 寫入使用同目錄 temporary file + `os.replace`。
- 失敗時自動還原。
- 回復工具限制 backup 必須位於 repository 的 `.hearflow-upgrade/backups`，並拒絕 path traversal。

## 部署建議

1. 不要把 Credential Manager 匯出檔或 runtime logs 提交到 Git。
2. 自訂 API base URL 優先使用 HTTPS；只有明確的 loopback 服務使用 HTTP。
3. OpenRouter HTTP-Referer 只填公開產品網址，不填帶 token 的內部 URL。
4. 遠端服務會收到音訊或逐字稿；敏感資料工作應使用本機模式或先完成組織法遵審查。
5. 對外部署 Gateway 時應另外加入 TLS、反向代理、rate limit 與 allowlist；目前 managed mode 設計為單機 loopback。
