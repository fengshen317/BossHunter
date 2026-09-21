#!/usr/bin/env node
// 使用当前 Windows Node.js 生成单文件 Connector，供 Release ZIP 直接分发。

import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(path.join(root, 'tools', 'browser-connector', 'package.json'));
const { build } = require('esbuild');
const outputDir = path.join(root, 'dist', 'connector');
const bundle = path.join(outputDir, 'browser-connector.bundle.cjs');
const blob = path.join(outputDir, 'bosshunter-browser-connector.blob');
const executable = path.join(outputDir, 'bosshunter-browser-connector.exe');
const seaConfig = path.join(root, 'scripts', 'sea-config.json');

if (process.platform !== 'win32') throw new Error('Browser Connector EXE 只能在 Windows 上构建。');
fs.mkdirSync(outputDir, { recursive: true });

await build({
  entryPoints: [path.join(root, 'src', 'bosshunter', 'browser', 'runtime', 'browser-connector.mjs')],
  outfile: bundle,
  bundle: true,
  platform: 'node',
  target: 'node22',
  format: 'cjs',
});

const sea = spawnSync(process.execPath, ['--experimental-sea-config', seaConfig], { cwd: root, stdio: 'inherit' });
if (sea.status !== 0) process.exit(sea.status ?? 1);
fs.copyFileSync(process.execPath, executable);

const postject = path.join(root, 'tools', 'browser-connector', 'node_modules', '.bin', 'postject.cmd');
const injection = spawnSync(
  postject,
  [executable, 'NODE_SEA_BLOB', blob, '--sentinel-fuse', 'NODE_SEA_FUSE_fce680ab2cc467b6e072b8b5df1996b2'],
  { cwd: root, stdio: 'inherit', shell: true },
);
if (injection.status !== 0) process.exit(injection.status ?? 1);
console.log(`已生成 ${executable}`);
