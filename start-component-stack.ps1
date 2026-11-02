# ============================================================
#  一键拉起：组件版数字人桥(8795) + 弹幕采集转发(reader) + OmniVoice(9877) + 打开舞台页
#  ------------------------------------------------------------
#  用法： 双击同目录 启动-组件版全链路.cmd     （或）
#         powershell -NoProfile -ExecutionPolicy Bypass -File start-component-stack.ps1
#  参数：  -NoBrowser   只起服务不开浏览器
#          -Restart     强制重启桥（用于"改了代码没生效"）
#  注意：  本文件必须保存为 UTF-8 with BOM，否则 PowerShell 5.1 中文会乱码
# ============================================================
param([switch]$NoBrowser, [switch]$Restart)

$ErrorActionPreference = 'Continue'

# ── 运行日志(2026-10-17 新增): 每次双击启动都留一份完整记录, 出问题能回看是哪一步炸的 ──
# 背景: 用户重启电脑后双击快捷方式"出 bug", 但当时没有任何痕迹可查(窗口 8 秒就关了)。
$LogDir = Join-Path '<项目目录>' 'logs'
try { [void](New-Item -ItemType Directory -Force -Path $LogDir) } catch { }
$LogPath = Join-Path $LogDir 'launcher.log'
try {
    Start-Transcript -Path $LogPath -Append -Force | Out-Null
    Write-Host ("[launcher] 启动 {0:yyyy-MM-dd HH:mm:ss}  日志 → {1}" -f (Get-Date), $LogPath) -ForegroundColor DarkGray
} catch {
    Write-Host ("[launcher] 日志开启失败({0}), 继续执行" -f $_.Exception.Message) -ForegroundColor DarkYellow
}

$ViduRoot    = '<项目目录>'
$DanmakuRoot = '<弹幕采集目录>'
$ViduPy      = Join-Path $ViduRoot '.venv\Scripts\python.exe'
$ViduCfg     = Join-Path $ViduRoot 'config.json'
$EnginePy    = Join-Path $ViduRoot 'src\engine.py'
$OmniCmd     = '<语音桥目录>\start-omnivoice-wsl.cmd'
$BridgeCfg   = '<语音桥目录>\bridge-config.json'
$StageUrl    = 'http://localhost:8795/stage'   # 用 localhost 而非字面 127.0.0.1: Chrome 里后者可能被代理扩展的"绕过列表"挡掉(2026-10-17 实测: localhost 能开、127.0.0.1 打不开)
$ApiBase     = 'http://127.0.0.1:8795'

function Say($t, $c = 'Gray') { Write-Host $t -ForegroundColor $c }
function Rule() { Say ('  ' + ('-' * 60)) 'DarkGray' }

function Test-Port([string]$h, [int]$p, [int]$ms = 2000) {
    # ⚠️ 必须带超时(2026-10-14 故障修复): 原来 $c.Connect($h, $p) 无超时, 探"黑洞地址"时
    #    (本机 WSL 镜像网络下 LAN IP 192.168.x.x 从 Windows 不可达) 会挂几十秒/次;
    #    Wait-Port 循环 150 次 → 实际卡几小时, 表现就是"重启电脑后快捷方式拉不起来"。
    #    这里改用 BeginConnect + WaitOne 兜住, 单次最多 $ms 毫秒。
    try {
        $c = New-Object Net.Sockets.TcpClient
        $iar = $c.BeginConnect($h, $p, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne($ms, $false) -and $c.Connected
        $c.Close()
        return $ok
    } catch { return $false }
}
function Wait-Port([string]$h, [int]$p, [int]$sec) {
    for ($i = 0; $i -lt $sec; $i++) {
        if (Test-Port $h $p) { return $true }
        if ($i -gt 0 -and ($i % 15) -eq 0) {
            Say ("         …等 OmniVoice 就绪 {0}s/{1}s（冷启动通常 1-3 分钟）" -f $i, $sec) DarkGray
        }
        Start-Sleep 1
    }
    return (Test-Port $h $p)
}
function Get-OmniBase {
    # ⭐ 2026-10-10 实测: 本机 WSL2 已切镜像网络模式, 服务从 localhost 直达——
    #    LAN IP 随路由器换段(192.168.1.x→192.168.3.x)且对 LAN IP 的探测会被 Hyper-V
    #    防火墙吞掉(连接挂死 150s) → 永远先试 localhost, 活着就用它。
    # ⭐ 2026-10-14 加固: 候选地址**每个都先验证可达**(Test-Port 带超时), 不可达的一律不采用;
    #    全部不通时**回落 localhost**(服务此刻可能还没起, 交给后面的 Wait-Port 等它),
    #    绝不返回一个本机不可达的 LAN IP —— 那是"卡死在 [2/4]"的根因。
    $cands = New-Object System.Collections.ArrayList
    [void]$cands.Add('http://127.0.0.1:9877')
    foreach ($f in @($BridgeCfg, $ViduCfg)) {
        try {
            $j = Get-Content $f -Raw -Encoding UTF8 | ConvertFrom-Json
            $b = if ($f -eq $BridgeCfg) { $j.omnivoice.base } else { $j.tts.omnivoice_base }
            if ($b) { [void]$cands.Add([string]$b) }
        } catch {}
    }
    foreach ($c in $cands) {
        try { $u = [Uri]$c } catch { continue }
        if ($u.Host -and $u.Port -gt 0 -and (Test-Port $u.Host $u.Port 2000)) { return $c }
    }
    return 'http://127.0.0.1:9877'
}
function Get-ViduBuild {           # 源码里的版本号
    try {
        $m = Select-String -Path $EnginePy -Pattern 'BUILD\s*=\s*"([^"]+)"' | Select-Object -First 1
        if ($m) { return $m.Matches[0].Groups[1].Value }
    } catch {}
    return ''
}
function Get-LiveBuild {           # 正在跑的进程的版本号
    try { return [string](Invoke-RestMethod "$ApiBase/api/health" -TimeoutSec 3).build } catch { return '' }
}
function Get-ReaderProcs {
    # ⚠️ `return ,$list`: PS 5.1 的 return 会展开数组; 只剩一个对象时调用方 .Count = $null
    #    → `$null -gt 0` 为假 → 会误判"没在跑"并重复拉起一个 reader
    $list = @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
              Where-Object { $_.CommandLine -and $_.CommandLine -match 'reader\.py' })
    return ,$list
}

Say ''
Say '  ╔══════════════════════════════════════════════════════════╗' 'Magenta'
Say '  ║   组件版数字人 + 弹幕朗读   一键全链路                   ║' 'Magenta'
Say '  ╚══════════════════════════════════════════════════════════╝' 'Magenta'
Rule

# ── [1/4] 组件版数字人桥 8795（先起桥: 4 秒就绪且不依赖 OmniVoice, 防中途关窗全断） ──
$omniBase = Get-OmniBase
$omniUri  = [Uri]$omniBase
Say ''; Say '  [1/4] 组件版数字人桥 :8795' Cyan
$needRestart = $false
if ($Restart -and (Test-Port '127.0.0.1' 8795)) {
    Say '        -Restart：停掉旧桥…' Yellow
    Get-NetTCPConnection -LocalPort 8795 -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
        $ownerId = $_.OwningProcess
        $own = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerId" -ErrorAction SilentlyContinue
        $parId = if ($own) { $own.ParentProcessId } else { 0 }
        $par = if ($parId) { Get-CimInstance Win32_Process -Filter "ProcessId=$parId" -ErrorAction SilentlyContinue } else { $null }
        Stop-Process -Id $ownerId -Force -ErrorAction SilentlyContinue
        # ⚠️⚠️ 关键安全判断(2026-10-14): 只有父进程是 python(venv 包装父进程) 时才一起杀。
        #     桥改由计划任务托管后, 它的父进程是 **svchost.exe(任务计划服务)** ——
        #     照原逻辑"连父进程一起杀"会把 svchost 干掉, 那是系统级事故。绝不可省这个判断。
        if ($par -and $par.Name -like 'python*') { Stop-Process -Id $parId -Force -ErrorAction SilentlyContinue }
    }
    Start-Sleep 2
}
if (Test-Port '127.0.0.1' 8795) {
    $live = Get-LiveBuild
    $src  = Get-ViduBuild
    Say "        ✅ 已在运行（build=$live）" Green
    if ($src -and $live -and $src -ne $live) {
        Say "        ⚠ 进程跑的是旧代码：源码 build=$src ≠ 运行 build=$live" Red
        Say '          改完代码必须重启桥才生效 → 重跑本脚本并加 -Restart' Red
        $needRestart = $true
    }
} else {
    Say '        ⏳ 未运行，启动中…（计划任务托管 · 无窗口）' Yellow
    # ⭐ 2026-10-14: 服务改由 Windows 计划任务托管(父进程=svchost) —— 与窗口彻底解耦,
    #    关窗口/关终端都不会带走服务; 任务不存在时(换机器/被清理)回退成隐藏窗口启动。
    if (Get-ScheduledTask -TaskName 'DSH-vidu-bridge' -ErrorAction SilentlyContinue) {
        Start-ScheduledTask -TaskName 'DSH-vidu-bridge'
    } else {
        Start-Process -FilePath $ViduPy -ArgumentList '-m', 'src.main', '--no-browser' `
            -WorkingDirectory $ViduRoot -WindowStyle Hidden
    }
    $ok = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep 1
        if (Test-Port '127.0.0.1' 8795) { $ok = $true; break }
    }
    if ($ok) { Say "        ✅ 已就绪（build=$(Get-LiveBuild)）" Green }
    else { Say '        ❌ 启动失败：看 logs\bridge.log 末尾的报错' Red }
}

# ── [2/4] OmniVoice 引擎（数字人的嗓子，WSL2；冷启动 1-3 分钟，桥不等它也能用） ──
Say ''; Say "  [2/4] OmniVoice 引擎 $omniBase" Cyan
if (Test-Port $omniUri.Host $omniUri.Port) {
    Say '        ✅ 已在运行' Green
} else {
    Say '        ⏳ 没在跑，正在拉起 WSL2 OmniVoice（首次约 1-3 分钟）…' Yellow
    if (Test-Path $OmniCmd) {
        # 隐藏窗口拉起(2026-10-14 用户要求"别弹这些窗口"): 进度看 WSL 里的 server.log
        Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', "`"$OmniCmd`"" -WindowStyle Hidden
        if (Wait-Port $omniUri.Host $omniUri.Port 240) { Say '        ✅ 已就绪' Green }
        else { Say '        ⚠ 超时未就绪——数字人可能没声音，先继续（桥/采集器已就绪）' Red }
    } else {
        Say "        ⚠ 找不到启动脚本 $OmniCmd" Red
    }
}

# OmniVoice 地址若变了，同步给桥（桥的 tts.omnivoice_base）
if (Test-Port '127.0.0.1' 8795) {
    try {
        $cur = (Invoke-RestMethod "$ApiBase/api/settings" -TimeoutSec 5).tts.omnivoice_base
        if ($cur -and $omniBase -and $cur -ne $omniBase) {
            Invoke-RestMethod "$ApiBase/api/settings" -Method Post -ContentType 'application/json' `
                -Body (@{ tts = @{ omnivoice_base = $omniBase } } | ConvertTo-Json -Compress) -TimeoutSec 5 | Out-Null
            Say "        ↻ OmniVoice 地址已更新为 $omniBase（WSL IP 变了）" Yellow
        }
    } catch {}
}

# ── [3/4] 弹幕采集转发（reader：抖音/B站/微信 → 数字人） ────────
Say ''; Say '  [3/4] 弹幕采集转发（reader.py + douyinLive + 微信）' Cyan
$rd = Get-ReaderProcs
if ($rd.Count -gt 0) {
    Say "        ✅ 已在运行（$($rd.Count) 个进程）" Green
} else {
    Say '        ⏳ 启动中…（计划任务托管 · 无窗口）' Yellow
    if (Get-ScheduledTask -TaskName 'DSH-danmaku-reader' -ErrorAction SilentlyContinue) {
        Start-ScheduledTask -TaskName 'DSH-danmaku-reader'
    } else {
        Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', 'start.cmd' `
            -WorkingDirectory $DanmakuRoot -WindowStyle Hidden
    }
    Start-Sleep 10
    $rd = Get-ReaderProcs
    if ($rd.Count -gt 0) { Say '        ✅ 已就绪（托盘图标在任务栏）' Green }
    else { Say "        ⚠ 没起来，手动跑 $DanmakuRoot\启动-弹幕朗读全链路.cmd 看看" Red }
}

# ── [4/4] 打开舞台页 ──────────────────────────────────────────
Say ''; Say '  [4/4] 舞台页' Cyan
if ($NoBrowser) { Say "        （-NoBrowser）$StageUrl" DarkGray }
else { Start-Process $StageUrl; Say "        ✅ 已打开 $StageUrl" Green }

# ── 汇总 ──────────────────────────────────────────────────────
Say ''; Rule
$h = $null
try { $h = Invoke-RestMethod "$ApiBase/api/health" -TimeoutSec 3 } catch {}
Say '  【状态】' White
Say ("    桥 build = {0}    phase = {1}" -f $(if ($h) { $h.build } else { '未响应' }), $(if ($h) { $h.phase } else { '-' })) Gray
Say ("    OmniVoice {0}    reader {1} 个进程" -f $omniBase, @(Get-ReaderProcs).Count) Gray
Say ''; Say '  【弹幕朗读规则（当前生效）】' White
Say '    优先级：歌曲 > 你的打字聊天 > 弹幕复读+回答' Gray
Say '      · 歌在播 → 弹幕/聊天回复全部排队（连 LLM 都不请求），等歌完或点「⏹ 停止演唱」' DarkGray
Say '      · 每 5 秒一拍：把这一拍里的多条弹幕一起喂 LLM（最多 14 条合并成一次回答）' DarkGray
Say '      · 只复读其中 1 条（优先“提问那条”）；普通弹幕不念昵称，VIP 送礼观众照念' DarkGray
Say '      · 送礼观众(VIP) 优先取件，但守自己的 5 秒间隔' DarkGray
Say '    弹幕模式：托盘「念弹幕内容」开关控制' Gray
Say '      · 开(默认) = 复读弹幕 + 回答；关 = 不复读，只让数字人回应（带观众名标签问 LLM）' DarkGray
Say '    过滤：命中「触审词/黑名单」整条丢弃（空翻/倒立/劈叉/国旗/国徽/站起来等 23+43 词）' Gray
Say '    昵称：中文前2字 / 数字前4位 / 英文首词 / 混排从首个汉字起取2个可读字符' Gray
Say '      · 不再加「水友」前缀（省朗读时间）；纯符号 ID 走泛称「小可爱/宝子」' DarkGray
Say '    礼物感谢：6 秒窗口合并成一句话（≥4 人或 ≥50 钻立刻发；每 25 秒最多一句、每分钟≤2 句）' Gray
Say ''; Say '  【常用操作】' White
Say '    · 桥 / 采集器由 Windows 计划任务托管（无窗口），关窗口/关终端都不影响运行' Gray
Say '        · 重启桥：Start-ScheduledTask -TaskName DSH-vidu-bridge' DarkGray
Say '        · 重启采集器：Start-ScheduledTask -TaskName DSH-danmaku-reader' DarkGray
Say '        · 查状态：Get-ScheduledTask DSH-vidu-bridge,DSH-danmaku-reader | Get-ScheduledTaskInfo' DarkGray
Say '    · 改 config.json 里不热加载的项（门槛/模板/间隔）→ 托盘右键「重启朗读器」' Gray
Say '    · 改了桥的代码 → 重跑本脚本并加 -Restart（脚本会核对 build 版本）' Gray
Say '    · 歌词与画面字幕：放歌时画面字幕层自动让位，歌完自动贴回；位置/字号在设置抽屉调' Gray
Say ''; Say '  ⚠ 弹幕只在与数字人「连线」后转发：未连线时采集器只抓不转发' Yellow
Rule

# ── 收尾: 关掉本次运行日志(2026-10-17) ──
try { Write-Host ('[launcher] 完成 {0:HH:mm:ss}' -f (Get-Date)) -ForegroundColor DarkGray; Stop-Transcript | Out-Null } catch { }
