#!/usr/bin/env node
'use strict';

// postinstall: fetch the standalone binary for this platform.
//
// The binary is NOT vendored into the npm package. A single tarball carrying
// four platforms would be well over a gigabyte, and npm would hand every user
// the three they cannot run. Downloading the one that matches keeps the package
// a few kilobytes and the install a single archive.
//
// Every failure to fetch or run the binary is fatal and loud. A postinstall that
// swallows an error leaves `llm-router` on PATH as a command that does not
// work, and the user discovers it mid-session rather than at install time.
//
// Once the binary runs, the postinstall wires the hosts it finds by running
// `llm-router install --auto` (v16 P2.12). That step is NOT fatal: the binary
// works, so `npm i` should not fail because, say, one host's config could not
// be written. The installer records the failure in its receipt; here it is a
// loud warning plus the manual command. LLM_ROUTER_NO_AUTO_INSTALL=1 skips it.

const fs = require('fs');
const os = require('os');
const path = require('path');
const https = require('https');
const { execFileSync, spawnSync } = require('child_process');

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

  runInstaller(bin, process.env);
}

const OPT_OUT = 'LLM_ROUTER_NO_AUTO_INSTALL';
const MANUAL = 'llm-router install';

// Run `<bin> install --auto` and echo its one `receipt:` line. Returns 0 in
// every case, including failure: wiring hosts is a convenience on top of a
// working binary, and a failed `npm i` would take the working binary away too.
// `spawn` is injectable so tests can observe the call without a real binary.
function runInstaller(bin, env = process.env, spawn = spawnSync) {
  const optOut = String(env[OPT_OUT] || '').trim().toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(optOut)) {
    console.log(`llm-routing: ${OPT_OUT} is set, so no host was wired.`);
    console.log(`llm-routing: to wire your hosts later, run:  ${MANUAL}`);
    // The plugin is an alternative only while nothing is wired: after a
    // successful `install --auto` it would register the same hooks twice.
    console.log(
      'llm-routing: or, inside Claude Code ->  /plugin marketplace add ypollak2/llm-router' +
        '  then  /plugin install llm-router@ypollak2'
    );
    return 0;
  }

  // stdin is closed: `--auto` never prompts, and if something did, EOF beats a
  // postinstall that hangs forever waiting for a keypress npm will not pass on.
  let res;
  try {
    res = spawn(bin, ['install', '--auto'], {
      env,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
    });
  } catch (err) {
    res = { error: err };
  }
  const stdout = String((res && res.stdout) || '');
  const stderr = String((res && res.stderr) || '');
  const receipt = stdout.split(/\r?\n/).find((line) => line.startsWith('receipt: '));
  if (receipt) console.log(`llm-routing: ${receipt}`);

  if (res && !res.error && res.status === 0) {
    if (!receipt) console.log('llm-routing: hosts wired (the installer printed no receipt line).');
    return 0;
  }

  const why = res && res.error
    ? res.error.message
    : res && res.signal
      ? `killed by ${res.signal}`
      : `exit code ${res ? res.status : 'unknown'}`;
  console.error(`\nllm-routing: WARNING: the binary is installed, but wiring your hosts failed (${why}).`);
  const detail = stderr.trim();
  if (detail) console.error(detail.split(/\r?\n/).slice(-20).map((l) => `  ${l}`).join('\n'));
  console.error(
    '  npm install still succeeded. To retry the host wiring, run:\n' +
      `    ${MANUAL}\n` +
      '  and check the result with:\n' +
      '    llm-router doctor\n'
  );
  return 0;
}

module.exports = { runInstaller };

if (require.main === module) {
  main().catch((err) => fail('unexpected error during install.', err.stack || err.message));
}
