# GPG Signing Setup for Releases

This guide explains how to set up GPG signing for package releases. When enabled, all wheel and source distributions are signed with GPG before publishing to PyPI.

## Why Sign Releases?

GPG signing provides:
- ✅ **Authenticity**: Proves the package came from your key
- ✅ **Integrity**: Detects tampering during download/storage
- ✅ **Non-repudiation**: You cannot deny creating the release
- ✅ **User verification**: Users can verify packages before installation

## Prerequisites

- GPG 2.x installed locally
- An existing GPG key or willingness to create one
- GitHub repository admin access

## Step 1: Create or Export Your GPG Key

### Option A: Create a New GPG Key

```bash
gpg --gen-key
```

Follow the prompts to create a key. Recommended settings:
- Key type: RSA (4096 bit)
- Validity: 3 years (set an expiration and renew before it expires)
- Name: Your project name or GitHub username
- Email: Your GitHub account email
- Passphrase: Use a strong passphrase

### Option B: Use an Existing Key

List your keys:
```bash
gpg --list-secret-keys
```

Choose the key ID you want to use.

## Step 2: Export the Private Key to GitHub

Export your private key in ASCII format:

```bash
# Replace YOUR_KEY_ID with your actual key ID
gpg --armor --export-secret-key YOUR_KEY_ID > private-key.asc
```

**⚠️ CRITICAL SECURITY**
- Keep `private-key.asc` secret
- Never commit it to git
- Delete it locally after uploading to GitHub

## Step 3: Configure GitHub Secrets

1. Go to GitHub repo → **Settings** → **Secrets and variables** → **Actions**

2. Create `GPG_PRIVATE_KEY` secret:
   - Click "New repository secret"
   - Name: `GPG_PRIVATE_KEY`
   - Value: Paste the contents of `private-key.asc` (including `-----BEGIN` and `-----END` lines)
   - Click "Add secret"

3. Create `GPG_PASSPHRASE` secret:
   - Click "New repository secret"
   - Name: `GPG_PASSPHRASE`
   - Value: Your GPG key's passphrase
   - Click "Add secret"

4. Enable GPG signing variable:
   - Click "New repository variable"
   - Name: `GPG_SIGNING_ENABLED`
   - Value: `true`
   - Click "Add variable"

## Step 4: Verify Configuration

The publish workflow will now:
1. Check if `GPG_SIGNING_ENABLED` is true
2. Import the private key from the secret
3. Sign each distribution file (`.whl`, `.tar.gz`)
4. Create `.asc` signature files
5. Upload both distributions and signatures to PyPI

## Step 5: Verify Installation

Users can verify your releases:

```bash
# Download package and signature
pip install claude-code-llm-router==7.6.1
pip download claude-code-llm-router==7.6.1 --no-deps

# Import your public key (publish it on keyservers)
gpg --recv-key YOUR_KEY_ID

# Verify the signature
gpg --verify claude_code_llm_router-7.6.1-py3-none-any.whl.asc
```

## Publishing Your Public Key

Make your public key publicly available:

```bash
# Export public key
gpg --armor --export YOUR_KEY_ID > public-key.asc

# Upload to MIT's key server
gpg --send-keys --keyserver pgp.mit.edu YOUR_KEY_ID

# Or upload to keys.openpgp.org
# Visit https://keys.openpgp.org and paste your key
```

Add the public key fingerprint to your project's `SECURITY.md`:

```markdown
## Release Signing

All releases are signed with GPG key:
```
gpg --fingerprint YOUR_KEY_ID
```

Users can verify packages before installation.
```

## Rotating Your Key

Before your key expires:

1. Create a new GPG key (follow Step 1)
2. Update GitHub secrets with the new private key
3. Tag a new release - it will use the new key
4. Update `SECURITY.md` with the new fingerprint
5. Consider signing the transition with the old key

## Troubleshooting

### "GPG: command not found"

Install GPG:
```bash
# macOS
brew install gnupg

# Ubuntu/Debian
sudo apt-get install gpg

# Windows (GitHub Actions)
# Pre-installed in ubuntu-latest images
```

### "No secret/variable found"

- Verify you created the secrets in the correct GitHub repo
- Check the exact names: `GPG_PRIVATE_KEY`, `GPG_PASSPHRASE`, `GPG_SIGNING_ENABLED`
- Make sure the variable `GPG_SIGNING_ENABLED` is set to `true`

### "Bad passphrase"

The passphrase must match your GPG key exactly. Re-export and check:
```bash
gpg --list-secret-keys --keyid-format=long
```

### Workflow Still Not Signing

1. Check GitHub Actions run logs for the "Sign distributions" step
2. Verify `GPG_SIGNING_ENABLED` variable exists and is `true`
3. Verify secrets `GPG_PRIVATE_KEY` and `GPG_PASSPHRASE` exist
4. Temporarily add debug output:
   ```yaml
   - run: echo "GPG signing enabled: ${{ vars.GPG_SIGNING_ENABLED }}"
   - run: echo "Has GPG key secret: ${{ secrets.GPG_PRIVATE_KEY != '' }}"
   ```

## Disabling Signing

To turn off GPG signing:

- **Option 1**: Delete `GPG_SIGNING_ENABLED` variable
- **Option 2**: Set `GPG_SIGNING_ENABLED` to `false`
- **Option 3**: Delete the GPG secrets (the workflow will skip signing automatically)

The release will still publish normally; signatures just won't be created.

## Security Best Practices

✅ **DO:**
- Use a strong passphrase (16+ characters)
- Rotate your key every 2-3 years
- Keep the private key secure
- Announce your key fingerprint in documentation
- Sign major releases, especially security releases

❌ **DON'T:**
- Commit `private-key.asc` to git
- Share your passphrase
- Use the same key for multiple projects
- Publish your passphrase anywhere
- Ignore key rotation deadlines

## Additional Resources

- [GnuPG Manual](https://gnupg.org/documentation/manuals.html)
- [Python: Signing Distributions](https://packaging.python.org/specifications/digital-signatures/)
- [PyPI Signed Distributions](https://warehouse.pypa.io/)
