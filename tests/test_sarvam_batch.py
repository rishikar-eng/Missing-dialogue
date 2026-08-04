"""Unit tests for the Sarvam Batch-API adapter (`audio_qc._sarvam_batch`).

The one thing that MUST be exact here is the timestamp back-mapping: the batch path uploads
the `_prep_chunks` concatenation (speech windows joined with 0.5 s gaps), so Saaras returns
times on the CONCATENATED timeline and every segment must come back through the piecewise
map onto the real episode timeline. An off-by-one-window bug would silently place every
flag on the wrong line — no crash, just wrong QC. So the whole job flow is exercised
against a stubbed API and the returned times are checked against hand-computed values.

    python -m tests.test_sarvam_batch
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from backend import audio_qc  # noqa: E402

# Fixed VAD windows (start, end) on a 100 s file. With PAD=0.2 / GAP=0.5 in _prep_chunks
# the concatenated timeline is, window by window (ps = s-0.2, pe = e+0.2):
#   w0: real 9.8..12.2  -> concat 0.0..2.4   (then 0.5 s gap)
#   w1: real 29.8..33.2 -> concat 2.9..6.3   (then 0.5 s gap)
#   w2: real 59.8..65.2 -> concat 6.8..12.2
WINS = [(10.0, 12.0), (30.0, 33.0), (60.0, 65.0)]


class _Resp:
    def __init__(self, status_code=200, body=None, content=b""):
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body)[:300]

    def json(self):
        return self._body


class _FakeHttpx:
    """Just enough of httpx for _sarvam_req: request() routed per (method, url)."""

    class RequestError(Exception):
        pass

    def __init__(self, result_json, fail_first_init=0):
        self.calls: list[tuple[str, str]] = []
        self.uploaded: bytes | None = None
        self.upload_headers: dict | None = None
        self.result_json = result_json
        self._init_fails = fail_first_init
        self._polls = 0

    def request(self, method, url, timeout=None, **kw):
        self.calls.append((method, url.split("?")[0]))
        base = audio_qc._SARVAM_BASE
        if method == "POST" and url == base:
            if self._init_fails > 0:
                self._init_fails -= 1
                return _Resp(500, {"error": "boom"})
            assert kw["json"]["job_parameters"]["with_timestamps"] is True
            # Measured, not documented: without diarization the batch output is 2
            # mega-chunks per file — sentence-level timestamps REQUIRE this flag.
            assert kw["json"]["job_parameters"]["with_diarization"] is True
            return _Resp(202, {"job_id": "job_test", "job_state": "Accepted"})
        if method == "POST" and url.endswith("/upload-files"):
            assert kw["json"] == {"job_id": "job_test", "files": ["0.flac"]}
            return _Resp(200, {"upload_urls": {"0.flac": {
                "file_url": "https://blob.example/in/0.flac?sas=1"}}})
        if method == "PUT":
            self.uploaded = kw.get("content")
            self.upload_headers = kw.get("headers")
            return _Resp(201, {})
        if method == "POST" and url.endswith("/start"):
            return _Resp(200, {"job_state": "Running"})
        if method == "GET" and url.endswith("/status"):
            self._polls += 1
            if self._polls < 2:
                return _Resp(200, {"job_state": "Running"})
            return _Resp(200, {"job_state": "Completed", "job_details": [
                {"state": "Success",
                 "inputs": [{"file_name": "0.flac"}],
                 "outputs": [{"file_name": "0.json"}]}]})
        if method == "POST" and url.endswith("/download-files"):
            assert kw["json"] == {"job_id": "job_test", "files": ["0.json"]}
            return _Resp(200, {"download_urls": {"0.json": {
                "file_url": "https://blob.example/out/0.json?sas=2"}}})
        if method == "GET" and url.startswith("https://blob.example/out/"):
            return _Resp(200, self.result_json)
        raise AssertionError(f"unexpected call {method} {url}")


def _run_batch(fake):
    """Run _sarvam_batch against the fake with fixed windows and no real sleeps."""
    from contextlib import ExitStack
    audio = np.zeros(int(100 * 16000), dtype="float32")
    with ExitStack() as st:
        st.enter_context(mock.patch.object(audio_qc, "segment", lambda a: list(WINS)))
        st.enter_context(mock.patch.dict(sys.modules, {"httpx": fake}))
        st.enter_context(mock.patch("time.sleep", lambda s: None))
        st.enter_context(mock.patch.dict(os.environ, {"SARVAM_API_KEY": "k"}))
        return audio_qc._sarvam_batch(audio)


class SarvamBatchTest(unittest.TestCase):
    def test_backmap_and_flow(self):
        fake = _FakeHttpx({
            "transcript": "a b c",
            "timestamps": {
                # concat-time chunks, hand-mapped in the header comment above:
                #   0.2..2.0  inside w0            -> real 10.0..11.8
                #   3.0..6.0  inside w1            -> real 29.9..32.9
                #   6.5..11.0 starts in w1's GAP   -> start clamps to w1's edge 33.2,
                #                                     end lands inside w2 at 64.0
                # The REAL API puts the text array under "words" (docs say "chunks").
                "words": ["नमस्ते दुनिया", "दूसरी पंक्ति", "तीसरी पंक्ति"],
                "start_time_seconds": [0.2, 3.0, 6.5],
                "end_time_seconds": [2.0, 6.0, 11.0],
            },
            "language_code": "hi-IN",
        })
        out = _run_batch(fake)
        self.assertEqual(len(out), 3)
        (s0, e0, t0, q0), (s1, e1, _, _), (s2, e2, _, _) = out
        self.assertAlmostEqual(s0, 10.0, places=2)
        self.assertAlmostEqual(e0, 11.8, places=2)
        self.assertAlmostEqual(s1, 29.9, places=2)
        self.assertAlmostEqual(e1, 32.9, places=2)
        self.assertAlmostEqual(s2, 33.2, places=2)  # gap start clamped to window edge
        self.assertAlmostEqual(e2, 64.0, places=2)
        self.assertEqual(t0, "नमस्ते दुनिया")
        # Synthesized quality signals, same contract as the sync path.
        self.assertEqual(q0["nsp"], 0.0)
        self.assertEqual(q0["alp"], 0.0)
        self.assertGreater(q0["cr"], 0.0)
        # The full job lifecycle ran, in order, exactly once each (bar polling).
        kinds = [u.rsplit("/", 1)[-1] if u != audio_qc._SARVAM_BASE else "init"
                 for _, u in fake.calls]
        self.assertEqual([k for k in kinds if k != "status"],
                         ["init", "upload-files", "0.flac", "start",
                          "download-files", "0.json"])
        # Azure Block Blob presigned PUT contract (what the official SDK sends).
        self.assertEqual(fake.upload_headers["x-ms-blob-type"], "BlockBlob")
        self.assertTrue(fake.uploaded.startswith(b"fLaC"))

    def test_retry_then_success(self):
        fake = _FakeHttpx({"transcript": "x",
                           "timestamps": {"chunks": ["ठीक है"],  # documented key variant
                                          "start_time_seconds": [0.5],
                                          "end_time_seconds": [2.0]}},
                          fail_first_init=2)  # two 500s, third attempt succeeds
        out = _run_batch(fake)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0][0], 10.3, places=2)

    def test_missing_timestamps_is_unusable(self):
        # A transcript with no timestamps cannot be mapped -> None (caller falls to sync).
        fake = _FakeHttpx({"transcript": "कुछ बोला गया", "timestamps": None})
        self.assertIsNone(_run_batch(fake))

    def test_silent_side_is_empty_not_fallback(self):
        fake = _FakeHttpx({"transcript": "", "timestamps": None})
        self.assertEqual(_run_batch(fake), [])

    def test_no_key_returns_none(self):
        fake = _FakeHttpx({})
        audio = np.zeros(16000, dtype="float32")
        with mock.patch.dict(os.environ, {"SARVAM_API_KEY": ""}), \
                mock.patch.dict(sys.modules, {"httpx": fake}):
            self.assertIsNone(audio_qc._sarvam_batch(audio))
        self.assertEqual(fake.calls, [])

    def test_transcribe_sarvam_falls_back_to_sync_on_batch_error(self):
        """AQC_SARVAM_BATCH=1 + a dead batch API must degrade to the sync path, not fail."""
        calls = []

        def _fake_sync(a, s, e):
            calls.append((s, e))
            return (round(s, 2), round(e, 2), "line", {"nsp": 0.0, "alp": 0.0, "cr": 1.0})

        class _DeadHttpx(_FakeHttpx):
            def request(self, method, url, timeout=None, **kw):
                return _Resp(500, {"error": "down"})

        audio = np.zeros(int(100 * 16000), dtype="float32")
        with mock.patch.dict(os.environ,
                             {"SARVAM_API_KEY": "k", "AQC_SARVAM_BATCH": "1"}), \
                mock.patch.object(audio_qc, "segment", lambda a: list(WINS)), \
                mock.patch.object(audio_qc, "_sarvam_call", _fake_sync), \
                mock.patch.dict(sys.modules, {"httpx": _DeadHttpx({})}), \
                mock.patch("time.sleep", lambda s: None):
            out = audio_qc.transcribe_sarvam(audio)
        self.assertEqual(len(out), 3)                    # sync answered
        self.assertEqual(sorted(calls), sorted([(s, e) for s, e in WINS]))

    def test_batch_result_is_cached_for_later_draws(self):
        fake = _FakeHttpx({"transcript": "x",
                           "timestamps": {"chunks": ["पहली बात"],
                                          "start_time_seconds": [0.5],
                                          "end_time_seconds": [2.0]}})
        audio = np.zeros(int(100 * 16000), dtype="float32")
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "in.wav.sarvam.json")
            with mock.patch.dict(os.environ,
                                 {"SARVAM_API_KEY": "k", "AQC_SARVAM_BATCH": "1"}), \
                    mock.patch.object(audio_qc, "segment", lambda a: list(WINS)), \
                    mock.patch.dict(sys.modules, {"httpx": fake}), \
                    mock.patch("time.sleep", lambda s: None):
                out1 = audio_qc.transcribe_sarvam(audio, cache)
                n_calls = len(fake.calls)
                out2 = audio_qc.transcribe_sarvam(audio, cache)   # must hit the cache
            self.assertEqual([tuple(x) for x in out1], [tuple(x) for x in out2])
            self.assertEqual(len(fake.calls), n_calls)


if __name__ == "__main__":
    unittest.main()
