# -*- coding: utf-8 -*-
"""防火墙放行判定回归测试。

背景：1.4.1 及以前把「程序级规则」当成「端口已放行」，于是一台装了微信 /
网易云的机器上，任何端口都会被判成「已有放行规则，无需处理」，
用户按提示怎么都修不好连接。

本测试锁死判定语义：程序级规则只对它自己的程序生效，不能算全局放行；
另外必须校验协议（UDP 规则不能让 TCP 算通）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import clipshare as cs  # noqa: E402


FAKE = [
    # 只放行 UDP —— 不能算 TCP 通了
    {"name": "OnlyUdp", "enabled": "是", "direction": "入", "action": "允许",
     "protocol": "UDP", "localport": "19994"},
    # 别的程序 + 任意端口（就是误判的元凶）
    {"name": "wechatappex.exe", "enabled": "是", "direction": "入", "action": "允许",
     "protocol": "TCP", "localport": "任何", "program": r"C:\x\wechatappex.exe"},
    # 已禁用
    {"name": "Disabled", "enabled": "否", "direction": "入", "action": "允许",
     "protocol": "TCP", "localport": "19994"},
    # 出站
    {"name": "Out", "enabled": "是", "direction": "出", "action": "允许",
     "protocol": "TCP", "localport": "19994"},
]


def check(rules, port=19994):
    cs._win_rule_blocks = lambda timeout=20.0, verbose=False, cache_secs=0.0: list(rules)
    cs._self_program_names = lambda: {"clipshare.exe"}
    return cs._win_port_check(port)


def main() -> int:
    fails = []

    def case(name, cond, got=""):
        if cond:
            print(f"  ✓ {name}")
        else:
            print(f"  ✗ {name}  {got}")
            fails.append(name)

    print("[1] 只有别的程序的规则 → 必须判为未放行")
    r = check(FAKE)
    case("scope=other", r["scope"] == "other", r["scope"])
    case("allowed=False", r["allowed"] is False, r["allowed"])
    case("列出占用者", "wechatappex.exe" in r["other"], r["other"])

    print("[2] 自己的程序规则 → 算放行")
    r = check(FAKE + [{"name": "ClipShare.exe", "enabled": "是", "direction": "入",
                       "action": "允许", "protocol": "TCP", "localport": "19994",
                       "program": r"C:\a\ClipShare.exe"}])
    case("scope=self", r["scope"] == "self", r["scope"])
    case("allowed=True", r["allowed"] is True, r["allowed"])

    print("[3] 全局规则 → 算放行")
    r = check(FAKE + [{"name": "Global", "enabled": "是", "direction": "入",
                       "action": "允许", "protocol": "TCP", "localport": "19994"}])
    case("scope=global", r["scope"] == "global", r["scope"])
    case("allowed=True", r["allowed"] is True, r["allowed"])

    print("[4] 只有 UDP 规则 → 不能算 TCP 已放行")
    r = check([FAKE[0]])
    case("allowed=False", r["allowed"] is False, r["allowed"])

    print("[5] 禁用 / 出站规则 → 不算")
    r = check([FAKE[2], FAKE[3]])
    case("allowed=False", r["allowed"] is False, r["allowed"])

    print("[6] 端口区间的全局规则 → 算放行")
    r = check([{"name": "Range", "enabled": "是", "direction": "入", "action": "允许",
                "protocol": "TCP", "localport": "19990-20000"}])
    case("allowed=True", r["allowed"] is True, r["allowed"])

    print()
    if fails:
        print(f"失败 {len(fails)} 项：{', '.join(fails)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
