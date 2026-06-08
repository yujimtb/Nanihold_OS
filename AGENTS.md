# AGENTS.md

## 応答方針

- 日本語で回答してください。
- ユーザーの意図を読み取り、指示に従ってください。

## 開発環境

このプロジェクトの標準開発環境は WSL + Docker Compose です。

- Windows 側のローカル Python や `.venv` / `.venv-win` は使わないでください。
- Python、pytest、`vsm` CLI などの実行は Docker Compose の `app` サービス内で行ってください。
- `compose.yaml` の `app` サービスを標準の実行環境として扱ってください。
- Codex アプリのシェルが Windows PowerShell の場合は、`powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-dev.ps1 ...` を入口にしてください。
- Codex アプリから開発環境を起動する場合は `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-dev.ps1 up` を使ってください。
- 初回起動後、または依存関係変更後は必要に応じて `docker compose exec app python -m pip install -e .` を実行してください。
- テストは `docker compose exec app python -m pytest` で実行してください。
- CLI 確認は `docker compose exec app vsm --help` で実行してください。

## パスの扱い

Windows 側のプロジェクトパス:

```text
D:\userdata\docs\projects\Nanihold_OS
```

WSL 側の同一プロジェクトパス:

```text
/home/user/projects/Nanihold_OS
```

Codex のシェルが Windows PowerShell の場合、WSL 側で実行する必要があるコマンドは次の形を使ってください。

```text
wsl --cd /home/user/projects/Nanihold_OS -- <command>
```

ただし、Python やテストの実行は原則として Docker Compose 経由にしてください。

```text
docker compose exec app python -m pytest
```

Codex アプリからは次のラッパーを優先してください。

```text
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-dev.ps1 doctor
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-dev.ps1 up
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-dev.ps1 test
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-dev.ps1 vsm --help
```

## Codex アプリ運用メモ

- Codex アプリから作業を始める場合は、まず `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-dev.ps1 doctor` で WSL / Docker / Compose / `app` サービスの状態を確認してください。
- `app` サービスが起動していない場合は `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-dev.ps1 up` を実行してください。
- pytest や `vsm` CLI は `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-dev.ps1 test ...` または `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-dev.ps1 vsm ...` で実行してください。
- `.\codex-dev.cmd` は手元の PowerShell 用の短縮入口です。Codex アプリでは UNC パス警告を避けるため、PowerShell 直呼びを優先してください。
