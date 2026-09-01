#!/usr/bin/env node
'use strict';

// Thin launcher: hand off to the standalone binary fetched by postinstall.
//
// Uses spawnSync with stdio inherited rather than exec, because llm-router is
// an MCP stdio server — the host talks to it over stdin/stdout, and any
// buffering or re-encoding in between corrupts the protocol. The child's exit
// code is propagated verbatim so callers can branch on it.

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const bin = path.join(
  __dirname,
  '..',
  'vendor',
  'llm-router',
  process.platform === 'win32' ? 'llm-router.exe' : 'llm-router'
);

if (!fs.existsSync(bin)) {
  console.error(
    'llm-routing: the standalone binary is missing.\n' +
      '  This usually means postinstall was skipped (--ignore-scripts).\n' +
      '  Reinstall with scripts enabled, or use:  pip install llm-routing\n'
  );
  process.exit(1);
}

const result = spawnSync(bin, process.argv.slice(2), { stdio: 'inherit' });

if (result.error) {
  console.error(`llm-routing: could not run the binary: ${result.error.message}`);
  process.exit(1);
}

// A child killed by a signal has a null status; report it the way a shell does.
process.exit(result.status === null ? 1 : result.status);
