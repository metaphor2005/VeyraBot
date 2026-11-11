# 注册 Windows 计划任务：每天 23:50 跑一次日结巩固 + 快照冷存
#
# 用法（在 PowerShell 中以当前用户执行，不需要管理员权限即可注册用户级任务）：
#   .\install_schedule.ps1
#   .\install_schedule.ps1 -Hour 22 -Minute 30
#   .\install_schedule.ps1 -Remove        # 卸载
#
# 任务内容等价于：
#   python maintenance.py --daily --prune-snapshots --log-file data\maintenance.log
#
# --prune-snapshots：保留最近 30 版快照，更早的移入 archive/（不删除），
# 这样"每天跑一次日结"不会让快照目录无限膨胀。

param(
    [int]$Hour = 23,
    [int]$Minute = 50,
    [string]$TaskName = "CognitiveAgentDailyConsolidate",
    [string]$PythonExePath = "",
    [switch]$UsePythonW,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-Output "任务 $TaskName 不存在，无需卸载"
    } else {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "已卸载计划任务 $TaskName"
    }
    return
}

# 项目根目录 = 本脚本所在目录（必须解析成绝对路径，计划任务不继承当前目录）
$ProjectRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path

# 解释器路径解析：`python` 在本机可能只是 PowerShell 别名（Source 为空，直接取会崩），
# 所以先问解释器自己要 sys.executable，再退回 Get-Command python.exe 的 Path。
$PythonExe = $PythonExePath
if (-not $PythonExe) {
    try {
        $PythonExe = (& python.exe -c "import sys; print(sys.executable)" | Select-Object -First 1).Trim()
    } catch {
        $PythonExe = ""
    }
}
if (-not $PythonExe -or -not (Test-Path -LiteralPath $PythonExe)) {
    $candidate = Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $candidate) { $PythonExe = $candidate.Path }
}
if (-not $PythonExe -or -not (Test-Path -LiteralPath $PythonExe)) {
    throw "找不到可用的 python.exe，请加参数 -PythonExePath 'C:\...\python.exe' 指定"
}

# 默认用 python.exe：计划任务在后台会话里跑，本来就不会弹窗口。
# （踩过的坑：改用它旁边的 pythonw.exe 后，进程没有 stdout，脚本启动即退出，
#   任务返回码 120、日志为空。要试 pythonw 请显式加 -UsePythonW。）
if ($UsePythonW) {
    $PythonW = Join-Path (Split-Path -Parent $PythonExe) "pythonw.exe"
    if (Test-Path -LiteralPath $PythonW) {
        $PythonExe = $PythonW
    }
}

$Script = Join-Path $ProjectRoot "maintenance.py"
$LogFile = Join-Path $ProjectRoot "data\maintenance.log"

if (-not (Test-Path -LiteralPath $Script)) {
    throw "未找到 $Script，请把本脚本放在 cognitive-agent 根目录下"
}

$Arguments = "`"$Script`" --daily --prune-snapshots --log-file `"$LogFile`""

$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument $Arguments -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.AddHours($Hour).AddMinutes($Minute))

# StartWhenAvailable：错过的执行（比如关机）在下次开机后补跑一次
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "认知体：每天固定一次日结巩固 + 快照冷存（把当天往事归并成主线）" `
    -Force | Out-Null

Write-Output "已注册计划任务：$TaskName"
Write-Output "  执行时间：每天 $($Hour.ToString('00')):$($Minute.ToString('00'))"
Write-Output "  执行命令：$PythonExe $Arguments"
Write-Output "  运行日志：$LogFile"
Write-Output ""
Write-Output "立即试跑一次：Start-ScheduledTask -TaskName '$TaskName'"
Write-Output "查看结果：Get-ScheduledTaskInfo -TaskName '$TaskName'"
