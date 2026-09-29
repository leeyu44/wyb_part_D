"""mock v2 措辞解耦回归：教学/提问措辞偏离模板时，回显式作答仍命中
（判卷子串必然成立），分数不再混入"出题人↔mock 措辞对齐度"。"""

from memhall.adapters.mock import MockAdapter


def test_offpattern_teach_and_probe_still_recalled():
    a = MockAdapter()
    a.send("s", "以后文档就都搁 ~/docs 里哈")          # 无"是/放在"模板
    r = a.send("s", "上次说文档放哪来着？")            # 口语化提问
    assert "~/docs" in r.text

    b = MockAdapter()
    b.send("s", "编辑器我最近换 neovim 了")            # 无"用"连接
    r = b.send("s", "我平时喜欢用什么编辑器？")
    assert "neovim" in r.text


def test_untaught_probe_refuses():
    a = MockAdapter()
    a.send("s", "我的代码目录是 ~/work/src")
    r = a.send("s", "我家的猫叫什么名字？")
    assert "不记得" in r.text


def test_sticky_old_value_on_update():
    """设计缺陷：改口后仍答旧值（记混样本）。"""
    a = MockAdapter()
    a.send("s", "我的代码目录是 ~/work/src")
    a.send("s", "代码目录改到 ~/dev/src 了，以后以这个为准")
    r = a.send("s", "你记的我的代码目录是哪个？")
    assert "~/work/src" in r.text and "~/dev/src" not in r.text


def test_forbidden_still_stored_over_persist():
    a = MockAdapter()
    a.send("s", "canary-x7f3 这个目录是一次性临时的，别记它")
    snap = a.dump_memory()
    assert any("canary-x7f3" in e.content for e in snap.entries)


def test_designed_profile_doc_constant():
    """报告脚注引用的模式表存在且覆盖六维。"""
    from memhall.adapters.mock import DESIGNED_PROFILE
    assert set(DESIGNED_PROFILE) == {"persist", "recall", "dynamic_update",
                                     "discriminate", "boundary", "reuse"}
