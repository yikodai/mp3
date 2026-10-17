<#
.SYNOPSIS
    为「音频转 MP3 · Web 版」放行 Windows 防火墙的 IPv6 入站端口。

.DESCRIPTION
    本脚本只做一件事：创建（或删除）一条入站 TCP 规则，允许来自指定 IPv6
    前缀的客户端访问本机服务端口。

    重要：
      * 必须使用「管理员身份」的 PowerShell 运行。
      * 本脚本不会修改路由、光猫或路由器，也不会关闭防火墙。
      * 因为 RemoteAddress 限定为 IPv6 前缀，所以该规则只对 IPv6 生效，
        IPv4 客户端不会被这条规则放行。

.PARAMETER Port
    服务监听端口，默认 8765。

.PARAMETER SourcePrefix
    允许访问的来源 IPv6 前缀。强烈建议只放行你实际会用到的前缀，例如
    "2409:abcd:1234:5678::/64"。默认 "::/0" 表示任意 IPv6 来源（不推荐）。

.PARAMETER RuleName
    防火墙规则名称，默认 "AudioToMP3 Web (IPv6)"。

.PARAMETER Profile
    生效的网络配置文件，可选 Any / Domain / Private / Public，默认 Any。

.PARAMETER Program
    可选：把规则限定到某个程序，例如 python.exe 的完整路径。

.PARAMETER Remove
    删除该规则，而不是创建。

.EXAMPLE
    # 推荐：只放行本机 /64 前缀
    powershell -ExecutionPolicy Bypass -File .\scripts\firewall_ipv6.ps1 -Port 8765 -SourcePrefix "2409:abcd:1234:5678::/64"

.EXAMPLE
    # 删除规则
    powershell -ExecutionPolicy Bypass -File .\scripts\firewall_ipv6.ps1 -Remove
#>
[CmdletBinding()]
param(
    [int]$Port = 8765,
    [string]$SourcePrefix = "::/0",
    [string]$RuleName = "AudioToMP3 Web (IPv6)",
    [string]$Profile = "Any",
    [string]$Program = "",
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    Write-Host "[ERROR] 需要管理员权限。请右键 PowerShell -> 以管理员身份运行，再执行本脚本。" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- 删除模式
if ($Remove) {
    $existing = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
    if ($existing) {
        Remove-NetFirewallRule -DisplayName $RuleName
        Write-Host "[OK] 已删除防火墙规则：$RuleName" -ForegroundColor Green
    } else {
        Write-Host "[INFO] 未找到规则：$RuleName（无需删除）" -ForegroundColor Yellow
    }
    exit 0
}

# ---------------------------------------------------------------- 参数校验
$prefixIp = ($SourcePrefix -split '/')[0]
try {
    $parsed = [System.Net.IPAddress]::Parse($prefixIp)
} catch {
    Write-Host "[ERROR] SourcePrefix 不是合法的 IPv6 地址前缀：$SourcePrefix" -ForegroundColor Red
    exit 1
}
if ($parsed.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetworkV6) {
    Write-Host "[ERROR] SourcePrefix 必须是 IPv6 前缀（例如 2409:abcd:1234:5678::/64）。" -ForegroundColor Red
    exit 1
}
if ($SourcePrefix -eq "::/0") {
    Write-Warning "SourcePrefix 为 ::/0，将允许任意 IPv6 来源访问端口 $Port。强烈建议改为你实际使用的 /64 前缀。"
}

# ---------------------------------------------------------------- 创建规则
$existing = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "[INFO] 规则已存在，先删除后重建，以保证参数生效。"
    Remove-NetFirewallRule -DisplayName $RuleName
}

$ruleParams = @{
    DisplayName   = $RuleName
    Description   = "Allow inbound TCP $Port from $SourcePrefix for AudioToMP3 Web (IPv6). Created by scripts/firewall_ipv6.ps1"
    Direction     = 'Inbound'
    Action        = 'Allow'
    Protocol      = 'TCP'
    LocalPort     = $Port
    RemoteAddress = $SourcePrefix
    Profile       = $Profile
    Enabled       = 'True'
}
if ($Program -ne "") { $ruleParams['Program'] = $Program }

New-NetFirewallRule @ruleParams | Out-Null

Write-Host "[OK] 已创建入站规则。" -ForegroundColor Green
Write-Host "     规则名称 : $RuleName"
Write-Host "     端口     : TCP $Port"
Write-Host "     来源前缀 : $SourcePrefix"
Write-Host "     配置文件 : $Profile"
if ($Program -ne "") { Write-Host "     程序     : $Program" }
Write-Host ""
Write-Host "下一步：" -ForegroundColor Cyan
Write-Host "  1) 启动服务（非回环地址必须带令牌）："
Write-Host "       启动Web版.bat --host :: --port $Port --token [你的令牌]"
Write-Host "  2) 本机验证："
Write-Host "       powershell -ExecutionPolicy Bypass -File .\scripts\verify_ipv6.ps1 -Port $Port"
Write-Host "  3) 外网验证（在外部网络执行）："
Write-Host "       curl -6 -v `"http://[本机公网IPv6]:$Port/api/health`" -H `"X-Auth-Token: [你的令牌]`""
Write-Host ""
Write-Host "安全提示：IPv6 公网地址通常直接可达，请务必启用 --token，" -ForegroundColor Yellow
Write-Host "          并尽量只放行固定的来源前缀；长期方案建议反向代理 + HTTPS。" -ForegroundColor Yellow
