"""穿戴顺序(_wear_layer)的单元测试。

跑法:  cd web/backend && python tests\\test_wear_order.py

规则:贴身先穿 → 下装/整身一件 → 上衣/外套 → 配件(包/鞋/袜/帽/围巾/腰带/首饰)收尾。
顺序错会出真事故(实测:开衫排在内搭前面 → 模型"先套外套再往里塞",凭空多穿一件背心)。
"""
from __future__ import annotations

import os
import sys

_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _APP)
sys.path.insert(0, os.path.join(_APP, ".pylibs"))

import unittest  # noqa: E402

from app.quick_start import (  # noqa: E402
    _displaced_groups,
    _float_piece_lines,
    _replace_group,
    _wear_layer,
    change_events,
)


class TestReplaceGroups(unittest.TestCase):
    """「它和谁互相替换」由读图给的两件事推出来:穿在哪儿 + 是不是穿在别的衣服外面。"""

    def test_place_and_outside_decide(self):
        self.assertEqual(_replace_group("ACME G1", "上身·外"), "outer")   # 雪服 / 开衫
        self.assertEqual(_replace_group("ACME G2", "上身"), "body")       # 普通上衣 / 内搭
        self.assertEqual(_replace_group("ACME G3", "下身"), "bottom")
        self.assertEqual(_replace_group("ACME G4", "全身"), "whole")      # 连衣裙
        self.assertEqual(_replace_group("ACME G5", "配饰"), "other")
        self.assertEqual(_replace_group("ACME G6", "脚"), "other")

    def test_outer_is_its_own_group(self):
        # 外套**不**顶掉打底的上衣(它是加穿在外面的)
        self.assertNotIn("body", _displaced_groups("开衫", "上身·外"))

    def test_dress_takes_both_layers(self):
        # 连衣裙一件盖住上下半身:上装和裤/裙都要消失
        self.assertEqual(_displaced_groups("连衣裙", "全身"), {"body", "bottom", "whole"})

    def test_legacy_slot_still_works(self):
        # 老素材缓存里存的是品类词,读回来还得能对上(重新识别之前那一批)
        self.assertEqual(_replace_group("开衫", "外套"), "outer")
        self.assertEqual(_replace_group("阔腿裤", "裤"), "bottom")
        self.assertEqual(_replace_group("连衣裙", "整身"), "whole")
        self.assertEqual(_replace_group("吊带背心", "内搭"), "body")

    def test_accessory_displaces_nothing(self):
        self.assertEqual(_replace_group("墨镜", "头"), "other")
        self.assertEqual(_displaced_groups("墨镜", "头"), set())


class TestWearLayer(unittest.TestCase):
    def test_layers(self):
        # 0 本体 → 1 穿在外面的 → 2 配件
        cases = {"蕾丝吊带背心": 0, "阔腿裤": 0, "连衣裙": 0, "睡裤": 0,
                 "开衫": 1, "大衣": 1, "羽绒服": 1, "雪服": 1,
                 "拖鞋": 2, "贝雷帽": 2, "围巾": 2, "项链": 2, "托特包": 2}
        for name, want in cases.items():
            self.assertEqual(_wear_layer(name), want, f"{name} 应在第 {want} 层")

    def test_slot_wins_over_name(self):
        # 没见过的名字靠读图给的两条定层
        self.assertEqual(_wear_layer("ACME 滑雪装 A1", "上身·外"), 1)
        self.assertEqual(_wear_layer("ACME G2", "下身"), 0)
        self.assertEqual(_wear_layer("ACME G3", "全身"), 0)
        self.assertEqual(_wear_layer("ACME G4", "上身"), 0)
        self.assertEqual(_wear_layer("ACME G5", "配饰"), 2)

    def test_legacy_slot_still_works(self):
        self.assertEqual(_wear_layer("ACME 滑雪装 A1", "外套"), 1)
        self.assertEqual(_wear_layer("ACME G2", "裤"), 0)
        self.assertEqual(_wear_layer("ACME G3", "整身"), 0)

    def test_order_uses_slot(self):
        parts = ["ACME 滑雪装 A1", "阔腿裤"]
        slots = {"ACME 滑雪装 A1": "上身·外", "阔腿裤": "下身"}
        got = sorted(parts, key=lambda n: _wear_layer(n, slots.get(n, "")))
        self.assertEqual(got, ["阔腿裤", "ACME 滑雪装 A1"])


class TestOrderingEndToEnd(unittest.TestCase):
    def test_sorted_order(self):
        # 本体先穿(同一层保持原顺序)→ 外套 → 配件收尾
        parts = ["托特包", "棉袄", "阔腿裤", "吊带背心", "贝雷帽"]
        got = sorted(parts, key=_wear_layer)
        self.assertEqual(got, ["阔腿裤", "吊带背心", "棉袄", "托特包", "贝雷帽"])

    def test_dress_before_coat(self):
        parts = ["风衣", "连衣裙"]
        self.assertEqual(sorted(parts, key=_wear_layer), ["连衣裙", "风衣"])

    def test_change_events_keeps_order(self):
        ev = change_events(["风衣", "阔腿裤", "吊带背心"])
        self.assertEqual(ev, ["阔腿裤", "吊带背心", "风衣"])


class TestPieceLinesOneOldEach(unittest.TestCase):
    """按件剧本:一件旧件只能被换掉一次 —— 配不到就是「加穿」,不许再写"换掉它"。"""

    def lines(self):
        # 镜序:打底展示 → [件 k 展示 → 件 k 换装] × 3
        return _float_piece_lines(["吊带背心", "阔腿裤", "开衫"], "短袖衫 + 长裤")

    def test_line_count(self):
        self.assertEqual(len(self.lines()), 1 + 3 * 2)

    def test_first_piece_swaps_the_base_top(self):
        self.assertIn("短袖衫", self.lines()[2])        # 件 1 换掉短袖衫

    def test_third_piece_is_an_add_on(self):
        s07 = self.lines()[-1]
        # 短袖衫在件 1 就被换掉了 —— 件 3(开衫)绝不许再声称换掉它,
        # 否则提示词里出现"把短袖衫换成开衫"这种和前面自相矛盾的句子
        self.assertNotIn("短袖衫", s07)
        self.assertIn("多穿了一件开衫", s07)
        self.assertNotIn("原来那件", s07)              # 不许留悬空指代


class TestPieceLinesReplaceSemantics(unittest.TestCase):
    """剧本里的「换掉谁」按**替换组**算 —— 外套是加穿,整身一件顶两件。"""

    def test_coat_over_a_top_is_add_on(self):
        lines = _float_piece_lines(["开衫"], "长袖衫")
        self.assertEqual(len(lines), 1 + 1 * 2)
        swap = lines[2]
        self.assertNotIn("换成「开衫」", swap)          # 不许说"把长袖衫换成开衫"
        self.assertIn("多穿了一件开衫", swap)
        self.assertIn("长袖衫", swap)                   # 打底那件照旧穿着

    def test_same_layer_still_swaps(self):
        lines = _float_piece_lines(["短裙"], "长裤")
        self.assertIn("换成「短裙」", lines[2])
        self.assertNotIn("多穿了一件短裙", lines[2])

    def test_dress_displaces_both_base_pieces(self):
        lines = _float_piece_lines(["连衣裙"], "睡衣上衣 + 睡裤")
        state, swap = lines[1], lines[2]
        # 展示镜:打底那两件都该消失,只剩连衣裙
        self.assertNotIn("睡衣上衣", state)
        self.assertNotIn("睡裤", state)
        self.assertIn("连衣裙", state)
        # 换装镜:一次顶掉两件
        self.assertIn("「睡衣上衣」、「睡裤」", swap)

    def test_slots_drive_the_order(self):
        # 读图给的两条(部位 + 是不是穿在外面)决定顺序:本体先穿、外套最后
        # 镜序:打底展示 → [件 k 展示 → 件 k 换装] × 3,所以件 k 的换装镜 = lines[2k]
        lines = _float_piece_lines(["开衫", "长裤", "吊带背心"], "短袖衫 + 短裤",
                                  ["上身·外", "下身", "上身"])
        self.assertIn("长裤", lines[2])                 # 第 1 件 = 本体
        self.assertIn("吊带背心", lines[4])             # 第 2 件
        self.assertIn("开衫", lines[6])                 # 第 3 件 = 外套收尾


if __name__ == "__main__":
    unittest.main(verbosity=2)
