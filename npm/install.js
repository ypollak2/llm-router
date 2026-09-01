#!/usr/bin/env node
'use strict';

// postinstall: fetch the standalone binary for this platform.
//
// The binary is NOT vendored into the npm package. A single tarball carrying
// four platforms would be well over a gigabyte, and npm would hand every user
// the three they cannot run. Downloading the one that matches keeps the package
// a few kilobytes and the install a single archive.
//
// Every failure here is fatal and loud. A postinstall that swallows an error
// leaves `llm-router` on PATH as a command that does not work, and the user
// discovers it mid-session rather than at install time.

const fs = require('fs');
const os = require('os');
const path = require('path');
const https = require('https');
const { execFileSync } = require('child_process');

const VERSION = require('./package.json').version;
const REPO = 'ypollak2/llm-router';

// Matches the asset names produced by .github/workflows/binary.yml. If those
// change, this breaks loudly at install time rather than silently at run time.
const TARGETS = {
  'darwin-arm64': 'llm-router-macos-arm64.tar.gz',
  'darwin-x64': 'llm-router-macos-x86_64.tar.gz',
  'linux-x64': 'llm-router-linux-x86_64.tar.gz',
  'win32-x64': 'llm-router-windows-x86_64.zip',
};

function fail(message, detail) {
  console.error(`\nllm-routing: ${message}`);
  if (detail) console.error(`  ${detail}`);
  console.error(
    '\n  You can install without Node instead:\n' +
      '    pip install llm-routing\n' +
      '\n  Or report this with your platform:\n' +
      `    https://github.com/${REPO}/issues\n`
  );
  process.exit(1);
}

function download(url, dest, redirectsLeft = 5) {
  return new Promise((resolve, reject) => {
    https
      .get(url, { headers: { 'User-Agent': 'llm-routing-npm' } }, (res) => {
        // GitHub release assets redirect to a signed object-store URL.
        if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
          res.resume();
          if (redirectsLeft === 0) return reject(new Error('too many redirects'));
          return resolve(download(res.headers.location, dest, redirectsLeft - 1));
        }
        if (res.statusCode !== 200) {
          res.resume();
          return reject(new Error(`HTTP ${res.statusCode} for ${url}`));
        }
        const file = fs.createWriteStream(dest);
        res.pipe(file);
        file.on('finish', () => file.close(resolve));
        file.on('error', reject);
      })
      .on('error', reject);
  });
}

async function main() {
  const key = `${process.platform}-${process.arch}`;
  const asset = TARGETS[key];
  if (!asset) {
    fail(
      `no standalone binary is published for ${key}.`,
      'Supported: ' + Object.keys(TARGETS).join(', ')
    );
  }

  const url = `https://github.com/${REPO}/releases/download/v${VERSION}/${asset}`;
  const vendor = path.join(__dirname, 'vendor');
  fs.mkdirSync(vendor, { recursive: true });
  const archive = path.join(vendor, asset);

  console.log(`llm-routing: fetching ${asset} (v${VERSION})`);
  try {
    await download(url, archive);
  } catch (err) {
    fail(`could not download the binary for ${key}.`, `${err.message}\n  ${url}`);
  }

  try {
    if (asset.endsWith('.zip')) {
      execFileSync('powershell', [
        '-NoProfile',
        '-Command',
        `Expand-Archive -Force -Path "${archive}" -DestinationPath "${vendor}"`,
      ]);
    } else {
      execFileSync('tar', ['-xzf', archive, '-C', vendor]);
    }
  } catch (err) {
    fail('could not unpack the binary.', err.message);
  }

  fs.unlinkSync(archive);

  // Prove it runs before declaring success. An archive that extracts but cannot
  // execute — wrong arch, missing loader, Gatekeeper — is exactly the failure a
  // postinstall must not pass along silently.
  const bin = path.join(
    vendor,
    'llm-router',
    process.platform === 'win32' ? 'llm-router.exe' : 'llm-router'
  );
  if (!fs.existsSync(bin)) fail('the archive did not contain the expected binary.', bin);
  if (process.platform !== 'win32') fs.chmodSync(bin, 0o755);

  try {
    const out = execFileSync(bin, ['--version'], { encoding: 'utf8' }).trim();
    console.log(`llm-routing: installed ${out}`);
  } catch (err) {
    const hint =
      process.platform === 'darwin'
        ? 'On macOS this is usually Gatekeeper. Until the build is notarised, ' +
          'run:\n    xattr -dr com.apple.quarantine ' + path.dirname(bin)
        : err.message;
    fail('the binary was downloaded but will not run.', hint);
  }

  console.log('llm-routing: next step ->  llm-router install');
}

main().catch((err) => fail('unexpected error during install.', err.stack || err.message));
