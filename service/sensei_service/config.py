import os
import re
from pydantic import BaseModel, field_validator

DEFAULT_ALLOWED_ORIGINS = "http://127.0.0.1:5180,http://localhost:5180,sensei://app"

class Settings(BaseModel):
    # Browser origins allowed to open WebSockets (comma-separated env override).
    # Native clients such as host adapters send no Origin header and are always allowed.
    allowed_origins: list[str]
    # Optional shared secret. When set, every WebSocket client must offer the subprotocol
    # "sensei.token.<token>" (browsers can't set WebSocket headers; this also keeps it out of URLs).
    token: str | None = None

    @field_validator("token")
    @classmethod
    def token_is_subprotocol_safe(cls, token):
        if token is not None and not re.fullmatch(r"[A-Za-z0-9._~-]+", token):
            raise ValueError("SENSEI_TOKEN may only contain letters, digits and . _ ~ -")
        return token

settings = Settings(
    allowed_origins=[o.strip() for o in os.environ.get("SENSEI_ALLOWED_ORIGINS", DEFAULT_ALLOWED_ORIGINS).split(",") if o.strip()],
    token=os.environ.get("SENSEI_TOKEN") or None,
)
