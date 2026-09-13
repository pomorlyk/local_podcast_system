# 把「听间」推送到你的 GitHub 仓库。
#
# 用法（在仓库根目录，直接回车即可用下面的默认值）：
#   powershell -ExecutionPolicy Bypass -File scripts/push-to-github.ps1
#
# 只检查、不推送（演练）：
#   powershell -ExecutionPolicy Bypass -File scripts/push-to-github.ps1 -DryRun
#
# 换仓库或换身份时才加参数：
#   ... -RepoUrl https://github.com/其他用户名/别的仓库.git
#
# 首次推送会弹出浏览器让你授权 GitHub（Git Credential Manager），
# 不需要输入邮箱密码，也不需要把密码交给任何人。
param(
    [string]$RepoUrl = 'https://github.com/pomorlyk/local_podcast_system.git',
    [string]$Branch = 'main',
    [string]$Name = '',
    [string]$Email = '',
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'

$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

if (-not (Test-Path (Join-Path $root 'run.py'))) {
    throw '没有在仓库根目录找到 run.py，请确认这个脚本位于 <仓库>/scripts/ 下。'
}
if (-not (Test-Path (Join-Path $root '.git'))) {
    throw '这里还不是 git 仓库。请先运行：git init'
}

# --- 0. 挑一个带凭据助手的 git ---------------------------------------------
# 机器上可能装了不止一个 git。PATH 里那个（例如工具自带的 PortableGit）不一定
# 带 Git Credential Manager，会报 “could not read Username” 这种看不懂的错。
# 这里优先用官方的 Git for Windows，它自带 GCM。
$gitExe = 'git'
foreach ($candidate in @('C:\Program Files\Git\cmd\git.exe',
                         'C:\Program Files (x86)\Git\cmd\git.exe')) {
    if (Test-Path -LiteralPath $candidate) { $gitExe = $candidate; break }
}
Write-Output ('使用的 git  ：' + $gitExe)
Write-Output ('版本        ：' + (& $gitExe --version))
Write-Output ('凭据助手    ：' + $(& $gitExe config --get credential.helper 2>$null))
Write-Output ''

# --- 1. 身份 ---------------------------------------------------------------
if ($Name)  { & $gitExe config user.name  $Name }
if ($Email) { & $gitExe config user.email $Email }

$identityName  = (& $gitExe config user.name)
$identityEmail = (& $gitExe config user.email)
if (-not $identityName -or -not $identityEmail) {
    throw "还没有配置 git 身份。请加上参数重跑：`n" +
          "  -Name `"你的名字`" -Email `"你的邮箱@example.com`""
}
Write-Output ("提交身份    ：$identityName <$identityEmail>")
Write-Output ''

# --- 2. 推之前最后检查一遍：有没有把不该提交的东西带上 ----------------------
$tracked = & $gitExe ls-files
$forbidden = @(
    'data/ui.sqlite3',
    'data/index/translation-settings.json',
    'data/index/discovery-settings.json',
    'data/index/interests.json',
    'data/index/feed-state.json'
)
$bad = @()
foreach ($pattern in $forbidden) {
    $hit = $tracked | Where-Object { $_ -like $pattern -or $_ -like "$pattern-*" }
    if ($hit) { $bad += $hit }
}
$bad += ($tracked | Where-Object { $_ -match '/episodes/.*\.(mp3|m4a)$' })
if ($bad.Count -gt 0) {
    Write-Output '发现不该进入仓库的文件：'
    $bad | Sort-Object -Unique | ForEach-Object { Write-Output "  $_" }
    throw '已中止。请先 git rm --cached 这些文件，或在 .gitignore 里排除它们。'
}
Write-Output ('安全检查    ：已跟踪 ' + ($tracked | Measure-Object).Count + ' 个文件，没有密钥 / 学习记录 / 音频。')

# --- 3. 远程仓库是否存在 ---------------------------------------------------
$owner = $null
$repo = $null
if ($RepoUrl -match 'github\.com[/:]([^/]+)/([^/]+?)(\.git)?$') {
    $owner = $Matches[1]
    $repo  = $Matches[2]
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    try {
        $info = Invoke-RestMethod -Uri "https://api.github.com/repos/$owner/$repo" `
            -Headers @{ 'User-Agent' = 'push-to-github' } -TimeoutSec 30
        Write-Output ('远程仓库    ：' + $owner + '/' + $repo + ' 存在（' +
            $(if ($info.private) { '私有' } else { '公开' }) + '），默认分支 ' + $info.default_branch)
        if ($info.size -gt 0) {
            Write-Output '              注意：远程已有内容，直接推可能被拒，需要先 git pull --rebase origin main'
        }
    } catch {
        $code = $null
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        if ($code -eq 404) {
            throw "GitHub 上还没有 $owner/$repo 这个仓库。`n" +
                  "请先在浏览器打开 https://github.com/new 建一个空仓库：`n" +
                  "  仓库名填 $repo，不要勾 Add README / .gitignore / license，然后点 Create repository。`n" +
                  '建好后重跑本脚本即可。'
        }
        Write-Output ('远程仓库    ：无法确认状态（' + $_.Exception.Message + '），仍然继续尝试推送。')
    }
}

# --- 4. remote 与分支 ------------------------------------------------------
if ((& $gitExe remote) -contains 'origin') {
    & $gitExe remote set-url origin $RepoUrl
} else {
    & $gitExe remote add origin $RepoUrl
}
if (-not (& $gitExe branch --show-current)) {
    throw '当前不在任何分支上。请先完成一次提交：git add -A; git commit -m "初始提交"'
}
& $gitExe branch -M $Branch
Write-Output ('origin      ：' + $RepoUrl)
Write-Output ('分支        ：' + $Branch + '，共 ' + (& $gitExe rev-list --count HEAD) + ' 个提交待推送')

# --- 5. 推送（失败绝不假装成功） -------------------------------------------
if ($DryRun) {
    Write-Output ''
    Write-Output '=== 演练模式：以上检查全部通过，没有真的推送 ==='
    Write-Output '去掉 -DryRun 重跑一次即会真正推送。'
    return
}

Write-Output ''
Write-Output "正在推送到 origin/$Branch …"
Write-Output '（首次会弹出浏览器让你授权 GitHub，请在浏览器里点完授权，不要关掉那个窗口）'
& $gitExe push -u origin $Branch
$pushCode = $LASTEXITCODE

if ($pushCode -ne 0) {
    Write-Output ''
    Write-Output '=================== 推送失败 ==================='
    Write-Output '按上面的报错对照下面三种情况：'
    Write-Output ''
    Write-Output '1) could not read Username / Authentication failed'
    Write-Output '   → 凭据没拿到。确认弹出的浏览器窗口里完成了 GitHub 授权；'
    Write-Output '     如果根本没弹窗，改用 Personal Access Token：'
    Write-Output '       a. 打开 https://github.com/settings/tokens 生成 classic token'
    Write-Output '       b. 勾选 repo 权限，复制那串 token（只显示一次）'
    Write-Output '       c. 重跑，Username 填 pomorlyk，Password 粘贴 token（不是账号密码）'
    Write-Output ''
    Write-Output '2) rejected / non-fast-forward'
    Write-Output '   -> 远程已有内容。先执行：git pull --rebase origin main'
    Write-Output ''
    Write-Output '3) could not resolve host / Failed to connect'
    Write-Output '   -> 网络或代理问题。先确认浏览器能打开 https://github.com'
    Write-Output ''
    throw "推送没有成功（git 退出码 $pushCode）。修好后重跑本脚本即可。"
}

# --- 6. 真的推上去了才算成功 -----------------------------------------------
$remoteHeads = & $gitExe ls-remote --heads origin
if (-not $remoteHeads) {
    throw '推送命令返回成功，但远程仓库里仍然没有任何分支。请把完整输出发给助手排查。'
}

Write-Output ''
Write-Output '推送成功。远程分支：'
$remoteHeads | Select-Object -First 3 | ForEach-Object { Write-Output ('  ' + $_) }
if ($owner -and $repo) {
    Write-Output ''
    Write-Output "打开 https://github.com/$owner/$repo 确认一下：README、LICENSE、.gitignore 都在，data/library 里只有目录和封面。"
}
