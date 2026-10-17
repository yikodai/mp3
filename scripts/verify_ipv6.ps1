<#
.SYNOPSIS
    验证「音频转 MP3 · Web 版」的 IPv6 监听与访问是否就绪。

.DESCRIPTION
    本脚本是「只读」的：不修改防火墙、不改路由、不启动/停止服务。
    依次检查：
      1. 本机 IPv6 地址
      2. 服务端口监听状态
      3. 本机 IPv6 回环健康检查  http://[::1]:Port/api/health
      4. 若提供 -PublicIPv6，则用该地址自测一次
      5. 相关防火墙规则是否存在
      6. 打印外网验证用的 curl -6 命令（不代跑）

.PARAMETER Port
    服务监听端口，默认 8765。

.PARAMETER PublicIPv6
    可选：本机的公网 IPv6 地址，用于在本机自测该地址是否可达。

.PARAMETER Token
    可选：服务启用了访问令牌时填写，脚本会在请求里带上。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\verify_ipv6.ps1 -Port 8765

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\verify_ipv6.ps1 -Port 8765 -PublicIPv6 2409:abcd:1234:5678:1::100 -Token [你的令牌]
#>
[CmdletBinding()]
param(
    [int]$Port = 8765,
    [string]$PublicIPv6 = "",
    [string]$Token = ""
)

function Write-Section([string]$Title) {
    Write-Host ""
    Write-Host "== $Title ==" -ForegroundColor Cyan
}

Write-Host "AudioToMP3 Web - IPv6 就绪检查（只读，不修改任何系统设置）" -ForegroundColor White

Write-Section "1. 本机 IPv6 地址"
try {
    Get-NetIPAddress -AddressFamily IPv6 -ErrorAction Stop |
        Where-Object { $_.AddressState -eq 'Preferred' } |
        Select-Object IPAddress, PrefixLength, InterfaceAlias |
        Format-Table -AutoSize
} catch {
    Write-Host "[WARN] 无法读取 IPv6 地址：$($_.Exception.Message)" -ForegroundColor Yellow
}

Write-Section "2. 监听状态（端口 $Port）"
$listen = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($listen) {
    foreach ($c in $listen) {
        $proc = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
        $name = if ($proc) { $proc.ProcessName } else { "pid=$($c.OwningProcess)" }
        Write-Host ("  监听 {0}:{1}  <- 进程 {2}" -f $c.LocalAddress, $c.LocalPort, $name)
    }
} else {
    Write-Host "[WARN] 端口 $Port 当前没有监听，请先启动服务：" -ForegroundColor Yellow
    Write-Host "       启动Web版.bat --host :: --port $Port --token [你的令牌]"
}

Write-Section "3. 本机 IPv6 回环健康检查"
$url = "http://[::1]:$Port/api/health"
if ($Token -ne "") { $url += "?token=$Token" }
try {
    $h = Invoke-RestMethod -Uri $url -TimeoutSec 5
    Write-Host "[OK] $url" -ForegroundColor Green
    Write-Host ("     status={0}  family={1}  dual_stack={2}  auth_required={3}" -f $h.status, $h.bind_family, $h.dual_stack, $h.auth_required)
    Write-Host ("     listen={0}  is_loopback={1}" -f $h.listen, $h.is_loopback)
} catch {
    Write-Host "[FAIL] $url" -ForegroundColor Red
    Write-Host "       $($_.Exception.Message)"
    Write-Host "       若服务启用了令牌，请加 -Token；未启用则不要带令牌。"
}

if ($PublicIPv6 -ne "") {
    Write-Section "4. 通过指定公网 IPv6 自测"
    $purl = "http://[$PublicIPv6]:$Port/api/health"
    if ($Token -ne "") { $purl += "?token=$Token" }
    try {
        $h2 = Invoke-RestMethod -Uri $purl -TimeoutSec 5
        Write-Host "[OK] $purl  status=$($h2.status)" -ForegroundColor Green
    } catch {
        Write-Host "[FAIL] $purl" -ForegroundColor Red
        Write-Host "       $($_.Exception.Message)"
        Write-Host "       注意：这里只验证「本机经该地址可达」，不代表外网可达。"
    }
}

Write-Section "5. 相关防火墙规则"
$rules = Get-NetFirewallRule -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -like "*AudioToMP3*" }
if ($rules) {
    $rules | Select-Object DisplayName, Enabled, Direction, Action, Profile | Format-Table -AutoSize
} else {
    Write-Host "[INFO] 未找到名称含 AudioToMP3 的入站规则。" -ForegroundColor Yellow
    Write-Host "       如需放行 IPv6 入站（管理员身份执行）："
    Write-Host "       powershell -ExecutionPolicy Bypass -File .\scripts\firewall_ipv6.ps1 -Port $Port -SourcePrefix [你的/64前缀]"
}

Write-Section "6. 外网验证命令（请在外部网络执行）"
Write-Host "  curl -6 -v `"http://[本机公网IPv6]:$Port/api/health`" -H `"X-Auth-Token: [你的令牌]`""
Write-Host "  浏览器： http://[本机公网IPv6]:$Port/?token=[你的令牌]"
