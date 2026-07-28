# Upstream revision record

## qwen3-asr-llama-cpp

- Source: <https://github.com/stevenke1981/qwen3-asr-llama-cpp>
- Pinned commit: `363b60618e4029977d6492d4f41d852638740a43`
- Vendored on: 2026-07-28
- Upstream package validation: passed
- `cargo fmt --all -- --check`: passed
- `cargo check --all-targets --locked`: passed with local Rust stable
- `cargo test --all-targets --locked`: 38 passed

The upstream repository declares Rust 1.85+, but its locked dev dependency
`wiremock 0.6.5` no longer compiles on the upstream CI's Rust 1.85.1. HearFlow
therefore requires Rust 1.88+ only when rebuilding the vendored Gateway.
Packaged users do not need Rust.

## llama.cpp runtime

- Candidate release: `b10155`
- Runtime must be pinned in the generated runtime manifest only after real CPU
  and CUDA Qwen3-ASR smoke tests pass.
- Copy the complete extracted runtime directory; `llama-server.exe` alone is
  insufficient because it depends on llama, mtmd, GGML, backend, and runtime
  DLLs.

