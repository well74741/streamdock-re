# M18 控制台 · 运行被拦截时的诊断（由「诊断.bat」调用，也可以把 exe 拖到「诊断.bat」上）
param([string[]]$Paths = @())
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$root = Split-Path -Parent $PSScriptRoot
function Title($t) { Write-Host ""; Write-Host "== $t ==" -ForegroundColor Cyan }

Write-Host "M18 控制台 · 运行问题诊断" -ForegroundColor White
Write-Host ("系统：{0}  用户：{1}" -f (Get-CimInstance Win32_OperatingSystem).Caption, $env:USERNAME)

# ---------------------------------------------------------------- 安全软件
Title "已安装的安全软件"
$avs = Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct
if ($avs) {
  foreach ($a in $avs) {
    $on = (([int]$a.productState -shr 12) -band 0xF) -eq 1
    Write-Host ("  {0}  {1}" -f $a.displayName, $(if ($on) { '（运行中）' } else { '（未启用）' }))
  }
} else { Write-Host "  查不到（可能是服务器版系统）" }

# ---------------------------------------------------------------- 受控文件夹访问
Title "Windows 受控文件夹访问（勒索软件防护）"
$pref = Get-MpPreference
$cfa = $pref.EnableControlledFolderAccess
$cfaText = @{ 0 = '关闭'; 1 = '开启 ← 会阻止未签名程序写文件'; 2 = '仅审核（不拦截）' }[[int]$cfa]
if ($null -eq $cfa) { $cfaText = '查不到（Defender 可能没在用，由其他安全软件接管）' }
Write-Host "  状态：$cfaText"
if ($pref.ControlledFolderAccessProtectedFolders) {
  Write-Host "  额外保护的文件夹：" ($pref.ControlledFolderAccessProtectedFolders -join '；')
}
$allowed = @($pref.ControlledFolderAccessAllowedApplications | Where-Object { $_ -and $_ -notmatch '^N/A' })
if ($allowed.Count) { Write-Host "  已允许的程序：" ($allowed -join '；') }

# ---------------------------------------------------------------- 最近的拦截记录
Title "最近被 Windows 拦截的记录（与 M18 相关的）"
$events = Get-WinEvent -LogName 'Microsoft-Windows-Windows Defender/Operational' -MaxEvents 400 |
  Where-Object { $_.Id -in 1123, 1124, 1121, 1122, 1116, 1117 -and $_.Message -match 'M18|m18-mcp' } |
  Select-Object -First 8
if ($events) {
  foreach ($e in $events) {
    $kind = @{ 1123 = '受控文件夹访问拦截'; 1124 = '受控文件夹访问（审核）'; 1121 = '攻击面减少规则拦截';
               1122 = '攻击面减少规则（审核）'; 1116 = '检测到威胁'; 1117 = '已处理威胁' }[$e.Id]
    $proc = ([regex]::Match($e.Message, '(进程名称|Process Name):\s*(.+)')).Groups[2].Value.Trim()
    $path = ([regex]::Match($e.Message, '(路径|Path):\s*(.+)')).Groups[2].Value.Trim()
    Write-Host ("  {0:MM-dd HH:mm}  {1}  程序：{2}  目标：{3}" -f $e.TimeCreated, $kind, $proc, $path) -ForegroundColor Yellow
  }
} else { Write-Host "  没有找到 Windows Defender 的相关拦截记录（如果装了第三方安全软件，去它的「拦截日志 / 隔离区」里看）" }

# ---------------------------------------------------------------- 要放行的程序
$cands = @()
$cands += Get-ChildItem (Join-Path $root 'dist\release\M18Console-Portable-*.zip') | ForEach-Object FullName
$cands += Join-Path $root 'dist\M18Console\M18Console.exe'
$cands += Join-Path $root 'dist\M18Console\mcp\m18-mcp.exe'
$cands += $Paths
$cands = $cands | Where-Object { $_ -and (Test-Path $_) } | ForEach-Object { (Resolve-Path $_).Path } | Select-Object -Unique

Title "结论"
$todo = @($cands | Where-Object { $allowed -notcontains $_ })
if ([int]$cfa -eq 1) {
  Write-Host "  受控文件夹访问已开启，它很可能就是原因。" -ForegroundColor Yellow
  if (-not $cands) {
    Write-Host "  没找到 M18 的程序文件：把解压后的 M18Console.exe 拖到「诊断.bat」上再运行一次。"
  } elseif (-not $todo.Count) {
    Write-Host "  这些程序都已经在允许列表里了。还是被拦的话，检查第三方安全软件。"
  } else {
    Write-Host "  需要放行的程序："
    $todo | ForEach-Object { Write-Host "    $_" }
    $ans = Read-Host "  现在把它们加入「受控文件夹访问」的允许列表吗？（需要管理员确认）[Y/n]"
    if ($ans -eq '' -or $ans -match '^[yY是]') {
      $cmd = ($todo | ForEach-Object { "Add-MpPreference -ControlledFolderAccessAllowedApplications '" + $_.Replace("'", "''") + "'" }) -join '; '
      Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile', '-Command', $cmd
      $now = @((Get-MpPreference).ControlledFolderAccessAllowedApplications)
      $ok = @($todo | Where-Object { $now -contains $_ })
      Write-Host ("  已放行 {0}/{1} 个，重新运行 M18Console.exe 即可。" -f $ok.Count, $todo.Count) -ForegroundColor Green
      Write-Host "  （以后在别的位置装 / 解压，换了路径的 exe 要再放行一次，或直接再运行本诊断）"
    }
  }
} else {
  Write-Host "  受控文件夹访问没有开启。如果运行时提示「拒绝访问」，拦截来自上面列出的第三方安全软件："
  Write-Host "  打开它的「信任区 / 白名单 / 排除项」，添加 M18Console.exe 和它所在的整个文件夹，"
  Write-Host "  再看看「隔离区」里有没有被隔离的 M18 文件，有就恢复。"
  if ($cands) { Write-Host "  需要加入信任的程序："; $cands | ForEach-Object { Write-Host "    $_" } }
}
Write-Host ""
Write-Host "  也可以在这台电脑上直接用源码版：双击「启动 M18 控制台.bat」（Python 是受信任的签名程序，不会被拦）。"
