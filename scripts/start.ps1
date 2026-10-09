$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path -LiteralPath '.venv')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '独立虚拟环境创建失败' }
}
& '.\.venv\Scripts\python.exe' -m pip install -r requirements-lock.txt
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败' }
& '.\.venv\Scripts\python.exe' scripts/init_instance.py
if ($LASTEXITCODE -ne 0) { throw '本地密钥初始化失败' }
docker compose -p airdrop-v2-dev up --build -d
if ($LASTEXITCODE -ne 0) { throw 'Docker 启动失败，请检查引擎状态' }
Write-Output 'AirDrop V2: http://127.0.0.1:18080；登录信息：secrets/dev-login.txt'
