"""piece_prompt(按件排镜提示词生成器)的单元测试。

跑法(不需要 pytest,用标准库):
    python web/backend/tests/test_piece_prompt.py
或:
    cd web/backend && python -m unittest tests.test_piece_prompt -v

覆盖的是"通用性"这件事:件名 -> 类型、代词(他/她/模特)、任意件数、
缺旧件 / 缺配饰 / 缺风格图时不写错话、以及六个小节的结构。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.piece_prompt import build_piece_prompt, kind_of  # noqa: E402

SECTIONS = ["subject_definitions:", "summary:", "retention_analysis:",
            "detailed_description:", "overall_soundscape:", "non_diegetic_music:"]


def make(**kw) -> str:
    return build_piece_prompt(title=kw.pop("title", "测试镜"), index=kw.pop("index", 2),
                              total=kw.pop("total", 5), **kw)


class TestKindOf(unittest.TestCase):
    def test_categories(self):
        cases = {"大棉袄": "outer", "风衣": "outer", "羽绒服": "outer", "西装": "outer",
                 "连衣裙": "whole", "连体衣": "whole", "睡裙": "whole",
                 "百褶短裙": "bottom", "阔腿裤": "bottom", "牛仔裤": "bottom",
                 "蕾丝长袖上衣": "top", "T恤": "top", "卫衣": "top", "吊带背心": "top",
                 "拖鞋": "shoe", "运动鞋": "shoe", "短靴": "shoe",
                 "托特包": "bag", "贝雷帽": "hat", "墨镜": "glasses",
                 "项链": "acc", "围巾": "acc"}
        for name, want in cases.items():
            self.assertEqual(kind_of(name), want, f"{name} 应判为 {want}")

    def test_bottom_beats_top(self):
        # "裙" 必须压过 "衫" 之类:半身裙不能被当成上衣
        self.assertEqual(kind_of("针织半裙"), "bottom")
        self.assertEqual(kind_of("蕾丝吊带裙"), "whole")


class TestStructure(unittest.TestCase):
    def test_sections_in_order(self):
        t = make(props=[{"name": "短裙", "pics": [2], "role": "new"}])
        pos = [t.find(s) for s in SECTIONS]
        self.assertTrue(all(p >= 0 for p in pos), f"缺小节:{list(zip(SECTIONS, pos))}")
        self.assertEqual(pos, sorted(pos), "小节顺序不对")

    def test_picture_refs_use_given_numbers(self):
        t = make(props=[{"name": "短裙", "pics": [3], "role": "new"}])
        self.assertIn("<Picture 3>", t)
        self.assertNotIn("<Picture 2>", t)


class TestPronoun(unittest.TestCase):
    def test_male(self):
        t = make(person_desc="男性，全身入画", props=[{"name": "卫衣", "pics": [2], "role": "new"}])
        self.assertIn("他", t)
        self.assertNotIn("她", t)

    def test_female(self):
        t = make(person_desc="女性，全身入画", props=[{"name": "卫衣", "pics": [2], "role": "new"}])
        self.assertIn("她", t)
        self.assertNotIn("他", t)

    def test_unknown_uses_neutral_word(self):
        t = make(person_desc="", props=[{"name": "卫衣", "pics": [2], "role": "new"}])
        self.assertIn("模特", t)
        self.assertNotIn("她", t)

    def test_loose_gender_wording(self):
        # 描述写成「一名年轻男子」「少女」也要认出来(以前只认完整词「男性 / 女士」)
        t = make(person_desc="一名年轻男子，全身入画", props=[{"name": "卫衣", "pics": [2], "role": "new"}])
        self.assertIn("他", t)
        self.assertNotIn("模特", t)
        t2 = make(person_desc="少女感，长直发", props=[{"name": "卫衣", "pics": [2], "role": "new"}])
        self.assertIn("她", t2)


class TestChangeShot(unittest.TestCase):
    def props(self):
        return [{"name": "短裙", "pics": [2], "role": "new"},
                {"name": "墨镜", "pics": [3], "role": "accessory"},
                {"name": "睡裤", "pics": [4], "role": "old"},
                {"name": "睡衣上衣", "pics": [5], "role": "worn"}]

    def test_key_sentences(self):
        t = make(props=self.props())
        self.assertIn("这一镜要飞进来的只有一件「短裙」", t)
        self.assertIn("开始时", t)                       # 开始时她身上穿的是…
        self.assertIn("横扫过去", t)
        self.assertIn("像贴纸一下贴好", t)
        self.assertIn("这两种状态之间不许有任何中间状态", t)
        self.assertIn("不许出现在空中", t)                # 旧件不许飞
        self.assertIn("结束时", t)

    def test_starting_outfit_names_which_piece_gets_replaced(self):
        # 「开始时她身上是「A、B」」这两件里只有一件会被换掉 —— 必须点明是哪一件。
        # 以前写"(就是 <Picture N> 那一件)"紧跟在两件后面,列两件却说"那一件",模型会搞混。
        t = make(props=self.props())
        self.assertIn("要被换掉的是", t)
        self.assertNotIn("(就是", t)

    def test_fly_in_follows_the_reference_clip(self):
        # 换装动作照样板片(用户给的 1.4 秒参考片):衣服从画外一角**甩进来 → 快速横扫过她身前
        # → 扫到即穿好(硬切)**。样板片里:① 衣服扫过来那一帧**比人还宽**,② 一次换装只要 ~0.7 秒。
        # 所以"只平移 / 正面始终对着镜头 / 悬停 / 不许比她还宽"这些都要**没有**。
        t = make(props=self.props())
        self.assertNotIn("只平移", t)
        self.assertNotIn("正面始终对着镜头", t)
        self.assertNotIn("悬停", t)
        self.assertNotIn("绝不许比她还宽", t)          # 样板片里它就是比人宽
        self.assertIn("横扫过去", t)
        self.assertIn("硬切", t)
        self.assertIn("不许翻滚", t)                   # 可以斜着入画,但那个斜角要保持住
        # 这句守卫不能少:不加它模型会让衣服**朝镜头飞**(先凑到镜头前变大、再缩回去)——
        # 见 quick_start._PIECE_DEPTH 的注释,用户报过"先把衣服移到屏幕前面,然后往后"。
        self.assertIn("同一个景深平面上走", t)
        self.assertIn("不许凑到镜头跟前", t)
        self.assertIn("没有形状的平板", t)             # defs:必须是一整件摊开、有轮廓的衣服

    def test_only_old_gets_replaced(self):
        # 顺序:new → 小配件 → worn → old(照 155)
        t = make(props=self.props())
        self.assertIn("<Prop 4>: replaced", t)           # 睡裤 = 被换掉的那条
        self.assertIn("<Prop 3>: fully_preserved", t)    # 同镜不动的上衣
        self.assertIn("<Prop 1>: fully_preserved", t)    # 要换上的那件
        self.assertNotIn("<Prop 1>: replaced", t)

    def test_end_state_excludes_replaced_piece(self):
        # 「结束时她身上是…」只列 不动的 + 换上的 + 小配件 —— 被换掉的那件列进去等于自相矛盾
        # (实测漏过:写成「短裙、长袖衫、短袖衫」,模型会照着把旧件又穿回去)
        line = [ln for ln in make(props=self.props()).split("\n") if "结束时" in ln][0]
        self.assertIn("短裙", line)                      # 这一镜换上的
        self.assertIn("睡衣上衣", line)                   # 本镜不动的那件
        self.assertIn("墨镜", line)                      # 一直戴着的小配件
        self.assertNotIn("睡裤", line)                   # 被换掉的旧件

    def test_prop_order_follows_155(self):
        # 换装镜:<Prop 1> = 要飞进来的那件,小配件在中间,旧件在最后
        t = make(props=self.props())
        defs = t.split("summary:")[0]
        body = [ln for ln in defs.split("\n") if ln.startswith("<Prop ")]
        self.assertTrue(body[0].startswith("<Prop 1> 是「短裙」"), body[0])
        self.assertTrue(body[-1].startswith("<Prop 4> 是「睡裤」"), body[-1])


class TestFlyShapeByKind(unittest.TestCase):
    def _shape(self, name):
        t = make(props=[{"name": name, "pics": [2], "role": "new"}])
        return [ln for ln in t.split("\n") if ln.startswith(f"<Prop 1>")][0]

    def test_bottom_says_waist_up(self):
        self.assertIn("腰头朝上", self._shape("百褶短裙"))
        self.assertIn("腰头朝上、裤脚朝下", self._shape("阔腿裤"))

    def test_top_says_collar_up(self):
        self.assertIn("领口朝上、下摆朝下", self._shape("蕾丝长袖上衣"))

    def test_long_gets_bigger_anchor(self):
        self.assertIn("肩到大腿中部", self._shape("长款大衣"))
        self.assertIn("肩到胯部", self._shape("短款卫衣"))

    def test_whole_dress(self):
        s = self._shape("连衣裙")
        self.assertIn("肩到膝盖", s)

    def test_shoe_and_bag_and_hat(self):
        self.assertIn("一整双", self._shape("运动鞋"))
        self.assertIn("整只包", self._shape("托特包"))
        self.assertIn("整顶帽子", self._shape("贝雷帽"))

    def test_glasses_and_small_accessory_have_their_own_shape(self):
        # 眼镜 / 围巾这种小件被当成「要换上的那一件」时,不能套上衣那套"领口朝上、到胯部"
        self.assertIn("镜腿朝后", self._shape("护目镜"))
        self.assertNotIn("领口朝上", self._shape("护目镜"))
        self.assertIn("不许缠成一团", self._shape("格子围巾"))


class TestMissingPieces(unittest.TestCase):
    def test_no_old_no_acc_no_style(self):
        t = make(props=[{"name": "短裙", "pics": [2], "role": "new"}])
        self.assertNotIn("开始时", t)                    # 没旧件就不写"开始时穿的是…"
        self.assertNotIn("一直戴着", t)                  # 没配饰就不写那句
        self.assertNotIn("实拍穿着参考", t)              # 没风格图就不写那句
        self.assertNotIn("replaced", t)                  # 没旧件就没有 replaced

    def test_style_block_only_when_given(self):
        t = make(props=[{"name": "短裙", "pics": [2], "role": "new"}], wear_ref_pics=[7])
        self.assertIn("实拍穿着参考", t)
        self.assertIn("<Picture 7>", t)


class TestDisplayShot(unittest.TestCase):
    def props(self):
        return [{"name": "睡衣上衣", "pics": [2], "role": "worn"},
                {"name": "睡裤", "pics": [3], "role": "worn"},
                {"name": "墨镜", "pics": [4], "role": "accessory"},
                {"name": "拖鞋", "pics": [5], "role": "accessory"}]

    def test_short_defs_and_no_change(self):
        t = make(props=self.props(), person_desc="女性，全身入画")
        self.assertIn("<Subject 1> 是她(见 <Picture 1>)。", t)
        self.assertIn("<Prop 1> 是「睡衣上衣」:参考图 <Picture 2>", t)
        self.assertIn("这一镜只做展示", t)
        self.assertNotIn("要飞进来的", t)                 # 展示镜不许出现"要飞进来"
        self.assertNotIn("换上", t)
        self.assertIn("两件都穿在身上", t)

    def test_single_item_wording(self):
        t = make(props=[{"name": "连衣裙", "pics": [2], "role": "worn"}])
        self.assertIn("穿在身上、实心不透明", t)
        self.assertNotIn("两件都穿在身上", t)

    def test_unbound_piece_leaves_no_empty_slot(self):
        # 打底那件手上没图(没绑参考图)时,不能拼出「… +  这件短袖衫」这种空档
        t = make(props=[{"name": "短袖衫", "pics": [], "role": "worn"},
                        {"name": "短裙", "pics": [3], "role": "worn"}])
        line = [ln for ln in t.split("\n") if "穿着" in ln and "短袖衫" in ln][0]
        self.assertIn("这件短袖衫 + <Picture 3> 这条短裙", line)
        self.assertNotIn("+ +", line)
        self.assertNotIn("  +", line)

    def test_display_shot_tells_what_got_swapped(self):
        # 展示镜要**自己把这一件换上去** —— 只说"她穿着 A+B"是状态描述,模型会保持不动;
        # 必须给「原来哪件 → 换成哪件」这个动作(实测:该换阔腿裤那镜的末帧里还是睡裤)
        t = make(props=[{"name": "吊带背心", "pics": [2], "role": "worn", "slot": "上身"},
                        {"name": "阔腿裤", "pics": [3], "role": "worn", "slot": "下身"}],
                 swap=(["睡裤"], ["阔腿裤"]), person_desc="女性，全身入画")
        self.assertIn("原来那件「睡裤」已经换成了「阔腿裤」", t)
        self.assertIn("「睡裤」不许再出现", t)
        # 「不换装 / 不换衣服」是给"状态图早就做好、只需站着"的场景写的 —— 这里是错的
        self.assertNotIn("不换衣服", t)
        self.assertNotIn("不换装", t)

    def test_add_on_display_shot(self):
        # 加穿外套那一镜:没有"换掉"的件,只有"多穿一件"
        t = make(props=[{"name": "吊带背心", "pics": [2], "role": "worn", "slot": "上身"},
                        {"name": "针织开衫", "pics": [3], "role": "worn", "slot": "上身·外"}],
                 swap=([], ["针织开衫"]), person_desc="女性，全身入画")
        self.assertIn("多穿了一件「针织开衫」", t)
        self.assertNotIn("已经换成了", t)

    def test_display_shot_pins_the_bottom_shape(self):
        # 展示镜也要写下装的形状 / 长度约束 —— 实测参考图是到脚踝的阔腿长睡裤,
        # 生成出来却是同花色的短裤(模型会把下装"压缩")
        t = make(props=[{"name": "睡裤", "pics": [2], "role": "worn", "slot": "下身"},
                        {"name": "睡衣上衣", "pics": [3], "role": "worn", "slot": "上身"}],
                 person_desc="女性，全身入画")
        self.assertIn("绝不许画成短裤 / 五分裤", t)
        self.assertIn("两条分开的裤腿", t)

    def test_display_shot_pins_every_piece_to_its_reference(self):
        # 展示镜没有首帧锚兜底,不写这句"没被换掉的那几件"会跟着整体色调漂
        # (实测:深灰底白雏菊的睡衣上衣,下一张状态图里变成浅灰、花纹几乎看不见)
        t = make(props=[{"name": "睡裤", "pics": [2], "role": "worn", "slot": "下身"},
                        {"name": "睡衣上衣", "pics": [3], "role": "worn", "slot": "上身"}],
                 person_desc="女性，全身入画")
        self.assertIn("都必须和它自己的参考图完全一致", t)
        self.assertIn("颜色、深浅、花纹", t)

    def test_piece_desc_goes_into_prompt(self):
        # 认件的「外观描述」必须进提示词:没图时它是唯一依据;有图时它把"这一件长什么样"说死
        # (实测只给「阔腿裤」+一张参考图,模型会顺着上身印花睡衣把下装也画成一套印花睡裤)
        t = make(props=[{"name": "阔腿裤", "pics": [2], "role": "worn", "slot": "下身",
                         "desc": "浅灰阔腿长裤,松紧腰头带抽绳,垂坠感强"}],
                 person_desc="女性，全身入画")
        self.assertIn("浅灰阔腿长裤,松紧腰头带抽绳,垂坠感强", t)

    def test_display_swap_names_the_new_piece_and_warns_off_matching_set(self):
        t = make(props=[{"name": "睡衣上衣", "pics": [3], "role": "worn", "slot": "上身",
                         "desc": "深灰底白雏菊印花短袖睡衣上衣"},
                        {"name": "阔腿裤", "pics": [2], "role": "worn", "slot": "下身",
                         "desc": "浅灰阔腿长裤,松紧腰头带抽绳"}],
                 swap=(["睡裤"], ["阔腿裤"]), person_desc="女性，全身入画")
        self.assertIn("已经换成了", t)
        self.assertIn("不要把它们配成一套花色", t)

    def test_display_added_layer_must_not_be_dropped(self):
        # 加穿一层(叠穿:吊带背心 + 开衫):模型会默认把外面那层省掉(实测两个项目开衫整条不见),
        # 所以这一镜必须把"新加上的那层"写死:连外观点出来 + 不许少 / 不许和里面那件合成一件。
        t = make(props=[{"name": "吊带背心", "pics": [2], "role": "worn", "slot": "上身",
                         "desc": "浅灰细吊带背心"},
                        {"name": "阔腿裤", "pics": [3], "role": "worn", "slot": "下身",
                         "desc": "浅灰阔腿长裤"},
                        {"name": "针织开衫", "pics": [4], "role": "worn", "slot": "上身·外",
                         "desc": "浅灰长款无扣开衫"}],
                 swap=([], ["针织开衫"]), person_desc="女性，全身入画")
        self.assertIn("多穿了一件「针织开衫」", t)
        self.assertIn("「针织开衫」绝不能少", t)
        self.assertIn("绝不许和里面那一件合成一件", t)
        self.assertIn("浅灰长款无扣开衫", t)          # 新加的那层要带外观描述

    def test_three_items_wording(self):
        t = make(props=[{"name": "上衣", "pics": [2], "role": "worn"},
                        {"name": "牛仔裤", "pics": [3], "role": "worn"},
                        {"name": "连衣裙", "pics": [4], "role": "worn"}])
        self.assertIn("全都穿在身上", t)


class TestCounts(unittest.TestCase):
    def _acc_line(self, names):
        props = [{"name": "短裙", "pics": [2], "role": "new"}]
        props += [{"name": n, "pics": [10 + i], "role": "accessory"}
                  for i, n in enumerate(names)]
        t = make(props=props)
        return [ln for ln in t.split("\n") if "一直戴着" in ln][0]

    def test_one_two_three(self):
        self.assertIn("这件小配件", self._acc_line(["墨镜"]))
        self.assertIn("这两件小配件", self._acc_line(["墨镜", "拖鞋"]))
        self.assertIn("3 件小配件", self._acc_line(["墨镜", "拖鞋", "项链"]))

    def test_multiple_new_pieces(self):
        t = make(props=[{"name": "卫衣", "pics": [2], "role": "new"},
                        {"name": "运动鞋", "pics": [3], "role": "new"}])
        self.assertIn("2 件", t)
        self.assertIn("第 2 件「运动鞋」", t)


class TestKindFromRecognition(unittest.TestCase):
    """没见过的名字也要判对:读图只答两件事(部位 + 是不是穿在外面),名字全靠它推。"""

    def test_snow_jacket_by_keyword(self):
        self.assertEqual(kind_of("雪服"), "outer")
        self.assertEqual(kind_of("始祖鸟冲锋衣"), "outer")

    def test_unknown_name_uses_desc(self):
        # 没给部位时,名字和描述里一个关键词都没有也能靠描述里的"外套"
        self.assertEqual(kind_of("ACME 滑雪装 A1", desc="厚实的滑雪外套,防寒防水面料"), "outer")

    def test_place_decides_without_any_keyword(self):
        # 部位 + 是不是穿在外面 —— 名字一个关键词都没有也判得对(睡裤 / 雪服 / 汉服 都靠它)
        self.assertEqual(kind_of("ACME G1", slot="上身·外"), "outer")
        self.assertEqual(kind_of("ACME G2", slot="上身"), "top")
        self.assertEqual(kind_of("ACME G3", slot="下身"), "bottom")
        self.assertEqual(kind_of("ACME G4", slot="全身"), "whole")
        self.assertEqual(kind_of("ACME G5", slot="脚"), "shoe")
        self.assertEqual(kind_of("ACME G6", slot="手"), "bag")
        self.assertEqual(kind_of("ACME G7", slot="配饰"), "acc")
        # 「头」上既可能是帽子也可能是眼镜 —— 靠件名分(它们飞进来的姿态不一样)
        self.assertEqual(kind_of("ACME G8", slot="头"), "hat")
        self.assertEqual(kind_of("墨镜", slot="头"), "glasses")

    def test_legacy_slot_still_works(self):
        # 老素材缓存里存的是品类词,读回来还得能对上(重新识别之前那一批)
        self.assertEqual(kind_of("ACME G9", slot="外套"), "outer")
        self.assertEqual(kind_of("ACME G10", slot="内搭"), "top")
        self.assertEqual(kind_of("ACME G11", slot="整身"), "whole")
        self.assertEqual(kind_of("ACME G12", slot="眼镜"), "hat")   # 头和帽子同档,认不出就按帽子

    def test_piece_prefix_is_not_the_garment(self):
        # 拆件名「套装-XXX」:前缀说的是"这是哪一套",不是这一件的样子 ——
        # 不剥掉的话「套装-高腰百褶短裙」会先命中「套装」被当成连衣裙
        self.assertEqual(kind_of("套装-高腰百褶短裙", slot="下身"), "bottom")
        self.assertEqual(kind_of("套装-高腰百褶短裙"), "bottom")          # 没给部位也要判对
        self.assertEqual(kind_of("套装-蕾丝一字肩长袖上衣", slot="上身"), "top")
        self.assertEqual(kind_of("套装-棉袄"), "outer")

    def test_piece_prefix_dress_keeps_its_shape(self):
        # 剥前缀之后,下装该给的姿态 / 尺寸锚不能被「套装」带成连衣裙那一套
        t = make(props=[{"name": "套装-高腰百褶短裙", "pics": [2], "role": "new",
                         "slot": "裙", "len": "短"}])
        prop1 = [ln for ln in t.split("\n") if ln.startswith("<Prop 1>")][0]
        self.assertIn("腰头朝上、裙摆朝下", prop1)
        self.assertIn("腰到大腿中部", prop1)
        self.assertNotIn("领口朝上", prop1)

    def test_build_passes_hint_through(self):
        t = make(props=[{"name": "ACME 滑雪装 A1", "pics": [2], "role": "new",
                         "slot": "上身·外", "desc": "厚实的滑雪外套,防寒防水面料"},
                        {"name": "滑雪裤", "pics": [3], "role": "old"}])
        prop1 = [ln for ln in t.split("\n") if ln.startswith("<Prop 1>")][0]
        self.assertIn("肩到大腿中部", prop1)        # 穿在外面的 → 更大的尺寸锚
        self.assertIn("下半身穿的那一条", t)        # 滑雪裤 → 下装(旧件措辞)


class TestAddOnShot(unittest.TestCase):
    """要换的那件在旧件里没有同部位的 → 这是「加穿」(外套叠到内搭上),不是「换掉」。"""

    def props(self):
        return [{"name": "开衫", "pics": [2], "role": "new"},
                {"name": "吊带背心", "pics": [3], "role": "worn"},
                {"name": "阔腿裤", "pics": [4], "role": "worn"}]

    def test_says_add_on_not_replace(self):
        t = make(props=self.props())
        self.assertIn("这是「加穿」,不是「换掉」", t)
        self.assertIn("多穿了一件「开衫」", t)
        self.assertNotIn("原来那件", t)          # 不许留"原来那件"这种悬空指代
        self.assertIn("开始时", t)               # 开始时穿着什么还是要写清

    def test_end_state_keeps_everything(self):
        line = [ln for ln in make(props=self.props()).split("\n") if "结束时" in ln][0]
        for nm in ("开衫", "吊带背心", "阔腿裤"):
            self.assertIn(nm, line)


class TestAddOnVsReplace(unittest.TestCase):
    """配对按「替换组」:外套只跟外套互换(否则加穿),整身一件顶两件。"""

    def test_coat_over_a_top_is_add_on(self):
        # 打底长袖衫 + 开衫:开衫是**加穿**在外面的,不能写成"把长袖衫换成开衫"
        t = make(props=[{"name": "开衫", "pics": [2], "role": "new"},
                        {"name": "长袖衫", "pics": [3], "role": "old"}])
        self.assertIn("这是「加穿」,不是「换掉」", t)
        self.assertNotIn("彻底没有了", t)

    def test_same_layer_still_replaces(self):
        t = make(props=[{"name": "短裙", "pics": [2], "role": "new"},
                        {"name": "长裤", "pics": [3], "role": "old"}])
        self.assertIn("彻底没有了", t)
        self.assertNotIn("这是「加穿」", t)

    def test_dress_replaces_both_base_pieces(self):
        # 连衣裙一件盖住上下半身:打底的上装和裤/裙**都要消失**
        t = make(props=[{"name": "连衣裙", "pics": [2], "role": "new"},
                        {"name": "睡衣上衣", "pics": [3], "role": "old"},
                        {"name": "睡裤", "pics": [4], "role": "old"}])
        self.assertIn("「睡衣上衣」、「睡裤」从画面里彻底没有了", t)
        self.assertNotIn("这是「加穿」", t)
        line = [ln for ln in t.split("\n") if "结束时" in ln][0]
        self.assertIn("连衣裙", line)
        self.assertNotIn("睡裤", line)


class TestLayeredDisplay(unittest.TestCase):
    """上装叠穿两层(内搭 + 敞开的外套)时:得说清"两层 + 外套敞开",否则两件会被揉成一件。"""

    def props(self):
        return [{"name": "吊带背心", "pics": [2], "role": "worn", "slot": "内搭"},
                {"name": "阔腿裤", "pics": [3], "role": "worn", "slot": "裤"},
                {"name": "开衫", "pics": [4], "role": "worn", "slot": "外套"}]

    def test_display_says_two_layers(self):
        # 只说清"上装是两件叠着穿的"(模型会把两件揉成一件);
        # **不描述形态**(敞开 / 位置)— — 怎么穿由参考图说了算
        t = make(props=self.props())
        self.assertIn("上装是两件叠着穿的", t)
        self.assertNotIn("敞开", t)

    def test_add_on_shot_does_not_describe_form(self):
        t = make(props=[{"name": "开衫", "pics": [2], "role": "new", "slot": "外套"},
                        {"name": "吊带背心", "pics": [3], "role": "worn", "slot": "内搭"},
                        {"name": "阔腿裤", "pics": [4], "role": "worn", "slot": "裤"}])
        self.assertIn("这是「加穿」,不是「换掉」", t)
        self.assertNotIn("敞开", t)

    def test_plain_two_piece_keeps_the_old_wording(self):
        t = make(props=[{"name": "短裙", "pics": [2], "role": "worn", "slot": "裙"},
                        {"name": "短袖衫", "pics": [3], "role": "worn", "slot": "上装"}])
        self.assertIn("两件都穿在身上", t)
        self.assertNotIn("叠穿的两层", t)
        self.assertNotIn("敞开", t)


class TestPiecePrefixStripped(unittest.TestCase):
    """拆件名前缀「套装-」不该进提示词正文(模型会读成"一整套飞进来")。"""

    def test_name_in_text_is_clean(self):
        t = make(props=[{"name": "套装-阔腿裤", "pics": [2], "role": "new",
                         "slot": "裤", "len": "长"}])
        self.assertIn("「阔腿裤」", t)
        self.assertNotIn("套装-", t)


class TestLengthHint(unittest.TestCase):
    """长短靠读图给的那一档(短 / 常规 / 长),不靠件名里的「长」字。"""

    def _prop1(self, **kw):
        t = make(props=[{"name": kw.pop("name"), "pics": [2], "role": "new", **kw}])
        return [ln for ln in t.split("\n") if ln.startswith("<Prop 1>")][0]

    def test_len_long_gets_bigger_anchor(self):
        self.assertIn("肩到大腿中部", self._prop1(name="卫衣", len="长"))

    def test_len_short_shrinks_even_a_long_named_piece(self):
        # 「长款大衣」但读图说它其实是短款 —— 尺寸锚要按读图的走
        self.assertIn("肩到胯部", self._prop1(name="大衣", len="短"))

    def test_sleeve_length_word_is_not_garment_length(self):
        # 「长袖」的"长"是袖子长,不是衣长
        self.assertIn("肩到胯部", self._prop1(name="长袖衫"))

    def test_true_long_name_still_works_without_hint(self):
        self.assertIn("肩到大腿中部", self._prop1(name="长款大衣"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
