"""Behavioral trajectory analysis + risk scoring"""
import math
from typing import List, Optional, NamedTuple

# Simple named tuple to replace Pydantic TracePoint
class TracePoint(NamedTuple):
    t: int
    x: int
    y: int


def analyze_trajectory(trace: List[TracePoint], expected_distance: int) -> dict:
    """Analyze drag trajectory for human-like patterns.

    Returns dict with {human_score (0-1), risk_level (low/medium/high), details}
    """
    if not trace or len(trace) < 3:
        return {"human_score": 0.0, "risk_level": "high",
                "detail": "insufficient_data"}

    n = len(trace)
    pts = [(p.t, p.x, p.y) for p in trace]

    # ── 1. Duration ──
    duration_ms = pts[-1][0] - pts[0][0]
    duration_ok = 300 < duration_ms < 8000

    # ── 2. Speed profile ──
    speeds = []
    for i in range(1, n):
        dt = (pts[i][0] - pts[i-1][0]) / 1000.0
        dx = pts[i][1] - pts[i-1][1]
        dy = pts[i][2] - pts[i-1][2]
        if dt > 0.001:
            speeds.append(math.sqrt(dx*dx + dy*dy) / dt)

    if len(speeds) < 2:
        return {"human_score": 0.0, "risk_level": "high",
                "detail": "too_few_speed_samples"}

    avg_speed = sum(speeds) / len(speeds)
    speed_var = sum((s - avg_speed)**2 for s in speeds) / len(speeds)

    # ── 3. Acceleration ──
    accels = []
    for i in range(1, len(speeds)):
        dt = (pts[i+1][0] - pts[i][0]) / 1000.0
        if dt > 0.001:
            accels.append((speeds[i] - speeds[i-1]) / dt)

    accel_var = 0.0
    if len(accels) >= 2:
        avg_a = sum(accels) / len(accels)
        accel_var = sum((a - avg_a)**2 for a in accels) / len(accels)

    # ── 4. Y-axis wobble ──
    y_vals = [p[2] for p in pts]
    y_mean = sum(y_vals) / len(y_vals)
    y_var = sum((y - y_mean)**2 for y in y_vals) / len(y_vals)

    # ── 5. Backtracking ──
    backtracks = 0
    for i in range(2, n):
        if pts[i][1] < pts[i-1][1]:
            backtracks += 1

    # ── 6. Pauses ──
    pauses = 0
    total_pause_ms = 0
    for i in range(1, n):
        dt = pts[i][0] - pts[i-1][0]
        dx = abs(pts[i][1] - pts[i-1][1])
        if dt > 150 and dx < 3:
            pauses += 1
            total_pause_ms += dt

    # ── 7. Linearity ──
    x_deltas = [pts[i][1] - pts[i-1][1] for i in range(1, n)]
    dx_var = 0.0
    if len(x_deltas) >= 2:
        dx_mean = sum(x_deltas) / len(x_deltas)
        dx_var = sum((d - dx_mean)**2 for d in x_deltas) / len(x_deltas)

    # ── 8. Timing uniformity （源自系统 B：检测采样间隔是否完全一致）──
    time_deltas = [pts[i][0] - pts[i-1][0] for i in range(1, n)]
    td_mean = sum(time_deltas) / len(time_deltas) if len(time_deltas) > 0 else 1
    if len(time_deltas) >= 3:
        td_variance = sum((td - td_mean)**2 for td in time_deltas) / len(time_deltas)
        td_std = math.sqrt(td_variance)
        timing_cv = td_std / max(td_mean, 1.0)
    else:
        timing_cv = 0.0

    # ── Scoring ──
    scores = []

    # Duration: too fast or too slow
    scores.append(1.0 if duration_ok else 0.3)

    # Speed variance: humans vary speed
    sv_score = min(1.0, speed_var / 50.0) if speed_var > 1 else 0.2
    scores.append(sv_score)

    # Y wobble: some vertical movement is human
    # HARD penalty: zero vertical movement = almost certain bot （源自系统 B）
    if y_var < 0.05:
        y_score = 0.0
    else:
        y_score = min(1.0, y_var / 5.0) if y_var > 0.1 else 0.1
    scores.append(y_score)

    # Backtracks: small corrections are human
    bt_score = min(1.0, backtracks / 3.0) if backtracks > 0 else 0.6
    scores.append(bt_score)

    # Pauses: at least one is human-like
    # 拒绝停顿占比超过 70% 的轨迹（源自系统 B）
    duration_ratio = total_pause_ms / max(duration_ms, 1)
    if duration_ratio > 0.7:
        pause_score = 0.5 * 0.5  # 基础分数 × 过度停顿惩罚
    elif 1 <= pauses <= 5:
        pause_score = 0.8
    elif pauses == 0:
        pause_score = 0.4
    else:
        pause_score = 0.5
    scores.append(pause_score)

    # Linearity: too consistent = bot
    lin_score = 0.3 if dx_var < 20 else (0.7 if dx_var < 100 else 1.0)
    scores.append(lin_score)

    # Timing uniformity: perfectly uniform intervals = bot （源自系统 B）
    if timing_cv < 0.02:
        timing_score = 0.0  # essentially perfect timing = bot
    elif timing_cv < 0.15:
        timing_score = timing_cv / 0.15 * 0.5
    else:
        timing_score = 1.0
    scores.append(timing_score)

    human_score = sum(scores) / len(scores)

    risk = "low" if human_score >= 0.65 else ("medium" if human_score >= 0.4 else "high")

    return {
        "human_score": round(human_score, 4),
        "risk_level": risk,
        "details": {
            "duration_ms": duration_ms,
            "avg_speed": round(avg_speed, 1),
            "speed_var": round(speed_var, 1),
            "y_var": round(y_var, 2),
            "backtracks": backtracks,
            "pauses": pauses,
            "pause_ratio": round(duration_ratio, 3),
            "dx_var": round(dx_var, 1),
            "timing_cv": round(timing_cv, 4),
            "timing_score": round(timing_score, 4),
        }
    }


def compute_risk(position_match: bool, behavior: dict) -> float:
    """Compute final risk score with weighted model （源自系统 B 加权体系）.

    Position weight: 0.30  |  Trajectory weight: 0.35  |  Behavior weight: 0.35
    Hard gate: position must match AND behavior_score >= 0.45.
    """
    if not position_match:
        return 0.0

    pos_score = 1.0

    # Trajectory sub-score: combine speed variance + linearity + backtrack + timing
    details = behavior.get("details", {})
    speed_var = details.get("speed_var", 0)
    sv_sub = min(1.0, speed_var / 50.0) if speed_var > 1 else 0.2
    dx_var = details.get("dx_var", 0)
    lin_sub = 0.3 if dx_var < 20 else (0.7 if dx_var < 100 else 1.0)
    backtracks = details.get("backtracks", 0)
    bt_sub = min(1.0, backtracks / 3.0) if backtracks > 0 else 0.6
    timing_score = details.get("timing_score", 0.6)
    trajectory_score = sv_sub * 0.35 + lin_sub * 0.30 + bt_sub * 0.20 + timing_score * 0.15

    # Behavior sub-score: duration + pauses + wobble
    duration_ms = details.get("duration_ms", 1000)
    dur_sub = 1.0 if 300 < duration_ms < 8000 else 0.3
    pauses = details.get("pauses", 0)
    pause_ratio = details.get("pause_ratio", 0)
    if pause_ratio > 0.7:
        pause_sub = 0.25
    elif 1 <= pauses <= 5:
        pause_sub = 0.8
    elif pauses == 0:
        pause_sub = 0.4
    else:
        pause_sub = 0.5
    y_var = details.get("y_var", 0)
    if y_var < 0.05:
        y_sub = 0.0
    else:
        y_sub = min(1.0, y_var / 5.0) if y_var > 0.1 else 0.1
    behavior_score = dur_sub * 0.30 + pause_sub * 0.30 + y_sub * 0.40

    # Hard gate: must have meaningful behavioral signal （源自系统 B）
    if behavior_score < 0.45:
        return round(0.30 * pos_score + 0.35 * trajectory_score + 0.35 * behavior_score, 4)

    final = 0.30 * pos_score + 0.35 * trajectory_score + 0.35 * behavior_score
    return round(final, 4)
