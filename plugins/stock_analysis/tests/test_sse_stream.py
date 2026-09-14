#!/usr/bin/env python3
"""test_sse_stream.py — D1-d SSE 出流模块单元测试（sse_stream.py，契约 §4）。

覆盖：parse_topics（默认/组合/未知/quotes 暂缓）/ event_name（命名帧映射）/
format_frame（id/无 id/Decimal 负载序列化）/ stream_events（首验失败即关、
游标推进、空闲心跳、周期重验失效即关、poll 异常容错续跑）。

运行（需 stock 依赖环境，.stock_deps 在 PYTHONPATH）：
    cd F:\\Sites\\VeroRun
    python -m unittest plugins.stock_analysis.tests.test_sse_stream -v

说明：纯函数测试；stream_events 用注入 poll_fn/auth_check + 伪造时钟推进，不触网不连库。
"""
import json
import os
import sys
import unittest
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')))

import plugins.stock_analysis.sse_stream as ss
from plugins.stock_analysis import sse_stream

_AUTH_OK = {"sub": 1, "is_admin": True}


def _monotonic_counter(step=1.0):
    """逐次自增的 time.monotonic 替身（side_effect 可调用对象）。"""
    state = {"v": 0.0}

    def _next():
        v = state["v"]
        state["v"] += step
        return v

    return _next


class ParseTopicsTest(unittest.TestCase):
    def test_defaults_when_empty(self):
        self.assertEqual(ss.parse_topics(None), (["alerts", "jobs"], None))
        self.assertEqual(ss.parse_topics(""), (["alerts", "jobs"], None))
        self.assertEqual(ss.parse_topics(" , "), (["alerts", "jobs"], None))

    def test_single_and_combo(self):
        self.assertEqual(ss.parse_topics("alerts"), (["alerts"], None))
        self.assertEqual(ss.parse_topics("jobs"), (["jobs"], None))
        topics, err = ss.parse_topics("alerts,jobs")
        self.assertIsNone(err)
        self.assertEqual(sorted(topics), ["alerts", "jobs"])

    def test_trims_case_and_space(self):
        topics, err = ss.parse_topics("  Alerts , JOBS ")
        self.assertIsNone(err)
        self.assertEqual(sorted(topics), ["alerts", "jobs"])

    def test_quotes_deferred(self):
        topics, err = ss.parse_topics("quotes")
        self.assertIsNone(topics)
        self.assertIn("not available yet", err)

    def test_unknown_topic_rejected(self):
        topics, err = ss.parse_topics("bogus")
        self.assertIsNone(topics)
        self.assertIn("bogus", err)


class EventNameTest(unittest.TestCase):
    def test_alerts_maps_to_triggered(self):
        self.assertEqual(ss.event_name("alerts", {}), "alert.triggered")

    def test_jobs_status_mapping(self):
        self.assertEqual(ss.event_name("jobs", {"status": "running"}), "job.running")
        self.assertEqual(ss.event_name("jobs", {"status": "done"}), "job.completed")
        self.assertEqual(ss.event_name("jobs", {"status": "failed"}), "job.failed")

    def test_unknown_status_fallback(self):
        self.assertEqual(ss.event_name("jobs", {"status": "weird"}), "job.status")
        self.assertEqual(ss.event_name("jobs", None), "job.status")


class FormatFrameTest(unittest.TestCase):
    def test_frame_with_id(self):
        frame = ss.format_frame(7, "alert.triggered",
                                {"alert_id": 1, "symbol": "600519"})
        self.assertTrue(frame.startswith("id: 7\nevent: alert.triggered\ndata: "), frame)
        self.assertIn('"symbol": "600519"', frame)
        self.assertTrue(frame.endswith("\n\n"))

    def test_frame_without_id(self):
        frame = ss.format_frame(None, "system.notice", {"code": "AUTH_EXPIRED"})
        self.assertTrue(frame.startswith("event: system.notice\ndata: "), frame)
        self.assertIn("AUTH_EXPIRED", frame)
        self.assertNotIn("id:", frame.splitlines()[0])

    def test_decimal_payload_serialized(self):
        frame = ss.format_frame(1, "job.completed",
                                {"symbol": "600519", "confidence": Decimal("88.50")})
        self.assertIn('"confidence": 88.5', frame)

    def test_utf8_not_escaped(self):
        frame = ss.format_frame(1, "alert.triggered", {"message": "贵州茅台 触发"})
        self.assertIn("贵州茅台", frame)


class StreamEventsTest(unittest.TestCase):
    def _drive(self, gen, limit=20, until=None):
        """消费生成器至多 limit 帧；until 命中即停；close 兜底防泄漏。"""
        frames = []
        try:
            for _ in range(limit):
                frame = next(gen)
                frames.append(frame)
                if until is not None and until(frame):
                    break
        except StopIteration:
            pass
        finally:
            gen.close()
        return frames

    def test_auth_fail_closes_with_notice(self):
        def fake_auth(token):
            return None

        polls = []

        def fake_poll(after_id=None, topics=None, limit=None):
            polls.append(1)
            return []

        gen = sse_stream.stream_events(["alerts"], "3", "tok", fake_auth,
                                       poll_fn=fake_poll)
        frames = list(gen)
        self.assertEqual(len(frames), 1)
        self.assertIn("system.notice", frames[0])
        self.assertIn("AUTH_EXPIRED", frames[0])
        self.assertEqual(polls, [], "auth 失败不得触发轮询")

    def test_delivers_events_and_advances_cursor(self):
        rounds = iter([
            [{"id": 1, "topic": "alerts",
              "payload": {"alert_id": 1, "symbol": "600519"}},
             {"id": 2, "topic": "jobs",
              "payload": {"job_id": "j-1", "symbol": "600519", "status": "running"}}],
            [{"id": 3, "topic": "jobs",
              "payload": {"job_id": "j-1", "symbol": "600519", "status": "done"}}],
            [{"id": 4, "topic": "jobs",
              "payload": {"job_id": "j-1", "symbol": "600519", "status": "failed"}}],
            [],
        ])
        polls = []

        def fake_poll(after_id=None, topics=None, limit=None):
            polls.append(after_id)
            return next(rounds)

        def fake_auth(token):
            return _AUTH_OK

        with mock.patch("time.monotonic", side_effect=_monotonic_counter()), \
             mock.patch("time.sleep", return_value=None):
            gen = sse_stream.stream_events(
                ["alerts", "jobs"], None, "tok", fake_auth, poll_fn=fake_poll,
                poll_interval=1.0, heartbeat_interval=9999, reauth_interval=9999)
            frames = self._drive(gen, until=lambda f: f.startswith("id: 4"))

        self.assertEqual(len(frames), 4)
        self.assertIn("id: 1\nevent: alert.triggered", frames[0])
        self.assertIn('"symbol": "600519"', frames[0])
        self.assertIn("id: 2\nevent: job.running", frames[1])
        self.assertIn("id: 3\nevent: job.completed", frames[2])
        self.assertIn("id: 4\nevent: job.failed", frames[3])
        # 游标逐轮推进：0 → 最后事件 id
        self.assertEqual(polls, [0, 2, 3])

    def test_heartbeat_on_idle(self):
        def fake_poll(after_id=None, topics=None, limit=None):
            return []

        def fake_auth(token):
            return _AUTH_OK

        with mock.patch("time.monotonic", side_effect=_monotonic_counter()), \
             mock.patch("time.sleep", return_value=None):
            gen = sse_stream.stream_events(
                ["alerts"], None, "tok", fake_auth, poll_fn=fake_poll,
                poll_interval=1.0, heartbeat_interval=3, reauth_interval=9999)
            frames = self._drive(gen, limit=3)

        self.assertGreaterEqual(len(frames), 1)
        self.assertTrue(all(f == ss._HEARTBEAT for f in frames), frames)

    def test_reauth_expiry_closes_with_notice(self):
        auth_calls = []

        def fake_auth(token):
            auth_calls.append(token)
            return _AUTH_OK if len(auth_calls) == 1 else None

        def fake_poll(after_id=None, topics=None, limit=None):
            return []

        with mock.patch("time.monotonic", side_effect=_monotonic_counter()), \
             mock.patch("time.sleep", return_value=None):
            gen = sse_stream.stream_events(
                ["jobs"], None, "tok", fake_auth, poll_fn=fake_poll,
                poll_interval=1.0, heartbeat_interval=9999, reauth_interval=2)
            frames = self._drive(gen, limit=10)

        self.assertEqual(len(frames), 1, frames)
        self.assertIn("system.notice", frames[0])
        self.assertIn("AUTH_EXPIRED", frames[0])
        # 初次验权 + reauth_interval 后重验一次
        self.assertEqual(auth_calls, ["tok", "tok"])

    def test_poll_error_keeps_stream_alive(self):
        def fake_poll(after_id=None, topics=None, limit=None):
            raise RuntimeError("db down")

        def fake_auth(token):
            return _AUTH_OK

        with mock.patch("time.monotonic", side_effect=_monotonic_counter()), \
             mock.patch("time.sleep", return_value=None):
            gen = sse_stream.stream_events(
                ["alerts"], None, "tok", fake_auth, poll_fn=fake_poll,
                poll_interval=1.0, heartbeat_interval=1, reauth_interval=9999)
            frames = self._drive(gen, limit=2)

        # poll 抛错被吞，流不中断，随后仍按空闲发心跳
        self.assertGreaterEqual(len(frames), 1)
        self.assertTrue(all(f == ss._HEARTBEAT for f in frames), frames)


if __name__ == "__main__":
    unittest.main()
