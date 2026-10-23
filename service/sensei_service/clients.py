"""Per-client credentials: a JSON file of named clients, each with roles and the SHA-256 of its token.

    python -m sensei_service.clients add <name> --role host [--role ui]   # prints the new token once
    python -m sensei_service.clients remove <name>
    python -m sensei_service.clients list

The file defaults to $SENSEI_CLIENTS_FILE (or --file). Only token hashes are stored.
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROLES = ("host", "ui")  # host: may stream frames on /ws/host; ui: may follow suggestions on /ws/ui


@dataclass(frozen=True)
class Client:
    name: str
    roles: tuple[str, ...]
    token_sha256: str


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def shared_client(token: str) -> Client:
    # The single-token mode: one client, allowed on every endpoint.
    if not re.fullmatch(r"[A-Za-z0-9._~-]+", token):
        raise ValueError("SENSEI_TOKEN may only contain letters, digits and . _ ~ -")
    return Client("shared", ROLES, digest(token))


def load(path) -> list[Client]:
    clients = []
    for entry in json.loads(Path(path).read_text(encoding="utf-8"))["clients"]:
        client = Client(entry["name"], tuple(entry["roles"]), entry["token_sha256"])
        if not client.roles or set(client.roles) - set(ROLES):
            raise ValueError(f"client {client.name!r} has invalid roles {list(client.roles)}")
        clients.append(client)
    return clients


def save(path, clients: list[Client]):
    data = {"clients": [{**asdict(c), "roles": list(c.roles)} for c in clients]}
    Path(path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def authenticate(clients: list[Client], token: str) -> Client | None:
    presented = digest(token)
    match = None
    for c in clients:  # check every entry, so timing doesn't reveal which client matched
        if hmac.compare_digest(presented, c.token_sha256):
            match = c
    return match


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m sensei_service.clients", description=__doc__.split("\n")[0])
    parser.add_argument("--file", default=os.environ.get("SENSEI_CLIENTS_FILE", "clients.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="create a client and print its token")
    add.add_argument("name")
    add.add_argument("--role", choices=ROLES, action="append", required=True, help="repeat for several roles")
    commands.add_parser("remove", help="revoke a client").add_argument("name")
    commands.add_parser("list", help="list clients")
    args = parser.parse_args(argv)

    path = Path(args.file)
    clients = load(path) if path.exists() else []
    if args.command == "list":
        for c in clients:
            print(f"{c.name}\t{','.join(c.roles)}")
    elif args.command == "add":
        if any(c.name == args.name for c in clients):
            sys.exit(f"client {args.name!r} already exists")
        token = secrets.token_urlsafe(24)  # URL-safe base64: valid in a WebSocket subprotocol
        save(path, clients + [Client(args.name, tuple(dict.fromkeys(args.role)), digest(token))])
        print(token)
    elif args.command == "remove":
        if not any(c.name == args.name for c in clients):
            sys.exit(f"no client named {args.name!r}")
        save(path, [c for c in clients if c.name != args.name])


if __name__ == "__main__":
    main()
