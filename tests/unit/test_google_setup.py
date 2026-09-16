import json

import pytest

from simon.google_setup import configure, main


def test_import_preserves_other_settings_and_existing_token_key(tmp_path):
    client = tmp_path / "client.json"
    env = tmp_path / ".env"
    client.write_text(
        json.dumps(
            {"web": {"client_id": "test.apps.googleusercontent.com", "client_secret": "secret"}}
        )
    )
    env.write_text("# existing config\nSIMON_OPENAI_API_KEY=keep-this\nSIMON_GOOGLE_TOKEN_KEY=\n")
    configure(client, env)
    first = env.read_text()
    assert (
        "SIMON_OPENAI_API_KEY=keep-this" in first and "SIMON_GOOGLE_CLIENT_SECRET=secret" in first
    )
    assert first.count("SIMON_GOOGLE_TOKEN_KEY=") == 1
    configure(client, env)
    assert first == env.read_text()
    key_line = next(
        line for line in first.splitlines() if line.startswith("SIMON_GOOGLE_TOKEN_KEY=")
    )
    env.write_text(first.replace(key_line, key_line + " # keep this key"))
    configure(client, env)
    assert env.read_text() == first
    assert not list(tmp_path.glob(".simon-google-*"))


def test_import_rejects_non_web_client_and_secret_injection(tmp_path):
    client, env = tmp_path / "client.json", tmp_path / ".env"
    for body in [
        {"installed": {}},
        {"web": {"client_id": "test", "client_secret": "bad\nINJECTED=value"}},
    ]:
        client.write_text(json.dumps(body))
        with pytest.raises((KeyError, ValueError)):
            configure(client, env)
        assert not env.exists()


def test_setup_cli_sanitizes_failure_and_success(tmp_path, monkeypatch, capsys):
    client, env = tmp_path / "client.json", tmp_path / ".env"
    monkeypatch.setattr(
        "sys.argv", ["google_setup", "--client-file", str(client), "--env-file", str(env)]
    )
    with pytest.raises(SystemExit):
        main()
    assert "Could not import" in capsys.readouterr().err
    client.write_text(json.dumps({"web": {"client_id": "test", "client_secret": "secret-private"}}))
    main()
    output = capsys.readouterr().out
    assert "settings saved" in output and "secret-private" not in output
