"""Authorized, private S2 microphone durability test; receipts contain no speech.

Use --preflight-only while core changes are in progress. Start the full run only
after the coordinating agent has frozen the core source. No legacy app imports.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import sys
import time
import uuid
import wave

APP = Path(__file__).resolve().parents[1]
WORKSPACE = APP.parent
CORE = WORKSPACE / "shared-recording-core"
ASR_HOME = Path(r"E:\tools\audio-transcription\FunASR")
PYTHON = ASR_HOME / ".venv" / "Scripts" / "python.exe"
AUDIO_ROOT = APP / "data" / "recording-tests"
RECEIPTS = WORKSPACE / "planning" / "2026-10-30" / "implementation" / "s2-live"
STOP_MARKER = APP / "data" / "live-recording-test.stop"
TZ = timezone(timedelta(hours=8))
STOP_REQUESTED = False
DRIVER_VERSION = "s2-live-v2"
LONG_CAPTURE_SECONDS = 1802
MINIMUM_ACCEPTED_FRAMES = 1800 * 16000
STOP_POLL_SECONDS = 0.1
sys.path.insert(0, str(CORE))

from shared_recording_core import CaptureFormat, SessionConfig, Supervisor
from shared_recording_core.archive import read_manifest


def now():
    return datetime.now(timezone.utc).isoformat()


def local_now():
    return datetime.now(TZ).isoformat()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(temporary, path)


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def core_hashes():
    return {p.name: sha(p) for p in sorted((CORE / "shared_recording_core").glob("*.py"))}


def safe_snapshot(supervisor):
    value = supervisor.snapshot()
    keys = ("capture_state", "asr_state", "saved_frames", "transcribed_frames", "audio_available",
            "last_saved_at", "started_at", "stopped_at", "paused", "received_device_frames",
            "unsaved_output_frames", "capture_worker_pid", "asr_worker_pid", "level", "cursor",
            "historical_transcribed_frames", "current_run_transcribed_frames", "asr_run_id",
            "checkpoint_run_id", "transcript_is_from_previous_run", "requested_frames",
            "requested_duration_seconds", "duration_target_reached")
    result = {key: value.get(key) for key in keys}
    result["capture_error"] = value.get("capture_error", "")
    result["asr_error_present"] = bool(value.get("asr_error"))
    result["segment_count"] = len(value.get("segments", []))
    result["saved_seconds"] = (value.get("saved_frames") or 0) / 16000
    # Never persist segments, transcript text or arbitrary event payloads here.
    return result


def audio_statistics(path):
    import numpy as np
    count = nonzero = clipped = peak = 0
    total = squared = 0.0
    with wave.open(str(path), "rb") as source:
        fmt = {"sample_rate": source.getframerate(), "channels": source.getnchannels(), "sample_width": source.getsampwidth()}
        declared = source.getnframes()
        while raw := source.readframes(160000):
            values = np.frombuffer(raw, dtype="<i2").astype(np.float64)
            count += len(values)
            nonzero += int(np.count_nonzero(values))
            clipped += int(np.count_nonzero(np.abs(values) >= 32760))
            peak = max(peak, int(np.max(np.abs(values))))
            total += float(values.sum())
            squared += float(np.dot(values, values))
    rms = math.sqrt(squared / count) if count else 0.0
    variance = max(0.0, squared / count - (total / count) ** 2) if count else 0.0
    return {**fmt, "declared_frames": declared, "read_frames": count, "duration_seconds": count / 16000,
            "peak_counts": peak, "rms_counts": rms, "std_counts": math.sqrt(variance),
            "rms_dbfs": 20 * math.log10(rms / 32768) if rms else None,
            "nonzero_samples": nonzero, "clipped_samples": clipped, "sha256": sha(path)}


def device_inventory():
    import sounddevice as sd
    input_device, output_device = map(int, sd.default.device)
    selected = dict(sd.query_devices(1, "input"))
    output = dict(sd.query_devices(output_device, "output")) if output_device >= 0 else None
    if "EDIFIER M30" not in selected["name"] or selected["max_input_channels"] < 1:
        raise RuntimeError("Input index1 is not the authorized EDIFIER M30 microphone")
    if any(word in selected["name"].lower() for word in ("loopback", "stereo mix", "立体声混音")):
        raise RuntimeError("A loopback input cannot replace the authorized microphone")
    return {"default_input": input_device, "selected_input": 1, "input": selected,
            "default_output": output_device, "output": output,
            "capture_route": "physical microphone index1, not loopback"}


def play_marker(inventory):
    """Short, modest-level acoustic marker attempt on the actual default output."""
    import numpy as np
    import sounddevice as sd
    output = inventory["output"]
    if output is None or inventory["default_output"] == 1:
        raise RuntimeError("No separate default output is available for a marker")
    rate = int(output["default_samplerate"])
    channels = min(2, int(output["max_output_channels"]))
    pieces = []
    for frequency in (880, 1320, 880):
        samples = int(rate * 0.12)
        t = np.arange(samples, dtype=np.float32) / rate
        envelope = np.sin(np.linspace(0, np.pi, samples, dtype=np.float32)) ** 2
        pieces.extend([0.05 * np.sin(2 * np.pi * frequency * t) * envelope,
                       np.zeros(int(rate * 0.08), dtype=np.float32)])
    tone = np.concatenate(pieces).astype(np.float32)
    with sd.OutputStream(device=inventory["default_output"], samplerate=rate, channels=channels,
                         dtype="float32", latency="high") as stream:
        stream.write(np.repeat(tone[:, None], channels, axis=1))
    return {"output_device": inventory["default_output"], "sample_rate": rate,
            "frequencies_hz": [880, 1320, 880], "peak_amplitude": 0.05,
            "attempted": True, "captured_in_microphone": "not_confirmed"}


class Driver:
    def __init__(self, preflight_only):
        self.id = "s2live-" + datetime.now(TZ).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        self.folder = RECEIPTS / self.id
        self.folder.mkdir(parents=True, exist_ok=True)
        self.preflight_only = preflight_only
        self.driver_hash = sha(Path(__file__).resolve())
        self.inventory = None
        self.initial_hashes = core_hashes()
        self.progress = {"run_id": self.id, "driver_pid": os.getpid(), "driver_started_at": now(),
                         "driver_version": DRIVER_VERSION, "driver_sha256": self.driver_hash,
                         "planned_long_capture_seconds": LONG_CAPTURE_SECONDS,
                         "minimum_accepted_frames": MINIMUM_ACCEPTED_FRAMES,
                         "driver_started_local": local_now(), "status": "initializing", "preflight_only": preflight_only,
                         "stop_marker": str(STOP_MARKER), "receipt_dir": str(self.folder),
                         "audio_root": str(AUDIO_ROOT / self.id), "speech_quality_evaluated": False,
                         "real_customer_data_authorized": False, "non_private_microphone_test_authorized": True}
        self.fault = {"planned_kill_seconds": 600, "planned_restart_seconds": 900,
                      "minimum_saved_audio_during_outage_seconds": 300, "injection_performed": False}
        self.markers = []
        self.stop_controls = []
        self.supervisor = None
        self.write()

    def write(self, **patch):
        self.progress.update(patch, updated_at=now(), updated_local=local_now())
        atomic_json(self.folder / "status.json", self.progress)
        atomic_json(RECEIPTS / "latest.json", self.progress)
        with (self.folder / "progress.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self.progress, ensure_ascii=False, allow_nan=False) + "\n")
        print(json.dumps({k: self.progress.get(k) for k in ("run_id", "driver_pid", "status", "updated_local", "actual_capture_started_at")}, ensure_ascii=False), flush=True)

    def stopped(self):
        return STOP_REQUESTED or STOP_MARKER.exists()

    def config(self, phase):
        return SessionConfig(app_id="due-diligence", instance_id=self.id, session_id=phase + "-" + uuid.uuid4().hex[:8],
                             output_root=AUDIO_ROOT / self.id, device_id=1, capture_format=CaptureFormat(16000, 1, 2),
                             asr_config={"engine": "disabled" if phase == "preflight" else "local-funasr",
                                         "home": str(ASR_HOME), "punctuate": False})

    async def monitor_stop(self, supervisor, phase):
        """Independent control task: no snapshots, statistics or ASR work."""
        while supervisor.capture_process is not None and supervisor.capture_process.returncode is None:
            if self.stopped():
                detected = time.monotonic()
                item = {"phase": phase, "detected_at": now(),
                        "reason": "signal" if STOP_REQUESTED else "stop_marker",
                        "poll_interval_seconds": STOP_POLL_SECONDS}
                self.stop_controls.append(item)
                # The official public API sends stop before its return snapshot.
                # Driver observations run in threads, so this task need not
                # wait for the driver's in-progress manifest/checkpoint scan.
                try:
                    await supervisor.stop()
                    item.update(stop_api_returned_at=now(), stop_api_elapsed_seconds=time.monotonic() - detected)
                except Exception as exc:
                    item.update(stop_error_type=type(exc).__name__)
                    raise
                finally:
                    atomic_json(self.folder / "stop-control.json", self.stop_controls)
                return
            await asyncio.sleep(STOP_POLL_SECONDS)

    async def finish_monitor(self, task):
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def marker(self, phase, elapsed, planned):
        item = {"phase": phase, "planned_elapsed_seconds": planned, "actual_elapsed_seconds": elapsed, "attempted_at": now()}
        try:
            item.update(await asyncio.to_thread(play_marker, self.inventory))
        except Exception as exc:
            item.update(attempted=False, error_type=type(exc).__name__, captured_in_microphone="not_confirmed")
        self.markers.append(item)
        atomic_json(self.folder / "marker-attempts.json", self.markers)

    async def preflight(self):
        config = self.config("preflight")
        supervisor = Supervisor(PYTHON, ASR_HOME)
        self.supervisor = supervisor
        monitor = None
        try:
            await supervisor.start(config, duration_seconds=5)
            monitor = asyncio.create_task(self.monitor_stop(supervisor, "preflight"))
            self.write(status="preflight_recording", preflight_session_id=config.session_id)
            began = time.monotonic()
            marked = False
            while supervisor.capture_process.returncode is None:
                snap = await asyncio.to_thread(safe_snapshot, supervisor)
                if monitor.done() and not monitor.cancelled() and monitor.exception() is not None:
                    raise monitor.exception()
                if snap["started_at"]:
                    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(snap["started_at"])).total_seconds()
                    if elapsed >= 2 and not marked:
                        marked = True
                        await self.marker("preflight", elapsed, 2)
                if time.monotonic() - began > 25:
                    await supervisor.stop()
                if time.monotonic() - began > 35:
                    raise RuntimeError("Microphone preflight did not stop in its allowed window")
                await asyncio.sleep(0.25)
            await supervisor.wait_capture(10)
            await supervisor.wait_asr(10)
            manifest = await asyncio.to_thread(read_manifest, config, True)
            stats = await asyncio.to_thread(audio_statistics, config.resolve_output("recording.wav"))
            signal_present = stats["nonzero_samples"] > 0 and stats["peak_counts"] >= 8 and stats["std_counts"] >= 1
            passed = (manifest["capture_state"] == "stopped" and not manifest["capture_error"]
                      and stats["read_frames"] == manifest["saved_frames"] and stats["read_frames"] >= 72000
                      and signal_present and not self.stopped())
            receipt = {"phase": "5_second_microphone_preflight", "passed": passed,
                       "core_hashes": self.initial_hashes, "session_id": config.session_id,
                       "audio_path_private": str(config.resolve_output("recording.wav")), "statistics": stats,
                       "capture_state": manifest["capture_state"], "capture_error": manifest["capture_error"],
                       "signal_presence": "detected_content_unverified" if signal_present else "insufficient_or_all_zero",
                       "signal_quality_note": "Nonzero variation is not proof of intelligible speech or confirmed acoustic markers.",
                       "low_level_warning": stats["rms_dbfs"] is None or stats["rms_dbfs"] < -60,
                       "marker_status": "attempted_not_confirmed", "speech_quality_evaluated": False}
            atomic_json(self.folder / "mic-preflight.json", receipt)
            self.write(status="preflight_passed" if passed else "preflight_failed", preflight=receipt)
            return passed
        finally:
            await self.finish_monitor(monitor)
            await supervisor.shutdown(asr_timeout=5)
            self.supervisor = None

    async def long_test(self):
        config = self.config("long")
        supervisor = Supervisor(PYTHON, ASR_HOME)
        self.supervisor = supervisor
        source_hashes = core_hashes()
        if source_hashes != self.initial_hashes or sha(Path(__file__).resolve()) != self.driver_hash:
            raise RuntimeError("Frozen core or driver source changed after the run initialized")
        self.write(status="long_starting", core_hashes=source_hashes, long_session_id=config.session_id)
        capture_clock = None
        last_progress = time.monotonic() - 10
        killed_at = None
        next_marker = 0
        monitor = None
        early_stop_reported = False
        try:
            await supervisor.start(config, duration_seconds=LONG_CAPTURE_SECONDS)
            monitor = asyncio.create_task(self.monitor_stop(supervisor, "long"))
            startup_deadline = time.monotonic() + 35
            while supervisor.capture_process.returncode is None:
                snap = await asyncio.to_thread(safe_snapshot, supervisor)
                if monitor.done() and not monitor.cancelled() and monitor.exception() is not None:
                    raise monitor.exception()
                if capture_clock is None and snap["started_at"]:
                    utc_elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(snap["started_at"])).total_seconds()
                    capture_clock = time.monotonic() - max(0, utc_elapsed)
                    self.write(status="long_recording", actual_capture_started_at=snap["started_at"],
                               actual_capture_started_local=datetime.fromisoformat(snap["started_at"]).astimezone(TZ).isoformat(), snapshot=snap)
                if capture_clock is None and time.monotonic() > startup_deadline:
                    raise RuntimeError("Long microphone capture did not start in 35 seconds")
                elapsed = max(0, time.monotonic() - capture_clock) if capture_clock else 0
                if self.stopped() and not early_stop_reported:
                    early_stop_reported = True
                    self.write(status="early_stop_requested", elapsed_seconds=elapsed, snapshot=snap)
                if not self.stopped() and capture_clock and next_marker < 3 and elapsed >= (2, 900, 1790)[next_marker]:
                    await self.marker("long", elapsed, (2, 900, 1790)[next_marker])
                    next_marker += 1
                if not self.stopped() and capture_clock and elapsed >= 600 and killed_at is None:
                    process = supervisor.asr_process
                    self.fault.update(kill_requested_at=now(), capture_frames_at_kill=snap["saved_frames"],
                                      transcribed_frames_at_kill=snap["transcribed_frames"], asr_pid_before=process.pid if process else None,
                                      injection_performed=bool(process and process.returncode is None))
                    await supervisor.kill_asr()
                    self.fault["kill_completed_at"] = now()
                    outage_start = await asyncio.to_thread(safe_snapshot, supervisor)
                    killed_at = time.monotonic()
                    self.fault.update(outage_start_saved_frames=outage_start["saved_frames"],
                                      outage_start_observed_at=now())
                    self.write(status="long_recording_asr_outage", snapshot=outage_start, fault=self.fault)
                if (not self.stopped() and killed_at is not None and "restart_requested_at" not in self.fault
                        and time.monotonic() - killed_at >= 300
                        and snap["saved_frames"] - self.fault["outage_start_saved_frames"] >= 300 * 16000):
                    self.fault.update(restart_requested_at=now(), capture_frames_at_restart=snap["saved_frames"],
                                      outage_wall_seconds=time.monotonic()-killed_at)
                    try:
                        await supervisor.start_asr()
                        self.fault.update(restart_spawned=True, asr_pid_after=supervisor.asr_process.pid)
                    except Exception as exc:
                        self.fault.update(restart_spawned=False, restart_error_type=type(exc).__name__)
                    self.write(status="long_recording_after_asr_restart", fault=self.fault,
                               snapshot=await asyncio.to_thread(safe_snapshot, supervisor))
                if (self.fault.get("restart_spawned") and snap.get("current_run_transcribed_frames", 0)
                        and snap.get("checkpoint_run_id") == snap.get("asr_run_id")
                        and "restart_progress_first_observed_at" not in self.fault):
                    self.fault.update(restart_progress_first_observed_at=now(),
                                      restart_progress_first_observed_frames=snap["current_run_transcribed_frames"],
                                      restart_run_id=snap.get("asr_run_id"))
                if time.monotonic() - last_progress >= 10:
                    self.write(status=self.progress["status"], elapsed_seconds=elapsed, snapshot=snap, fault=self.fault)
                    last_progress = time.monotonic()
                await asyncio.sleep(0.25)
            await supervisor.wait_capture(15)
            self.write(status="capture_finished_asr_draining", snapshot=await asyncio.to_thread(safe_snapshot, supervisor))
            try:
                await supervisor.wait_asr(60)
            except asyncio.TimeoutError:
                self.fault["asr_drain_timeout"] = True
                await supervisor.kill_asr()
            manifest = await asyncio.to_thread(read_manifest, config, True)
            stats = await asyncio.to_thread(audio_statistics, config.resolve_output("recording.wav"))
            final = await asyncio.to_thread(safe_snapshot, supervisor)
            hash_unchanged = core_hashes() == source_hashes
            driver_unchanged = sha(Path(__file__).resolve()) == self.driver_hash
            archive_integrity = (stats["read_frames"] == manifest["saved_frames"]
                                 and manifest["capture_state"] == "stopped" and not manifest["capture_error"]
                                 and not manifest.get("losses") and hash_unchanged and driver_unchanged)
            technical_pass = (not self.stopped() and stats["read_frames"] >= MINIMUM_ACCEPTED_FRAMES
                              and archive_integrity)
            outage_start_frames = self.fault.get("outage_start_saved_frames")
            outage_frames = (self.fault.get("capture_frames_at_restart", 0) - outage_start_frames
                             if isinstance(outage_start_frames, int) else 0)
            self.fault["outage_saved_frames"] = outage_frames
            isolation_pass = bool(self.fault["injection_performed"] and self.fault.get("outage_wall_seconds", 0) >= 300
                                  and outage_frames >= 300 * 16000 and archive_integrity)
            same_asr_run = bool(final.get("asr_run_id") and final.get("checkpoint_run_id") == final.get("asr_run_id"))
            replay_progress = bool(self.fault.get("restart_spawned") and same_asr_run
                                   and final["transcribed_frames"] > self.fault.get("transcribed_frames_at_kill", 0))
            asr_drained = bool(same_asr_run and final["asr_state"] == "completed"
                               and final["transcribed_frames"] >= manifest["saved_frames"]
                               and not self.fault.get("asr_drain_timeout"))
            automatic_pass = technical_pass and isolation_pass and replay_progress and asr_drained
            receipt = {"run_id": self.id, "finished_at": now(), "finished_local": local_now(),
                       "driver_version": DRIVER_VERSION, "driver_sha256": self.driver_hash,
                       "driver_hash_unchanged": driver_unchanged, "planned_capture_seconds": LONG_CAPTURE_SECONDS,
                       "minimum_accepted_frames": MINIMUM_ACCEPTED_FRAMES,
                       "archive_integrity_passed": archive_integrity, "all_automatic_checks_passed": automatic_pass,
                       "technical_duration_integrity_passed": technical_pass,
                       "asr_fault_capture_isolation_passed": isolation_pass,
                       "asr_replay_progress_observed": replay_progress, "asr_final_current_run_completed": asr_drained,
                       "core_hashes_unchanged": hash_unchanged,
                       "core_hashes": source_hashes, "statistics": stats, "snapshot": final,
                       "verified_chunk_count": len(manifest["chunks"]), "manifest_sha256": sha(config.resolve_output("manifest.json")),
                       "losses": manifest.get("losses", []), "fault": self.fault, "stop_controls": self.stop_controls,
                       "audio_path_private": str(config.resolve_output("recording.wav")),
                       "early_stop": self.stopped(), "marker_attempts": [m for m in self.markers if m["phase"] == "long"],
                       "marker_acceptance": "pending_recorded_marker_review", "normal_speed_relisten": "pending_human_review",
                       "speech_tuple_quality": "not_evaluated_no_recorded_100_tuple_reference",
                       "note": "Duration/hash verification does not prove speech intelligibility, marker audibility or ASR accuracy."}
            atomic_json(self.folder / "live-recording-result.json", receipt)
            self.write(status="completed_automatic_checks" if automatic_pass else "completed_with_unpassed_checks", result=receipt)
            return automatic_pass
        finally:
            await self.finish_monitor(monitor)
            await supervisor.shutdown(asr_timeout=10)
            self.supervisor = None

    async def run(self):
        try:
            if self.stopped():
                self.write(status="not_started_stop_marker_present")
                return 2
            self.inventory = await asyncio.to_thread(device_inventory)
            atomic_json(self.folder / "devices.json", self.inventory)
            if not await self.preflight():
                return 3
            if self.preflight_only:
                self.write(status="preflight_only_complete_long_not_started")
                return 0
            return 0 if await self.long_test() else 4
        except Exception as exc:
            self.write(status="driver_failed", error_type=type(exc).__name__, error_message=str(exc)[:240])
            return 1
        finally:
            if self.supervisor is not None:
                await self.supervisor.shutdown(asr_timeout=10)


def request_stop(*_args):
    global STOP_REQUESTED
    STOP_REQUESTED = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    return asyncio.run(Driver(args.preflight_only).run())


if __name__ == "__main__":
    raise SystemExit(main())
