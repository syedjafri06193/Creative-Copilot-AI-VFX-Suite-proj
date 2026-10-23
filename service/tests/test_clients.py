import pytest

from sensei_service.clients import authenticate, digest, load, main
from sensei_service.config import load_settings


def test_auth_modes(tmp_path):
    assert load_settings({}).clients is None  # no auth

    shared = load_settings({"SENSEI_TOKEN": "s3cret"}).clients
    assert [(c.name, c.roles) for c in shared] == [("shared", ("host", "ui"))]

    clients_file = tmp_path / "clients.json"
    main(["--file", str(clients_file), "add", "resolve", "--role", "host"])
    assert [c.name for c in load_settings({"SENSEI_CLIENTS_FILE": str(clients_file)}).clients] == ["resolve"]


def test_both_auth_modes_at_once_are_refused(tmp_path):
    clients_file = tmp_path / "clients.json"
    main(["--file", str(clients_file), "add", "resolve", "--role", "host"])
    with pytest.raises(ValueError, match="not both"):
        load_settings({"SENSEI_CLIENTS_FILE": str(clients_file), "SENSEI_TOKEN": "s3cret"})


def test_shared_token_must_be_subprotocol_safe():
    with pytest.raises(ValueError):
        load_settings({"SENSEI_TOKEN": "has space"})


def test_cli_stores_only_token_hashes(tmp_path, capsys):
    clients_file = tmp_path / "clients.json"
    main(["--file", str(clients_file), "add", "desktop", "--role", "ui"])
    token = capsys.readouterr().out.strip()

    stored = clients_file.read_text(encoding="utf-8")
    assert token not in stored and digest(token) in stored
    assert authenticate(load(clients_file), token).name == "desktop"
    assert authenticate(load(clients_file), "wrong") is None


def test_cli_rejects_duplicates_and_revokes(tmp_path, capsys):
    clients_file = tmp_path / "clients.json"
    main(["--file", str(clients_file), "add", "both", "--role", "host", "--role", "ui"])
    token = capsys.readouterr().out.strip()
    assert load(clients_file)[0].roles == ("host", "ui")

    with pytest.raises(SystemExit):
        main(["--file", str(clients_file), "add", "both", "--role", "ui"])

    main(["--file", str(clients_file), "remove", "both"])
    assert authenticate(load(clients_file), token) is None


def test_invalid_roles_in_the_file_are_refused(tmp_path):
    clients_file = tmp_path / "clients.json"
    clients_file.write_text('{"clients": [{"name": "x", "roles": ["admin"], "token_sha256": "00"}]}', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid roles"):
        load(clients_file)
