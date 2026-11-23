# Windows / Windows Server installer for the student bot.
# Run in PowerShell *as Administrator* from the project folder:
#   powershell -ExecutionPolicy Bypass -File .\install_windows.ps1
# What it does: creates .venv, installs requirements, creates .env (asks the values),
# and registers a Scheduled Task "StudentBot" that starts at boot (even with no one logged in)
# and restarts the bot if it crashes. Run again to update packages; .env and data are kept.
#   Uninstall the task:  powershell -ExecutionPolicy Bypass -File .\install_windows.ps1 -Uninstall
param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
$TaskName = "StudentBot"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Say($text, $color = "Green") { Write-Host $text -ForegroundColor $color }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Say "Please run PowerShell as Administrator (right click > Run as administrator)." "Red"
    exit 1
}

if ($Uninstall) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*bot.main*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Say "Task removed and bot stopped. Files, .env and database were kept."
    exit 0
}

# 1) Python 3.10+
Say "[1/5] Checking Python..."
$py = $null
foreach ($cand in @("py -3", "python")) {
    try {
        $ver = & cmd /c "$cand -c ""import sys;print(sys.version_info[0]*100+sys.version_info[1])""" 2>$null
        if ($LASTEXITCODE -eq 0 -and [int]$ver -ge 310) { $py = $cand; break }
    } catch {}
}
if (-not $py) {
    Say "Python 3.10+ not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH')" "Red"
    Say "or run:  winget install -e --id Python.Python.3.12   then run this script again." "Red"
    exit 1
}

# 2) virtual environment + packages
Say "[2/5] Creating .venv and installing packages (may take a minute)..."
if (-not (Test-Path ".venv\Scripts\python.exe")) { & cmd /c "$py -m venv .venv"; if ($LASTEXITCODE) { exit 1 } }
& .\.venv\Scripts\python.exe -m pip install -q --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -q -r requirements.txt
if ($LASTEXITCODE) { Say "pip install failed." "Red"; exit 1 }

# 3) .env
Say "[3/5] Settings (.env)..."
if (Test-Path ".env") {
    Say "  .env already exists and was kept. Edit it with: notepad .env" "Yellow"
} else {
    $token = Read-Host "  Bot token from @BotFather"
    if (-not $token) { Say "Bot token is required." "Red"; exit 1 }
    $admins = Read-Host "  Numeric Telegram IDs of the main admins (comma separated)"
    $group = Read-Host "  Group ID (like -1001234567890)"
    $contact = Read-Host "  Contact text for students whose code is not found (optional, Persian is fine)"
    $proxy = Read-Host "  Telegram proxy if Telegram is blocked (optional, e.g. socks5://127.0.0.1:1080)"
    $group = ($group -replace '(?i)^\s*id\s*:?\s*', '').Trim()
    if ($admins -notmatch '^\s*\d+(\s*,\s*\d+)*\s*$') { Say "ADMIN_IDS must be numbers, e.g. 123456789" "Red"; exit 1 }
    if ($group -notmatch '^-\d+$') { Say "Group ID must be a negative number, e.g. -1001234567890" "Red"; exit 1 }
    # single-quoted .env values: python-dotenv keeps '#' and '$' literally; \\ and \' are escapes
    function Q($v) { "'" + ($v -replace '\\', '\\' -replace "'", "\'") + "'" }
    $lines = @("BOT_TOKEN=$(Q $token)", "ADMIN_IDS=$($admins -replace '\s', '')", "DB_PATH=bot.db",
               "GROUP_ID=$group", "DEFAULT_CONTACT=$(Q $contact)", "PROXY_URL=$(Q $proxy)")
    [IO.File]::WriteAllLines((Join-Path $Root ".env"), $lines, (New-Object Text.UTF8Encoding $false))
    # only Administrators and SYSTEM may read the token
    icacls ".env" /inheritance:r /grant:r "*S-1-5-32-544:F" "*S-1-5-18:F" | Out-Null
}

# 4) Scheduled Task: at startup, as SYSTEM, restart on failure, no time limit
Say "[4/5] Registering the auto-start task '$TaskName'..."
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$Root\run_bot.bat`"" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Force | Out-Null

# 5) start now
Say "[5/5] Starting the bot..."
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*bot.main*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 6
$running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*bot.main*" }
if ($running) {
    Say "Done. The bot is running and will start automatically after every reboot."
} else {
    Say "The bot did not stay running. Check logs\bot.log" "Red"
}
Write-Host ""
Write-Host "Logs:        $Root\logs\bot.log"
Write-Host "Stop:        Stop-ScheduledTask $TaskName   (and close python)"
Write-Host "Start:       Start-ScheduledTask $TaskName"
Write-Host "Uninstall:   powershell -ExecutionPolicy Bypass -File .\install_windows.ps1 -Uninstall"
