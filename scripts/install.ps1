# Agents Island — Windows 侧一键安装（以仓库根目录为基准）
# 用法: 右键"使用 PowerShell 运行"，或 powershell -ExecutionPolicy Bypass -File scripts\install.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

Write-Host "== Agents Island 安装 ==" -ForegroundColor Cyan

# 1. Python 检查
try { $pyv = python --version 2>&1 } catch { Write-Host "❌ 未找到 Windows Python，请先安装 https://www.python.org/downloads/（勾选 Add to PATH）" -ForegroundColor Red; exit 1 }
Write-Host "  Python: $pyv"

# 2. pywebview
python -m pip show pywebview *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  安装 pywebview ..."
    python -m pip install pywebview
} else { Write-Host "  pywebview: 已安装" }

# 3. WSL 侧 hooks（合并进 ~/.claude/settings.json）
$distroFile = Join-Path $root 'launch\distro.txt'
$distroArg = @()
if (Test-Path $distroFile) {
    $d = (Get-Content $distroFile -First 1).Trim()
    if ($d) { $distroArg = @('-d', $d) }
}
$hookScript = "python3 `"`$(wslpath '$root\scripts\install_hooks.py')`""
wsl.exe @distroArg -- bash -c $hookScript

# 4. 开机自启（可选）
$ans = Read-Host "  设置开机自启? (y/N)"
if ($ans -eq 'y') {
    $startup = [Environment]::GetFolderPath('Startup')
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut((Join-Path $startup 'AgentsIsland.lnk'))
    $lnk.TargetPath = Join-Path $root 'launch\AgentsIsland.vbs'
    $lnk.Save()
    Write-Host "  已加入开机自启: $startup"
}

Write-Host "✅ 安装完成。双击 launch\AgentsIsland.vbs 启动。" -ForegroundColor Green
