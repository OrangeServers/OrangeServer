#!/usr/bin/env bash
# 一键把散落在各文档/配置里的当前稳定版本号从 <旧版本> 刷到 <新版本>。
#
# 用法: bash ops/bump-release-version.sh <旧版本> <新版本>
# 例:   bash ops/bump-release-version.sh v1.1.1 v1.2.0
#
# 覆盖清单（与 docs/operations/BACKEND_IMAGE_RELEASE.md「版本号收敛」一致）:
#   website/.vitepress/theme/installCommands.ts   (global + china 两处安装命令)
#   website/guide/deployment.md / getting-started.md
#   website/zh/guide/deployment.md / getting-started.md
#   README.md / README.zh-CN.md
#   backend/Dockerfile   (org.opencontainers.image.version LABEL，不带 v 前缀)
#
# 注意: 用 python 二进制替换而非 sed -i，避免在 Windows 上把 CRLF 文件的行尾破坏。
#       「功能自某版本起提供」的历史标记（如 v1.0.3）不在替换范围内，只替换当前
#       稳定版本号这一个字面量。
set -Eeuo pipefail

old="${1:?用法: bash ops/bump-release-version.sh <旧版本> <新版本>}"
new="${2:?用法: bash ops/bump-release-version.sh <旧版本> <新版本>}"

# 版本号必须是稳定 SemVer（vX.Y.Z）
if [[ ! "$old" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || [[ ! "$new" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "错误: 版本号必须是稳定 SemVer，例如 v1.2.3" >&2
  exit 1
fi
if [[ "$old" == "$new" ]]; then
  echo "错误: 新旧版本相同，无需刷新" >&2
  exit 1
fi

# 跨平台定位 python：WSL/Linux 用 python3，Windows Git Bash 可能只有 python。
# 注意 Git Bash 下 `command -v python3` 会命中 Windows 商店的占位程序：它不
# 打印任何东西就以非零码退出，配合 set -e 会让脚本静默失败、版本号原封不动。
PYTHON=''
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 \
        && "$candidate" -c 'import sys' >/dev/null 2>&1; then
        PYTHON="$(command -v "$candidate")"
        break
    fi
done
if [[ -z "$PYTHON" ]]; then
  echo "错误: 未找到可用的 python3/python（Windows 商店占位程序不算），无法执行二进制替换" >&2
  exit 1
fi

"$PYTHON" - "$old" "$new" <<'PYEOF'
import sys
import pathlib

old = sys.argv[1]
new = sys.argv[2]

files = [
    "website/.vitepress/theme/installCommands.ts",
    "website/guide/deployment.md",
    "website/guide/getting-started.md",
    "website/zh/guide/deployment.md",
    "website/zh/guide/getting-started.md",
    "README.md",
    "README.zh-CN.md",
]

old_b = old.encode("utf-8")
new_b = new.encode("utf-8")

for f in files:
    p = pathlib.Path(f)
    b = p.read_bytes()
    n = b.count(old_b)
    if n:
        p.write_bytes(b.replace(old_b, new_b))
    print(f"[OK] {f}: {old} -> {new} ({n} 处)")

# backend/Dockerfile 的 LABEL 版本不带 v 前缀
p = pathlib.Path("backend/Dockerfile")
b = p.read_bytes()
old_label = b'"' + old[1:].encode("utf-8") + b'"'
new_label = b'"' + new[1:].encode("utf-8") + b'"'
n = b.count(old_label)
if n:
    p.write_bytes(b.replace(old_label, new_label))
print(f'[OK] backend/Dockerfile: "{old[1:]}" -> "{new[1:]}" ({n} 处)')
PYEOF

echo ""
echo "完成。请 git diff 检查，并跑 pwsh -File ops/check-docs.ps1 确认无残留旧版本引用。"
