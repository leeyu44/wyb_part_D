#!/bin/bash
# 在 openKylin 目标机上原生构建 memhall .deb（源码置于 ~/memhall 后执行）
# 产物：~/deb-stage/memhall_0.2.1_all.deb（内置离线 wheels，安装不依赖网络）
set -e
SRC=${SRC:-$HOME/memhall}
cd $SRC

W=~/wheels
rm -rf $W && mkdir -p $W
pip3 download -q -i https://pypi.tuna.tsinghua.edu.cn/simple -d $W \
  pydantic pyyaml matplotlib paramiko fastapi uvicorn
pip3 wheel -q --no-deps -i https://pypi.tuna.tsinghua.edu.cn/simple -w $W .

STAGE=~/deb-stage/memhall
rm -rf ~/deb-stage
mkdir -p $STAGE/DEBIAN $STAGE/usr/share/memhall/scripts $STAGE/usr/bin
cp -r $W $STAGE/usr/share/memhall/wheels
cp -r cases $STAGE/usr/share/memhall/cases
cp README.md LICENSE $STAGE/usr/share/memhall/
cp scripts/judge_selftest.py $STAGE/usr/share/memhall/scripts/

cat > $STAGE/usr/bin/memhall <<'WEOF'
#!/bin/sh
export PYTHONPATH=/usr/lib/memhall/pylib${PYTHONPATH:+:$PYTHONPATH}
exec python3 -c 'import sys; from memhall.cli import main; sys.exit(main())' "$@"
WEOF
chmod 755 $STAGE/usr/bin/memhall

cat > $STAGE/DEBIAN/control <<'CEOF'
Package: memhall
Version: 0.2.1
Architecture: all
Maintainer: MemHall Team <memhall@openkylin.example>
Depends: python3 (>= 3.11)
Section: utils
Priority: optional
Homepage: https://gitee.com/mazhuoran23/MemHall
Description: 麟阁 MemHall —— 面向 openKylin 生态的智能体记忆能力评测基准
 三阶段剧本（教-隔-考）驱动被测智能体，采集对话/记忆快照/动作/文件系统四类证据，
 混合判卷（规则断言 + LLM 单判）输出六维能力雷达。内置 mock 适配器可离线演示，
 hermes/kylinbot 适配器经 SSH 驱动真机评测。
CEOF

cat > $STAGE/DEBIAN/postinst <<'PEOF'
#!/bin/sh
set -e
pip3 install --quiet --no-index --no-deps --upgrade --break-system-packages \
  --target /usr/lib/memhall/pylib /usr/share/memhall/wheels/*.whl
PEOF

cat > $STAGE/DEBIAN/prerm <<'REOF'
#!/bin/sh
rm -rf /usr/lib/memhall/pylib
REOF
chmod 755 $STAGE/DEBIAN/postinst $STAGE/DEBIAN/prerm

cd ~/deb-stage
fakeroot dpkg-deb --root-owner-group -Zxz --build memhall memhall_0.2.1_all.deb 2>/dev/null || dpkg-deb -Zxz --build memhall memhall_0.2.1_all.deb
ls -lh memhall_0.2.1_all.deb
