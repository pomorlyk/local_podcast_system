# 把「听间」推送到你的 GitHub 仓库。
#
# 用法（在仓库根目录，直接回车即可用下面的默认值）：
#   powershell -ExecutionPolicy Bypass -File scripts/push-to-github.ps1
#
# 需要换仓库或换身份时才加参数：
#   ... -RepoUrl https://github.com/其他用户名/别的仓库.git
#
# 首次推送会弹出浏览器让你授权 GitHub（Git Credential Manager），
# 不需要在这里输入邮箱密码，也不需要把密码交给任何人。
param(
    [string]$RepoUrl = 'https://github.com/pomorlyk/local_podcast_system.git',
    [string]$Branch = 'main',
    [string]$Name = '',
    [string]$Email = ''
)
$ErrorActionPreference = 'Stop'

$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

if (-not (Test-Path (Join-Path $root 'run.py'))) {
    throw "没有在仓库根目录找到 run.py，请确认这个脚本位于 <仓库>/scripts/ 下。"
}
if (-not (Test-Path (Join-Path $root '.git'))) {
    throw "这里还不是 git 仓库。请先运行：git init"
}

# --- 1. 身份 ---------------------------------------------------------------
if ($Name)  { git config user.name  $Name }
if ($Email) { git config user.email $Email }
$currentName  = (git config user.name)
$currentEmail = (git config user.email)
if (-not $currentName -or -not $currentEmail) {
    throw "还没有配置 git 身份。请加上参数重跑，例如：`n" +
          "  -Name `"你的名字`" -Email `"你的邮箱@example.com`""
}
Write-Output "提交身份：$currentName <$currentEmail>"

# --- 2. 推之前最后检查一遍：有没有把不该提交的东西带上 ----------------------
Write-Output '检查将要提交的文件…'
$tracked = git ls-files
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
    Write-Output ''
    Write-Output '发现不该进入仓库的文件：'
    $bad | Sort-Object -Unique | ForEach-Object { Write-Output "  $_" }
    throw '已中止。请先 git rm --cached 这些文件，或在 .gitignore 里排除它们。'
}
Write-Output ("  已跟踪 " + ($tracked | Measure-Object).Count + " 个文件，没有发现密钥或个人数据。")

# --- 3. 远程仓库是否存在（避免推送时报一段看不懂的认证错误） ----------------
if ($RepoUrl -match 'github\.com[/:]([^/]+)/([^/]+?)(\.git)?$') {
    $owner = $Matches[1]
    $repo  = $Matches[2]
    Write-Output "检查远程仓库 $owner/$repo …"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    try {
        $info = Invoke-RestMethod -Uri "https://api.github.com/repos/$owner/$repo" `
            -Headers @{ 'User-Agent' = 'push-to-github' } -TimeoutSec 30
        $visibility = if ($info.private) { '私有' } else { '公开' }
        Write-Output ("  仓库存在（$visibility），默认分支 " + $info.default_branch)
        if ($info.size -gt 0) {
            Write-Output '  注意：远程仓库里已经有内容了。直接推送可能因为“非快进”被拒，'
            Write-Output '        需要先执行：git pull --rebase origin main'
        }
    } catch {
        $code = $null
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        if ($code -eq 404) {
            throw "GitHub 上还没有 $owner/$repo 这个仓库。`n" +
                  "请先在浏览器打开 https://github.com/new 建一个空仓库：`n" +
                  "  仓库名填 $repo，不要勾 Add README / .gitignore / license，然后点 Create repository。`n" +
                  "建好后重跑本脚本即可。"
        }
        Write-Output "  无法确认仓库状态（$($_.Exception.Message)），仍然继续尝试推送。"
    }
}

# --- 4. remote + 分支 ------------------------------------------------------
$existing = (git remote) 2>$null
if ($existing -contains 'origin') {
    git remote set-url origin $RepoUrl
} else {
    git remote add origin $RepoUrl
}
Write-Output "origin -> $RepoUrl"

if (-not (git branch --show-current)) {
    throw "当前不在任何分支上。请先完成一次提交：git add -A; git commit -m `"初始提交`""
}
git branch -M $Branch

# --- 5. 推送 ---------------------------------------------------------------
Write-Output ''
Write-Output "正在推送到 origin/$Branch …"
Write-Output '（首次推送会弹出浏览器让你登录 / 授权 GitHub，那是 Git Credential Manager 在走 OAuth）'
git push -u origin $Branch

Write-Output ''
Write-Output "完成。打开 https://github.com/$owner/$repo 确认一下：README、LICENSE、.gitignore 都在，data/library 里只有目录和封面。"
