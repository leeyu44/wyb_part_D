"""memhall.report —— 指标、雷达图、报告、对比。"""

from memhall.report.compare import compare_runs
from memhall.report.metrics import compute_metrics
from memhall.report.radar import render_radar, render_radar_from_metrics
from memhall.report.report import render_report
from memhall.report.stability import analyze_stability

__all__ = ["compute_metrics", "render_radar", "render_radar_from_metrics",
           "render_report", "compare_runs", "analyze_stability"]
