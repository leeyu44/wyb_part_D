"""P0 对账项测试：桌面通知（design.md §10）+ 写入卫生指标（§8）。"""

from memhall.notify import desktop_notify
from memhall.report import compute_metrics
from memhall.schema.evidence import Verdict, VerdictValue
from memhall.schema.models_case import Capability, MemoryCase


def test_notify_noop_when_headless(monkeypatch):
    """无 notify-send / 无 DISPLAY：静默跳过不抛错。"""
    monkeypatch.setattr("memhall.notify.shutil.which", lambda n: None)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    desktop_notify("麟阁评测完成", "总体 81.6%", open_path="/tmp/x.png")


def test_notify_invokes_notify_send_when_desktop(monkeypatch):

    calls: list = []
    monkeypatch.setattr("memhall.notify.shutil.which",
                        lambda n: "/usr/bin/notify-send" if n == "notify-send"
                        else "/usr/bin/xdg-open")
    monkeypatch.setattr("memhall.notify.subprocess.run",
                        lambda cmd, **kw: calls.append(cmd) or None)
    monkeypatch.setattr("memhall.notify.subprocess.Popen",
                        lambda cmd, **kw: calls.append(cmd) or None)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr("memhall.notify.os.path.exists", lambda p: True)
    desktop_notify("麟阁评测完成", "总体 81.6%", open_path="/tmp/radar.png")
    assert calls[0][0] == "/usr/bin/notify-send" and calls[0][-2] == "麟阁评测完成"
    assert calls[1][0] == "/usr/bin/xdg-open"   # 顺手弹雷达图


def _case(cid: str, cap: Capability) -> MemoryCase:
    from pathlib import Path

    import yaml
    raw = yaml.safe_load((Path(__file__).resolve().parents[1]
                          / "cases/full/boundary-001.yaml").read_text(encoding="utf-8"))
    raw["case_id"] = cid
    raw["capability"] = cap.value
    return MemoryCase.model_validate(raw)


def _v(pid: str, case_id: str, val: VerdictValue) -> Verdict:
    return Verdict(verdict_id=pid, run_id="r", case_id=case_id, probe_id=pid,
                   verdict=val, decided_by="rule", confidence=1.0,
                   explanation="")


def test_write_hygiene_metric():
    """写入卫生 = boundary 族 over_persist 占有效探测比；无 boundary 题时为 None。"""
    cases = {"b1": _case("b1", Capability.BOUNDARY),
             "r1": _case("r1", Capability.RECALL)}
    vs = [_v("p1", "b1", VerdictValue.OVER_PERSIST),
          _v("p2", "b1", VerdictValue.CORRECT),
          _v("p3", "b1", VerdictValue.INVALID_RUN),
          _v("p4", "r1", VerdictValue.OVER_PERSIST)]  # 非 boundary 不计入
    m = compute_metrics(vs, cases)
    assert m["write_hygiene"] == 0.5
    assert compute_metrics([_v("p4", "r1", VerdictValue.CORRECT)],
                           {"r1": cases["r1"]})["write_hygiene"] is None
