<#
.SYNOPSIS
    看有(高齡家庭文書輔助):把 start-services.ps1 登記成工作排程器的「開機就執行」工作,不必登入 Windows。

.DESCRIPTION
    請在「以系統管理員身分執行」的 PowerShell 裡執行。
    工作用目前這個使用者的身分在開機時執行(S4U:不必存密碼),所以找得到這個使用者的
    Ollama 模型(%USERPROFILE%\.ollama)、通道設定與憑證(%USERPROFILE%\.cloudflared),
    以及使用者環境變數(雲端模式的 CF_ACCOUNT_ID、CF_API_TOKEN)。
    執行時間不設上限(工作排程器預設 72 小時就會把工作停掉);監看腳本意外結束時每分鐘重試一次。
    本檔存成 UTF-8 with BOM,Windows PowerShell 5.1 才不會把中文讀錯。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\install-autostart.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\install-autostart.ps1 -StorePassword
    S4U 登記失敗時(例如用 Microsoft 帳戶登入的電腦):改成輸入一次 Windows 密碼,交給工作排程器保存。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\install-autostart.ps1 -Remove
#>
param(
    [string]$TaskName = "senior-doc-assistant",
    [switch]$StorePassword,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "請在開始選單的 PowerShell 按右鍵 →「以系統管理員身分執行」,再執行這個腳本。"
}

if ($Remove) {
    if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
        Write-Host "沒有找到排程工作 ${TaskName},不必移除。"
        return
    }
    Stop-ScheduledTask -TaskName $TaskName
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已移除排程工作 ${TaskName}。已經在跑的 Ollama、網頁與通道不會跟著關,重開機或在工作管理員結束即可。"
    return
}

$script = Join-Path $PSScriptRoot "start-services.ps1"
$user = $identity.Name
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

if ($StorePassword) {
    $credential = Get-Credential -UserName $user -Message "輸入 ${user} 的 Windows 密碼(由工作排程器保存,開機時用)"
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -User $credential.UserName -Password $credential.GetNetworkCredential().Password -RunLevel Limited -Force | Out-Null
} else {
    # 一般權限就夠:三個服務都只綁本機,不需要系統管理員權限
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Force | Out-Null
}

Write-Host "已登記排程工作 ${TaskName}:之後每次開機都會自動啟動。現在先啟動一次……"
Start-ScheduledTask -TaskName $TaskName
Write-Host "約一分鐘後檢查:瀏覽器開 http://127.0.0.1:8000/healthz,或看 logs\service\watchdog.log。"
