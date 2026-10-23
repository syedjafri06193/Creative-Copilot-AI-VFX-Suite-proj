import os
from pydantic import BaseModel
from .clients import Client, load, shared_client

DEFAULT_ALLOWED_ORIGINS = "http://127.0.0.1:5180,http://localhost:5180,sensei://app"

class Settings(BaseModel):
    # Browser origins allowed to open WebSockets (comma-separated env override).
    # Native clients such as host adapters send no Origin header and are always allowed.
    allowed_origins: list[str]
    # Who may connect. None = no auth. Otherwise every WebSocket client must offer the subprotocol
    # "sensei.token.<its token>" (browsers can't set WebSocket headers; this also keeps it out of URLs)
    # and may only use the endpoints its roles allow.
    clients: list[Client] | None = None
    # Where feedback is kept and learned from (":memory:" keeps it for this run only).
    feedback_db: str

def load_settings(env) -> Settings:
    clients_file = env.get("SENSEI_CLIENTS_FILE") or None
    token = env.get("SENSEI_TOKEN") or None
    if clients_file and token:
        # refuse rather than guess: silently ignoring either would weaken the auth someone configured
        raise ValueError("set SENSEI_CLIENTS_FILE (per-client tokens) or SENSEI_TOKEN (one shared token), not both")
    if clients_file:
        clients = load(clients_file)
    elif token:
        clients = [shared_client(token)]
    else:
        clients = None
    origins = env.get("SENSEI_ALLOWED_ORIGINS", DEFAULT_ALLOWED_ORIGINS)
    feedback_db = env.get("SENSEI_FEEDBACK_DB") or os.path.join(os.path.expanduser("~"), ".sensei", "feedback.sqlite3")
    return Settings(allowed_origins=[o.strip() for o in origins.split(",") if o.strip()], clients=clients,
                    feedback_db=feedback_db)

settings = load_settings(os.environ)
