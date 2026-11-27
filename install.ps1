# 安装到本机：复制到 %LOCALAPPDATA%\MusicDesk，并创建桌面 / 开始菜单快捷方式
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
$src = Join-Path $here 'dist\MusicDesk'
$exeName = 'MusicDesk.exe'

if (-not (Test-Path (Join-Path $src $exeName))) {
    Write-Host "× 找不到 dist\MusicDesk\$exeName" -ForegroundColor Red
    Write-Host "  请先打包：python -m PyInstaller ... desktop.py（见 README）" -ForegroundColor DarkGray
    exit 1
}

$dest = Join-Path $env:LOCALAPPDATA 'MusicDesk'
Write-Host "→ 安装到 $dest" -ForegroundColor Cyan

# 程序在运行就先关掉，否则 exe 覆盖不了
Get-Process -Name 'MusicDesk' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 600

New-Item -ItemType Directory -Force -Path $dest | Out-Null
# /XD 排除用户数据目录，重装不丢下载的歌
robocopy $src $dest /E /XD data downloads /XF *.log /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { Write-Host "× 复制文件失败 (robocopy $LASTEXITCODE)" -ForegroundColor Red; exit 1 }

if (Test-Path (Join-Path $here 'icon.ico')) {
    Copy-Item (Join-Path $here 'icon.ico') (Join-Path $dest 'icon.ico') -Force
}

$exe = Join-Path $dest $exeName
$ico = Join-Path $dest 'icon.ico'
if (-not (Test-Path $ico)) { $ico = $exe }

$ws = New-Object -ComObject WScript.Shell
$targets = @(
    @{ Dir = [Environment]::GetFolderPath('Desktop'); Name = '音乐下载台.lnk' },
    @{ Dir = (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'); Name = '音乐下载台.lnk' }
)
foreach ($t in $targets) {
    if (-not (Test-Path $t.Dir)) { continue }
    # 先清掉旧名字留下的快捷方式，免得桌面出现两个图标
    $oldPath = Join-Path $t.Dir '音乐工具台.lnk'
    if (Test-Path $oldPath) { Remove-Item $oldPath -Force -ErrorAction SilentlyContinue }
    $path = Join-Path $t.Dir $t.Name
    $lnk = $ws.CreateShortcut($path)
    $lnk.TargetPath = $exe
    $lnk.WorkingDirectory = $dest
    $lnk.IconLocation = $ico
    $lnk.Description = '音乐下载台 — 贴链接 / 说歌名 自动抓 MP3'
    $lnk.WindowStyle = 1
    $lnk.Save()
    Write-Host "  ✓ $path" -ForegroundColor DarkGray
}

$size = [math]::Round(((Get-ChildItem $dest -Recurse -File | Measure-Object Length -Sum).Sum) / 1MB, 1)
Write-Host ""
Write-Host "✓ 安装完成（$size MB）" -ForegroundColor Green
Write-Host "  桌面双击「音乐下载台」即可启动" -ForegroundColor Green
Write-Host "  下载的 MP3 在：$dest\downloads" -ForegroundColor DarkGray
Write-Host ""