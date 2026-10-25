"""LC-12h repair: synthetic secrets and prompts for the secret door, generated at test time.

No real-looking secret is committed: every "real-shaped" value below is drawn from ``random.Random(seed)``
when a test runs. The placeholder set is the PR #721 reviewer's list (documentation keys, filler runs, YOUR_...
values, common-word URL passwords, .env.example lines, JWT fixtures).
"""
from __future__ import annotations

import base64
import json
import random
import string

B62 = string.ascii_letters + string.digits
B64URL = B62 + "-_"
HEX = "0123456789abcdef"
UPPER_DIGITS = string.ascii_uppercase + string.digits


def _draw(rng: random.Random, alphabet: str, n: int) -> str:
    return "".join(rng.choice(alphabet) for _ in range(n))


def _with_digit(body: str) -> str:
    """Real keys carry digits; the draw almost always does, and this makes it certain."""
    return body if any(c.isdigit() for c in body) else body[:3] + "7" + body[4:]


def real_shaped(kind: str, seed: int) -> str:
    """A random, high-entropy value in the shape of a real credential of ``kind``."""
    r = random.Random(f"{kind}:{seed}")
    if kind == "anthropic_key":
        return "sk-ant-api03-" + _draw(r, B64URL, 90)
    if kind == "openai_key":
        return "sk-proj-" + _with_digit(_draw(r, B62, 48))
    if kind == "openrouter_key":
        return "sk-or-v1-" + _draw(r, HEX, 64)
    if kind == "google_key":
        return "AIza" + _draw(r, B64URL, 35)
    if kind == "aws_key_id":
        return "AKIA" + _draw(r, UPPER_DIGITS, 16)
    if kind == "github_token":
        return "ghp_" + _draw(r, B62, 36)
    if kind == "slack_token":
        return f"xoxb-{_draw(r, string.digits, 12)}-{_draw(r, string.digits, 12)}-{_draw(r, B62, 24)}"
    if kind == "private_key":
        body = "\n".join(_draw(r, B62 + "+/", 64) for _ in range(6))
        return f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----"
    if kind == "bearer_token":
        return "Bearer " + _with_digit(_draw(r, B62, 40))
    if kind == "db_url_password":
        return f"postgres://svc_app:{_draw(r, B62, 18)}@db.internal.example.org:5432/app"
    raise ValueError(kind)


KINDS = ("anthropic_key", "openai_key", "openrouter_key", "google_key", "aws_key_id", "github_token",
         "slack_token", "private_key", "bearer_token", "db_url_password")
LABEL = {"anthropic_key": "Anthropic API key", "openai_key": "OpenAI API key", "openrouter_key": "OpenRouter API key",
         "google_key": "Google API key", "aws_key_id": "AWS access key", "github_token": "GitHub token",
         "slack_token": "Slack token", "private_key": "private key block", "bearer_token": "bearer token",
         "db_url_password": "database URL password"}


def _b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def jwt_fixtures() -> list[str]:
    head = _b64({"alg": "HS256", "typ": "JWT"})
    return [f"{head}.{_b64(claims)}.{sig}" for claims, sig in (
        ({"sub": "1234567890", "name": "John Doe", "iat": 1516239022}, "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"),
        ({"sub": "test-user", "role": "test", "exp": 9999999999}, "dGVzdC1zaWduYXR1cmUtZm9yLWZpeHR1cmVzMDAx"),
        ({"iss": "example.com", "aud": "unit-tests", "sub": "user-1"}, "ZXhhbXBsZS1zaWduYXR1cmUtbm90LXJlYWwtMDAwMQ"),
    )]


def placeholders() -> list[str]:
    """Texts a coding prompt legitimately contains that must never be refused."""
    out = [
        "aws_access_key_id = AKIAIOSFODNN7EXAMPLE",
        "export AWS_ACCESS_KEY_ID=AKIAI44QH8DHBEXAMPLE",
        'token = "ghp_' + "x" * 36 + '"',
        "GITHUB_TOKEN=ghp_" + "X" * 36,
        "ANTHROPIC_API_KEY=sk-ant-" + "x" * 40,
        "OPENAI_API_KEY=sk-" + "x" * 48,
        "OPENAI_API_KEY=sk-proj-" + "*" * 40,
        "SLACK_BOT_TOKEN=xoxb-" + "0" * 12 + "-" + "0" * 12 + "-" + "x" * 24,
        "GOOGLE_API_KEY=AIza" + "." * 35,
        'api_key = "YOUR_API_KEY_HERE_1234567890"',
        "OPENAI_API_KEY=sk-your-key-goes-here-replace-me-0000",
        "DATABASE_URL=postgres://postgres:postgres@db:5432/app",
        "DATABASE_URL=postgres://user:password@localhost:5432/mydb",
        "mysql://root:rootpassword@mysql:3306/shop",
        "redis://admin:admin@cache:6379/0",
        "amqp://guest:changeme123@rabbit:5672/",
        "postgres://app:<password>@db/app",
        "postgres://app:${DB_PASSWORD}@db/app",
        "postgres://app:YOUR_DB_PASSWORD@db/app",
        "mongodb://dev:placeholder-secret@mongo/dev",
        "Authorization: Bearer YOUR_ACCESS_TOKEN_REPLACE_ME_12345678",
        "Authorization: Bearer " + "x" * 40,
        "Authorization: Bearer 0123456789abcdefghijklmnopqrstuvwxyz",
        "-----BEGIN PRIVATE KEY-----\nWhy does openssl say this header is malformed when parsing my PEM file?",
        "How do I parse a block that starts with -----BEGIN RSA PRIVATE KEY----- in Python?",
        "-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join(["x" * 64] * 4) + "\n-----END RSA PRIVATE KEY-----",
        "# .env.example\nSTRIPE_SECRET_KEY=sk_test_replace_me\nJWT_SECRET=changeme\nAPI_TOKEN=\nSENTRY_DSN=https://<key>@o0.ingest.sentry.io/0",
        "services:\n  db:\n    image: postgres:16\n    environment:\n      POSTGRES_PASSWORD: postgres\n      DATABASE_URL: postgres://postgres:postgres@db/app",
    ]
    out += [f"fixture token = {t}" for t in jwt_fixtures()]
    return out


def ordinary_prompts(n: int = 240) -> list[str]:
    """>= ``n`` synthetic coding prompts of the kinds a developer sends, none holding a credential."""
    r = random.Random("ordinary-corpus")
    ident = lambda: _draw(r, string.ascii_lowercase, r.randint(4, 9))  # noqa: E731
    sha = lambda k=40: _draw(r, HEX, k)  # noqa: E731
    b64 = lambda k=88: _draw(r, B62 + "+/", k)  # noqa: E731
    uuid = lambda: "-".join(_draw(r, HEX, k) for k in (8, 4, 4, 4, 12))  # noqa: E731
    makers = [
        lambda: f"Why does this raise KeyError?\n\nTraceback (most recent call last):\n  File \"app/{ident()}.py\", line {r.randint(1, 900)}, in {ident()}\n    return cfg[\"{ident()}\"]\nKeyError: '{ident()}'",
        lambda: f"Explain this diff:\n--- a/{ident()}.py\n+++ b/{ident()}.py\n@@ -{r.randint(1, 99)},4 +{r.randint(1, 99)},6 @@\n-    x = {r.randint(1, 9)}\n+    x = compute_{ident()}()",
        lambda: f"git bisect says commit {sha()} introduced the regression; parent {sha()}. What should I look at?",
        lambda: f"Row id {uuid()} is duplicated in the {ident()} table; write a dedupe query.",
        lambda: f"package-lock integrity sha512-{b64()}== for {ident()}@{r.randint(1, 9)}.{r.randint(0, 20)}.0 fails to verify, why?",
        lambda: f"Decode this base64 payload and tell me its encoding: {b64(r.randint(40, 120))}",
        lambda: f"Fetch https://api.{ident()}.dev/v1/items?page={r.randint(1, 50)}&limit=100&sort={ident()}&cursor={_draw(r, B62, 22)} with retries.",
        lambda: f"The sha256 of the release tarball is {sha(64)}; add a checksum step to the Makefile.",
        lambda: f"Rename {ident().capitalize()}{ident().capitalize()}{ident().capitalize()}FactoryProviderStrategyImpl to something shorter across the repo.",
        lambda: f"Write a pytest for the auth middleware: it reads the token from the Authorization header, rejects an empty or malformed bearer value and returns 401 for {ident()}.",
        lambda: f"In {ident()}.py I do `token = get_token(user)` and `password = hash_password(raw)`; is storing the hash with {ident()} salt fine?",
        lambda: f"pytest output:\n{'.' * r.randint(20, 60)}F..\nFAILED tests/test_{ident()}.py::test_{ident()} - assert {r.randint(1, 9)} == {r.randint(10, 99)}",
        lambda: f"How should I rotate API keys safely? We use a secrets manager and a {ident()} service; describe the rollout, not any real key.",
        lambda: f"Refactor this function to async:\n\ndef {ident()}(a, b):\n    for i in range({r.randint(2, 50)}):\n        yield a[i] + b[i]",
        lambda: f"What does the regex ^[A-Za-z0-9_-]{{{r.randint(8, 40)},}}$ accept? Build a validator for usernames in {ident()}.",
        lambda: f"My Dockerfile COPY fails: layer {sha(64)} not found in {ident()}/{ident()}. Fix?",
        lambda: f"Set the env var {ident().upper()}_URL to http://localhost:{r.randint(3000, 9000)}/{ident()} in docker-compose and read it in Go.",
        lambda: f"Convert this SQL to SQLAlchemy: SELECT {ident()}, count(*) FROM {ident()} WHERE created > '2026-0{r.randint(1, 9)}-1{r.randint(0, 9)}' GROUP BY 1;",
        lambda: f"Explain how JWT verification works: header, payload, signature, and where {ident()} claims like exp and aud fit; no token provided.",
        lambda: f"Add type hints: def {ident()}(items, key=None, reverse=False): return sorted(items, key=key, reverse=reverse)",
    ]
    prompts = [makers[i % len(makers)]() for i in range(n + 20)]
    return prompts
