from pathlib import Path

import pytest

pytest.importorskip("discord", reason="discord.py is not installed")

from bot.discord_codex_bot import (
    RECENT_MESSAGE_CACHE_SIZE,
    CodexDiscordBot,
    Settings,
    _acquire_single_instance_lock,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        token="token",
        allowed_user_ids=frozenset({1}),
        allowed_channel_ids=frozenset(),
        codex_workdir=tmp_path,
        codex_bin="codex",
        codex_timeout_seconds=1,
        codex_log_dir=tmp_path / "logs",
    )


def test_remember_message_rejects_duplicate_ids(tmp_path: Path) -> None:
    bot = CodexDiscordBot(_settings(tmp_path))

    assert bot._remember_message(100)
    assert not bot._remember_message(100)


def test_remember_message_evicts_old_ids(tmp_path: Path) -> None:
    bot = CodexDiscordBot(_settings(tmp_path))

    for message_id in range(RECENT_MESSAGE_CACHE_SIZE + 1):
        assert bot._remember_message(message_id)

    assert bot._remember_message(0)


def test_single_instance_lock_rejects_second_process(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    first_lock = _acquire_single_instance_lock(settings)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            _acquire_single_instance_lock(settings)
    finally:
        first_lock.close()

    second_lock = _acquire_single_instance_lock(settings)
    second_lock.close()
