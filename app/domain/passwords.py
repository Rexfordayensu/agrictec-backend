"""Password rules (lib/admin/passwords.ts) and one-time passwords."""

import re
import secrets

ADMIN_PASSWORD_MIN_LENGTH = 10
FARMER_PASSWORD_MIN_LENGTH = 8

ADMIN_PASSWORD_RULES: list[tuple[str, re.Pattern[str] | None]] = [
    (f"At least {ADMIN_PASSWORD_MIN_LENGTH} characters", None),
    ("An uppercase letter", re.compile(r"[A-Z]")),
    ("A lowercase letter", re.compile(r"[a-z]")),
    ("A number", re.compile(r"\d")),
    ("A symbol", re.compile(r"[^A-Za-z0-9]")),
]


def password_problem(password: str) -> str | None:
    """The first admin rule the password breaks, or None when it is strong enough."""
    for label, pattern in ADMIN_PASSWORD_RULES:
        ok = len(password) >= ADMIN_PASSWORD_MIN_LENGTH if pattern is None else pattern.search(password)
        if not ok:
            return f"Password needs: {label.lower()}."
    return None


def make_temp_password() -> str:
    """A one-time password such as "Temp-48213-kq7v", shown once to the admin who created it.

    Longer than the browser demo's "Temp-48213": on a server it can be tried remotely."""
    return f"Temp-{10000 + secrets.randbelow(90000)}-{secrets.token_hex(2)}"
