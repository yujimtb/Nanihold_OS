"""Discord bridge for running Codex CLI inside the WSL project checkout."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shlex
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import discord
from dotenv import load_dotenv


COMMAND_PREFIX = "!codex"
THREAD_PREFIX = "codex-"
DISCORD_MESSAGE_LIMIT = 1900
TRANSCRIPT_LIMIT = 30

FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bgit\s+push\b", re.IGNORECASE),
    re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\s+/(?:\s|$)", re.IGNORECASE),
    re.compile(r"\b(cat|less|more|tail|head|grep|rg)\b.*\B\.env\b", re.IGNORECASE),
    re.compile(r"\b(printenv|env)\b.*(?:KEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE),
)


@dataclass(frozen=True)
class Settings:
    token: str
    allowed_user_ids: frozenset[int]
    allowed_channel_ids: frozenset[int]
    codex_workdir: Path
    codex_bin: str
    codex_timeout_seconds: int
    codex_log_dir: Path

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        token = _required_env("DISCORD_BOT_TOKEN")
        workdir = Path(os.environ.get("CODEX_WORKDIR", "/home/user/projects/Nanihold_OS")).expanduser()
        log_dir = Path(os.environ.get("CODEX_LOG_DIR", "logs/discord-codex")).expanduser()
        if not log_dir.is_absolute():
            log_dir = workdir / log_dir
        return cls(
            token=token,
            allowed_user_ids=_parse_id_set(os.environ.get("DISCORD_ALLOWED_USER_IDS", "")),
            allowed_channel_ids=_parse_id_set(os.environ.get("DISCORD_ALLOWED_CHANNEL_IDS", "")),
            codex_workdir=workdir,
            codex_bin=os.environ.get("CODEX_BIN", "codex"),
            codex_timeout_seconds=int(os.environ.get("CODEX_TIMEOUT_SECONDS", "1800")),
            codex_log_dir=log_dir,
        )

    def validate(self) -> None:
        if not self.allowed_user_ids:
            raise RuntimeError("DISCORD_ALLOWED_USER_IDS is required")
        if not self.codex_workdir.is_dir():
            raise RuntimeError(f"CODEX_WORKDIR does not exist: {self.codex_workdir}")
        if shutil.which(self.codex_bin) is None:
            raise RuntimeError(f"CODEX_BIN is not on PATH: {self.codex_bin}")


class CodexDiscordBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.settings = settings
        self._locks: dict[int, asyncio.Lock] = {}

    async def on_ready(self) -> None:
        print(f"discord-codex-bot ready as {self.user}", flush=True)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not self.user:
            return
        if message.author.id not in self.settings.allowed_user_ids:
            return
        if not self._channel_allowed(message.channel):
            return

        content = _strip_activation(message.content, self.user.id)
        if isinstance(message.channel, discord.Thread):
            if not self._is_codex_thread(message.channel) and content is None:
                return
            prompt = content if content is not None else message.content.strip()
            await self._run_in_thread(message.channel, message, prompt)
            return

        if content is None:
            return
        if not content:
            await message.channel.send("依頼内容を続けて書いてください。例: `!codex READMEを確認して改善案を出して`")
            return

        thread, created = await self._get_or_create_thread(message, content)
        if created:
            await thread.send(f"<@{message.author.id}> Codex に渡します。")
        else:
            await thread.send(f"<@{message.author.id}> 既存のスレッドで Codex に渡します。")
        await self._run_in_thread(thread, message, content)

    def _channel_allowed(self, channel: discord.abc.Messageable) -> bool:
        allowed = self.settings.allowed_channel_ids
        if not allowed:
            return True
        channel_id = getattr(channel, "id", None)
        parent_id = getattr(channel, "parent_id", None)
        return channel_id in allowed or parent_id in allowed

    @staticmethod
    def _is_codex_thread(channel: discord.Thread) -> bool:
        return channel.name.lower().startswith(THREAD_PREFIX)

    async def _get_or_create_thread(self, message: discord.Message, content: str) -> tuple[discord.Thread, bool]:
        existing = await _find_existing_thread(message)
        if existing is not None:
            return existing, False

        try:
            return await message.create_thread(name=_thread_name(content)), True
        except discord.HTTPException as exc:
            if exc.code != 160004:
                raise
            existing = await _find_existing_thread(message)
            if existing is None:
                raise
            return existing, False

    async def _run_in_thread(
        self,
        thread: discord.Thread,
        message: discord.Message,
        prompt: str,
    ) -> None:
        if _looks_forbidden(prompt):
            await thread.send("その操作は bot 側で止めました。`git push` や秘密情報表示などはローカルで明示的に実行してください。")
            return

        lock = self._locks.setdefault(thread.id, asyncio.Lock())
        if lock.locked():
            await thread.send("このスレッドでは別の Codex 実行が進行中です。完了後にもう一度送ってください。")
            return

        async with lock:
            status_message = await thread.send("Codex 実行中です。")
            transcript = await _thread_transcript(thread)
            result = await run_codex(self.settings, thread.id, message.author.id, prompt, transcript)
            await status_message.edit(content="Codex 実行が完了しました。" if result.returncode == 0 else "Codex 実行が失敗しました。")
            await _send_chunks(thread, result.render_for_discord())


@dataclass(frozen=True)
class CodexResult:
    returncode: int
    elapsed_seconds: float
    stdout: str
    stderr: str
    last_message: str
    git_status: str
    log_file: Path

    def render_for_discord(self) -> str:
        body = self.last_message.strip() or self.stdout.strip() or self.stderr.strip()
        if len(body) > 6000:
            body = body[:6000] + "\n\n[output truncated]"
        status = self.git_status.strip() or "(変更なし)"
        return (
            f"終了コード: `{self.returncode}` / {self.elapsed_seconds:.1f}s\n"
            f"ログ: `{self.log_file}`\n\n"
            f"{body}\n\n"
            f"git status:\n```text\n{status[:1200]}\n```"
        )


async def run_codex(
    settings: Settings,
    thread_id: int,
    author_id: int,
    user_prompt: str,
    transcript: str,
) -> CodexResult:
    started = datetime.now(timezone.utc)
    run_dir = settings.codex_log_dir / str(thread_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    prompt_file = run_dir / f"{stamp}-prompt.txt"
    stdout_file = run_dir / f"{stamp}-stdout.txt"
    stderr_file = run_dir / f"{stamp}-stderr.txt"
    last_file = run_dir / f"{stamp}-last.txt"

    codex_prompt = _codex_prompt(author_id, user_prompt, transcript)
    prompt_file.write_text(codex_prompt, encoding="utf-8")

    argv = [
        settings.codex_bin,
        "exec",
        "--cd",
        str(settings.codex_workdir),
        "--sandbox",
        "workspace-write",
        "--color",
        "never",
        "--output-last-message",
        str(last_file),
        "-",
    ]
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=settings.codex_workdir,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(codex_prompt.encode("utf-8")),
            timeout=settings.codex_timeout_seconds,
        )
        returncode = process.returncode if process.returncode is not None else 1
    except asyncio.TimeoutError:
        process.kill()
        stdout_bytes, stderr_bytes = await process.communicate()
        returncode = 124

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    stdout_file.write_text(stdout, encoding="utf-8")
    stderr_file.write_text(stderr, encoding="utf-8")

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    return CodexResult(
        returncode=returncode,
        elapsed_seconds=elapsed,
        stdout=stdout,
        stderr=stderr,
        last_message=_read_text_if_exists(last_file),
        git_status=await _git_status(settings.codex_workdir),
        log_file=stdout_file,
    )


def _codex_prompt(author_id: int, user_prompt: str, transcript: str) -> str:
    return f"""\
あなたは Discord bot から呼び出されている Codex です。
回答は日本語で行ってください。
作業ディレクトリはこのリポジトリです。必要ならファイル編集とテスト実行まで行ってください。
ただし、git commit / git push / 秘密情報の表示 / .env の内容表示 / リポジトリ外への破壊的操作は行わないでください。
完了時は変更点、検証結果、未実行の確認があれば簡潔に報告してください。

Discord author id: {author_id}

直近のスレッド履歴:
{transcript}

今回の依頼:
{user_prompt}
"""


async def _git_status(workdir: Path) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        "status",
        "--short",
        cwd=workdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return (stdout or stderr).decode("utf-8", errors="replace")


async def _find_existing_thread(message: discord.Message) -> discord.Thread | None:
    thread = getattr(message, "thread", None)
    if isinstance(thread, discord.Thread):
        return thread

    guild = message.guild
    if guild is None:
        return None

    for thread in guild.threads:
        if thread.id == message.id:
            return thread

    try:
        channel = await guild.fetch_channel(message.id)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        return None

    return channel if isinstance(channel, discord.Thread) else None


async def _thread_transcript(thread: discord.Thread) -> str:
    lines: list[str] = []
    async for msg in thread.history(limit=TRANSCRIPT_LIMIT, oldest_first=True):
        if not msg.content:
            continue
        author = getattr(msg.author, "display_name", str(msg.author))
        lines.append(f"{author}: {msg.clean_content}")
    return "\n".join(lines) if lines else "(履歴なし)"


async def _send_chunks(channel: discord.abc.Messageable, text: str) -> None:
    chunks = [text[i : i + DISCORD_MESSAGE_LIMIT] for i in range(0, len(text), DISCORD_MESSAGE_LIMIT)]
    for chunk in chunks or ["(出力なし)"]:
        await channel.send(chunk)


def _strip_activation(content: str, bot_user_id: int) -> str | None:
    text = content.strip()
    mention_pattern = re.compile(rf"^<@!?{bot_user_id}>\s*")
    if mention_pattern.match(text):
        return mention_pattern.sub("", text).strip()
    if text.lower().startswith(COMMAND_PREFIX):
        return text[len(COMMAND_PREFIX) :].strip()
    return None


def _thread_name(prompt: str) -> str:
    compact = re.sub(r"\s+", "-", prompt.strip().lower())
    compact = re.sub(r"[^a-z0-9ぁ-んァ-ヶ一-龠ー_-]", "", compact)
    date_stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return f"{THREAD_PREFIX}{compact[:60] or 'task'}:{date_stamp}"


def _looks_forbidden(text: str) -> bool:
    return any(pattern.search(text) for pattern in FORBIDDEN_PATTERNS)


def _parse_id_set(raw: str) -> frozenset[int]:
    values: set[int] = set()
    for part in re.split(r"[\s,]+", raw.strip()):
        if not part:
            continue
        values.add(int(part))
    return frozenset(values)


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _read_text_if_exists(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="validate configuration and exit")
    args = parser.parse_args()

    try:
        settings = Settings.from_env()
        settings.validate()
    except RuntimeError as exc:
        raise SystemExit(f"config error: {exc}") from exc
    if args.check:
        print(
            f"config ok: workdir={settings.codex_workdir} "
            f"codex={shlex.quote(settings.codex_bin)}"
        )
        return

    bot = CodexDiscordBot(settings)
    bot.run(settings.token)


if __name__ == "__main__":
    main()
