"""Org-grade YAML routing policy with secure secrets.

Designed for organizations using LLM Router at scale, where:
    - Routing policy lives in version-controlled YAML
    - Provider credentials must NEVER appear in plaintext in those YAML files
    - The same policy should work locally (env vars) and in production
      (HashiCorp Vault, AWS Secrets Manager, GCP Secret Manager, Doppler, ...)
    - Plaintext-looking secrets must be caught before they ever ship to git

Secrets in YAML are referenced via ``${scheme:identifier}`` indirections.
Five schemes ship out of the box:

    ``${env:NAME}``               read from the process environment
    ``${file:/path/to/secret}``   read once from disk (e.g. K8s mount)
    ``${vault:secret/path#key}``  HashiCorp Vault (lazy; via hvac client)
    ``${aws-sm:arn}``             AWS Secrets Manager (lazy; via boto3)
    ``${gcp-sm:projects/X/secrets/Y/versions/latest}``  GCP Secret Manager

The loader resolves these at runtime; the policy YAML stays
deployable-anywhere. Plaintext-looking secrets (API key patterns) are
rejected at load time as the last line of defense.

Example:

    # config/policies/prod.yaml
    name: prod-routing
    providers:
      openai:
        api_key: "${vault:secret/llm-providers#openai_key}"
        organization: "${env:OPENAI_ORG}"
      anthropic:
        api_key: "${aws-sm:arn:aws:secretsmanager:us-east-1:1234:secret:<SECRET_NAME>}"
    routing:
      default_chain: code_chain
      enforce: smart
      tier_budgets:
        local: unlimited
        cheap: 100.00
        mid: 50.00
        premium: 10.00
    audit:
      otlp_endpoint: "${env:OTEL_EXPORTER_OTLP_ENDPOINT}"
      lineage_retention_days: 90

    # Then in code:
    policy = OrgPolicy.load("config/policies/prod.yaml")
    api_key = policy.resolve("providers.openai.api_key")   # → real key

Tests in tests/qa/test_org_policy.py pin the resolution contract +
plaintext-secret rejection.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


# ────────────────────────────────────────────────────────────────────────
# Plaintext-secret detection — last-line-of-defense regex set
# ────────────────────────────────────────────────────────────────────────

_PLAINTEXT_SECRET_PATTERNS = (
    ("openai_key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("gemini_key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
)


class PlaintextSecretInPolicy(ValueError):
    """Raised when YAML contains a value that looks like a raw API key.

    Users must replace plaintext secrets with ``${env:NAME}`` or another
    indirection. This is the contract we enforce: secrets do not live in
    git, ever.
    """


# ────────────────────────────────────────────────────────────────────────
# Secret resolution
# ────────────────────────────────────────────────────────────────────────

_REF_RE = re.compile(r"\$\{(?P<scheme>[a-z-]+):(?P<id>[^}]+)\}")


class SecretResolver:
    """Resolve ``${scheme:identifier}`` references to actual secret values.

    Five built-in schemes; users can register custom providers via
    ``register_scheme()`` for in-house secret backends.
    """

    def __init__(self):
        self._providers: dict[str, Callable[[str], str]] = {
            "env": self._resolve_env,
            "file": self._resolve_file,
            "vault": self._resolve_vault,
            "aws-sm": self._resolve_aws_sm,
            "gcp-sm": self._resolve_gcp_sm,
        }

    def register_scheme(self, scheme: str, provider: Callable[[str], str]) -> None:
        self._providers[scheme] = provider

    def resolve(self, ref: str) -> str:
        """Resolve a single ``${scheme:id}`` reference to a string value.

        If `ref` doesn't match the ${...} pattern, returns it unchanged.
        """
        m = _REF_RE.fullmatch(ref)
        if not m:
            return ref
        scheme, identifier = m.group("scheme"), m.group("id")
        if scheme not in self._providers:
            raise ValueError(
                f"Unknown secret scheme: {scheme!r}. "
                f"Available: {sorted(self._providers)}. "
                f"Register custom schemes via SecretResolver.register_scheme()."
            )
        return self._providers[scheme](identifier)

    def resolve_in_value(self, value: Any) -> Any:
        """Walk a nested structure (dict/list/scalar) and resolve every
        ${ref} substring it contains. Returns a new structure with all
        references replaced."""
        if isinstance(value, str):
            # Whole-string reference → return resolved value as-is
            if _REF_RE.fullmatch(value.strip()):
                return self.resolve(value.strip())
            # Embedded references → substitute inline
            return _REF_RE.sub(
                lambda m: self.resolve(f"${{{m.group('scheme')}:{m.group('id')}}}"),
                value,
            )
        if isinstance(value, dict):
            return {k: self.resolve_in_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve_in_value(v) for v in value]
        return value

    # ── Built-in providers ─────────────────────────────────────────────

    @staticmethod
    def _resolve_env(name: str) -> str:
        value = os.environ.get(name)
        if value is None:
            raise KeyError(f"env var {name!r} not set")
        return value

    @staticmethod
    def _resolve_file(path_str: str) -> str:
        path = Path(path_str)
        if not path.exists():
            raise FileNotFoundError(f"secret file {path} does not exist")
        return path.read_text().strip()

    @staticmethod
    def _resolve_vault(identifier: str) -> str:
        """secret/path#key → HashiCorp Vault KV v2 read.

        Lazy import: hvac is optional. Production deployments using Vault
        must `pip install "llm-routing[secrets-vault]"`.
        """
        try:
            import hvac
        except ImportError:
            raise ImportError(
                "Vault references need hvac: "
                'pip install "llm-routing[secrets-vault]"'
            )
        if "#" not in identifier:
            raise ValueError(
                f"Vault ref must be 'path#key', got {identifier!r}"
            )
        path, key = identifier.split("#", 1)
        client = hvac.Client(
            url=os.environ.get("VAULT_ADDR"),
            token=os.environ.get("VAULT_TOKEN"),
        )
        result = client.secrets.kv.v2.read_secret_version(path=path)
        return result["data"]["data"][key]

    @staticmethod
    def _resolve_aws_sm(arn: str) -> str:
        try:
            import boto3
        except ImportError:
            raise ImportError(
                "AWS Secrets Manager refs need boto3: "
                'pip install "llm-routing[secrets-aws]"'
            )
        client = boto3.client("secretsmanager")
        return client.get_secret_value(SecretId=arn)["SecretString"]

    @staticmethod
    def _resolve_gcp_sm(resource: str) -> str:
        try:
            from google.cloud import secretmanager
        except ImportError:
            raise ImportError(
                "GCP Secret Manager refs need google-cloud-secret-manager: "
                'pip install "llm-routing[secrets-gcp]"'
            )
        client = secretmanager.SecretManagerServiceClient()
        return client.access_secret_version(
            request={"name": resource}
        ).payload.data.decode("utf-8")


# ────────────────────────────────────────────────────────────────────────
# Policy loader + validator
# ────────────────────────────────────────────────────────────────────────

@dataclass
class OrgPolicy:
    """Loaded org policy. `raw` is the parsed YAML, `resolver` does
    secret resolution lazily on demand."""

    name: str
    raw: dict
    resolver: SecretResolver = field(default_factory=SecretResolver)
    path: Path | None = None

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        resolver: SecretResolver | None = None,
        skip_secret_check: bool = False,
    ) -> "OrgPolicy":
        """Load + validate. Raises PlaintextSecretInPolicy when the YAML
        contains values that look like raw API keys."""
        import yaml

        path = Path(path)
        text = path.read_text()
        if not skip_secret_check:
            _scan_for_plaintext_secrets(text, source=str(path))
        raw = yaml.safe_load(text)
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: top-level must be a YAML mapping")
        name = str(raw.get("name", path.stem))
        return cls(name=name, raw=raw, resolver=resolver or SecretResolver(),
                   path=path)

    def resolve(self, dotted_path: str) -> Any:
        """Navigate the policy by dotted path (e.g. 'providers.openai.api_key')
        and resolve any secret reference in the value.

        Returns the resolved scalar value, not the literal ${ref} string.
        """
        cur: Any = self.raw
        for part in dotted_path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                raise KeyError(
                    f"path {dotted_path!r} not found at {part!r}"
                )
            cur = cur[part]
        return self.resolver.resolve_in_value(cur)

    def resolve_all(self) -> dict:
        """Return the whole policy as a dict with every secret resolved.

        Convenient for one-shot apply; not recommended in production
        because it materializes all secrets into one in-memory object."""
        return self.resolver.resolve_in_value(self.raw)

    def get(self, dotted_path: str, default: Any = None) -> Any:
        """Like `resolve` but returns the literal value (with ${refs}
        unresolved) — useful for reading non-secret config without
        triggering external calls."""
        cur: Any = self.raw
        for part in dotted_path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur


def _scan_for_plaintext_secrets(text: str, *, source: str = "<policy>") -> None:
    """Raise PlaintextSecretInPolicy if the YAML text contains anything
    that looks like a raw API key.

    This is the contract: secrets do NOT live in version-controlled YAML.
    Every credential must be ``${env:...}`` / ``${vault:...}`` / etc.
    """
    findings: list[tuple[str, str]] = []
    for pattern_name, regex in _PLAINTEXT_SECRET_PATTERNS:
        for match in regex.finditer(text):
            # Skip if the match is inside an obviously-quoted ${ref} block
            # (some patterns can collide with reference paths)
            start = max(0, match.start() - 5)
            preceding = text[start:match.start()]
            if "${" in preceding:
                continue
            findings.append((pattern_name, match.group(0)[:8] + "…"))

    if findings:
        details = "\n  ".join(f"- {p} (pattern: {n})" for n, p in findings)
        raise PlaintextSecretInPolicy(
            f"{source}: plaintext-looking secret(s) detected:\n  {details}\n\n"
            f"Secrets must be referenced via ${{scheme:id}}, never inlined.\n"
            f"Supported schemes: env, file, vault, aws-sm, gcp-sm."
        )
