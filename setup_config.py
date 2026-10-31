"""Create a local config with a random control secret; never overwrite existing credentials."""
from pathlib import Path
import secrets

root = Path(__file__).resolve().parent
target = root / "config.yml"
if not target.exists():
    template = (root / "config.example.yml").read_text(encoding="utf-8")
    value = template.replace("REPLACE_WITH_A_RANDOM_SECRET_AT_LEAST_24_CHARS", secrets.token_urlsafe(32))
    with target.open("x", encoding="utf-8") as stream:
        stream.write(value)
    print("Created config.yml with a random control token. Fill in your mailbox and API settings locally.")
else:
    print("Existing config.yml preserved.")
