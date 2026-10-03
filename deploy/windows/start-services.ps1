<#
.SYNOPSIS
    看有(高齡家庭文書輔助):啟動 Ollama、網頁與 Cloudflare Tunnel,之後持續監看,掛了就自動重啟。

.DESCRIPTION
    開機時由工作排程器執行(見 install-autostart.ps1),也可以手動執行來測試。
    - 網頁(uvicorn)與 Ollama 只綁 127.0.0.1;外面只能經 Cloudflare Tunnel 進來,登入由 Access 負責。
    - Python 一律用 .venv\Scripts\python.exe -m …:資料夾改過名後,.venv\Scripts\ 裡的
      uvicorn.exe、pip.exe 等啟動器還記著舊路徑,會直接壞掉。
    - 每 CheckSeconds 秒看一次 /healthz:連續 MaxFailures 次沒回應就重啟網頁;
      回報 ollama = unreachable 就重啟 Ollama;cloudflared 結束了就重開。
    - 紀錄寫在 logs\service\(logs\ 已在 .gitignore)。紀錄可能含文件編號與時間,不要貼到公開的地方。
    Windows PowerShell 5.1 就能執行;本檔存成 UTF-8 with BOM,中文才不會被當成系統編碼讀錯。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\start-services.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\start-services.ps1 -NoTunnel
    只在本機測試、不開通道:瀏覽器開 http://127.0.0.1:8000
#>
param(
    [int]$Port = 8000,
    [string]$TunnelConfig = (Join-Path $env:USERPROFILE ".cloudflared\config.yml"),
    [int]$CheckSeconds = 60,
    [int]$MaxFailures = 3,
    [switch]$NoTunnel
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$LogDir = Join-Path $Root "logs\service"
$HealthUrl = "http://127.0.0.1:$Port/healthz"
$OllamaUrl = "http://127.0.0.1:11434/api/version"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Log([string]$Message) {
    $line = "{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $Message
    Add-Content -Path (Join-Path $LogDir "watchdog.log") -Value $line -Encoding UTF8
    Write-Host $line
}

function Find-Program([string]$Name, [string[]]$Candidates) {
    # 先找 PATH,再找安裝程式的預設位置
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($path in $Candidates) {
        if (Test-Path $path) { return $path }
    }
    throw "找不到 ${Name},請先安裝(見 docs\部署指南.md)。"
}

function Start-Logged([string]$Name, [string]$FilePath, [string[]]$Arguments) {
    # Windows PowerShell 5.1 不會替含空白的參數加引號(例如使用者資料夾名稱有空白),這裡自己加
    $quoted = $Arguments | ForEach-Object { if ($_ -match '\s') { '"{0}"' -f $_ } else { $_ } }
    Write-Log "啟動 $Name"
    Start-Process -FilePath $FilePath -ArgumentList $quoted -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $LogDir "$Name.out.log") `
        -RedirectStandardError (Join-Path $LogDir "$Name.err.log")
}

function Get-Json([string]$Url) {
    # 回傳解析好的 JSON;連不上、逾時或不是 200 就回 $null
    try {
        Invoke-RestMethod -Uri $Url -TimeoutSec 10 -UseBasicParsing
    } catch {
        $null
    }
}

function Stop-Listener([int]$ListenPort, [string]$ProcessName) {
    # 關掉佔著這個埠的舊程式(例如上次留下、已經卡住的網頁);只關名稱相符的,不誤殺別的服務
    Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue } |
        Where-Object { $_.ProcessName -like $ProcessName } |
        Stop-Process -Force -ErrorAction SilentlyContinue
}

function Start-Ollama {
    $env:OLLAMA_HOST = "127.0.0.1:11434"    # 只綁本機;就算使用者環境變數設成 0.0.0.0 也蓋掉
    $exe = Find-Program "ollama" @(Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe")
    Start-Logged "ollama" $exe @("serve") | Out-Null
}

function Restart-Ollama {
    # 系統匣程式叫「ollama app」,不會被這裡關掉;關的是卡住的伺服器
    Get-Process -Name "ollama" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Stop-Listener 11434 "ollama*"
    Start-Ollama
}

function Start-Web {
    Stop-Listener $Port "python*"
    $env:PYTHONUTF8 = "1"    # 紀錄裡的中文一律用 UTF-8 寫,不受系統編碼影響
    # 只信任本機 cloudflared 轉來的 X-Forwarded-Proto:網頁才知道外面是 https,CSRF cookie 才會加 Secure
    $arguments = @("-m", "uvicorn", "web.app:app", "--host", "127.0.0.1", "--port", "$Port",
                   "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1")
    Start-Logged "web" $Python $arguments | Out-Null
}

function Start-Tunnel {
    if (-not (Test-Path $TunnelConfig)) {
        throw "找不到通道設定 ${TunnelConfig}(範例:deploy\cloudflared\config.example.yml)。"
    }
    # 上次留下的通道先關掉,同一條通道才不會開兩份連線
    Get-Process -Name "cloudflared" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    $exe = Find-Program "cloudflared" @("${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe",
                                         "$env:ProgramFiles\cloudflared\cloudflared.exe")
    Start-Logged "cloudflared" $exe @("tunnel", "--config", $TunnelConfig, "run")
}

if (-not (Test-Path $Python)) {
    throw "找不到 ${Python},請先建立虛擬環境並安裝套件(見 docs\部署指南.md)。"
}

Write-Log "開始:網頁埠 ${Port},每 ${CheckSeconds} 秒檢查一次"
try {
    if ($null -eq (Get-Json $OllamaUrl)) { Start-Ollama }   # Ollama 的系統匣程式可能已經開好了
} catch {
    Write-Log "Ollama 沒有啟動:$($_.Exception.Message)"   # 網頁照開,監看迴圈會再試
}
Start-Web
$tunnel = $null
if (-not $NoTunnel) {
    try {
        $tunnel = Start-Tunnel
    } catch {
        Write-Log "通道沒有啟動:$($_.Exception.Message)"
    }
}

$failures = 0
while ($true) {
    Start-Sleep -Seconds $CheckSeconds
    try {
        $health = Get-Json $HealthUrl
        if ($null -eq $health) {
            $failures++
            Write-Log "/healthz 沒有回應(${failures}/${MaxFailures})"
            if ($failures -ge $MaxFailures) {
                Write-Log "重新啟動網頁"
                Start-Web
                $failures = 0
            }
        } else {
            $failures = 0
            if ($health.ollama -eq "unreachable") {
                Write-Log "Ollama 連不上,重新啟動 Ollama"
                Restart-Ollama
            }
        }
        if ($tunnel -and $tunnel.HasExited) {
            Write-Log "cloudflared 結束了,重新啟動"
            $tunnel = Start-Tunnel
        }
    } catch {
        # 監看本身不能停:記下錯誤,下一輪再試
        Write-Log "錯誤:$($_.Exception.Message)"
    }
}
