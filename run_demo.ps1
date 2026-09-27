# run_demo.ps1 —— 一键开播演示（双 AI 主播）
#
# 用法（在仓库根目录）：
#   .\run_demo.ps1                    # 默认：脚本弹幕 + 扬声器出声，6 轮后自动下播
#   .\run_demo.ps1 -Mode stdin        # 自己打字当弹幕（回车发一条，/quit 下播）
#   .\run_demo.ps1 -Mode record       # 不出声，音频落盘 + 复盘报告 + history（适合发给别人看）
#   .\run_demo.ps1 -NoHandoff         # 关掉"搭档接话"
#   .\run_demo.ps1 -Keyword "CABLE"   # 装了虚拟声卡后指定设备关键字
#
# 若提示"禁止运行脚本"，用这条绕过：
#   powershell -ExecutionPolicy Bypass -File .\run_demo.ps1

param(
    [ValidateSet('listen', 'stdin', 'record')]
    [string]$Mode = 'listen',
    [int]$Turns = 6,
    [int]$ScriptInterval = 12,
    [string]$Keyword = 'Realtek',
    [switch]$NoHandoff,
    [switch]$NoMemory
)

$ErrorActionPreference = 'Stop'

# 仓库要求 Python >= 3.9；基础环境是 3.8，所以固定用 lumi_nox 环境
$py = "C:\Users\HP\miniconda3\envs\lumi_nox\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "找不到 Python：$py" -ForegroundColor Red
    Write-Host "请改成你自己的环境路径（仓库需要 Python >= 3.9）" -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path 'lumi.py')) {
    Write-Host "请在仓库根目录运行本脚本（当前：$(Get-Location)）" -ForegroundColor Red
    exit 1
}

# 中文输出在 PowerShell 5.1 下会乱码，这里固定 UTF-8
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$handoff = 0
if (-not $NoHandoff) { $handoff = 1 }

$args = @(
    'lumi.py',
    '--arch', 'duplex',
    '--handoff-every', "$handoff",
    '--turns', "$Turns"
)
if ($NoMemory) { $args += '--no-memory' }

switch ($Mode) {
    'listen' {
        Write-Host "== 演示：脚本弹幕 + 扬声器出声（关键字 '$Keyword'）==" -ForegroundColor Cyan
        $args += @('--danmaku', 'script', '--script-interval', "$ScriptInterval",
                   '--audio-device-keyword', $Keyword,
                   '--report', 'logs/demo/report.json')
    }
    'stdin' {
        Write-Host "== 演示：自己打字当弹幕（回车发一条；/quit 下播）==" -ForegroundColor Cyan
        Write-Host "   支持 弹幕：名字：内容 / SC：名字：内容 / 礼物：名字：内容" -ForegroundColor DarkGray
        $args += @('--danmaku', 'stdin', '--audio-device-keyword', $Keyword)
    }
    'record' {
        Write-Host "== 演示：不出声，音频落盘 + 报告 + history ==" -ForegroundColor Cyan
        $args += @('--danmaku', 'script', '--script-interval', "$ScriptInterval",
                   '--no-audio', '--duplex-wav-dir', 'logs/demo',
                   '--report', 'logs/demo/report.json',
                   '--dump-history', 'logs/demo/history')
    }
}

& $py @args
$code = $LASTEXITCODE
Write-Host ""
if ($code -eq 0) {
    Write-Host "演示结束（退出码 0）。复盘报告：logs/demo/report.json" -ForegroundColor Green
    if ($Mode -eq 'record') {
        Write-Host "音频是裸 PCM；想转 WAV 可以直接问我，或用播放器按 24000Hz/16bit/单声道 打开。" -ForegroundColor DarkGray
    }
} else {
    Write-Host "退出码 $code —— 若报缺少密钥：填 .env 后先跑 python lumi.py --doctor 体检" -ForegroundColor Yellow
}
