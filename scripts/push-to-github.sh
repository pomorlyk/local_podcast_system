#!/usr/bin/env bash
# 把「听间」推送到你的 GitHub 仓库（macOS / Linux）。
#
#   ./scripts/push-to-github.sh                                  # 用默认仓库地址
#   ./scripts/push-to-github.sh https://github.com/用户名/仓库.git   # 指定仓库
#   ./scripts/push-to-github.sh <仓库地址> <分支名>
#
# 首次推送会要求授权：HTTPS 用 Personal Access Token 当密码（GitHub 不再接受
# 账号密码），或者把地址换成 SSH 形式 git@github.com:用户名/仓库.git。
set -euo pipefail

repo_url="${1:-https://github.com/pomorlyk/local_podcast_system.git}"
branch="${2:-main}"

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

[ -f run.py ] || { echo "没有在仓库根目录找到 run.py。" >&2; exit 1; }
[ -d .git ]   || { echo "这里还不是 git 仓库，请先运行 git init。" >&2; exit 1; }

echo "检查将要提交的文件…"
tracked="$(git ls-files)"
bad=""
for pattern in \
    data/ui.sqlite3 \
    data/index/translation-settings.json \
    data/index/discovery-settings.json \
    data/index/interests.json \
    data/index/feed-state.json; do
  hit="$(printf '%s\n' "$tracked" | grep -F "$pattern" || true)"
  [ -n "$hit" ] && bad="${bad}${hit}"$'\n'
done
audio="$(printf '%s\n' "$tracked" | grep -E '/episodes/.*\.(mp3|m4a)$' || true)"
[ -n "$audio" ] && bad="${bad}${audio}"$'\n'

if [ -n "${bad//[$'\n' ]/}" ]; then
  echo ""
  echo "发现不该进入仓库的文件："
  printf '%s\n' "$bad" | sed '/^$/d' | sort -u | sed 's/^/  /'
  echo "已中止。请先 git rm --cached 这些文件，或在 .gitignore 里排除它们。" >&2
  exit 1
fi
echo "  已跟踪 $(printf '%s\n' "$tracked" | grep -c . || true) 个文件，没有发现密钥或个人数据。"

if git remote | grep -qx origin; then
  git remote set-url origin "$repo_url"
else
  git remote add origin "$repo_url"
fi
echo "origin -> $repo_url"

git branch --show-current >/dev/null || { echo "当前不在任何分支上，请先提交一次。" >&2; exit 1; }
git branch -M "$branch"

echo ""
echo "正在推送到 origin/$branch …"
echo "（首次推送会要求登录：HTTPS 需要 Personal Access Token 作为密码，或改用 SSH 地址）"
git push -u origin "$branch"

echo ""
echo "完成。打开仓库页面确认 README、LICENSE、.gitignore 都在，data/library 里只有目录和封面。"
