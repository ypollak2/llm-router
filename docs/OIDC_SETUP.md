# PyPI OIDC Trusted Publisher Setup

This project uses OpenID Connect (OIDC) Trusted Publisher authentication for secure PyPI publishing. This replaces the legacy API token-based approach.

## What is OIDC Trusted Publisher?

OIDC Trusted Publisher allows GitHub Actions to authenticate with PyPI without storing any secrets. Each publish request is signed by GitHub and verified by PyPI, making it more secure and eliminating the need to manage API tokens.

**Benefits:**
- ✅ No API tokens to rotate or manage
- ✅ No secrets stored in GitHub (reduced attack surface)
- ✅ Per-repository and per-environment granular access control
- ✅ Automatic token refresh (no expiration concerns)

## Setup Instructions

### 1. Prerequisites

Ensure you have:
- Owner or maintainer access to the PyPI project
- A GitHub repository with publishing workflows
- Python 3.10+ (for testing locally)

### 2. Configure OIDC on PyPI

1. **Go to PyPI Project Settings**
   - Navigate to https://pypi.org/manage/project/claude-code-llm-router/publishing/
   - Sign in with your PyPI account

2. **Add a Trusted Publisher**
   - Click "Add a new pending publisher"
   - Select "GitHub" as the publisher type
   - Fill in the following:
     - **Repository name**: `ypollak2/llm-router`
     - **Workflow name**: `publish.yml`
     - **Environment name**: `pypi` (must match GitHub Actions environment)
   - Click "Add"

3. **Verify the Configuration**
   - The trusted publisher should appear in the "Pending publishers" list
   - Once you push a tag and the workflow runs, it will be promoted to "Active publishers"

### 3. Update GitHub Actions Workflow

✅ **Already configured** — The `.github/workflows/publish.yml` is already set up to use OIDC.

Verify these are in place:
- `permissions.id-token: write` — Required for OIDC token generation
- `environment: pypi` — Must match the PyPI configuration
- NO `secrets.PYPI_TOKEN` — We're not using API tokens anymore

### 4. Test the Configuration

#### Method 1: Dry Run (Recommended)

Create a test release with a pre-release version:

```bash
# Update version in pyproject.toml to a pre-release (e.g., 7.6.2rc1)
# Then push a tag
git tag -a v7.6.2rc1 -m "Test OIDC publishing"
git push origin v7.6.2rc1

# Monitor the workflow at: https://github.com/ypollak2/llm-router/actions
# Once successful, delete the tag and release
git tag -d v7.6.2rc1
git push origin --delete v7.6.2rc1
```

#### Method 2: Check Workflow Permissions

In the GitHub Actions run log, verify:
- The OIDC token request succeeds
- PyPI accepts the token
- Package is published successfully

### 5. Remove Old API Token Secret

Once OIDC is working:

1. Go to GitHub repo Settings → Secrets and variables → Actions
2. Delete the `PYPI_TOKEN` secret (if it still exists)
3. Confirm no other workflows use it

## Troubleshooting

### "Trusted publisher not found"

**Cause**: The trusted publisher isn't registered on PyPI
**Fix**: Follow step 2 above, ensuring:
- Repository name is exact: `ypollak2/llm-router`
- Workflow name is exact: `publish.yml`
- Environment name is exact: `pypi`

### "OIDC token invalid"

**Cause**: Wrong repository or workflow name registered
**Fix**: Delete the pending publisher on PyPI and re-add with correct values

### "403 Forbidden"

**Cause**: OIDC token is valid but user doesn't have upload permission
**Fix**: Ensure your PyPI account is listed as a maintainer/owner on the project

## Additional Resources

- [PyPI: Trusted Publishers Documentation](https://docs.pypi.org/trusted-publishers/)
- [GitHub: OIDC in Actions](https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/about-security-hardening-with-openid-connect)
- [PEP 680: Trusted Publishers](https://www.python.org/dev/peps/pep-0680/)

## CI Integration

The publish workflow automatically:
1. Validates git tag matches `pyproject.toml` version
2. Builds wheel and source distributions
3. Publishes to PyPI using OIDC authentication
4. Skips if package already exists (via `skip_existing: true`)

No manual intervention is needed after OIDC is configured.

## Security Model

```
┌─────────────┐         ┌──────────────┐         ┌─────────────┐
│   GitHub    │────────▶│  GitHub OIDC  │────────▶│  PyPI      │
│   Actions   │         │   Provider   │         │ (Trusted)   │
└─────────────┘         └──────────────┘         └─────────────┘
     (CI)                  (Token)                  (Package)

 Sign with GitHub's key   Verify with PyPI's key
```

No secrets are transmitted in cleartext — only cryptographically signed tokens.
