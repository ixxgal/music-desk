# -*- coding: utf-8 -*-
"""检查安装目录里的快手登录态是否还在（Cookies 是 Edge 加密的，只读名字与长度）。"""
import os
import shutil
import sqlite3
import tempfile
import uuid

CK = os.path.join(os.environ["LOCALAPPDATA"], "MusicDesk", "data", "cdp-kuaishou",
                  "Default", "Network", "Cookies")
tmp = os.path.join(tempfile.gettempdir(), "cks_%s.db" % uuid.uuid4().hex)
shutil.copy2(CK, tmp)
try:
    db = sqlite3.connect(tmp)
    rows = db.execute(
        "select host_key, name, length(encrypted_value) from cookies "
        "where host_key like '%kuaishou%' order by name"
    ).fetchall()
    print("快手 cookie 共 %d 条" % len(rows))
    keys = ("userId", "passToken", "kuaishou.server.webday7_st", "did", "kwfv1", "kwssectoken")
    for h, n, ln in rows:
        if n in keys:
            print("  %-28s %-30s %d bytes" % (h, n, ln))
    names = {n for _, n, _ in rows}
    ok = {"userId", "passToken"} <= names
    print("登录态:", "✓ 有效" if ok else "✗ 缺失")
finally:
    os.remove(tmp)