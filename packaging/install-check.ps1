# 安装包自查：确认「你双击的是哪个安装包」「是新版还是旧版」「哪些目录能写」「装得进去吗」
# 由「安装诊断.bat」调用；也可以把安装包拖到 bat 上
param([string]$Setup)

$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$root = Split-Path -Parent $PSScriptRoot
$transcript = Join-Path $root 'install-check.log'
function Title($t) { Write-Host ""; Write-Host "== $t ==" -ForegroundColor Cyan }

Write-Host "M18 控制台 · 安装包自查" -ForegroundColor White
try { Start-Transcript -Path $transcript -Force | Out-Null } catch { }

try {
  Write-Host ("系统：{0}  用户：{1}" -f (Get-CimInstance Win32_OperatingSystem).Caption, $env:USERNAME)

  # ------------------------------------------------------------ 找到安装包
  if (-not $Setup) {
    $Setup = Get-ChildItem (Join-Path $root 'dist\release') -Filter '*Setup*.exe' |
             Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
  }

  Title "要检查的安装包"
  if (-not $Setup -or -not (Test-Path $Setup)) {
    Write-Host "  没找到安装包：先双击「打包.bat」打一次，或把 exe 拖到「安装诊断.bat」上" -ForegroundColor Yellow
    return
  }
  $fi = Get-Item $Setup
  $hash = (Get-FileHash $fi.FullName -Algorithm SHA256).Hash.Substring(0, 16)
  Write-Host ("  路径：{0}" -f $fi.FullName)
  Write-Host ("  大小：{0:N1} MB   修改时间：{1}   SHA256 前 16 位：{2}" -f ($fi.Length / 1MB), $fi.LastWriteTime, $hash)

  $text = [System.Text.Encoding]::Unicode.GetString([System.IO.File]::ReadAllBytes($fi.FullName))
  $isNew = $text.Contains('探针') -and $text.Contains('改装到')
  Write-Host ("  版本：{0}" -f $(if ($isNew) { '新版（逐目录探测 + 启动即写日志 + 自动换可写位置）' } else { '旧版 ← 就是它导致"没有日志、装不上"' })) `
    -ForegroundColor $(if ($isNew) { 'Green' } else { 'Red' })

  $z = Get-Item $fi.FullName -Stream Zone.Identifier
  if ($z) { Write-Host "  注意：这个 exe 带「来自网络」标记，可能被 SmartScreen 直接拦掉（右键 → 属性 → 勾「解除锁定」）" -ForegroundColor Yellow }

  # ------------------------------------------------------------ 目录可写性
  Title "各目录能不能写（安装程序自己的探测）"
  $targets = @(
    (Join-Path $env:LOCALAPPDATA 'Programs\M18 Console'),
    (Join-Path $env:USERPROFILE 'M18 Console'),
    (Join-Path (Split-Path -Qualifier $fi.FullName) 'M18 Console')
  )
  foreach ($t in $targets) {
    $p = Start-Process -FilePath $fi.FullName -ArgumentList @('--probe', "`"$t`"") -Wait -PassThru -NoNewWindow `
          -RedirectStandardError "$env:TEMP\m18-probe.err" -RedirectStandardOutput "$env:TEMP\m18-probe.out"
    $msg = [System.Text.Encoding]::GetEncoding(936).GetString([System.IO.File]::ReadAllBytes("$env:TEMP\m18-probe.err")).Trim()
    $color = if ($p.ExitCode -eq 0) { 'Green' } else { 'Yellow' }
    Write-Host ("  " + $(if ($msg) { $msg } else { "退出码 $($p.ExitCode)" })) -ForegroundColor $color
  }

  # ------------------------------------------------------------ 真装一次
  Title "挨个位置试装（不需要管理员权限）"
  $cands = @(
    (Join-Path $env:LOCALAPPDATA 'Programs\M18 Console'),
    (Join-Path (Split-Path -Qualifier $fi.FullName) 'M18 Console'),
    (Join-Path $env:USERPROFILE 'M18 Console')
  )
  $installed = $null
  foreach ($dest in $cands) {
    $p = Start-Process -FilePath $fi.FullName -ArgumentList @('--silent', '--no-launch', '--no-shortcut', '--dir', "`"$dest`"") `
          -Wait -PassThru -NoNewWindow -RedirectStandardError "$env:TEMP\m18-inst.err" -RedirectStandardOutput "$env:TEMP\m18-inst.out"
    $errText = [System.Text.Encoding]::GetEncoding(936).GetString([System.IO.File]::ReadAllBytes("$env:TEMP\m18-inst.err")).Trim()
    $brief = (@($errText -split "`r?`n") | Where-Object { $_.Trim() } | Select-Object -First 3 | ForEach-Object { $_.Trim() }) -join ' / '
    if ($p.ExitCode -eq 0 -and (Test-Path (Join-Path $dest 'M18Console.exe'))) {
      Write-Host ("  √ 装好了：{0}" -f $dest) -ForegroundColor Green
      if ($brief) { Write-Host ("    警告：" + $brief) -ForegroundColor Yellow }
      $installed = $dest
      break
    }
    Write-Host ("  × {0} —— 退出码 {1}：{2}" -f $dest, $p.ExitCode, $brief) -ForegroundColor Yellow
  }
  if (-not $installed) {
    Write-Host "  三个位置都装不进去：这台电脑是按「程序」拦写文件的（未签名程序不给写），" -ForegroundColor Red
    Write-Host "  要么按下面日志里的办法放行，要么改用免安装版 zip。" -ForegroundColor Red
  }

  # ------------------------------------------------------------ 日志
  Title "安装日志"
  $logs = @(
    (Join-Path $env:LOCALAPPDATA 'M18Console\install.log'),
    (Join-Path (Split-Path -Parent $fi.FullName) 'install.log'),
    (Join-Path $env:TEMP 'M18Console-install.log')
  )
  $found = $false
  foreach ($lg in $logs) {
    if (Test-Path $lg) {
      $found = $true
      Write-Host ("  {0}（最后 30 行）" -f $lg) -ForegroundColor Gray
      Get-Content $lg -Encoding UTF8 -Tail 30 | ForEach-Object { Write-Host ("    " + $_) }
    }
  }
  if (-not $found) {
    Write-Host "  上面三个位置都没有日志：安装程序大概率根本没跑起来（被安全软件 / SmartScreen 直接拦掉），" -ForegroundColor Red
    Write-Host "  或者你双击的不是上面这个新版安装包。" -ForegroundColor Red
  }

  Title "接下来"
  Write-Host "  · 显示「新版」且退出码 0 → 可以用安装目录里的 M18Console.exe，或再双击一次安装包建快捷方式"
  Write-Host "  · 显示「旧版」→ 拉最新代码后重新双击「打包.bat」"
  Write-Host "  · 目录全不可写 / exe 被拦 → 双击「诊断.bat」查是哪个安全软件在拦"
}
finally {
  try { Stop-Transcript | Out-Null } catch { }
}
