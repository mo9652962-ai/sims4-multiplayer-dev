#!/usr/bin/env bash
# ============================================================
# Sims4Multiplayer 打包脚本
# 把 src/ 下的 .py 编译成 .pyc 并打包成 .ts4script
# 用法:
#   bash build.sh          # 打包全部模块
#   bash build.sh init     # 只打包 __init__.py (最小化实验)
# 产物: build/Sims4Multiplayer.ts4script → 自动复制到 Mods
# ============================================================

PROJECT_DIR="/d/Sims4-Multiplayer-Dev"
PROJECT_DIR_WIN="D:\\Sims4-Multiplayer-Dev"
PY="$PROJECT_DIR/tools/python37-embed/python.exe"
SRC="$PROJECT_DIR/src"
BUILD="$PROJECT_DIR/build"
OUT="$BUILD/Sims4Multiplayer.ts4script"
MODS_DIR="/c/Users/31954/Documents/Electronic Arts/The Sims 4/Mods"

echo "=== 1. 清理旧产物 ==="
rm -rf "$BUILD/pycache"
mkdir -p "$BUILD/pycache"

ONLY_MODULE="${1:-all}"
echo "打包模式: $ONLY_MODULE"

echo "=== 2. 编译 .py → .pyc ==="
cd "$SRC"
find . -name "*.py" | while read f; do
    if [ "$ONLY_MODULE" != "all" ]; then
        case "$f" in
            *"/__init__.py"|*"/$ONLY_MODULE.py") ;;
            *) continue ;;
        esac
    fi
    rel="${f#./}"
    target="$BUILD/pycache/${rel%.py}.pyc"
    mkdir -p "$(dirname "$target")"
    "$PY" -m py_compile "$f"
    cachefile="$(dirname "$f")/__pycache__/$(basename "$f" .py).cpython-37.pyc"
    mv "$cachefile" "$target"
    echo "   编译: $f"
done

echo "=== 3. 打包 .ts4script (zip 格式) ==="
cd "$BUILD/pycache"
"$PY" -c "
import zipfile, os
out = r'$PROJECT_DIR_WIN\\build\\Sims4Multiplayer.ts4script'
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk('.'):
        for fn in files:
            path = os.path.join(root, fn)
            z.write(path, path.replace('\\\\', '/').lstrip('./'))
print('打包完成:', out)
"

echo "=== 4. 部署到 Mods 目录 ==="
rm -f "$MODS_DIR/Sims4Multiplayer.ts4script"
cp "$OUT" "$MODS_DIR/Sims4Multiplayer.ts4script"
echo "已复制到: $MODS_DIR/Sims4Multiplayer.ts4script"
echo "✅ 完成!"
