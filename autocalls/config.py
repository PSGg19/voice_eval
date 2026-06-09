from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "AUTOCALLS_", "env_file": ".env"}

    # Twilio
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_phone_number: str  # caller ID for outbound calls

    # Deepgram (STT + TTS)
    deepgram_api_key: str

    # Anthropic (LLM evaluation)
    anthropic_api_key: str = ""

    # Server
    server_host: str = "0.0.0.0"
    server_port: int = 8765
    public_url: str = ""  # https://your-domain.ngrok.io

    # Storage
    db_path: str = "autocalls.db"

    @property
    def ws_public_url(self) -> str:
        """Convert https:// public URL to wss:// for WebSocket."""
        return self.public_url.replace("https://", "wss://").replace("http://", "ws://")
