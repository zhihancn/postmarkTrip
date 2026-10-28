# =============================================================================
# postmarkTrip · 运维入口（Windows / PowerShell）
#
#   .\pmtrip.ps1            （不带子命令 = help）
#   .\pmtrip.ps1 doctor
#   .\pmtrip.ps1 up
#   .\pmtrip.ps1 backup
#
# 与 Makefile **完全等价**。Windows 上通常没有 make，所以必须有这个入口。
# 所有子命令都只是 docker compose 的薄封装 —— 宿主机不需要装 Python / Node / Go。
#
# ⚠️ 本文件必须存为 **UTF-8 with BOM**。
#    Windows PowerShell 5.1 读取无 BOM 的 .ps1 时会按系统 ANSI 代码页解码，
#    所有中文都会变成乱码（甚至导致语法错误）。BOM 是 5.1 正确识别 UTF-8 的唯一线索。
#
# ⚠️ 存储：数据库默认在**命名卷** postmarktrip-db 里，Redis 在 postmarktrip-redis，
#    扫描件在宿主目录 .\data\assets。
#    如果 .env 里设了 PMTRIP_PGDATA，数据库改为绑定挂载到那个目录 ——
#    那是给"这台机器上反复出现 docker compose down -v"的情况准备的逃生口，
#    不是默认值（路径不存在时 postgres 会直接起不来）。
#    详见 docker-compose.yml 里 db.volumes 的注释。（doctor 也会打印真实位置）
#
# 若提示"因为在此系统上禁止运行脚本"，二选一：
#     powershell -ExecutionPolicy Bypass -File .\pmtrip.ps1 up
#     Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
# =============================================================================

param(
    [Parameter(Position = 0)]
    [string]$Command = 'help',

    # restore 用：转储文件路径（.dump）
    [string]$File,

    # restore / clean 用：跳过交互确认
    [switch]$Force
)

# 刻意**不用** 'Stop'。
#
# Windows PowerShell 5.1 会把原生命令（docker / netstat …）写到 stderr 的内容
# 包装成 ErrorRecord；在 Stop 策略下这会**直接终止脚本** —— 于是"引擎没启动"
# 这类完全可以预期的状态，变成了未处理异常，用户看到的是原始英文报错，
# 而不是我们准备好的中文修复建议。
#
# 我们要的是"读 $LASTEXITCODE 自己判断"。需要严格语义的地方一律显式写
# -ErrorAction Stop（例如 Invoke-WebRequest），不依赖全局策略。
$ErrorActionPreference = 'Continue'

# 固定工作目录为脚本所在目录 —— 这样从任何地方调用都能找到 docker-compose.yml。
if ($PSScriptRoot) {
    Set-Location -LiteralPath $PSScriptRoot
}

$script:Root        = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$script:EnvFile     = Join-Path $script:Root '.env'
$script:ComposeDev  = @('-f', 'docker-compose.yml', '-f', 'docker-compose.dev.yml')
$script:Failed      = 0
$script:Warned      = 0

# =============================================================================
#  输出助手
#
#  刻意与 infra\scripts\doctor.sh 用同一套符号与措辞：
#    ✓ 通过   ✗ 失败   ! 警告   · 提示   -> 下一步
#  "警告"计入统计且应当能被修掉；"提示"只是建议，不计入 —— 见 Show-Note。
# =============================================================================

function Show-Ok {
    param([string]$Message)
    Write-Host '  ' -NoNewline
    Write-Host '✓' -ForegroundColor Green -NoNewline
    Write-Host " $Message"
}

function Show-Bad {
    param([string]$Message)
    Write-Host '  ' -NoNewline
    Write-Host '✗' -ForegroundColor Red -NoNewline
    Write-Host " $Message"
    $script:Failed++
}

function Show-Warn {
    param([string]$Message)
    Write-Host '  ' -NoNewline
    Write-Host '!' -ForegroundColor Yellow -NoNewline
    Write-Host " $Message"
    $script:Warned++
}

function Show-Hint {
    param([string]$Message)
    Write-Host "      -> $Message" -ForegroundColor DarkGray
}

function Show-Note {
    <#
      提示（不计入警告数）。

      与 Show-Warn 的区别：Show-Warn 是"有东西不对，去修"；
      Show-Note 是"给你个建议"。两者的计数必须分开 —— 一条**无法清除**的
      警告会让 $script:Warned 永远不为 0，于是末尾那句"全部通过。"成了死代码，
      而且会稀释真正警告的信号。
    #>
    param([string]$Message)
    Write-Host '  ' -NoNewline
    Write-Host '·' -ForegroundColor DarkGray -NoNewline
    Write-Host " $Message"
}

function Show-Section {
    param([string]$Title)
    Write-Host ''
    Write-Host $Title
}

function Show-Info {
    param([string]$Message)
    Write-Host $Message
}

# =============================================================================
#  基础工具
# =============================================================================

function Test-DockerCli {
    return [bool](Get-Command docker -ErrorAction SilentlyContinue)
}

function Invoke-Native {
    <#
      .SYNOPSIS
        执行原生命令，返回输出与退出码。
      .NOTES
        为什么需要它：读只读探针（docker version / volume inspect / compose ps）
        时，我们**不希望**命令失败就抛异常 —— 那正是要报告的诊断信息。
        这里临时把 ErrorActionPreference 降到 Continue，让 stderr 不再变成
        终止性错误，然后由调用方读 ExitCode 自己决定怎么说。
    #>
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments
    )

    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $FilePath @Arguments 2>$null
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }

    return [pscustomobject]@{ Output = $out; ExitCode = $code }
}

function Invoke-Compose {
    <#
      .SYNOPSIS
        调用 docker compose，并在非零退出时抛出可读的错误。
      .NOTES
        用 -ComposeArgs 显式传数组，而不是 ValueFromRemainingArguments：
        后者会把 "-d" 之类当成 PowerShell 参数名解析而报错。
    #>
    param(
        [string[]]$ComposeArgs,
        [string[]]$Files          # 额外前置的 -f 参数（dev 模式用）
    )

    $all = @('compose')
    if ($Files) { $all += $Files }
    $all += $ComposeArgs

    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & docker @all
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }

    if ($code -ne 0) {
        throw "docker compose $($ComposeArgs -join ' ') 失败（退出码 $code）"
    }
}

function Get-EnvValue {
    <#
      .SYNOPSIS
        从 .env 读一个值。
      .NOTES
        用 Get-Content -Encoding UTF8 而不是 Select-String -Encoding：
        后者的 -Encoding 参数在部分 PowerShell 版本上不存在。
    #>
    param([Parameter(Mandatory = $true)][string]$Name)

    if (-not (Test-Path -LiteralPath $script:EnvFile)) { return $null }

    $pattern = '^' + [regex]::Escape($Name) + '='
    $line = Get-Content -LiteralPath $script:EnvFile -Encoding UTF8 -ErrorAction SilentlyContinue |
            Where-Object { $_ -match $pattern } |
            Select-Object -Last 1

    if (-not $line) { return $null }

    # -split 限制为 2 段：值里若含 '=' 也不会被截断
    $parts = $line -split '=', 2
    if ($parts.Count -lt 2) { return $null }
    return $parts[1].Trim()
}

function Get-EnvInt {
    param([string]$Name, [int]$Default)
    $raw = Get-EnvValue -Name $Name
    if (-not $raw) { return $Default }
    $val = 0
    if ([int]::TryParse($raw, [ref]$val)) { return $val }
    return $Default
}

function New-RandomBytes {
    param([int]$Count)
    $bytes = New-Object byte[] $Count
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return $bytes
}

function New-EnvSecret {
    param([int]$Count = 48)
    return [Convert]::ToBase64String((New-RandomBytes -Count $Count))
}

function New-ReadablePassword {
    param([int]$Length = 16)
    # 去掉 0/O/1/l/I 等易混字符：这个口令要被人从终端抄到浏览器里
    $alphabet = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    $bytes = New-RandomBytes -Count $Length
    $chars = foreach ($b in $bytes) { $alphabet[$b % $alphabet.Length] }
    return -join $chars
}

function Set-EnvFileValue {
    <#
      .SYNOPSIS
        改写 .env 里某个键（不存在则追加），保持 UTF-8 **无 BOM**。
      .NOTES
        BOM 会让 pydantic 把 \uFEFF 当成第一个键名的一部分 —— 表现为
        "明明写了 SECRET_KEY，启动还是说没设置"。
    #>
    param([string]$Name, [string]$Value)

    $lines = @(Get-Content -LiteralPath $script:EnvFile -Encoding UTF8)
    $pattern = '^' + [regex]::Escape($Name) + '='
    $found = $false
    $out = foreach ($line in $lines) {
        if ($line -match $pattern) {
            $found = $true
            "$Name=$Value"
        } else {
            $line
        }
    }
    if (-not $found) { $out = @($out) + "$Name=$Value" }
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($script:EnvFile, [string[]]$out, $enc)
}

function Initialize-EnvFile {
    <#
      .SYNOPSIS
        没有 .env 时自动生成一份，并打印一次性凭据。
      .NOTES
        为什么不在网页里让用户填：容器启动**之前**没有网页，而
        POSTGRES_PASSWORD / SECRET_KEY 在 compose 解析阶段就必需。
        浏览器能收集的只有登录之后的运行时设置（VLM / Colnect Key）。
    #>
    if (Test-Path -LiteralPath $script:EnvFile) { return }

    $example = Join-Path $script:Root '.env.example'
    if (-not (Test-Path -LiteralPath $example)) {
        Write-Host "错误：缺少 $example，无法生成 .env" -ForegroundColor Red
        exit 1
    }

    Copy-Item -LiteralPath $example -Destination $script:EnvFile -Force

    $secret = New-EnvSecret -Count 48
    $pg     = New-ReadablePassword -Length 24
    $admin  = New-ReadablePassword -Length 16

    Set-EnvFileValue -Name 'SECRET_KEY'        -Value $secret
    Set-EnvFileValue -Name 'POSTGRES_PASSWORD' -Value $pg
    Set-EnvFileValue -Name 'ADMIN_PASSWORD'    -Value $admin
    Set-EnvFileValue -Name 'DATABASE_URL'      -Value "postgresql+psycopg://postmarktrip:${pg}@db:5432/postmarktrip"

    $dataDir = Join-Path $script:Root 'data'
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
    $credFile = Join-Path $dataDir 'initial-credentials.txt'
    @(
        'postmarkTrip 首次启动生成的凭据'
        '用户名: admin'
        "口令:   $admin"
        ''
        '登录后请立刻修改口令，并删除本文件。'
    ) | Set-Content -LiteralPath $credFile -Encoding UTF8

    Write-Host ''
    Write-Host '未发现 .env，已生成一份（随机密钥与口令）：' -ForegroundColor Green
    Write-Host ''
    Write-Host '  管理员账号：admin'
    Write-Host "  管理员口令：$admin" -ForegroundColor Yellow
    Write-Host ''
    Write-Host "  同时写入：$credFile（登录后请删除）"
    Write-Host '  如需从局域网其他设备访问，改 .env 的 APP_HOST_BIND。'
    Write-Host ''
}

function Assert-EnvFile {
    Initialize-EnvFile
    if (Test-Path -LiteralPath $script:EnvFile) { return }

    Write-Host ''
    Write-Host "错误：无法生成 $($script:EnvFile)" -ForegroundColor Red
    Write-Host '  请检查仓库是否完整（需要 .env.example），以及当前目录是否可写。'
    Write-Host ''
    exit 1
}

function Assert-FrontendDockerfile {
    <#
      gateway 的默认镜像是 apps\web\Dockerfile（前端负责人维护）。
      它不存在时 docker compose 只会丢一句 "failed to read dockerfile"，很难定位，
      所以这里提前拦下来并给出两条现成的替代路径。
    #>
    $df = Join-Path $script:Root 'apps\web\Dockerfile'
    if (Test-Path -LiteralPath $df) { return }

    Write-Host ''
    Write-Host '提示：apps\web\Dockerfile 尚不存在（前端镜像由前端负责人维护）。' -ForegroundColor Yellow
    Write-Host ''
    Write-Host '  默认拓扑里 gateway 用它构建，所以现在无法直接 up。两个替代方案：'
    Write-Host ''
    Write-Host '    1) 先用「分离式网关」把后端跑起来（不依赖前端产物）：'
    Write-Host '       docker compose -f docker-compose.yml -f infra\compose\gateway-standalone.yml up -d'
    Write-Host ''
    Write-Host '    2) 开发模式（用 Vite dev server 代替静态产物，访问 5173）：'
    Write-Host '       .\pmtrip.ps1 dev'
    Write-Host ''
    exit 1
}

function Get-AppPort {
    return (Get-EnvInt -Name 'APP_PORT' -Default 9999)
}

function Get-BaseUrl {
    return "http://127.0.0.1:$(Get-AppPort)"
}

function Confirm-Action {
    <#
      .SYNOPSIS
        破坏性操作的二次确认。$Force 时跳过。
    #>
    param([string]$Prompt)

    if ($Force) { return $true }

    Write-Host ''
    Write-Host '⚠️  ' -ForegroundColor Yellow -NoNewline
    Write-Host $Prompt
    Write-Host ''
    Write-Host '    确认请输入 y：' -NoNewline
    $ans = Read-Host
    if ($ans -ne 'y') {
        Write-Host ''
        Write-Host '已取消。'
        return $false
    }
    return $true
}

function New-DirectoryIfMissing {
    param([string]$Path)

    if (Test-Path -LiteralPath $Path) {
        Show-Ok "$Path 存在"
        return
    }
    try {
        New-Item -ItemType Directory -Force -Path $Path | Out-Null
        Show-Ok "$Path 已创建"
    } catch {
        Show-Bad "$Path 不存在且无法创建"
        Show-Hint "检查当前用户对 $(Split-Path -Parent $Path) 的写权限"
    }
}

# =============================================================================
#  子命令：日常
# =============================================================================

function Invoke-Up {
    Assert-EnvFile
    Assert-FrontendDockerfile
    Invoke-Compose -ComposeArgs @('up', '-d')
    Write-Host ''
    Write-Host '启动完成。'
    Write-Host "  访问：http://localhost:$(Get-AppPort)"
    Write-Host '  自检：.\pmtrip.ps1 doctor'
    Write-Host '  日志：.\pmtrip.ps1 logs'
}

function Invoke-Down {
    Assert-EnvFile
    Invoke-Compose -ComposeArgs @('down', '--remove-orphans')
    Write-Host ''
    Write-Host '容器已移除。数据保留在绑定挂载目录、Redis 卷与 .\data 目录中。'
}

function Invoke-Restart {
    Assert-EnvFile
    Invoke-Compose -ComposeArgs @('restart')
}

function Invoke-Logs {
    Assert-EnvFile
    Invoke-Compose -ComposeArgs @('logs', '-f', '--tail=100')
}

function Invoke-Ps {
    Assert-EnvFile
    Invoke-Compose -ComposeArgs @('ps')
}

function Invoke-ShApi {
    Assert-EnvFile
    Invoke-Compose -ComposeArgs @('exec', 'api', 'sh')
}

function Get-PgEnv {
    <#
      .SYNOPSIS
        从 .env 取数据库用户名与库名，并校验为安全标识符。
      .NOTES
        刻意在 **PowerShell 侧**取值并拼好参数数组，而不是把 $POSTGRES_USER 塞进
        `sh -c "..."`。两个原因：

        1. Windows PowerShell 5.1 对**内嵌双引号**的原生命令参数转义不正确 ——
           嵌套引号会被 mangling，这是最难排查的一类 bug。
        2. 根本不需要 shell：pg_dump / pg_restore / psql 都在镜像的 PATH 上。

        校验标识符是在没有 shell 引用的情况下防止参数被拆开的必要措施。
    #>
    $user = Get-EnvValue -Name 'POSTGRES_USER'
    if (-not $user) { $user = 'postmarktrip' }
    $db = Get-EnvValue -Name 'POSTGRES_DB'
    if (-not $db) { $db = 'postmarktrip' }

    if ($user -notmatch '^[A-Za-z0-9_]+$') {
        throw "POSTGRES_USER 含不支持的字符：'$user'（只允许字母、数字、下划线）"
    }
    if ($db -notmatch '^[A-Za-z0-9_]+$') {
        throw "POSTGRES_DB 含不支持的字符：'$db'（只允许字母、数字、下划线）"
    }

    return [pscustomobject]@{ User = $user; Db = $db }
}

function Invoke-ShDb {
    Assert-EnvFile
    $pg = Get-PgEnv
    # 直接把参数交给 psql，不经 shell —— 引号问题不存在，Tab 补全也正常
    Invoke-Compose -ComposeArgs @('exec', 'db', 'psql', '-U', $pg.User, '-d', $pg.Db)
}

# =============================================================================
#  子命令：备份 / 恢复
# =============================================================================

function New-DbDump {
    <#
      .SYNOPSIS
        取一份**经过校验**的数据库转储，写到宿主上的 $OutFile，返回字节数。

      .NOTES
        两个调用点：backup 命令，以及 restore 之前的**安全转储**。
        后者是"恢复失败就回不去了"的解药 —— `pg_restore --clean` 会先删表，
        如果转储本身有问题（被截断、或者选错了文件），当前数据就没了，
        而用户唯一的依靠可能是一份更旧的备份。花几秒钟换一条退路，很值。

        **为什么不能直接 `docker compose exec ... > $out`**：
        PowerShell 的原生命令输出重定向会把字节流当文本处理（PS 5.1 尤甚），
        二进制转储会被破坏 —— 而恢复时才会发现，那就太晚了。
        正确做法：在容器内写文件（pg_dump -f）→ docker compose cp 取回 →
        删掉容器内临时文件。cp 两个方向都是字节安全的。

        （Makefile 那边用的是 sh 的 `>`，那是字节安全的；两个脚本各自用了
        对自己 shell 正确的机制，不要为了"看起来一致"去改它们。）

        **校验**：pg_dump 退出码为 0 只说明它没报错，不说明产出的文件可恢复。
        这里再跑一次 `pg_restore --list` 确认它是**能解析的归档** ——
        把一个"恢复时才发现备份是坏的"变成"备份时就知道"。
    #>
    param(
        [Parameter(Mandatory = $true)][string]$OutFile,
        [Parameter(Mandatory = $true)][string]$Label
    )

    # 唯一文件名：任何残留的旧文件都不可能被 cp 成"这次的新备份"
    $suffix = [guid]::NewGuid().ToString('N').Substring(0, 12)
    $remote = "/tmp/pmtrip-dump-$suffix.dump"
    $pg = Get-PgEnv

    Write-Host "$Label -> $OutFile"
    try {
        Invoke-Compose -ComposeArgs @(
            'exec', '-T', 'db', 'pg_dump',
            '-U', $pg.User, '-d', $pg.Db,
            '--format=custom', '-f', $remote
        )
        # 校验归档可解析。在容器内重定向到 /dev/null，免得把目录清单刷到屏幕上。
        Invoke-Compose -ComposeArgs @(
            'exec', '-T', 'db', 'sh', '-c', "pg_restore --list '$remote' > /dev/null"
        )
        Invoke-Compose -ComposeArgs @('cp', "db:$remote", $OutFile)
    } finally {
        # 清理容器内的临时文件。刻意吞掉这里的异常：清理失败不该掩盖真正的错误
        # （比如 pg_dump 的报错），也不该让一次成功的备份变成失败。
        try { Invoke-Compose -ComposeArgs @('exec', '-T', 'db', 'rm', '-f', $remote) } catch { }
    }

    return (Get-Item -LiteralPath $OutFile).Length
}


function Invoke-Backup {
    Assert-EnvFile

    $dbDir  = Join-Path $script:Root 'backups\db'
    $manDir = Join-Path $script:Root 'backups\assets-manifest'
    New-Item -ItemType Directory -Force -Path $dbDir, $manDir | Out-Null

    $ts  = Get-Date -Format 'yyyyMMdd_HHmmss'
    $out = Join-Path $dbDir "$ts.dump"

    $size = New-DbDump -OutFile $out -Label '转储数据库（pg_dump）'
    Show-Ok "已写入 $out（$size 字节，归档已校验）"

    # 资产清单：恢复后用来核对文件数量与路径
    $assetsDir = Join-Path $script:Root 'data\assets'
    $man = Join-Path $manDir "$ts.txt"
    $files = @()
    if (Test-Path -LiteralPath $assetsDir) {
        $files = Get-ChildItem -LiteralPath $assetsDir -Recurse -File -ErrorAction SilentlyContinue |
                 ForEach-Object { $_.FullName.Substring($script:Root.Length).TrimStart('\') }
    }
    $header = @(
        "# postmarkTrip 资产清单 $ts"
        '# 恢复后请核对：文件数量与路径应与实际一致'
    )
    Set-Content -LiteralPath $man -Value ($header + $files) -Encoding UTF8
    Show-Ok "资产清单 $man（$($files.Count) 个文件）"

    # -------------------------------------------------------------------------
    # 清理过期转储
    #
    # ⚠️ 按**天数**清理，不是按个数。
    #
    # 这里原来是 `Sort-Object LastWriteTime -Descending | Select-Object -Skip $keep`，
    # 而 $keep 来自 BACKUP_KEEP_DAYS —— 于是"保留的天数"被当成了"保留的文件数"：
    #   * 一天备份三次（做危险操作前各来一次，restore 的说明就是这么建议的），
    #     实际只留 10 天，而用户以为自己有 30 天；
    #   * 一周备份一次，则会留 30 周。
    # 而打印出来的还是"已清理过期转储（超过 N 天）"—— 那句话本身就不成立。
    #
    # 另外：**永不删除最新那一份**，哪怕它比 $keep 还旧。
    # 一份旧备份聊胜于无，一份都没有就真的没有了。
    # -------------------------------------------------------------------------
    $keep = Get-EnvInt -Name 'BACKUP_KEEP_DAYS' -Default 30
    if ($keep -gt 0) {
        $all = @(Get-ChildItem -LiteralPath $dbDir -Filter '*.dump' -File -ErrorAction SilentlyContinue |
                 Sort-Object LastWriteTime -Descending)
        $newest = if ($all.Count -gt 0) { $all[0].FullName } else { $null }
        $cutoff = (Get-Date).AddDays(-$keep)
        foreach ($f in ($all | Select-Object -Skip 1)) {
            if ($f.LastWriteTime -ge $cutoff) { continue }
            if ($f.FullName -eq $newest) { continue }
            Remove-Item -LiteralPath $f.FullName -Force
            Write-Host "  已清理超过 $keep 天的转储: $($f.Name)"
        }
    }

    Write-Host ''
    Write-Host '⚠️  .env 不在本次备份内（含密钥，刻意不写进 backups\）。' -ForegroundColor Yellow
    Write-Host '    请单独把它抄一份到安全的地方 —— 丢了 SECRET_KEY，所有人都要重新登录。'
    Write-Host '    数据库在绑定挂载 ~/pmtrip-pgdata 里；备份走 pg_dump，**不要**直接拷贝该目录。'
}

function Invoke-Restore {
    Assert-EnvFile

    if (-not $File) {
        Write-Host ''
        Write-Host '错误：必须用 -File 指定要恢复的转储。' -ForegroundColor Red
        Write-Host ''
        Write-Host '  用法： .\pmtrip.ps1 restore -File backups\db\20261008_030000.dump'
        Write-Host ''
        Write-Host '  可用的转储（最新在前）：'
        $dumps = Get-ChildItem -LiteralPath (Join-Path $script:Root 'backups\db') -Filter '*.dump' -File -ErrorAction SilentlyContinue |
                 Sort-Object LastWriteTime -Descending |
                 Select-Object -First 10
        if ($dumps) {
            foreach ($d in $dumps) { Write-Host "    $($d.FullName)" }
        } else {
            Write-Host '    （还没有任何转储，先跑 .\pmtrip.ps1 backup）'
        }
        Write-Host ''
        exit 1
    }

    if (-not (Test-Path -LiteralPath $File)) {
        Write-Host ''
        Write-Host "错误：找不到文件 $File" -ForegroundColor Red
        Write-Host ''
        exit 1
    }

    $resolved = (Resolve-Path -LiteralPath $File).Path

    $ok = Confirm-Action -Prompt @"
这会**覆盖当前数据库的全部内容**。
    转储文件：$resolved
    扫描件目录 .\data\assets 不会被改动 —— 恢复后用资产清单核对。
"@
    if (-not $ok) { exit 1 }

    # -------------------------------------------------------------------------
    # ① 恢复前的安全转储 —— "恢复失败就回不去了"的解药
    #
    # 下面的 pg_restore --clean 会**先删表再重建**。如果这份转储有问题
    # （被截断、或者 -File 指错了文件），当前数据在 pg_restore 报错之前
    # 就已经被删掉了 —— 而用户唯一的依靠可能是一份更旧的备份。
    #
    # 取一份安全转储只要几秒钟，却把"不可挽回"变成"再恢复一次就行"。
    # 若数据库本身已经不可用导致安全转储失败，就明确告知没有退路并再确认一次。
    # -------------------------------------------------------------------------
    Write-Host '① 恢复前的安全转储'
    $safety = Join-Path (Join-Path $script:Root 'backups\db') `
                       "pre-restore_$(Get-Date -Format 'yyyyMMdd_HHmmss').dump"
    try {
        $n = New-DbDump -OutFile $safety -Label '  当前数据库'
        Show-Ok "  $safety（$n 字节）"
    } catch {
        Write-Host ''
        Write-Host '  警告：安全转储失败 —— 数据库可能没起来或已经不可用。' -ForegroundColor Yellow
        Write-Host '        继续恢复就**没有退路**了：pg_restore --clean 会先删表，' -ForegroundColor Yellow
        Write-Host '        万一这份转储也有问题，恢复前的数据将无法找回。' -ForegroundColor Yellow
        if (-not (Confirm-Action -Prompt '仍要继续恢复吗？')) { exit 1 }
    }

    $remote = '/tmp/pmtrip-restore.dump'
    $pg = Get-PgEnv

    Write-Host '② 停掉 api 与 worker（恢复期间不要写库）'
    try { Invoke-Compose -ComposeArgs @('stop', 'api', 'worker') } catch { Write-Host '   （部分服务未运行，继续）' }

    Write-Host '③ 恢复'
    Invoke-Compose -ComposeArgs @('cp', $resolved, "db:$remote")
    Invoke-Compose -ComposeArgs @(
        'exec', '-T', 'db', 'pg_restore',
        '-U', $pg.User, '-d', $pg.Db,
        '--clean', '--if-exists', '--no-owner', $remote
    )
    Invoke-Compose -ComposeArgs @('exec', '-T', 'db', 'rm', '-f', $remote)

    Write-Host '④ 重新启动'
    Invoke-Compose -ComposeArgs @('start', 'api', 'worker')

    Write-Host ''
    Write-Host "恢复前的状态已存为：$safety" -ForegroundColor DarkGray
    Write-Host '请执行 .\pmtrip.ps1 doctor 确认。'
}

# =============================================================================
#  子命令：升级
# =============================================================================

function Invoke-Upgrade {
    Assert-EnvFile
    Assert-FrontendDockerfile

    Write-Host ''
    Write-Host '① 升级前强制备份'
    Invoke-Backup

    Write-Host ''
    Write-Host '② 重建镜像（--pull 会顺带拉取更新的基础镜像）'
    Invoke-Compose -ComposeArgs @('build', '--pull')

    Write-Host ''
    Write-Host '③ 重启（数据库迁移由 api 容器启动时自动执行）'
    Invoke-Compose -ComposeArgs @('up', '-d')

    Write-Host ''
    Write-Host '④ 重载 gateway'
    Write-Host '   （api 容器被重建后 IP 会变，而 nginx 在启动时只解析一次上游名）'
    try {
        Invoke-Compose -ComposeArgs @('exec', '-T', 'gateway', 'nginx', '-s', 'reload')
    } catch {
        Write-Host '   gateway 未运行或尚未就绪，跳过重载'
    }

    Write-Host ''
    Write-Host '⑤ 看迁移结果：.\pmtrip.ps1 logs    然后：.\pmtrip.ps1 doctor'
}

# =============================================================================
#  子命令：开发 / 构建
# =============================================================================

function Invoke-Dev {
    Assert-EnvFile
    Invoke-Compose -Files $script:ComposeDev -ComposeArgs @('up', '-d')
    $webPort = Get-EnvInt -Name 'WEB_DEV_PORT' -Default 5173
    $port = Get-AppPort
    Write-Host ''
    Write-Host '开发模式已启动（后端热重载）。'
    Write-Host "  应用（经网关）：http://localhost:$port"
    Write-Host '  后端改代码：容器内 uvicorn 自动重载，无需重启'
    Write-Host ''
    Write-Host '  前端 HMR 请在**宿主机**另开一个终端：'
    Write-Host "      cd apps\web; npm install; npm run dev   ->  http://localhost:$webPort"
    Write-Host "  （代理目标默认跟随 .env 的 APP_PORT=$port；若 vite 进了容器就设 VITE_PROXY_TARGET=http://gateway）"
}

function Invoke-Build {
    Assert-EnvFile
    Assert-FrontendDockerfile
    Invoke-Compose -ComposeArgs @('build')
}

function Invoke-Pull {
    Assert-EnvFile
    try {
        Invoke-Compose -ComposeArgs @('pull', '--ignore-pull-failures')
    } catch {
        Write-Host '  基础镜像拉取有失败项（不影响本地已构建的镜像）'
    }
}

function Invoke-Clean {
    Assert-EnvFile
    Write-Host '停止并移除容器与网络 …'
    Invoke-Compose -ComposeArgs @('down', '--remove-orphans')
    Write-Host ''
    Write-Host '已清理。以下内容**未被触碰**：'
    Write-Host '  数据库 ~/pmtrip-pgdata（绑定挂载）+ Redis 命名卷（队列）'
    Write-Host '  .\data\assets  .\inbox  .\public-data  .\models  .\backups'
    Write-Host ''
    Write-Host '⚠️  绝不要用 `docker compose down -v` —— 那会删掉数据库卷。' -ForegroundColor Yellow
    Write-Host '    真要清空重建，请先 backup，再手工 docker volume rm。'
}

# =============================================================================
#  子命令：doctor
#
#  与 infra\scripts\doctor.sh 实现同一套检查。
#  ⚠️ 增删检查项时请同时改那个脚本。
# =============================================================================

function Invoke-Doctor {
    $script:Failed = 0
    $script:Warned = 0

    Write-Host 'postmarkTrip · 自检'
    Write-Host "目录：$($script:Root)"

    # -------------------------------------------------------------------------
    Show-Section '1/9 Docker 引擎'
    if (Test-DockerCli) {
        $r = Invoke-Native -FilePath 'docker' -Arguments @('version', '--format', '{{.Server.Version}}')
        if ($r.ExitCode -eq 0 -and $r.Output) {
            Show-Ok "docker 引擎运行中（Server $($r.Output)）"
        } else {
            Show-Bad 'docker 命令存在，但连不上引擎'
            Show-Hint '启动 Docker Desktop，等托盘图标显示 Running 后重试'
        }
    } else {
        Show-Bad '找不到 docker 命令'
        Show-Hint '安装 Docker Desktop for Windows（安装时选 WSL2 后端）'
    }

    # -------------------------------------------------------------------------
    Show-Section '2/9 Docker Compose'
    $r = Invoke-Native -FilePath 'docker' -Arguments @('compose', 'version', '--short')
    if ($r.ExitCode -eq 0 -and $r.Output) {
        Show-Ok "docker compose $($r.Output)"
    } else {
        Show-Bad 'docker compose 不可用（Compose v2 是 docker 的子命令）'
        Show-Hint '更新 Docker Desktop 到较新版本'
    }

    # -------------------------------------------------------------------------
    Show-Section '3/9 配置文件 .env'
    if (Test-Path -LiteralPath $script:EnvFile) {
        Show-Ok '.env 存在'
    } else {
        Show-Bad '缺少 .env'
        Show-Hint 'Copy-Item .env.example .env'
        Show-Hint '然后至少改 SECRET_KEY / ADMIN_PASSWORD / POSTGRES_PASSWORD'
    }

    # -------------------------------------------------------------------------
    Show-Section '4/9 示例值是否还没改'
    $placeholderChecks = @(
        @{ Name = 'SECRET_KEY';        Why = '$b=[byte[]]::new(48); [System.Security.Cryptography.RandomNumberGenerator]::Fill($b); [Convert]::ToBase64String($b)  （改了它所有会话失效）' }
        @{ Name = 'ADMIN_PASSWORD';    Why = '设一个至少 8 位的强口令' }
        @{ Name = 'POSTGRES_PASSWORD'; Why = '改完记得同步改 DATABASE_URL 里那一段密码' }
    )
    foreach ($c in $placeholderChecks) {
        $val = Get-EnvValue -Name $c.Name
        if (-not $val) {
            Show-Warn "$($c.Name) 为空"
            Show-Hint $c.Why
        } elseif ($val.StartsWith('CHANGE_ME')) {
            Show-Warn "$($c.Name) 仍是 .env.example 里的示例值"
            Show-Hint $c.Why
        } else {
            Show-Ok "$($c.Name) 已设置"
        }
    }

    # DATABASE_URL 与 POSTGRES_PASSWORD 的一致性 —— 最常见的"连不上库"
    $dbUrl = Get-EnvValue -Name 'DATABASE_URL'
    $pgPw  = Get-EnvValue -Name 'POSTGRES_PASSWORD'
    if ($dbUrl -and $pgPw) {
        # 用 .Contains 而不是 -like：-like 会把口令里的 * 和 ? 当成通配符，
        # 于是不一致的口令也可能被判为"一致"。这个检查的全部价值就在于
        # 排除"连不上库"的原因，一个假的 OK 比没有检查更糟。
        # （本脚本推荐的 [Convert]::ToBase64String 不会产生 * ?，但用户自选的口令可能会。）
        if ($dbUrl.Contains(":$pgPw@")) {
            Show-Ok 'DATABASE_URL 里的口令与 POSTGRES_PASSWORD 一致'
        } else {
            Show-Bad 'DATABASE_URL 里的口令与 POSTGRES_PASSWORD 不一致'
            Show-Hint '两者必须是同一串；改了口令请同步改 DATABASE_URL'
        }
    }

    # -------------------------------------------------------------------------
    Show-Section '5/9 存储（宿主目录 + 绑定挂载 + 命名卷）'
    # 只有这些是宿主目录。数据库用绑定挂载、Redis 用命名卷 —— 见 docker-compose.yml。
    foreach ($d in @('data', 'data\assets', 'inbox', 'public-data', 'models', 'backups\db')) {
        New-DirectoryIfMissing -Path (Join-Path $script:Root $d)
    }

    $assetsDir = Join-Path $script:Root 'data\assets'
    if (Test-Path -LiteralPath $assetsDir) {
        # 真的写一个探针文件，而不是只看属性 —— Windows 上的权限继承很绕
        try {
            $probe = Join-Path $assetsDir '.write-probe'
            [System.IO.File]::WriteAllBytes($probe, @())
            Remove-Item -LiteralPath $probe -Force
            Show-Ok 'data\assets 可写（storage 层要在这里建上传临时文件）'
        } catch {
            Show-Bad 'data\assets 不可写'
            Show-Hint '容器内 pmtrip(10001) 需要写权限；entrypoint 会对齐顶层属主'
        }
    }

    # ⚠️ 数据库是**绑定挂载**（不是命名卷）—— 2026-10-11 改，原因是踩出来的：
    # 这台机器上反复出现 `docker compose down -v`，而 `-v` 只删本项目声明的命名卷，
    # 数据库因此被反复清空。绑定挂载 `down -v` 碰不到。
    # 详见 docker-compose.yml 里 db.volumes 的注释与 ok/09 E-06。
    $dbSrc = if ($env:PMTRIP_PGDATA) { $env:PMTRIP_PGDATA } else { Join-Path $HOME 'pmtrip-pgdata' }
    if (Test-Path $dbSrc) {
        Show-Ok "数据库（绑定挂载）$dbSrc"
    } else {
        Show-Warn "数据库目录 $dbSrc 还不存在"
        Show-Hint "首次 make up 前先建：mkdir -p $dbSrc（见 _setup_pgdata.sh）"
    }

    foreach ($vol in @('postmarktrip-redis')) {
        $r = Invoke-Native -FilePath 'docker' -Arguments @('volume', 'inspect', '--format', '{{.Mountpoint}}', $vol)
        if ($r.ExitCode -eq 0 -and $r.Output) {
            Show-Ok "卷 $vol"
            Show-Hint "$($r.Output)"
        } else {
            Show-Warn "卷 $vol 还不存在"
            Show-Hint "首次 up 时自动创建。查路径：docker volume inspect $vol"
        }
    }
    Show-Hint '上面是卷在 WSL2 虚拟机内的路径。**不要手工改动里面的文件** ——'
    Show-Hint '数据库备份请用 .\pmtrip.ps1 backup（pg_dump），而不是拷目录。'

    # -------------------------------------------------------------------------
    Show-Section '6/9 容器状态'
    $r = Invoke-Native -FilePath 'docker' -Arguments @('compose', 'ps', '--format', '{{.Service}}|{{.State}}|{{.Health}}')
    if ($r.ExitCode -eq 0 -and $r.Output) {
        foreach ($line in @($r.Output)) {
            if (-not $line) { continue }
            $parts  = $line -split '\|'
            $svc    = $parts[0]
            $state  = if ($parts.Count -gt 1) { $parts[1] } else { '' }
            $health = if ($parts.Count -gt 2) { $parts[2] } else { '' }

            if ($health -eq 'healthy') {
                Show-Ok "$svc：$state / healthy"
            } elseif ($health -eq 'unhealthy') {
                Show-Bad "$svc：$state / unhealthy"
                Show-Hint "docker compose logs --tail=50 $svc"
            } elseif ($state -eq 'running') {
                Show-Ok "$svc：running"
            } else {
                Show-Bad "$svc：$state$(if ($health) { "/$health" })"
                Show-Hint "docker compose logs --tail=50 $svc"
            }
        }
    } else {
        Show-Warn '没有正在运行的容器（或无法查询状态）'
        Show-Hint '.\pmtrip.ps1 up'
    }

    # -------------------------------------------------------------------------
    Show-Section '7/9 HTTP 端点'
    $base = Get-BaseUrl
    $iwrBase = @{ TimeoutSec = 5; ErrorAction = 'Stop' }
    if ($PSVersionTable.PSVersion.Major -le 5) { $iwrBase['UseBasicParsing'] = $true }

    $gatewayOk = $false
    try {
        $resp = Invoke-WebRequest -Uri "$base/gateway-health" @iwrBase
        if ($resp.StatusCode -eq 200) {
            Show-Ok "gateway 有响应：$base/gateway-health"
            $gatewayOk = $true
        } else {
            Show-Bad "gateway 返回 $($resp.StatusCode)"
        }
    } catch {
        Show-Bad "网关无响应：$base/gateway-health"
        Show-Hint 'docker compose ps 看 gateway 是否 healthy'
        Show-Hint '若 gateway 一直是 created（没启动），多半是 api 没变 healthy —— gateway 依赖它'
        Show-Hint 'docker compose logs --tail=50 api'
    }

    if ($gatewayOk) {
        try {
            $h = Invoke-RestMethod -Uri "$base/api/health" -TimeoutSec 5 -ErrorAction Stop

            if ($h.ok -eq $true) {
                Show-Ok 'api /api/health ok=true'
            } else {
                Show-Bad 'api /api/health ok=false（下面逐项看是哪一环）'
            }

            foreach ($k in @('database', 'assets_writable', 'inbox_present')) {
                $v = $h.$k
                if ($v -eq $true) {
                    Show-Ok "$k = true"
                } else {
                    Show-Bad "$k = false"
                    switch ($k) {
                        'database'        { Show-Hint 'docker compose logs db；核对 DATABASE_URL 与 POSTGRES_*' }
                        'assets_writable' { Show-Hint '宿主 data\assets 的写权限，或 entrypoint 的 chown 失败' }
                        'inbox_present'   { Show-Hint '宿主 .\inbox 是否挂进了容器' }
                    }
                }
            }

            if ($h.ocr_engine) {
                Show-Ok "ocr_engine = $($h.ocr_engine)（v0.1 为 none，属正常）"
            }
        } catch {
            Show-Bad "api 无响应：$base/api/health"
            Show-Hint 'docker compose logs --tail=50 api'
        }
    }

    # -------------------------------------------------------------------------
    Show-Section "8/9 端口 $(Get-AppPort)"
    $port = Get-AppPort
    $listening = $false
    try {
        $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop
        if ($conns) { $listening = $true }
    } catch {
        # Get-NetTCPConnection 不可用（很老的系统）时退回 netstat
        $ns = netstat -ano | Select-String -Pattern ":$port\s" | Select-String -Pattern 'LISTENING'
        if ($ns) { $listening = $true }
    }
    if ($listening) {
        Show-Ok "端口 $port 正在监听"
    } else {
        Show-Warn "端口 $port 没有监听"
        Show-Hint '容器没起来，或 .env 里的 APP_PORT 是别的值'
    }

    # -------------------------------------------------------------------------
    Show-Section '9/9 备份'
    $dbDir = Join-Path $script:Root 'backups\db'
    $lastDump = $null
    if (Test-Path -LiteralPath $dbDir) {
        $lastDump = Get-ChildItem -LiteralPath $dbDir -Filter '*.dump' -File -ErrorAction SilentlyContinue |
                    Sort-Object LastWriteTime -Descending |
                    Select-Object -First 1
    }
    if ($lastDump) {
        Show-Ok "最近一次数据库转储：$($lastDump.Name)（$($lastDump.LastWriteTime.ToString('yyyy-MM-dd HH:mm'))）"
    } else {
        Show-Warn '还没有任何数据库转储'
        Show-Hint '.\pmtrip.ps1 backup'
    }
    if (Test-Path -LiteralPath $script:EnvFile) {
        # 用 Show-Note 而不是 Show-Warn：这是**建议**，不是缺陷。
        # 用 Show-Warn 会让 $script:Warned 永远 >= 1，于是上面那句
        # "全部通过。"永远打印不出来 —— 一个清不掉的警告还会稀释真正的警告。
        Show-Note '.env 刻意不在自动备份范围内（里面有密钥）'
        Show-Hint '请**单独**把它抄一份到安全的地方 —— 丢了 SECRET_KEY，所有人都要重新登录'
    }

    # -------------------------------------------------------------------------
    Write-Host ''
    Write-Host '----------------------------------------'
    if ($script:Failed -eq 0 -and $script:Warned -eq 0) {
        Write-Host '全部通过。' -ForegroundColor Green
    } elseif ($script:Failed -eq 0) {
        Write-Host "通过，但有 $($script:Warned) 项警告。" -ForegroundColor Yellow
    } else {
        Write-Host "失败 $($script:Failed) 项，警告 $($script:Warned) 项。" -ForegroundColor Red
    }

    if ($script:Failed -gt 0) { exit 1 }
}

# =============================================================================
#  子命令：help
# =============================================================================

function Show-Help {
    Write-Host 'postmarkTrip —— 跑戳日志 · 运维入口（Windows）'
    Write-Host ''
    Write-Host '用法: .\pmtrip.ps1 <子命令> [参数]'
    Write-Host ''
    Write-Host '日常:'
    Write-Host '  up              启动全部服务（后台运行）'
    Write-Host '  down            停止并移除容器（保留数据）'
    Write-Host '  restart         重启全部服务（不重建镜像）'
    Write-Host '  logs            跟随全部服务日志（最近 100 行）'
    Write-Host '  ps              查看容器状态与健康'
    Write-Host '  sh-api          进入 api 容器的 shell'
    Write-Host '  sh-db           进入数据库交互式 psql'
    Write-Host ''
    Write-Host '备份 / 恢复:'
    Write-Host '  backup          备份：pg_dump 数据库 + 生成资产清单'
    Write-Host '  restore         从转储恢复（需 -File；加 -Force 跳过确认）'
    Write-Host '                  例: .\pmtrip.ps1 restore -File backups\db\x.dump'
    Write-Host ''
    Write-Host '升级 / 自检:'
    Write-Host '  upgrade         备份 → 重建镜像 → 重启 → 重载网关'
    Write-Host '  doctor          自检：Docker、.env、示例值、存储、容器、HTTP、端口、备份'
    Write-Host ''
    Write-Host '构建 / 开发:'
    Write-Host '  build           构建/重建镜像'
    Write-Host '  pull            拉取基础镜像'
    Write-Host '  dev             开发模式：api 热重载 + Vite dev server'
    Write-Host '  clean           停止并移除容器与网络（绝不删除数据卷）'
    Write-Host '  help            显示这份帮助'
    Write-Host ''
    Write-Host '首次使用：'
    Write-Host '  .\pmtrip.ps1 up      （会自动生成 .env，并打印一次管理员口令）'
    Write-Host '  .\pmtrip.ps1 doctor'
    Write-Host ''
    Write-Host 'Linux / macOS / WSL 上请用 make，两者等价。'
    Write-Host ''
    Write-Host '存储说明：数据库默认在命名卷 postmarktrip-db；'
    Write-Host '          若 .env 设了 PMTRIP_PGDATA，则改为绑定挂载到该目录（down -v 删不到）。'
    Write-Host '          Redis 在命名卷 postmarktrip-redis，扫描件在 .\data\assets。'
}

# =============================================================================
#  分发
# =============================================================================

try {
    switch ($Command.ToLowerInvariant()) {
        'up'        { Invoke-Up }
        'down'      { Invoke-Down }
        'restart'   { Invoke-Restart }
        'logs'      { Invoke-Logs }
        'ps'        { Invoke-Ps }
        'sh-api'    { Invoke-ShApi }
        'sh-db'     { Invoke-ShDb }
        'backup'    { Invoke-Backup }
        'restore'   { Invoke-Restore }
        'upgrade'   { Invoke-Upgrade }
        'doctor'    { Invoke-Doctor }
        'dev'       { Invoke-Dev }
        'build'     { Invoke-Build }
        'pull'      { Invoke-Pull }
        'clean'     { Invoke-Clean }
        'help'      { Show-Help }
        '-h'        { Show-Help }
        '--help'    { Show-Help }
        default {
            Write-Host ''
            Write-Host "未知子命令：$Command" -ForegroundColor Red
            Show-Help
            exit 1
        }
    }
} catch {
    Write-Host ''
    Write-Host "错误：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host '  运行 .\pmtrip.ps1 doctor 做一次自检。' -ForegroundColor DarkGray
    exit 1
}
