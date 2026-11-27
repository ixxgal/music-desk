# -*- coding: utf-8 -*-
"""列出快手 profile 里的 cookie 名（不解密值，只看有没有登录凭证）。"""
import sqlite3
import sys
from pathlib import Path

import kuaishou_login as K

src = (Path(K.PROFILE) / "Default" / "Network" / "Cookies").as_posix()
con = sqlite3.connect("file:%s?immutable=1" % src, uri=True)
rows = con.execute(
    "select host_key,name,length(encrypted_value),expires_utc from cookies "
    "where host_key like '%kuaishou%' order by name"
).fetchall()
print("kuaishou cookies:", len(rows))
for h, n, l, e in rows:
    print("  %-32s %-30s len=%-5d exp=%s" % (h, n, l, e))
con.close()

names = {r[1] for r in rows}
keys = [k for k in ("userId", "passToken", "web_st", "kuaishou.web.cp.api_ph", "kuaishou.web.cp.api_st") if k in names]
print("\n登录凭证命中:", keys or "无")