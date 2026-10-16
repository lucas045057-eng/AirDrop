// 端到端验收原生运行、日志续读、能力开关与账号资料导入；仅使用本地假 Docker。
import { chromium } from '../../frontend/node_modules/@playwright/test/index.mjs';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import net from 'node:net';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const output = path.resolve(process.env.AIRDROP_QA_OUTPUT || path.join(root, '../../work/native-browser'));
fs.mkdirSync(output, { recursive: true });
const owned = fs.mkdtempSync(path.join(output, 'run-'));
const py = process.env.AIRDROP_QA_PYTHON || path.join(root, '.venv', 'Scripts', 'python.exe');
const port = async () => {
  const server = net.createServer();
  await new Promise((resolve, reject) => server.listen(0, '127.0.0.1', (error) => error ? reject(error) : resolve()));
  const value = server.address().port;
  await new Promise((resolve) => server.close(resolve));
  return value;
};
const [apiPort, socketPort] = await Promise.all([port(), port()]);
const base = `http://127.0.0.1:${apiPort}`;
const fakeState = path.join(owned, 'fake-docker');
const env = {
  ...process.env,
  PYTHONIOENCODING: 'utf-8',
  AIRDROP_BROWSER_QA: 'local-test-only',
  AIRDROP_QA_NATIVE: 'local-test-only',
  AIRDROP_QA_API_PORT: String(apiPort),
  AIRDROP_DATABASE_URL: 'sqlite:///' + path.join(owned, 'qa.db').replaceAll('\\', '/'),
  AIRDROP_DATA_DIR: owned,
  AIRDROP_MASTER_KEY_FILE: path.join(owned, 'master.key'),
  AIRDROP_ADMIN_USERNAME: 'browser-qa',
  AIRDROP_ADMIN_PASSWORD: 'browser-test-only-1234',
  AIRDROP_EXECUTION_ENABLED: 'false',
  AIRDROP_NATIVE_EXECUTION_ENABLED: 'true',
  AIRDROP_NATIVE_SOCKET_PATH: `tcp://127.0.0.1:${socketPort}`,
  AIRDROP_NATIVE_RUN_ROOT: path.join(owned, 'runs'),
  AIRDROP_ALLOWED_ORIGINS: base,
  FAKE_DOCKER_STATE: fakeState,
};

function run(args) {
  const result = spawnSync(py, args, { cwd: root, env, encoding: 'utf-8', windowsHide: true });
  if (result.status !== 0) throw new Error(result.stderr || result.stdout || `命令失败：${args.join(' ')}`);
}

run(['-c', 'import os;from pathlib import Path;from cryptography.fernet import Fernet;Path(os.environ["AIRDROP_MASTER_KEY_FILE"]).write_bytes(Fernet.generate_key())']);
run(['-m', 'alembic', 'upgrade', 'head']);

const children = [];
const checks = [];
const pageErrors = [];
const responseErrors = [];
let browser;
let page;
function start(args, name) {
  const fd = fs.openSync(path.join(owned, `${name}.log`), 'w');
  const child = spawn(py, args, { cwd: root, env, stdio: ['ignore', fd, fd], windowsHide: true });
  children.push(child);
  return child;
}
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function ready(url) {
  for (let i = 0; i < 120; i += 1) {
    try { if ((await fetch(url)).ok) return; } catch {}
    await wait(250);
  }
  throw new Error(`本地夹具未启动：${url}`);
}
function check(name, condition) {
  if (!condition) throw new Error(name);
  checks.push(name);
}
async function apiJson(page, route, init) {
  const method = String(init?.method || 'GET').toUpperCase();
  const headers = { ...(init?.headers || {}), ...(method === 'GET' ? {} : { 'X-CSRF-Token': csrfToken }) };
  const result = await page.evaluate(async ({ route, method, headers, data }) => {
    const response = await fetch(route, {
      method,
      credentials: 'same-origin',
      headers,
      body: data === undefined ? undefined : JSON.stringify(data),
    });
    return { status: response.status, body: await response.text() };
  }, { route, method, headers, data: init?.data });
  if (result.status < 200 || result.status >= 300) throw new Error(`${route} 返回 ${result.status}: ${result.body.slice(0, 800)}`);
  return result.body ? JSON.parse(result.body) : {};
}
let csrfToken = '';
function logTail(name) {
  const file = path.join(owned, `${name}.log`);
  return fs.existsSync(file) ? fs.readFileSync(file, 'utf8').slice(-6000) : '';
}

try {
  start(['-m', 'tests.browser.serve'], 'api');
  await ready(`${base}/api/health`);
  start(['-m', 'tests.browser.serve', '--native-worker'], 'native-worker');
  browser = await chromium.launch({ headless: true, ...(process.env.AIRDROP_QA_CHROMIUM ? { executablePath: process.env.AIRDROP_QA_CHROMIUM } : {}) });
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
  page.setDefaultTimeout(20000);
  page.on('pageerror', (error) => pageErrors.push(error.message));
  page.on('response', async (response) => {
    if (response.url().includes('/api/') && response.status() >= 400) responseErrors.push({ url: response.url(), status: response.status(), body: (await response.text()).slice(0, 1000) });
  });
  await page.goto(base);
  await page.getByLabel('管理员账号').fill('browser-qa');
  await page.getByLabel('登录密码').fill('browser-test-only-1234');
  const loginResponsePromise = page.waitForResponse((response) => response.url().endsWith('/api/auth/login'));
  await page.getByRole('button', { name: /登录/ }).click();
  const loginResponse = await loginResponsePromise;
  if (!loginResponse.ok()) throw new Error(`登录失败：${loginResponse.status()}`);
  csrfToken = (await loginResponse.json()).csrf;
  await page.getByRole('button', { name: '脚本中心', exact: true }).click();

  const context = await apiJson(page, '/api/qa/native/context');
  const scriptRow = page.locator('tbody tr').filter({ hasText: 'qa-native-sample' });
  await scriptRow.getByRole('button', { name: '运行环境', exact: true }).click();
  const runtimePanels = page.locator('.runtime-config');
  await runtimePanels.first().getByText('原生执行开关：已启用', { exact: false }).waitFor();
  check('运行控制器在线且原生能力已启用', await runtimePanels.first().getByText('执行控制器：在线', { exact: false }).count() === 1);
  check('运行定义、审核与环境已就绪', await runtimePanels.first().getByText('运行审核已通过', { exact: true }).count() === 1 && await runtimePanels.first().getByText('环境就绪', { exact: true }).count() === 1);
  check('私钥交付有明确提示', await runtimePanels.first().getByText(/私钥写入脚本读取的文件字段/).count() === 1);
  const taskDetails = page.locator('details.runtime-task');
  for (let i = 0; i < await taskDetails.count(); i += 1) await taskDetails.nth(i).locator('summary').click();
  const saveButtons = page.getByRole('button', { name: '保存原生运行任务', exact: true });
  const enabledSaveIndex = await saveButtons.evaluateAll((buttons) => buttons.findIndex((button) => !button.disabled));
  const saveDisabled = await saveButtons.evaluateAll((buttons) => buttons.map((button) => button.disabled));
  check(`仅具备审核与环境的功能允许配置原生任务（按钮 ${JSON.stringify(saveDisabled)}）`, saveDisabled.length === 2 && enabledSaveIndex >= 0 && saveDisabled.filter(Boolean).length === 1);

  const activePanelIndex = await runtimePanels.evaluateAll((panels) => panels.findIndex((panel) => {
    const button = panel.querySelector('button[title="保存原生运行任务"]');
    return !!button && !button.disabled;
  }));
  const activePanel = runtimePanels.nth(activePanelIndex);
  await activePanel.getByLabel('原生任务项目').selectOption(context.project_id);
  await activePanel.getByLabel(`选择钱包 ${context.wallets[0].name}`).check();
  await activePanel.getByLabel(`选择钱包 ${context.wallets[1].name}`).check();
  await saveButtons.nth(enabledSaveIndex).click();
  await page.locator('.runtime-config [role="status"]').filter({ hasText: '原生运行任务已保存。' }).waitFor();
  check('界面可保存固定钱包的原生任务', true);
  await page.getByLabel('关闭').click();

  const tasks = await apiJson(page, '/api/tasks');
  const savedTask = tasks.find((task) => task.execution_backend === 'native' &&
    task.script_feature_id === context.feature_id &&
    task.wallet_selection?.wallet_ids?.length === 2);
  check(`可从业务 API 找到新建的原生任务（${JSON.stringify(savedTask)})`, typeof savedTask?.id === 'string');
  await page.getByRole('button', { name: '项目中心', exact: true }).click();
  const taskRow = page.locator('tbody tr').filter({ hasText: savedTask.name });
  const runResponsePromise = page.waitForResponse((response) =>
    response.request().method() === 'POST' && response.url().endsWith(`/api/tasks/${savedTask.id}/run`));
  await taskRow.getByRole('button', { name: '运行', exact: true }).click();
  const runResponse = await runResponsePromise;
  const started = await runResponse.json();
  if (!runResponse.ok()) throw new Error(`正式运行接口返回 ${runResponse.status()}: ${JSON.stringify(started)}`);
  check(`项目中心正式运行按钮创建了原生执行记录（${JSON.stringify(started)}）`, typeof started.execution_id === 'string' && started.execution_id.length > 0);
  let detail, workerState;
  for (let i = 0; i < 60; i += 1) {
    detail = await apiJson(page, `/api/executions/${started.execution_id}`);
    if (['COMPLETED', 'FAILED', 'UNKNOWN', 'MANUAL_REQUIRED'].includes(detail.status)) break;
    if (i % 10 === 0) workerState = await apiJson(page, '/api/qa/native/state');
    await wait(500);
  }
  check(`原生任务由独立 Worker 完成（状态 ${detail?.status}；夹具 ${JSON.stringify(workerState)}）`, detail?.status === 'COMPLETED' && detail.execution_backend === 'native');
  check('未回传钱包业务结果时不推断成功', detail?.counts?.NO_REPORT === 2 && detail.attempts?.[0]?.process_exit_code === 0);
  check('受控状态文件完成回写', detail?.state_status === 'COMMITTED');
  check('完整日志超过分页尺寸', detail?.attempts?.[0]?.log_bytes > 64 * 1024);

  await page.getByRole('button', { name: '执行中心', exact: true }).click();
  let executionRow = page.locator('tbody tr').filter({ hasText: savedTask.name });
  await executionRow.getByRole('button', { name: '明细', exact: true }).click();
  await page.getByText('后端 原生容器运行', { exact: true }).waitFor();
  await page.getByText(/每个钱包都是“未收到单钱包结果”/).waitFor();
  const logBox = page.locator('.exec-log-box');
  await page.getByText('QA-NATIVE-END', { exact: false }).waitFor();
  check('执行详情显示后端、钱包结果与日志尾部', true);
  const earlier = page.getByRole('button', { name: '加载较早内容', exact: true });
  for (let i = 0; i < 12 && !(await logBox.innerText()).includes('QA-NATIVE-START'); i += 1) {
    if (await earlier.isDisabled()) break;
    const previousFoot = await page.locator('.exec-log-foot').innerText();
    await earlier.click();
    await page.waitForFunction((previous) => document.querySelector('.exec-log-foot')?.innerText !== previous, previousFoot);
  }
  const logText = await logBox.innerText();
  const logFoot = await page.locator('.exec-log-foot').innerText();
  check(`日志可分页回读到开头（${logFoot}; 开头 ${JSON.stringify(logText.slice(0, 40))}; 结尾 ${JSON.stringify(logText.slice(-40))}）`, logText.includes('QA-NATIVE-START'));
  await page.getByText('参数快照与固定后的运行引用', { exact: true }).click();
  check('执行详情展示固定后的参数与镜像快照', await page.getByText('参数快照与固定后的运行引用', { exact: true }).count() === 1);

  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: '完整下载', exact: true }).click(),
  ]);
  const downloadPath = path.join(output, download.suggestedFilename());
  await download.saveAs(downloadPath);
  check('完整日志下载字节数与执行记录一致', fs.statSync(downloadPath).size === detail.attempts[0].log_bytes);

  await page.screenshot({ path: path.join(output, 'desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  check('执行详情页面没有横向溢出', await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
  await page.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });

  await page.reload();
  await page.getByRole('button', { name: '执行中心', exact: true }).click();
  executionRow = page.locator('tbody tr').filter({ hasText: savedTask.name });
  await executionRow.getByRole('button', { name: '明细', exact: true }).click();
  await page.getByText('QA-NATIVE-END', { exact: false }).waitFor();
  check('刷新页面后仍可从执行记录续读日志', true);

  await apiJson(page, '/api/qa/native/enabled', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    data: { enabled: false },
  });
  await page.reload();
  await page.getByRole('button', { name: '项目中心', exact: true }).click();
  const savedTaskRow = page.locator('tbody tr').filter({ hasText: savedTask.name });
  const runButton = savedTaskRow.getByRole('button', { name: '运行', exact: true });
  check('停用原生能力会禁用任务入口并说明原因', await runButton.isDisabled() && (await runButton.getAttribute('title')).includes('原生执行开关未启用'));
  await apiJson(page, '/api/qa/native/enabled', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    data: { enabled: true },
  });

  const project = page.locator('.project-card').filter({ hasText: context.project_name });
  await project.getByRole('button', { name: '项目资料', exact: true }).click();
  const accountDialog = page.getByRole('dialog', { name: '项目账号资料导入' });
  await accountDialog.getByLabel('导入目标脚本功能').selectOption(context.feature_id);
  const csv = await apiJson(page, '/api/qa/native/import-csv');
  await accountDialog.getByLabel('项目资料文件').setInputFiles({ name: csv.filename, mimeType: 'text/csv', buffer: Buffer.from(csv.text) });
  await accountDialog.getByLabel('字段映射').fill(JSON.stringify({ address: 'address', private_key: 'private_key', proxy: 'proxy' }));
  await accountDialog.getByRole('button', { name: '上传并预览', exact: true }).click();
  await accountDialog.getByText('新增 1', { exact: true }).waitFor();
  check('资料预览正确分类新增、复用、冲突和错误', await accountDialog.getByText('复用 1', { exact: true }).count() === 1 && await accountDialog.getByText('冲突 1', { exact: true }).count() === 1 && await accountDialog.getByText('错误 1', { exact: true }).count() === 1);
  await accountDialog.getByRole('button', { name: '确认导入', exact: true }).click();
  await accountDialog.getByText('还有 1 个冲突项未选择处理方式', { exact: false }).waitFor();
  await accountDialog.getByLabel(`${context.wallets[0].address.slice(0, 10)}… 覆盖`).check();
  await accountDialog.getByRole('button', { name: '确认导入', exact: true }).click();
  await accountDialog.getByText(/导入结果：新增 1 条、更新 2 条、跳过 0 条、新建钱包 0 个/).waitFor();
  await accountDialog.getByText(/修订 2/).nth(1).waitFor({ state: 'visible' });
  check('逐项确认冲突后导入完成且两个既有账号修订递增', await accountDialog.getByText(/修订 2/).count() === 2);

  await page.getByLabel('关闭项目资料导入').click();
  await page.getByRole('button', { name: '执行中心', exact: true }).click();
  check('浏览器流程没有未处理页面异常', pageErrors.length === 0);
  fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify({ status: 'PASS', checks, pageErrors, execution_id: started.execution_id, log_bytes: detail.attempts[0].log_bytes, state_status: detail.state_status }, null, 2));
  console.log(JSON.stringify({ status: 'PASS', checks: checks.length, output }));
} catch (error) {
  const report = { status: 'FAIL', checks, error: String(error), pageErrors, responseErrors, runtime_text: page ? await page.locator('.runtime-config').allInnerTexts().catch(() => []) : [], api_log_tail: logTail('api'), worker_log_tail: logTail('native-worker') };
  fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2));
  console.error(JSON.stringify({ status: 'FAIL', checks: checks.length, error: String(error), output }));
  process.exitCode = 1;
} finally {
  if (browser) await browser.close();
  for (const child of children) {
    if (!child.pid) continue;
    if (process.platform === 'win32') spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
    else child.kill('SIGTERM');
  }
}
