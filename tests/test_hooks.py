from __future__ import annotations

from usagetrim.core.hooks import (
    generate_zsh_alias,
    install_zsh_hook,
    setup_claude_code_mcp_config,
)


def test_generate_zsh_alias_contains_expected_aliases():
    alias = generate_zsh_alias()
    assert "alias cc-run=" in alias
    assert "alias tcut=" in alias
    assert "usagetrim" in alias


def test_install_zsh_hook_creates_file_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr("usagetrim.core.hooks.Path.home", lambda: tmp_path)
    zshrc = tmp_path / ".zshrc"
    assert not zshrc.exists()

    result = install_zsh_hook()

    assert result == zshrc
    assert zshrc.exists()
    assert "usagetrim" in zshrc.read_text(encoding="utf-8")


def test_install_zsh_hook_appends_when_file_exists_without_alias(monkeypatch, tmp_path):
    monkeypatch.setattr("usagetrim.core.hooks.Path.home", lambda: tmp_path)
    zshrc = tmp_path / ".zshrc"
    zshrc.write_text("export PATH=$PATH:/usr/local/bin\n", encoding="utf-8")

    install_zsh_hook()

    content = zshrc.read_text(encoding="utf-8")
    assert "export PATH=$PATH:/usr/local/bin" in content
    assert "usagetrim" in content


def test_install_zsh_hook_is_idempotent_when_alias_already_present(monkeypatch, tmp_path):
    monkeypatch.setattr("usagetrim.core.hooks.Path.home", lambda: tmp_path)
    zshrc = tmp_path / ".zshrc"
    zshrc.write_text("# already has\nusagetrim stuff here\n", encoding="utf-8")
    original = zshrc.read_text(encoding="utf-8")

    install_zsh_hook()

    assert zshrc.read_text(encoding="utf-8") == original


def test_setup_claude_code_mcp_config_returns_exact_command():
    assert setup_claude_code_mcp_config() == (
        "claude mcp add --scope user usagetrim -- usagetrim mcp"
    )
