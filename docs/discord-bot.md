# Discord Codex Bot

WSL 側のリポジトリ(`/home/user/projects/Nanihold_OS`)で Codex CLI を実行し、Discord
スレッドから自然言語でコーディングを依頼できる bot。

## 準備

Ubuntu 側で Codex CLI と bot 依存を用意する。

```bash
sudo apt-get install -y nodejs npm
sudo npm install -g @openai/codex
cd /home/user/projects/Nanihold_OS
. .venv/bin/activate
python -m pip install -e .
```

WSL 側で Codex 認証も済ませる。

```bash
codex login
codex doctor
```

`.env` に Discord bot 用の値を追加する。

```dotenv
DISCORD_BOT_TOKEN=...
DISCORD_ALLOWED_USER_IDS=123456789012345678,234567890123456789
DISCORD_ALLOWED_CHANNEL_IDS=345678901234567890
CODEX_WORKDIR=/home/user/projects/Nanihold_OS
CODEX_BIN=codex
CODEX_TIMEOUT_SECONDS=1800
CODEX_LOG_DIR=logs/discord-codex
```

Discord Developer Portal では bot の `Message Content Intent` を有効にする。

## 使い方

通常チャンネルでは `!codex <依頼内容>` または bot へのメンションで開始する。bot が作成した
`codex-...` スレッド内では、その後の自然文メッセージを Codex に渡す。

設定確認と手動起動:

```bash
python bot/discord_codex_bot.py --check
python bot/discord_codex_bot.py
```

## 常駐化

```bash
mkdir -p ~/.config/systemd/user
cp deploy/discord-codex-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now discord-codex-bot
systemctl --user status discord-codex-bot
journalctl --user -u discord-codex-bot -f
```

初期設定では `git push`、`git reset --hard`、`.env` の内容表示などは bot 側で止める。Codex
実行ログは `logs/discord-codex/` に保存される。
