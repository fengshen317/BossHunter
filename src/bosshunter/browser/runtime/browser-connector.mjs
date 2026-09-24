#!/usr/bin/env node
// Windows 宿主机连接器：启动独立浏览器 Profile，并将本地 CDP 安全桥接给 Docker。

import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';

const cwd = process.cwd();
const envFile = path.join(cwd, '.env.docker');
const dataDir = path.join(cwd, 'docker-data');
const pidFile = path.join(dataDir, 'browser-connector.pid');
const profileDir = path.join(dataDir, 'browser-profile');

function parseEnvFile(filePath) {
  if (!fs.existsSync(filePath)) return {};
  return Object.fromEntries(
    fs.readFileSync(filePath, 'utf8').split(/\r?\n/).filter((line) => line && !line.startsWith('#'))
      .map((line) => { const index = line.indexOf('='); return [line.slice(0, index), line.slice(index + 1)]; }),
  );
}

function randomSecret(bytes = 32) {
  return crypto.randomBytes(bytes).toString('base64url');
}

function initEnvironment() {
  if (fs.existsSync(envFile)) {
    console.log('[BossHunter] .env.docker 已存在，保留当前凭据。');
    return;
  }
  const content = [
    '# 由 Browser Connector 生成，请勿提交到版本库',
    `POSTGRES_PASSWORD=${randomSecret()}`,
    `BOSSHUNTER_BROWSER_PROXY_TOKEN=${randomSecret()}`,
    '',
  ].join(os.EOL);
  fs.writeFileSync(envFile, content, { encoding: 'utf8', flag: 'wx', mode: 0o600 });
  console.log('[BossHunter] 已创建 .env.docker。');
}

function browserCandidates(explicitPath) {
  if (explicitPath) return [explicitPath];
  const roots = [process.env.PROGRAMFILES, process.env['PROGRAMFILES(X86)'], process.env.LOCALAPPDATA].filter(Boolean);
  const relativePaths = [
    'Google/Chrome/Application/chrome.exe',
    'Microsoft/Edge/Application/msedge.exe',
  ];
  return relativePaths.flatMap((relativePath) => roots.map((root) => path.join(root, relativePath)));
}

function portAvailable(port, host = '127.0.0.1') {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once('error', () => resolve(false));
    server.once('listening', () => server.close(() => resolve(true)));
    server.listen(port, host);
  });
}

function requestJson(url, token = '') {
  return new Promise((resolve) => {
    const request = http.get(url, { headers: token ? { Authorization: `Bearer ${token}` } : {} }, (response) => {
      let body = '';
      response.on('data', (chunk) => { body += chunk; });
      response.on('end', () => {
        try { resolve({ statusCode: response.statusCode, body: JSON.parse(body) }); }
        catch { resolve({ statusCode: response.statusCode, body: null }); }
      });
    });
    request.on('error', () => resolve(null));
    request.setTimeout(2500, () => request.destroy());
  });
}

async function waitForCdp(port, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const result = await requestJson(`http://127.0.0.1:${port}/json/version`);
    if (result?.statusCode === 200 && result.body?.webSocketDebuggerUrl) return true;
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  return false;
}

async function startConnector(args) {
  initEnvironment();
  const config = parseEnvFile(envFile);
  const token = config.BOSSHUNTER_BROWSER_PROXY_TOKEN;
  if (!token) throw new Error('.env.docker 缺少 BOSSHUNTER_BROWSER_PROXY_TOKEN');
  const proxyPort = Number(config.BOSSHUNTER_BROWSER_PROXY_PORT || 3456);
  const chromePort = Number(config.BOSSHUNTER_CHROME_PORT || 9222);
  if (!(await portAvailable(proxyPort, '0.0.0.0'))) throw new Error(`端口 ${proxyPort} 已被占用，不会结束现有进程。`);

  const explicitIndex = args.indexOf('--browser-path');
  const explicitPath = explicitIndex >= 0 ? args[explicitIndex + 1] : '';
  const browserPath = browserCandidates(explicitPath).find((candidate) => candidate && fs.existsSync(candidate));
  if (!browserPath) throw new Error('未找到 Chrome 或 Edge；可使用 --browser-path 指定浏览器路径。');

  const existingCdp = await requestJson(`http://127.0.0.1:${chromePort}/json/version`);
  const cdpReady = existingCdp?.statusCode === 200 && existingCdp.body?.webSocketDebuggerUrl;
  if (!cdpReady) {
    if (!(await portAvailable(chromePort))) {
      throw new Error(`端口 ${chromePort} 已被其他程序占用，但不是可用的浏览器 CDP；不会结束现有进程。`);
    }
    fs.mkdirSync(profileDir, { recursive: true });
    const browser = spawn(browserPath, [
      `--remote-debugging-port=${chromePort}`,
      `--user-data-dir=${profileDir}`,
      '--no-first-run',
      '--no-default-browser-check',
      'http://127.0.0.1:8686',
    ], { detached: true, stdio: 'ignore' });
    browser.unref();
  }
  if (!cdpReady && !(await waitForCdp(chromePort, 30000))) {
    throw new Error(`浏览器 CDP 端口 ${chromePort} 未在 30 秒内就绪。`);
  }

  fs.mkdirSync(dataDir, { recursive: true });
  fs.writeFileSync(pidFile, String(process.pid), 'utf8');
  process.env.BOSSHUNTER_BROWSER_PROXY_HOST = '0.0.0.0';
  process.env.BOSSHUNTER_BROWSER_PROXY_PORT = String(proxyPort);
  process.env.BOSSHUNTER_BROWSER_PROXY_TOKEN = token;
  process.env.BOSSHUNTER_CHROME_PORTS = String(chromePort);
  await import('./cdp-proxy.mjs');
}

async function health() {
  const config = parseEnvFile(envFile);
  const token = config.BOSSHUNTER_BROWSER_PROXY_TOKEN || '';
  const proxyPort = Number(config.BOSSHUNTER_BROWSER_PROXY_PORT || 3456);
  const chromePort = Number(config.BOSSHUNTER_CHROME_PORT || 9222);
  const cdp = await requestJson(`http://127.0.0.1:${chromePort}/json/version`);
  if (!cdp) throw new Error('浏览器未启动或 CDP 未就绪。');
  const runtime = await requestJson(`http://127.0.0.1:${proxyPort}/targets`, token);
  if (!runtime) throw new Error('Browser Runtime 未就绪。');
  if (runtime.statusCode === 401) throw new Error('Browser Runtime Token 不匹配。');
  if (runtime.statusCode !== 200) throw new Error(`Browser Runtime 状态异常：HTTP ${runtime.statusCode}`);
  console.log('[BossHunter] Browser Connector 运行正常。');
}

function stopConnector() {
  if (!fs.existsSync(pidFile)) {
    console.log('[BossHunter] Browser Connector 未运行。');
    return;
  }
  const pid = Number(fs.readFileSync(pidFile, 'utf8').trim());
  try { process.kill(pid); } catch (error) {
    if (error.code !== 'ESRCH') throw error;
  }
  fs.rmSync(pidFile, { force: true });
  console.log('[BossHunter] 已停止 Browser Connector；浏览器与 Profile 保持不变。');
}

function usage() {
  console.log('用法: bosshunter-browser-connector <init|start|health|stop> [--browser-path PATH]');
}

async function main() {
  const [command, ...args] = process.argv.slice(2);
  if (command === 'init') initEnvironment();
  else if (command === 'start') await startConnector(args);
  else if (command === 'health') await health();
  else if (command === 'stop') stopConnector();
  else { usage(); process.exitCode = command ? 1 : 0; }
}

main().catch((error) => {
  console.error(`[BossHunter] ${error.message}`);
  process.exitCode = 1;
});
