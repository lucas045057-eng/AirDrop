// Real browser workflow with a local, injected fake provider and owned test database.
import { chromium } from '../../frontend/node_modules/@playwright/test/index.mjs';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import net from 'node:net';
import { fileURLToPath } from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../..');
const output=path.resolve(process.env.AIRDROP_QA_OUTPUT || path.join(root,'../../work/smart-browser'));
fs.mkdirSync(output,{recursive:true});
const owned=fs.mkdtempSync(path.join(output,'run-'));
const py=process.env.AIRDROP_QA_PYTHON || path.resolve(root,'../../work/venv/Scripts/python.exe');
const port=async()=>{const s=net.createServer();await new Promise(resolve=>s.listen(0,'127.0.0.1',resolve));const p=s.address().port;await new Promise(resolve=>s.close(resolve));return p;};
const apiPort=await port(),webPort=await port();
const env={...process.env,PYTHONIOENCODING:'utf-8',AIRDROP_BROWSER_QA:'local-test-only',AIRDROP_QA_API_PORT:String(apiPort),
  AIRDROP_DATABASE_URL:'sqlite:///'+path.join(owned,'qa.db').replaceAll('\\','/'),AIRDROP_DATA_DIR:owned,
  AIRDROP_MASTER_KEY_FILE:path.join(owned,'master.key'),AIRDROP_ADMIN_USERNAME:'browser-qa',
  AIRDROP_ADMIN_PASSWORD:'browser-test-only-1234',AIRDROP_EXECUTION_ENABLED:'false',AIRDROP_SECURE_COOKIE:'false',
  AIRDROP_ALLOWED_ORIGINS:`http://127.0.0.1:${webPort}`,
  AIRDROP_DEV_API_TARGET:`http://127.0.0.1:${apiPort}`};
const run=(args)=>{const r=spawnSync(py,args,{cwd:root,env,encoding:'utf-8',windowsHide:true});if(r.status!==0)throw new Error(r.stderr);};
run(['-c','import os;from pathlib import Path;from cryptography.fernet import Fernet;Path(os.environ["AIRDROP_MASTER_KEY_FILE"]).write_bytes(Fernet.generate_key())']);
run(['-m','alembic','upgrade','head']);
const children=[],checks=[],pageErrors=[],metrics=[];
function start(exe,args,cwd,name){const fd=fs.openSync(path.join(owned,name+'.log'),'w');const child=spawn(exe,args,{cwd,env,stdio:['ignore',fd,fd],windowsHide:true});children.push(child);return child;}
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function ready(url){for(let i=0;i<100;i++){try{if((await fetch(url)).ok)return;}catch{}await wait(250);}throw new Error('Local fixture failed to start: '+url);}
function check(name,condition){if(!condition)throw new Error(name);checks.push(name);}
let browser;
try {
  start(py,['-m','tests.browser.serve'],root,'api');
  start(process.execPath,[path.join(root,'frontend/node_modules/vite/bin/vite.js'),'--host','127.0.0.1','--port',String(webPort),'--strictPort'],path.join(root,'frontend'),'web');
  const base=`http://127.0.0.1:${webPort}`;
  await Promise.all([ready(`http://127.0.0.1:${apiPort}/api/health`),ready(base)]);
  start(py,['-m','tests.browser.serve','--worker'],root,'adapter');
  browser=await chromium.launch({headless:true,...(process.env.AIRDROP_QA_CHROMIUM?{executablePath:process.env.AIRDROP_QA_CHROMIUM}:{})});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  page.on('pageerror',e=>pageErrors.push(e.message));
  await page.goto(base);
  await page.getByLabel('管理员账号').fill('browser-qa');
  await page.getByLabel('登录密码').fill('browser-test-only-1234');
  await page.getByRole('button',{name:/登录/}).click();
  await page.getByRole('button',{name:'脚本中心',exact:true}).click();
  const menu='def register(count:int=1): return count\ndef checkin(): return True\nasync def points(): return 5\ndef main():\n choice=input("请选择功能")\n if choice=="1": register()\n elif choice=="2": checkin()\n elif choice=="3": points()\nif __name__=="__main__": main()\n';
  async function upload(name,source){
    if(await page.getByRole('button',{name:'返回脚本中心'}).count())await page.getByRole('button',{name:'返回脚本中心'}).click();
    await page.getByRole('button',{name:'上传脚本',exact:true}).click();
    await page.getByLabel('选择文件',{exact:true}).setInputFiles({name:name+'.py',mimeType:'text/x-python',buffer:Buffer.from(source)});
    await page.getByRole('button',{name:'确认提交'}).click();
    await page.getByRole('dialog').waitFor({state:'hidden',timeout:30000});
    await page.locator('tbody tr').filter({hasText:name}).getByRole('button',{name:'智能分析',exact:true}).click();
    await page.getByRole('button',{name:'开始结构发现'}).click();
    await page.getByText('2 · 选择需要适配的功能',{exact:true}).waitFor({timeout:30000});
  }
  async function aiDraft(){
    for(const box of await page.getByRole('checkbox',{name:/^选择功能/}).all())await box.check();
    await page.getByRole('checkbox',{name:/使用已有 AI Provider/}).check();
    await page.getByRole('button',{name:/生成所选功能/}).click();
    await page.getByTestId('ai-preview').waitFor({timeout:30000});
  }
  await upload('browser_menu',menu);
  check('UI upload and three static features',(await page.getByRole('checkbox',{name:/^选择功能/}).count())===3);
  await aiDraft();
  check('Explicit AI approval required',await page.getByRole('button',{name:'批准此内容并继续'}).isDisabled());
  const beforeHash=await page.getByLabel('预览 SHA-256').inputValue();
  await page.reload();
  await page.getByTestId('ai-preview').waitFor({timeout:15000});
  check('Refresh restores reviewed snapshot',beforeHash===await page.getByLabel('预览 SHA-256').inputValue());
  await page.getByLabel('确认 AI 发送内容').check();
  await page.getByRole('button',{name:'批准此内容并继续'}).click();
  await page.getByRole('button',{name:'查看分页 Diff'}).waitFor({timeout:30000});
  await page.getByRole('button',{name:'查看分页 Diff'}).click();
  check('Real generated source shown',await page.locator('.smart-diff pre').innerText().then(s=>s.includes('run_feature')&&s.includes('_business')));
  await page.getByLabel('确认已审核 Diff').check();
  const labels=await page.getByRole('button',{name:/^批准功能 /}).allTextContents();
  for(const label of labels)await page.getByRole('button',{name:label,exact:true}).click();
  await page.getByRole('button',{name:'将已批准功能保存为新版本'}).click();
  await page.getByLabel('已审核功能').waitFor();
  const options=await page.getByLabel('已审核功能').locator('option').evaluateAll(rows=>rows.filter(r=>r.value).map(r=>({value:r.value,text:r.textContent})));
  check('Three independent reviewed features saved',options.length===3);
  await page.getByLabel('已审核功能').selectOption(options[0].value);
  check('Entrypoint automatically populated',(await page.getByLabel('自动关联入口').inputValue()).startsWith('airdrop_generated/'));
  const firstEntry=await page.getByLabel('自动关联入口').inputValue();
  await page.getByLabel('已审核功能').selectOption(options.find(o=>o.text==='register').value);
  check('Choosing another feature changes entry',firstEntry!==await page.getByLabel('自动关联入口').inputValue());
  check('Only explicit source default is prefilled',(await page.getByLabel('参数 count').inputValue())==='1');
  await page.getByLabel('参数 count').fill('3');
  await page.getByLabel('功能任务项目').selectOption('__new__');
  await page.getByLabel('新项目名称',{exact:true}).fill('browser-owned-project');
  await page.getByRole('button',{name:'保存功能任务配置'}).click();
  await page.getByText('功能任务已保存，可在项目中心查看；执行保持关闭',{exact:true}).waitFor();
  check('Functional task configuration saved',true);
  const tasks=await (await page.request.get(base+'/api/tasks')).json();
  check('Task snapshot binds feature and code',tasks.length===1&&!!tasks[0].script_feature_id&&tasks[0].feature_config_snapshot.execution_enabled===false);
  check('Typed business parameter saved',tasks[0].feature_config_snapshot.parameters.count===3);
  const jobs=await (await page.request.get(base+'/api/analysis-jobs')).json();
  for(const j of jobs){const d=await (await page.request.get(base+'/api/analysis-jobs/'+j.id)).json(); if(d.result.resources)metrics.push(d.result.resources);if(d.result.validation_resources)metrics.push(d.result.validation_resources);}
  await page.evaluate(()=>window.scrollTo(0,0));
  await page.screenshot({path:path.join(output,'desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  check('Mobile page has no horizontal overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
  await page.screenshot({path:path.join(output,'mobile.png'),fullPage:true});
  await page.setViewportSize({width:1440,height:1000});
  await page.reload(); await page.getByLabel('已审核功能').waitFor();
  check('Saved version survives refresh',await page.getByLabel('已审核功能').locator('option').count()===4);
  await upload('browser_model_failure','def model_failure(): return 1\nif __name__=="__main__": model_failure()\n');
  await aiDraft(); await page.getByLabel('确认 AI 发送内容').check(); await page.getByRole('button',{name:'批准此内容并继续'}).click();
  await page.locator('.smart-progress strong').filter({hasText:'分析失败'}).waitFor({timeout:30000});
  check('Invalid AI output gives visible failure',await page.locator('.smart-warning').innerText().then(s=>s.includes('原版本保留')));
  await upload('browser_cancel','def slow_function(): return 1\nif __name__=="__main__": slow_function()\n');
  await aiDraft(); await page.getByLabel('确认 AI 发送内容').check(); await page.getByRole('button',{name:'批准此内容并继续'}).click();
  await page.locator('.smart-progress strong').filter({hasText:'模型规划中'}).waitFor({timeout:30000});
  await page.getByRole('button',{name:'取消当前分析'}).click();
  await page.locator('.smart-progress strong').filter({hasText:'已取消'}).waitFor({timeout:10000});
  check('Cancel terminates pending model request',true);
  await upload('browser_manual','def main(): return input("OTP 验证码")\nif __name__=="__main__": main()\n');
  check('Captcha receives concrete manual blocker',await page.locator('.smart-feature .smart-warning').innerText().then(s=>s.includes('交互')&&s.includes('验证码')));
  await page.getByRole('checkbox',{name:/^选择功能/}).check();
  await page.getByRole('button',{name:/生成所选功能/}).click();
  await page.getByRole('button',{name:'查看分页 Diff'}).waitFor({timeout:30000});
  await page.getByRole('button',{name:'查看分页 Diff'}).click();
  check('Manual feature cannot be approved',await page.getByRole('button',{name:/^批准功能 /}).isDisabled());
  await page.getByLabel('确认已审核 Diff').check();
  await page.getByRole('button',{name:'拒绝此功能'}).click();
  check('Rejected feature cannot be saved',await page.getByRole('button',{name:'将已批准功能保存为新版本'}).isDisabled());
  const scripts=await (await page.request.get(base+'/api/scripts')).json();
  check('Failures do not add partial script versions',scripts.length===5);
  const health=await (await page.request.get(base+'/api/health')).json();
  check('Management execution remains closed',health.execution_enabled===false);
  check('Browser has no runtime errors',pageErrors.length===0);
  const apiResources=await (await page.request.get(base+'/api/qa/resources')).json();
  const workerResources=JSON.parse(fs.readFileSync(path.join(owned,'worker-resources.json'),'utf-8'));
  const fakeCalls=JSON.parse(fs.readFileSync(path.join(owned,'fake-calls.json'),'utf-8'));
  check('Model calls are bounded to one per approved job',fakeCalls===3);
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify({status:'PASS',checks,metrics,apiResources,workerResources,pageErrors,isolated:true,worker_separate_process:true,fake_ai_calls:fakeCalls,external_ai_calls:0},null,2));
  console.log(JSON.stringify({status:'PASS',checks:checks.length,output}));
} catch(error){fs.writeFileSync(path.join(output,'report.json'),JSON.stringify({status:'FAIL',checks,error:String(error),pageErrors},null,2));console.error(String(error));process.exitCode=1;}
finally {
  if(browser)await browser.close();
  for(const child of children)if(child.pid){if(process.platform==='win32')spawnSync('taskkill',['/PID',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'ignore'});else child.kill('SIGTERM');}
}
