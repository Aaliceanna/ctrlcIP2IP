# -*- coding: utf-8 -*-
"""锁定「双向」判定语义：ok 只代表对方记下了本机，handshake 才代表对方真能连回。

1.4.3 及之前用 `bool(rev.ok)` 判 mutual，于是本机只监听 127.0.0.1 时会打印
「双向绑定成功（TimeoutError：…）」这种自相矛盾的日志，面板也显示双向。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import clipshare as cs  # noqa: E402


def check(name, rev, want_mutual, want_note_kw=None):
    got = cs.rev_is_mutual(rev)
    note = cs.reverse_bind_note(rev)
    ok = (got == want_mutual)
    if want_note_kw:
        ok = ok and (want_note_kw in note)
    print(("PASS" if ok else "FAIL"), name)
    print("      rev =", rev)
    print("      mutual =", got, "(期望", want_mutual, ")")
    print("      note   =", note)
    return ok


def main():
    fails = 0
    # 1) 对方记下了本机，但反向握手超时 —— 这正是用户遇到的情况
    fails += not check(
        "对端已加入但对端连不回（超时）",
        {"ok": True, "skipped": False, "handshake": False,
         "detail": "TimeoutError：检查 IP/端口/防火墙/对方是否开机"},
        False, "连不回本机")
    # 2) 真双向
    fails += not check(
        "真双向",
        {"ok": True, "skipped": False, "handshake": True, "detail": "PC-B v1.4.4"},
        True, "双向绑定成功")
    # 3) 对方跳过回绑
    fails += not check(
        "对方跳过回绑",
        {"ok": True, "skipped": True, "handshake": False, "error": "对端就是本机，已跳过"},
        False, "跳过")
    # 4) 压根连不上对方
    fails += not check(
        "连不上对方",
        {"ok": False, "skipped": False, "handshake": False,
         "error": "TimeoutError：连不上对方（IP/端口/防火墙/对方是否在线）"},
        False, "未能回绑")
    # 5) 对方回绑了但握手字段缺失（老版本对端不返回 handshake）—— 保守判为单向
    fails += not check(
        "对端老版本无 handshake 字段",
        {"ok": True, "skipped": False},
        False, "连不回本机")

    print()
    print("全部通过" if not fails else f"{fails} 项失败")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
