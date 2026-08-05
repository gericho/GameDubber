from __future__ import annotations

import hashlib
import hashlib
import shutil
import subprocess
import sqlite3
import struct
import tkinter as tk
import ctypes
import json
import math
import mmap
import random
import queue
import secrets
import sys
import threading
import wave
import winreg
import zlib
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

SOURCE_ROOT = Path(__file__).resolve().parent
APP_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else SOURCE_ROOT
WORK_ROOT = APP_ROOT / "work" if getattr(sys, "frozen", False) else SOURCE_ROOT / "work"
APP_VERSION = "ALPHA 0.1.68"
BUILD_TIMESTAMP = "2026-08-05 23:22:01"

class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]


def filetime_value(value: FILETIME) -> int:
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

HIDDEN_PROCESS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PIPELINE_STAGES = (
    "Discover dialogue sources",
    "Build target dialogue text index",
    "Build English voice manifest",
    "Build dialogue text index",
    "Build xTranslator voice map",
    "Prepare and decode validation sample",
    "Validate mapping with CUDA ASR",
    "Preflight full voice-over batch",
    "Extract English references",
    "Decode English reference WAVs",
    "Generate target-language WAVs with VoxCPM2",
)


def enable_high_dpi() -> None:
    """Enable per-monitor DPI awareness before Tk creates any windows."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()


class GameDubberApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"GameDubber {APP_VERSION} by Gericho — build {BUILD_TIMESTAMP}")
        self.geometry("1575x990")
        self._refresh_progress_bars()
        window_width = self.winfo_width()
        window_height = self.winfo_height()
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        position_x = max(0, (screen_width - window_width) // 2)
        position_y = max(0, (screen_height - window_height) // 2)
        self.geometry(f"{window_width}x{window_height}+{position_x}+{position_y}")
        self.minsize(1425, 840)
        self.game_path = tk.StringVar()
        self.target_language = tk.StringVar()
        self.gpu_status = tk.StringVar(value="GPU: Checking CUDA availability...")
        self.vram_usage_status = tk.StringVar(value="VRAM usage 0%")
        self.gpu_usage_status = tk.StringVar(value="GPU usage 0%")
        self.ram_usage_status = tk.StringVar(value="RAM usage: checking...")
        self.disk_status = tk.StringVar(value="Disk space: Select a game folder")
        self.overall_status = tk.StringVar(value="Overall: Ready")
        self.step_status = tk.StringVar(value="Current task: No task running")
        self.step_percent = tk.StringVar(value="0.00%")
        self.cpu_status = tk.StringVar(value=f"CPU usage 0% - {self._cpu_model()} | RAM: checking...")
        self.current_line = tk.StringVar(value="Current dialogue: —")
        self.preview_wav_playback_enabled = tk.BooleanVar(value=False)
        self._cpu_times: tuple[int, int] | None = None
        self._gpu_name: str | None = None
        self._gpu_total_mb = 0
        self._gpu_used_mb = 0
        self._gpu_usage_percent = 0
        self._gpu_usage_updates: queue.Queue[int] = queue.Queue(maxsize=1)
        self._gpu_usage_process: subprocess.Popen[str] | None = None
        self._build()
        self._detect_gpu()
        self._update_gpu_usage()
        self._update_cpu_bar()
        self._refresh_disk_status()
        self.after(3000, self._schedule_cpu_bar)
        self.after(5000, self._schedule_disk_status)

    def _refresh_progress_bars(self) -> None:
        """Flush pending layout and paint work for both determinate pipeline bars."""
        if hasattr(self, "step") and hasattr(self, "step_percent"):
            try:
                maximum = float(self.step.cget("maximum"))
                value = float(self.step.cget("value"))
                percent = 0.0 if maximum <= 0 else max(0.0, min(100.0, value * 100.0 / maximum))
                self.step_percent.set(f"{percent:.2f}%")
            except (tk.TclError, TypeError, ValueError):
                self.step_percent.set("0.00%")
        tk.Tk.update_idletasks(self)
        if hasattr(self, "overall"):
            self.overall.update_idletasks()
        if hasattr(self, "step"):
            self.step.update_idletasks()
        tk.Tk.update_idletasks(self)
    def _build(self) -> None:
        panel = ttk.Frame(self, padding=14); panel.grid(sticky="nsew")
        self.columnconfigure(0, weight=1); self.rowconfigure(0, weight=1)
        # The terminal has a deliberate fixed footprint of roughly ten text
        # rows.  The validation table receives all remaining vertical space.
        panel.columnconfigure(1, weight=1); panel.rowconfigure(17, weight=0, minsize=180); panel.rowconfigure(18, weight=1, minsize=260)
        folder_row = ttk.Frame(panel)
        folder_row.grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(folder_row, text="Game folder").grid(row=0, column=0, sticky="w")
        ttk.Entry(folder_row, textvariable=self.game_path, width=68).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Button(folder_row, text="Browse...", command=self._choose_folder).grid(row=0, column=2, sticky="w")
        gpu_usage_row = ttk.Frame(panel)
        gpu_usage_row.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 4))
        gpu_usage_row.columnconfigure(0, weight=1)
        ttk.Label(gpu_usage_row, textvariable=self.gpu_usage_status).grid(row=0, column=0, sticky="w")
        self.gpu_usage_bar = tk.Canvas(gpu_usage_row, height=4, highlightthickness=0, bg="#d9d9d9")
        self.gpu_usage_bar.grid(row=1, column=0, sticky="ew")
        ttk.Label(panel, textvariable=self.vram_usage_status).grid(row=3, column=0, columnspan=3, sticky="w")
        self.vram_bar = tk.Canvas(panel, height=4, highlightthickness=0, bg="#d9d9d9")
        self.vram_bar.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(0, 4))
        ttk.Label(panel, textvariable=self.cpu_status).grid(row=5, column=0, columnspan=3, sticky="w")
        self.cpu_bar = tk.Canvas(panel, height=4, highlightthickness=0, bg="#d9d9d9")
        self.cpu_bar.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 4))
        ttk.Label(panel, textvariable=self.ram_usage_status).grid(row=7, column=0, columnspan=3, sticky="w")
        self.ram_bar = tk.Canvas(panel, height=4, highlightthickness=0, bg="#d9d9d9")
        self.ram_bar.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(0, 4))
        ttk.Label(panel,textvariable=self.disk_status).grid(row=9,column=0,columnspan=2,sticky="w")
        ttk.Separator(panel).grid(row=10,column=0,columnspan=3,sticky="ew",pady=8)
        self.overall_label = ttk.Label(panel, textvariable=self.overall_status, anchor="w")
        self.overall_label.grid(row=11, column=0, columnspan=3, sticky="ew")
        self.overall=ttk.Progressbar(panel,maximum=7,value=0); self.overall.grid(row=12,column=0,columnspan=3,sticky="ew",pady=(2,7))
        self.step_label = ttk.Label(panel, textvariable=self.step_status, anchor="w")
        self.step_label.grid(row=13, column=0, columnspan=3, sticky="ew")
        self.step=ttk.Progressbar(panel,maximum=20,value=0); self.step.grid(row=14,column=0,columnspan=2,sticky="ew",pady=(2,7))
        ttk.Label(panel, textvariable=self.step_percent, width=8, anchor="e").grid(row=14, column=2, sticky="e", padx=(8, 0), pady=(2, 7))
        ttk.Label(panel, textvariable=self.current_line, justify="left", anchor="w").grid(row=15,column=0,columnspan=3,sticky="ew")
        ttk.Checkbutton(panel, text="Live preview target-language WAV (slows batch)", variable=self.preview_wav_playback_enabled).grid(row=16,column=0,columnspan=3,sticky="w",pady=(4,0))
        terminal_font = "Cascadia Code Light"
        self.log=tk.Text(panel,bg="black",fg="#ffd400",font=(terminal_font,8),wrap="word",state="disabled",height=10)
        self.log.tag_configure("english_dialogue", foreground="#a9a9a9")
        self.log.tag_configure("target_dialogue", foreground="#ffffff")
        self.log.tag_configure("asr_pass", foreground="#3dff6b")
        self.log.tag_configure("asr_fail", foreground="#ff4d4d")
        self.log.grid(row=17,column=0,columnspan=3,sticky="nsew",pady=(8,4))
        self.validation_report = ttk.LabelFrame(panel, text="Real-time validation report", padding=(6, 4))
        self.validation_report.grid(row=18, column=0, columnspan=3, sticky="nsew", pady=(0, 8))
        self.validation_report.columnconfigure(0, weight=1); self.validation_report.rowconfigure(1, weight=1)
        self.review_only_unvalidated = tk.BooleanVar(value=False)
        self.review_status = tk.StringVar(value="Report: no production WEMs available yet")
        report_toolbar = ttk.Frame(self.validation_report)
        report_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        report_toolbar.columnconfigure(11, weight=1)
        ttk.Checkbutton(report_toolbar, text="Only not validated", variable=self.review_only_unvalidated).grid(row=0, column=0, sticky="w")
        self.review_track_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(report_toolbar, text="Track", variable=self.review_track_enabled).grid(row=0, column=1, padx=(12, 8))
        self.review_search_text = tk.StringVar()
        ttk.Label(report_toolbar, text="Search:").grid(row=0, column=2, sticky="e")
        self.review_search_entry = ttk.Entry(report_toolbar, textvariable=self.review_search_text, width=28)
        self.review_search_entry.grid(row=0, column=3, sticky="w", padx=(4, 1))
        ttk.Button(report_toolbar, text="×", width=3, command=lambda: self.review_search_text.set("")).grid(row=0, column=4, padx=(0, 8))
        self.review_previous_button = ttk.Button(report_toolbar, text="◀", width=3, command=lambda: getattr(self, "_change_review_page", lambda _delta: None)(-1))
        self.review_previous_button.grid(row=0, column=5, padx=(0, 2))
        self.review_next_button = ttk.Button(report_toolbar, text="▶", width=3, command=lambda: getattr(self, "_change_review_page", lambda _delta: None)(1))
        self.review_next_button.grid(row=0, column=6, padx=(0, 4))
        self.review_jump_previous_button = ttk.Button(report_toolbar, text="-5000", width=6, command=lambda: getattr(self, "_change_review_page", lambda _delta: None)(-5))
        self.review_jump_previous_button.grid(row=0, column=7, padx=(0, 2))
        self.review_jump_next_button = ttk.Button(report_toolbar, text="+5000", width=6, command=lambda: getattr(self, "_change_review_page", lambda _delta: None)(5))
        self.review_jump_next_button.grid(row=0, column=8, padx=(0, 10))
        self.review_undo_button = ttk.Button(report_toolbar, text="Undo", width=5, padding=(2, 0), command=lambda: getattr(self, "_undo_review_override", lambda: None)(), state="disabled")
        self.review_undo_button.grid(row=0, column=9, padx=(0, 2))
        self.review_redo_button = ttk.Button(report_toolbar, text="Redo", width=5, padding=(2, 0), command=lambda: getattr(self, "_redo_review_override", lambda: None)(), state="disabled")
        self.review_redo_button.grid(row=0, column=10, padx=(0, 8))
        ttk.Label(report_toolbar, textvariable=self.review_status, anchor="e").grid(row=0, column=11, sticky="ew")
        report_columns = ("number", "subtitle", "validation", "attempts", "duration")
        self.review_tree = ttk.Treeview(self.validation_report, columns=report_columns, show="headings", selectmode="browse", height=14)
        self.review_tree.heading("number", text="#"); self.review_tree.heading("subtitle", text="Target dialogue"); self.review_tree.heading("validation", text="Validation"); self.review_tree.heading("attempts", text="Attempts"); self.review_tree.heading("duration", text="Duration")
        self.review_tree.column("number", width=72, minwidth=60, anchor="e", stretch=False)
        self.review_tree.column("subtitle", width=825, minwidth=260, anchor="w")
        self.review_tree.column("validation", width=130, minwidth=110, anchor="center", stretch=False)
        self.review_tree.column("attempts", width=80, minwidth=70, anchor="center", stretch=False)
        self.review_tree.column("duration", width=84, minwidth=78, anchor="e", stretch=False)
        self.review_tree.tag_configure("available", foreground="#808080")
        self.review_tree.tag_configure("deferred", foreground="#c6a100")
        self.review_tree.tag_configure("validated", foreground="#179b3a")
        self.review_tree.tag_configure("not_validated", foreground="#d32121")
        review_scroll = ttk.Scrollbar(self.validation_report, orient="vertical", command=self.review_tree.yview)
        self.review_tree.configure(yscrollcommand=review_scroll.set)
        self.review_tree.grid(row=1, column=0, sticky="nsew")
        review_scroll.grid(row=1, column=1, sticky="ns")
        self.action_button=ttk.Button(panel,text="Start discovery",command=self._discover_mapping)
        self.action_button.grid(row=20,column=1,columnspan=2,sticky="ew",padx=(8,0))
        self.overall_status.set("Overall: Ready to discover Starfield sources")
        self.step_status.set("Current task: Select the Starfield folder, then start discovery")
        self.current_line.set("Current dialogue: No source files have been processed.")
        self._append_log("> Clean start. Game files will be read only; pipeline data is written under work.\n")

    def _append_log(self, message: str, tag: str | None = None) -> None:
        line = message + ("" if message.endswith("\n") else "\n")
        self.log.configure(state="normal")
        self.log.insert("end", line, tag) if tag else self.log.insert("end", line)
        # The production batch can emit several technical events per line.
        # Keep the on-screen terminal responsive while the file log remains
        # complete and chronological for later inspection.
        visible_lines = int(self.log.index("end-1c").split(".", 1)[0])
        if visible_lines > 1800:
            self.log.delete("1.0", f"{visible_lines - 1500}.0")
        self.log.see("end")
        self.log.configure(state="disabled")
        log_path = WORK_ROOT / "logs" / "gamedubber.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {line}")
    def _stage_overall(self, stage: int, detail: str) -> str:
        return f"Overall: Step {stage} of {len(PIPELINE_STAGES)} — {detail}"

    def _stage_task(self, stage: int, detail: str) -> str:
        return f"Current task: {detail}"

    def _reader_path(self) -> Path:
        return Path(getattr(sys, "_MEIPASS", APP_ROOT)) / "archive_reader.exe" if getattr(sys, "frozen", False) else SOURCE_ROOT / "archive_reader" / "target" / "release" / "archive_reader.exe"

    def _decoder_path(self) -> Path:
        root = Path(getattr(sys, "_MEIPASS", APP_ROOT)) if getattr(sys, "frozen", False) else SOURCE_ROOT / "tools" / "vgmstream"
        return root / "vgmstream-cli.exe"

    def _decode_audio_test(self) -> None:
        decoder = self._decoder_path()
        jobs_path = WORK_ROOT / "manifests" / "audio_test_jobs.jsonl"
        if not decoder.is_file() or not jobs_path.is_file():
            messagebox.showerror("Decoder unavailable", "The bundled WEM decoder or audio test jobs are unavailable.")
            return
        jobs = [json.loads(line) for line in jobs_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.overall.configure(value=6)
        self.overall_status.set(self._stage_overall(6, "Decode 20-line audio test"))
        self.step.configure(maximum=max(1, len(jobs)), value=0)
        self._append_log(f"> Decoding {len(jobs)} WEM samples to WAV one at a time...")
        ready = 0
        for number, job in enumerate(jobs, 1):
            source = Path(job["workspace_wem_path"])
            output = source.with_suffix(".wav")
            self.step.configure(value=number - 1)
            self.step_status.set(self._stage_task(6, "Decoding validation samples"))
            self.current_line.set(f"Current dialogue: {job['dialogue_id']} | {job['official_subtitle'][:90]}")
            self._refresh_progress_bars()
            try:
                if not output.is_file():
                    result = subprocess.run([str(decoder), "-o", str(output), str(source)], capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=HIDDEN_PROCESS)
                    if result.returncode:
                        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
                with wave.open(str(output), "rb") as audio:
                    job["duration_ms"] = round(audio.getnframes() * 1000 / audio.getframerate())
                    job["sample_rate"] = audio.getframerate()
                    job["channels"] = audio.getnchannels()
                job["workspace_wav_path"] = str(output); job["status"] = "wav_ready"; ready += 1
                self._append_log(f"> Decoded validation sample | {job['duration_ms']} ms")
            except Exception as error:
                job["status"] = "decode_failed"; job["decode_error"] = str(error)
                self._append_log(f"> Validation sample decode failed: {error}")
            self.step.configure(value=number)
            self._refresh_progress_bars()
        jobs_path.write_text(''.join(json.dumps(job, ensure_ascii=False) + "\n" for job in jobs), encoding="utf-8")
        self._append_log("> Validation sample decoding complete")
        self.step_status.set(self._stage_task(6, f"complete | {ready} WAV samples ready"))
        self.current_line.set("Current dialogue: No source files have been processed.")
        self.action_button.configure(text="Analyze Validation Samples (CPU)", command=self._analyze_audio_test, state="normal")
    @staticmethod
    def _analyze_wav_file(path: Path) -> dict[str, object]:
        """Return lightweight PCM measurements without loading an AI model."""
        with wave.open(str(path), "rb") as audio:
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            sample_rate = audio.getframerate()
            frame_count = audio.getnframes()
            raw = audio.readframes(frame_count)
        if not frame_count or sample_width not in (1, 2, 3, 4):
            raise ValueError("unsupported or empty PCM WAV")
        maximum = float((1 << (sample_width * 8 - 1)) - 1)
        values: list[float] = []
        if sample_width == 1:
            values = [(item - 128) / 127.0 for item in raw]
        elif sample_width == 2:
            values = [item / maximum for item in struct.unpack("<" + "h" * (len(raw) // 2), raw)]
        elif sample_width == 4:
            values = [item / maximum for item in struct.unpack("<" + "i" * (len(raw) // 4), raw)]
        else:
            for offset in range(0, len(raw), 3):
                item = int.from_bytes(raw[offset:offset + 3] + (b"\xff" if raw[offset + 2] & 0x80 else b"\x00"), "little", signed=True)
                values.append(item / maximum)
        mono = [sum(values[index:index + channels]) / channels for index in range(0, len(values), channels)]
        peak = max(abs(item) for item in mono)
        rms = math.sqrt(sum(item * item for item in mono) / len(mono))
        block_frames = max(1, sample_rate // 50)  # 20 ms blocks
        silent = [max(abs(item) for item in mono[index:index + block_frames]) < 0.015 for index in range(0, len(mono), block_frames)]
        leading_blocks = next((index for index, item in enumerate(silent) if not item), len(silent))
        trailing_blocks = next((index for index, item in enumerate(reversed(silent)) if not item), len(silent))
        pauses = 0
        run = 0
        for item in silent:
            run = run + 1 if item else 0
            if not item and run >= 6:
                pauses += 1
        if run >= 6:
            pauses += 1
        duration_ms = round(frame_count * 1000 / sample_rate)
        return {
            "duration_ms": duration_ms, "sample_rate": sample_rate, "channels": channels,
            "rms_dbfs": round(20 * math.log10(max(rms, 1e-9)), 2),
            "peak_dbfs": round(20 * math.log10(max(peak, 1e-9)), 2),
            "leading_silence_ms": leading_blocks * 20,
            "trailing_silence_ms": trailing_blocks * 20,
            "internal_pause_count": pauses,
            "energy": round(min(1.0, rms / 0.22), 3),
        }

    def _analyze_audio_test(self) -> None:
        """Analyze decoded test WAVs on CPU only and cache every result locally."""
        jobs_path = WORK_ROOT / "manifests" / "audio_test_jobs.jsonl"
        if not jobs_path.is_file():
            messagebox.showerror("Audio test unavailable", "Decode the 20 WEM samples before analyzing them.")
            return
        jobs = [json.loads(line) for line in jobs_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        ready_jobs = [job for job in jobs if job.get("workspace_wav_path") and Path(job["workspace_wav_path"]).is_file()]
        if not ready_jobs:
            messagebox.showerror("WAV files required", "No decoded WAV samples were found in the local workspace.")
            return
        self.overall.configure(value=7)
        self.overall_status.set(self._stage_overall(7, "Analyze WAV sample test"))
        self.step.configure(maximum=len(ready_jobs), value=0)
        self._append_log(f"> Analyzing {len(ready_jobs)} WAV samples on CPU only; CUDA models are not loaded.")
        database_path = WORK_ROOT / "voice_pipeline.db"
        connection = sqlite3.connect(database_path)
        connection.execute("""CREATE TABLE IF NOT EXISTS audio_test_analysis (
            source_audio_path TEXT PRIMARY KEY, duration_ms INTEGER, sample_rate INTEGER, channels INTEGER,
            rms_dbfs REAL, peak_dbfs REAL, leading_silence_ms INTEGER, trailing_silence_ms INTEGER,
            internal_pause_count INTEGER, energy REAL, analyzed_at TEXT NOT NULL)""")
        report: list[dict[str, object]] = []
        completed = 0
        try:
            for number, job in enumerate(ready_jobs, 1):
                self.step.configure(value=number - 1)
                self.step_status.set(self._stage_task(7, "Analyzing validation samples"))
                self.current_line.set(f"Current dialogue: {job['dialogue_id']} | {job['official_subtitle'][:90]}")
                self._refresh_progress_bars()
                try:
                    metrics = self._analyze_wav_file(Path(job["workspace_wav_path"]))
                    row = {**job, **metrics, "status": "analysis_ready"}
                    connection.execute("INSERT OR REPLACE INTO audio_test_analysis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                        job["source_audio_path"], metrics["duration_ms"], metrics["sample_rate"], metrics["channels"],
                        metrics["rms_dbfs"], metrics["peak_dbfs"], metrics["leading_silence_ms"], metrics["trailing_silence_ms"],
                        metrics["internal_pause_count"], metrics["energy"], datetime.now(timezone.utc).isoformat()))
                    report.append(row); job.update(metrics); job["status"] = "analysis_ready"; completed += 1
                    self._append_log(f"> Analyzed validation sample | {metrics['duration_ms']} ms | RMS {metrics['rms_dbfs']} dBFS")
                except Exception as error:
                    job["analysis_error"] = str(error)
                    self._append_log(f"> Validation sample analysis failed: {error}")
                self.step.configure(value=number)
                self._refresh_progress_bars()
            connection.commit()
        finally:
            connection.close()
        (WORK_ROOT / "analysis").mkdir(parents=True, exist_ok=True)
        (WORK_ROOT / "analysis" / "audio_test_analysis.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in report), encoding="utf-8")
        jobs_path.write_text("".join(json.dumps(job, ensure_ascii=False) + "\n" for job in jobs), encoding="utf-8")
        self._append_log("> CPU validation sample analysis complete")
        self.step_status.set(self._stage_task(7, f"complete | {completed} audio analyses ready"))
        self.current_line.set("Current dialogue: Audio analysis is ready for separate CUDA ASR validation.")
        self.action_button.configure(text="ASR validation: next integration", state="disabled")
    @staticmethod
    def _parse_localized_strings(path: Path) -> list[tuple[int, str]]:
        """Parse Bethesda STRINGS, DLSTRINGS, or ILSTRINGS from a workspace copy."""
        data = path.read_bytes()
        if len(data) < 8:
            raise ValueError("localized string file is shorter than its header")
        count, _data_size = struct.unpack_from("<II", data, 0)
        directory_end = 8 + count * 8
        if directory_end > len(data):
            raise ValueError("localized string directory exceeds file size")
        kind = path.suffix.lower()
        records: list[tuple[int, str]] = []
        for number in range(count):
            string_id, offset = struct.unpack_from("<II", data, 8 + number * 8)
            position = directory_end + offset
            if position >= len(data):
                continue
            if kind == ".strings":
                end = data.find(b"\0", position)
                raw = data[position:] if end < 0 else data[position:end]
            else:
                if position + 4 > len(data):
                    continue
                length = struct.unpack_from("<I", data, position)[0]
                raw = data[position + 4:position + 4 + length].rstrip(b"\0")
            records.append((string_id, raw.decode("utf-8", errors="replace")))
        return records

    def _build_target_subtitle_index(self) -> None:
        """Extract only selected-language text files into work and index them in SQLite."""
        language = self.target_language.get().strip()
        if not language or "(" not in language:
            messagebox.showerror("Target language required", "Select a verified target voice-over language first.")
            return
        code = language.rsplit("(", 1)[1].rstrip(")").lower()
        reader = self._reader_path()
        report_path = WORK_ROOT / "discovery" / "dialogue_sources.json"
        if not reader.is_file() or not report_path.is_file():
            messagebox.showerror("Discovery required", "Run dialogue discovery and validate the internal reader first.")
            return
        report = json.loads(report_path.read_text(encoding="utf-8"))
        archives = [Path(item) for item in report["localization_archives"]]
        subtitle_codes = tuple(dict.fromkeys((code, "en")))
        suffixes = tuple(f"_{subtitle_code}{extension}" for subtitle_code in subtitle_codes for extension in (".strings", ".dlstrings", ".ilstrings"))
        self.overall.configure(value=2)
        self.overall_status.set(self._stage_overall(2, "Build target dialogue text index"))
        self._append_log(f"> Building subtitle index for: {language}, then English (en)")
        self.step.configure(maximum=max(1, len(archives)), value=0)
        database_path = WORK_ROOT / "voice_pipeline.db"
        connection = sqlite3.connect(database_path)
        connection.execute("""CREATE TABLE IF NOT EXISTS official_subtitles (
            source_archive TEXT NOT NULL, source_revision TEXT NOT NULL, internal_path TEXT NOT NULL,
            language TEXT NOT NULL, file_kind TEXT NOT NULL, string_id INTEGER NOT NULL, text TEXT NOT NULL,
            workspace_path TEXT NOT NULL, content_sha256 TEXT NOT NULL,
            PRIMARY KEY (source_archive, source_revision, internal_path, string_id))""")
        # Discovery is always read-only, but the local index must never mix a
        # previous game build with the current BA2 revision.  Prune database
        # rows for archives no longer installed before rebuilding this index.
        if archives:
            placeholders = ",".join("?" for _ in archives)
            connection.execute(
                f"DELETE FROM official_subtitles WHERE source_archive NOT IN ({placeholders})",
                tuple(str(archive) for archive in archives),
            )
        extracted = skipped = indexed = 0
        try:
            for number, archive in enumerate(archives, 1):
                stats = archive.stat()
                revision = f"{stats.st_size:x}-{stats.st_mtime_ns:x}"[-24:]
                # A changed BA2 gets a new revision key.  Remove the old key
                # now, otherwise a later dialogue/voice join could see stale
                # localisation from the previous Starfield version.
                connection.execute(
                    "DELETE FROM official_subtitles WHERE source_archive = ? AND source_revision <> ?",
                    (str(archive), revision),
                )
                result = subprocess.run([str(reader), "list", str(archive)], capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=HIDDEN_PROCESS)
                if result.returncode:
                    self._append_log(f"> Reader error: {archive.name}: {result.stderr.strip()}")
                    continue
                entries = [line for line in result.stdout.splitlines() if line.lower().replace("\\", "/").endswith(suffixes)]
                self._append_log(f"> {archive.name}: {len(entries)} selected-language/English subtitle files")
                for internal_path in entries:
                    filename_stem = Path(internal_path).stem.lower()
                    file_code = next((subtitle_code for subtitle_code in subtitle_codes if filename_stem.endswith("_" + subtitle_code)), code)
                    destination = WORK_ROOT / "input" / "official_subtitles" / file_code / revision / archive.stem / Path(*internal_path.split("/"))
                    if destination.exists():
                        skipped += 1
                    else:
                        command = [str(reader), "extract", str(archive), internal_path, str(destination)]
                        extraction = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=HIDDEN_PROCESS)
                        if extraction.returncode:
                            self._append_log(f"> Extraction error: {internal_path}: {extraction.stderr.strip()}")
                            continue
                        extracted += 1
                    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
                    rows = [(str(archive), revision, internal_path, file_code, destination.suffix.lower(), string_id, text, str(destination), digest)
                            for string_id, text in self._parse_localized_strings(destination)]
                    connection.executemany("INSERT OR REPLACE INTO official_subtitles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
                    indexed += len(rows)
                connection.commit()
                self.step.configure(value=number)
                self.step_status.set(f"Current task: Indexing {archive.name} ({number} of {len(archives)})")
                self._refresh_progress_bars()
        finally:
            connection.close()
        self._append_log(f"> Subtitle index complete | Languages: {", ".join(subtitle_codes)} | Extracted: {extracted} | Cached: {skipped} | Text entries: {indexed}")
        self._append_log(f"> SQLite database: {database_path}")
        self._append_log("> Game files were read only; all extracted text is in the workspace.")
        self.step_status.set(f"Current task: Subtitle index complete | {indexed} text entries")
        self.current_line.set(f"Current dialogue: {language} subtitle index is ready.")
        connection = sqlite3.connect(database_path)
        try:
            voice_rows = connection.execute("SELECT COUNT(*) FROM english_voice_assets").fetchone()[0]
        except sqlite3.Error:
            voice_rows = 0
        finally:
            connection.close()
        if voice_rows:
            self.action_button.configure(text="Build Dialogue Text Index", command=self._build_dialogue_text_index)
        else:
            self.action_button.configure(text="Build English Voice Manifest", command=self._build_english_voice_manifest)

    @staticmethod
    def _iter_info_text_ids(plugin_path: Path):
        """Yield INFO FormIDs and localized NAM1 string IDs from a Bethesda plugin."""
        with plugin_path.open("rb") as stream:
            data = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                offset, boundaries = 0, [len(data)]
                while offset + 24 <= len(data):
                    while boundaries and offset >= boundaries[-1]:
                        boundaries.pop()
                    if not boundaries:
                        break
                    signature = data[offset:offset + 4]
                    size = struct.unpack_from("<I", data, offset + 4)[0]
                    total_size = size if signature == b"GRUP" else 24 + size
                    if total_size < 24 or offset + total_size > boundaries[-1]:
                        break
                    if signature == b"GRUP":
                        boundaries.append(offset + size)
                        offset += 24
                        continue
                    if signature == b"INFO":
                        flags = struct.unpack_from("<I", data, offset + 8)[0]
                        form_id = struct.unpack_from("<I", data, offset + 12)[0]
                        payload = data[offset + 24:offset + total_size]
                        if flags & 0x00040000:
                            if len(payload) >= 4:
                                try:
                                    payload = zlib.decompress(payload[4:])
                                except zlib.error:
                                    payload = b""
                            else:
                                payload = b""
                        response_number, position = 0, 0
                        while position + 6 <= len(payload):
                            subrecord = payload[position:position + 4]
                            subrecord_size = struct.unpack_from("<H", payload, position + 4)[0]
                            position += 6
                            if subrecord == b"XXXX" and subrecord_size == 4 and position + 4 <= len(payload):
                                subrecord_size = struct.unpack_from("<I", payload, position)[0]
                                position += 4
                                if position + 6 > len(payload):
                                    break
                                subrecord = payload[position:position + 4]
                                position += 6
                            if position + subrecord_size > len(payload):
                                break
                            value = payload[position:position + subrecord_size]
                            position += subrecord_size
                            if subrecord == b"NAM1" and len(value) == 4:
                                response_number += 1
                                yield form_id, response_number, struct.unpack_from("<I", value)[0]
                    offset += total_size
            finally:
                data.close()

    def _build_dialogue_text_index(self) -> None:
        """Build exact INFO-to-subtitle records; voice filenames are resolved in a later map stage."""
        language = self.target_language.get().strip()
        if not language or "(" not in language:
            messagebox.showerror("Target language required", "Select and index a target voice-over language first.")
            return
        target_code = language.rsplit("(", 1)[1].rstrip(")").lower()
        report_path = WORK_ROOT / "discovery" / "dialogue_sources.json"
        if not report_path.is_file():
            messagebox.showerror("Discovery required", "Run dialogue discovery first.")
            return
        report = json.loads(report_path.read_text(encoding="utf-8"))
        plugins = [Path(item) for item in report.get("plugins", [])]
        database_path = WORK_ROOT / "voice_pipeline.db"
        manifest_path = WORK_ROOT / "manifests" / "dialogue_text_index.jsonl"
        self.overall.configure(value=4)
        self.overall_status.set(self._stage_overall(4, "Build dialogue text index"))
        self.step.configure(maximum=max(1, len(plugins)), value=0)
        self._append_log(f"> Building exact INFO-to-{target_code} dialogue text index from {len(plugins)} plugins...")
        connection = sqlite3.connect(database_path)
        connection.execute("""CREATE TABLE IF NOT EXISTS dialogue_text_records (
            source_plugin TEXT NOT NULL, source_revision TEXT NOT NULL, info_form_id TEXT NOT NULL,
            response_number INTEGER NOT NULL, string_id INTEGER NOT NULL, target_language TEXT NOT NULL,
            target_text TEXT NOT NULL, mapping_status TEXT NOT NULL,
            PRIMARY KEY (source_plugin, source_revision, info_form_id, response_number, target_language))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS plugin_index_cache (
            index_name TEXT NOT NULL, source_plugin TEXT NOT NULL, source_revision TEXT NOT NULL,
            record_count INTEGER NOT NULL, indexed_at TEXT NOT NULL,
            PRIMARY KEY (index_name, source_plugin, source_revision))""")
        target_texts = {}
        for string_id, text in connection.execute("SELECT string_id, text FROM official_subtitles WHERE language = ? AND file_kind = '.ilstrings'", (target_code,)):
            target_texts.setdefault(string_id, text)
        indexed = cached = 0
        try:
            for number, plugin in enumerate(plugins, 1):
                stats = plugin.stat()
                revision = f"{stats.st_size:x}-{stats.st_mtime_ns:x}"[-24:]
                cached_row = connection.execute("SELECT record_count FROM plugin_index_cache WHERE index_name = 'dialogue_text' AND source_plugin = ? AND source_revision = ?", (str(plugin), revision)).fetchone()
                if cached_row:
                    cached += cached_row[0]
                    self._append_log(f"> Skipped (cached): {plugin.name} | {cached_row[0]} dialogue responses")
                else:
                    connection.execute("DELETE FROM dialogue_text_records WHERE source_plugin = ?", (str(plugin),))
                    connection.execute("DELETE FROM plugin_index_cache WHERE index_name = 'dialogue_text' AND source_plugin = ?", (str(plugin),))
                    rows = []
                    for form_id, response_number, string_id in self._iter_info_text_ids(plugin):
                        target_text = target_texts.get(string_id)
                        if target_text is None:
                            continue
                        rows.append((str(plugin), revision, f"{form_id:08x}", response_number, string_id, target_code, target_text, "exact_info_nam1"))
                    connection.executemany("INSERT OR REPLACE INTO dialogue_text_records VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
                    connection.execute("INSERT INTO plugin_index_cache VALUES (?, ?, ?, ?, ?)", ("dialogue_text", str(plugin), revision, len(rows), datetime.now(timezone.utc).isoformat()))
                    connection.commit()
                    indexed += len(rows)
                    self._append_log(f"> Indexed plugin: {plugin.name} | {len(rows)} dialogue responses")
                self.step.configure(value=number)
                self.step_status.set(self._stage_task(4, f"Reading {plugin.name} ({number} of {len(plugins)})"))
                self._refresh_progress_bars()
            temporary = manifest_path.with_suffix(".jsonl.tmp")
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                cursor = connection.execute("""SELECT source_plugin, source_revision, info_form_id, response_number,
                    string_id, target_language, target_text, mapping_status FROM dialogue_text_records
                    WHERE target_language = ? ORDER BY source_plugin, info_form_id, response_number""", (target_code,))
                for row in cursor:
                    handle.write(json.dumps({"source_plugin": row[0], "source_revision": row[1], "dialogue_id": row[2],
                        "response_number": row[3], "string_id": row[4], "target_language": row[5],
                        "official_subtitle": row[6], "mapping_status": row[7]}, ensure_ascii=False) + "\n")
            temporary.replace(manifest_path)
        finally:
            connection.close()
        self._append_log(f"> Dialogue text index complete | New: {indexed} | Cached: {cached}")
        self._append_log(f"> Manifest saved: {manifest_path}")
        self._append_log("> This is an exact INFO-to-subtitle map. Voice filenames are not guessed in this stage.")
        self.step_status.set(self._stage_task(4, f"complete | {indexed} new dialogue responses"))
        self.current_line.set("Current dialogue: Dialogue text index is ready for voice-map integration.")
        self.action_button.configure(text="Build Voice Map", command=self._build_voice_map)
    def _build_voice_map(self) -> None:
        """Create conservative WEM-to-INFO mappings from the eight-digit INFO FormID filename."""
        language = self.target_language.get().strip()
        if not language or "(" not in language:
            messagebox.showerror("Target language required", "Select a target voice-over language first.")
            return
        code = language.rsplit("(", 1)[1].rstrip(")").lower()
        database_path = WORK_ROOT / "voice_pipeline.db"
        manifest_path = WORK_ROOT / "manifests" / "voice_map.jsonl"
        self.overall.configure(value=5)
        self.overall_status.set(self._stage_overall(5, "Build voice map"))
        connection = sqlite3.connect(database_path)
        connection.execute("""CREATE TABLE IF NOT EXISTS voice_map_entries (
            source_archive TEXT, source_audio_path TEXT, speaker_key TEXT, dialogue_plugin TEXT,
            dialogue_id TEXT, response_number INTEGER, string_id INTEGER, target_language TEXT,
            target_text TEXT, mapping_status TEXT, mapping_confidence REAL,
            PRIMARY KEY(source_audio_path, response_number, target_language))""")
        total = connection.execute("SELECT COUNT(*) FROM english_voice_assets").fetchone()[0]
        self.step.configure(maximum=max(1, total), value=0)
        self._append_log(f"> Building FormID voice map from {total} English voice assets...")
        try:
            connection.execute("DELETE FROM voice_map_entries WHERE target_language = ?", (code,))
            connection.execute("""INSERT OR REPLACE INTO voice_map_entries
                SELECT v.source_archive, v.internal_path, v.speaker_key, d.source_plugin, d.info_form_id,
                  d.response_number, d.string_id, d.target_language, d.target_text,
                  CASE WHEN c.response_count = 1 THEN 'exact_form_id' ELSE 'ambiguous_response_number' END,
                  CASE WHEN c.response_count = 1 THEN 1.0 ELSE 0.60 END
                FROM english_voice_assets v
                JOIN dialogue_text_records d ON lower(substr(v.file_name, 1, 8)) = d.info_form_id
                JOIN (SELECT info_form_id, target_language, COUNT(*) response_count
                      FROM dialogue_text_records GROUP BY info_form_id, target_language) c
                  ON c.info_form_id = d.info_form_id AND c.target_language = d.target_language
                WHERE d.target_language = ?""", (code,))
            connection.commit()
            exact = connection.execute("SELECT COUNT(*) FROM voice_map_entries WHERE target_language=? AND mapping_status='exact_form_id'", (code,)).fetchone()[0]
            ambiguous = connection.execute("SELECT COUNT(*) FROM voice_map_entries WHERE target_language=? AND mapping_status='ambiguous_response_number'", (code,)).fetchone()[0]
            self.step.configure(value=total)
            temporary = manifest_path.with_suffix(".jsonl.tmp")
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                for row in connection.execute("SELECT source_archive, source_audio_path, speaker_key, dialogue_plugin, dialogue_id, response_number, string_id, target_language, target_text, mapping_status, mapping_confidence FROM voice_map_entries WHERE target_language=? ORDER BY source_audio_path", (code,)):
                    handle.write(json.dumps({"original_archive_path":row[0],"source_audio_path":row[1],"speaker_id":row[2],"dialogue_plugin":row[3],"dialogue_id":row[4],"response_number":row[5],"string_id":row[6],"target_language":row[7],"official_subtitle":row[8],"mapping_status":row[9],"mapping_confidence":row[10]}, ensure_ascii=False)+"\n")
            temporary.replace(manifest_path)
        finally:
            connection.close()
        self._append_log(f"> Voice map complete | Exact: {exact} | Multi-response review: {ambiguous}")
        self._append_log(f"> Manifest saved: {manifest_path}")
        self._append_log("> Only exact mappings will be extracted automatically.")
        self.step_status.set(self._stage_task(5, f"complete | exact: {exact} | review: {ambiguous}"))
        self.current_line.set("Current dialogue: Voice map is ready for validation samples.")
        self.action_button.configure(text="Prepare Validation Samples", command=self._prepare_wav_test)
    def _prepare_wav_test(self) -> None:
        """Prepare 40 distinct WEM files with exact xTranslator FuzMap associations."""
        reader = self._reader_path()
        manifest_path = WORK_ROOT / "manifests" / "voice_map.jsonl"
        if not reader.is_file() or not manifest_path.is_file():
            messagebox.showerror("Voice map required", "Build the voice map before preparing the WAV test.")
            return
        rows = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        unique_rows: list[dict[str, object]] = []
        seen_paths: set[str] = set()
        for row in rows:
            if row.get("mapping_status") != "xtranslator_exact":
                continue
            path_key = str(row.get("source_audio_path", "")).lower()
            if path_key and path_key not in seen_paths:
                seen_paths.add(path_key)
                unique_rows.append(row)
        seed = secrets.randbits(64)
        sample_count = min(40, len(unique_rows))
        selections = (("validation", random.Random(seed).sample(unique_rows, sample_count)),)
        jobs_path = WORK_ROOT / "manifests" / "audio_test_jobs.jsonl"
        total = sum(len(items) for _, items in selections)
        self.overall.configure(value=6)
        self.overall_status.set(self._stage_overall(6, "Prepare validation samples"))
        self.step.configure(maximum=max(1, total), value=0)
        self._append_log(f"> Preparing randomly selected English validation samples | Seed: {seed}")
        jobs, extracted, cached, failed, number = [], 0, 0, 0, 0
        for group, items in selections:
            for row in items:
                number += 1
                internal_path = row["source_audio_path"].replace("\\", "/").lstrip("/")
                destination = WORK_ROOT / "samples" / "audio_test" / group / Path(*internal_path.split("/"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.is_file() and destination.stat().st_size > 0:
                    cached += 1
                    status = "cached"
                else:
                    result = subprocess.run([str(reader), "extract", row["original_archive_path"], row["source_audio_path"], str(destination)], capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=HIDDEN_PROCESS)
                    status = "prepared" if result.returncode == 0 and destination.is_file() and destination.stat().st_size > 0 else "failed"
                    if status == "prepared": extracted += 1
                    else:
                        failed += 1
                        self._append_log(f"> Audio sample extraction error: {internal_path}: {result.stderr.strip() or result.stdout.strip()}")
                if status != "failed":
                    job = dict(row)
                    job.update({"sample_group": group, "validation_seed": seed, "workspace_wem_path": str(destination), "status": "wem_ready"})
                    jobs.append(job)
                self.step.configure(value=number)
                self.step_status.set(self._stage_task(6, "Preparing validation samples"))
                self._refresh_progress_bars()
        jobs_path.write_text("".join(json.dumps(job, ensure_ascii=False) + "\n" for job in jobs), encoding="utf-8")
        self._append_log(f"> Validation sample preparation complete | Extracted: {extracted} | Cached: {cached} | Failed: {failed}")
        self._append_log(f"> Test manifest saved: {jobs_path}")
        self.step_status.set(self._stage_task(6, "validation samples ready"))
        self.action_button.configure(text="Decode Validation Samples to WAV (CPU)", command=self._decode_audio_test, state="normal")
    def _extract_exact_wem(self) -> None:
        """Extract only exact voice-map WEM assets into the local workspace."""
        reader = self._reader_path()
        manifest_path = WORK_ROOT / "manifests" / "voice_map.jsonl"
        if not reader.is_file() or not manifest_path.is_file():
            messagebox.showerror("Voice map required", "Build the voice map before extracting WEM files.")
            return
        entries = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        exact_entries = [row for row in entries if row.get("mapping_status") == "exact_form_id"]
        output_root = WORK_ROOT / "samples" / "original_en" / "exact"
        output_manifest = WORK_ROOT / "manifests" / "exact_wem_extraction.jsonl"
        self.overall.configure(value=6)
        self.overall_status.set(self._stage_overall(6, "Extract exact English WEM files"))
        self.step.configure(maximum=max(1, len(exact_entries)), value=0)
        self._append_log(f"> Extracting {len(exact_entries)} exact English WEM files into the workspace...")
        extracted = cached = failed = 0
        with output_manifest.open("w", encoding="utf-8", newline="\n") as handle:
            for number, row in enumerate(exact_entries, 1):
                internal_path = row["source_audio_path"].replace("\\", "/").lstrip("/")
                destination = output_root / Path(*internal_path.split("/"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.is_file() and destination.stat().st_size > 0:
                    cached += 1
                    status = "cached"
                else:
                    result = subprocess.run([str(reader), "extract", row["original_archive_path"], row["source_audio_path"], str(destination)], capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=HIDDEN_PROCESS)
                    if result.returncode or not destination.is_file() or destination.stat().st_size == 0:
                        failed += 1
                        status = "failed"
                        self._append_log(f"> Extraction error: {internal_path}: {result.stderr.strip() or result.stdout.strip()}")
                    else:
                        extracted += 1
                        status = "extracted"
                record = dict(row)
                record.update({"workspace_wem_path": str(destination), "extraction_status": status})
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                self.step.configure(value=number)
                self.step_status.set(self._stage_task(6, f"Extracting WEM {number} of {len(exact_entries)}"))
                self.current_line.set(f"Current dialogue: {row['dialogue_id']} | {row['official_subtitle'][:90]}")
                self._refresh_progress_bars()
        self._append_log(f"> Exact WEM extraction complete | Extracted: {extracted} | Cached: {cached} | Failed: {failed}")
        self._append_log(f"> Manifest saved: {output_manifest}")
        self.step_status.set(self._stage_task(6, f"complete | {extracted + cached} WEM files ready"))
        self.action_button.configure(text="Prepare WAV Decode Test: next integration", state="disabled")
    def _build_english_voice_manifest(self) -> None:
        """Index only original English WEM paths; do not extract audio in this stage."""
        report_path = WORK_ROOT / "discovery" / "dialogue_sources.json"
        reader = self._reader_path()
        if not report_path.is_file() or not reader.is_file():
            messagebox.showerror("Discovery required", "Run dialogue discovery and validate the internal reader first.")
            return
        report = json.loads(report_path.read_text(encoding="utf-8"))
        def is_english_voice(path: Path) -> bool:
            """Base archives without a locale suffix are English; DLCs use _en explicitly."""
            name = path.name.lower()
            if any(token in name for token in ("voices_es", "voices_de", "voices_fr", "voices_ja", "voices_it", "voices_ptbr", "voices_zh")):
                return False
            return "voices_en" in name or name in {
                "starfield - voices01.ba2", "starfield - voices02.ba2", "starfield - voicespatch.ba2",
            }
        archives = [Path(item) for item in report["voice_archives"] if is_english_voice(Path(item))]
        database_path = WORK_ROOT / "voice_pipeline.db"
        manifest_dir = WORK_ROOT / "manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_dir / "english_voice_manifest.jsonl"
        temporary_path = manifest_path.with_suffix(".jsonl.tmp")
        self.overall.configure(value=3)
        self.overall_status.set(self._stage_overall(3, "Build English voice manifest"))
        self.step.configure(maximum=max(1, len(archives)), value=0)
        self._append_log(f"> Building English voice manifest from {len(archives)} archives...")
        connection = sqlite3.connect(database_path)
        connection.execute("""CREATE TABLE IF NOT EXISTS english_voice_assets (
            source_archive TEXT NOT NULL, source_revision TEXT NOT NULL, internal_path TEXT NOT NULL,
            game_plugin TEXT NOT NULL, speaker_key TEXT NOT NULL, file_name TEXT NOT NULL,
            audio_format TEXT NOT NULL, source_audio_language TEXT NOT NULL, status TEXT NOT NULL,
            indexed_at TEXT NOT NULL, PRIMARY KEY (source_archive, source_revision, internal_path))""")
        # Match the current discovery report exactly.  The previous revision
        # of a changed or removed voice BA2 must not survive in the local map.
        if archives:
            placeholders = ",".join("?" for _ in archives)
            connection.execute(
                f"DELETE FROM english_voice_assets WHERE source_archive NOT IN ({placeholders})",
                tuple(str(archive) for archive in archives),
            )
        total = cached_archives = 0
        try:
            with temporary_path.open("w", encoding="utf-8", newline="\n") as handle:
                for number, archive in enumerate(archives, 1):
                    stats = archive.stat()
                    revision = f"{stats.st_size:x}-{stats.st_mtime_ns:x}"[-24:]
                    connection.execute(
                        "DELETE FROM english_voice_assets WHERE source_archive = ? AND source_revision <> ?",
                        (str(archive), revision),
                    )
                    existing = connection.execute("SELECT COUNT(*) FROM english_voice_assets WHERE source_archive = ? AND source_revision = ?", (str(archive), revision)).fetchone()[0]
                    if existing:
                        cached_archives += 1
                        rows = connection.execute("""SELECT source_archive, source_revision, internal_path, game_plugin,
                            speaker_key, file_name, audio_format, source_audio_language, status
                            FROM english_voice_assets WHERE source_archive = ? AND source_revision = ?
                            ORDER BY internal_path""", (str(archive), revision))
                        for row in rows:
                            item = {"source_archive": row[0], "source_revision": row[1], "internal_path": row[2],
                                    "game_plugin": row[3], "speaker_key": row[4], "file_name": row[5],
                                    "audio_format": row[6], "source_audio_language": row[7], "status": row[8]}
                            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                        self._append_log(f"> Exported cached archive: {archive.name} | {existing} voice entries")
                    else:
                        process = subprocess.Popen([str(reader), "list", str(archive)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", creationflags=HIDDEN_PROCESS)
                        assert process.stdout is not None
                        batch: list[tuple[str, str, str, str, str, str, str, str, str, str]] = []
                        for raw_path in process.stdout:
                            internal_path = raw_path.strip().replace("\\", "/")
                            lowered = internal_path.lower()
                            if not (lowered.startswith("sound/voice/") and lowered.endswith(".wem")):
                                continue
                            parts = internal_path.split("/")
                            if len(parts) < 5:
                                continue
                            game_plugin, speaker_key, file_name = parts[2], parts[3], parts[-1]
                            item = {"source_archive": str(archive), "source_revision": revision, "internal_path": internal_path,
                                    "game_plugin": game_plugin, "speaker_key": speaker_key, "file_name": file_name,
                                    "audio_format": "wem", "source_audio_language": "en", "status": "indexed"}
                            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                            batch.append((str(archive), revision, internal_path, game_plugin, speaker_key, file_name, "wem", "en", "indexed", datetime.now(timezone.utc).isoformat()))
                            if len(batch) >= 1000:
                                connection.executemany("INSERT OR REPLACE INTO english_voice_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", batch)
                                total += len(batch); batch.clear()
                        stderr = process.stderr.read() if process.stderr else ""
                        if process.wait() != 0:
                            self._append_log(f"> Reader error: {archive.name}: {stderr.strip()}")
                        if batch:
                            connection.executemany("INSERT OR REPLACE INTO english_voice_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", batch)
                            total += len(batch)
                        connection.commit()
                        self._append_log(f"> Indexed English voice archive: {archive.name}")
                    self.step.configure(value=number)
                    self.step_status.set(f"Current task: Reading {archive.name} ({number} of {len(archives)})")
                    self._refresh_progress_bars()
            temporary_path.replace(manifest_path)
        finally:
            connection.close()
        manifest_entries = sum(1 for _ in manifest_path.open("r", encoding="utf-8")) if manifest_path.is_file() else 0
        self._append_log(f"> English voice manifest complete | New entries: {total} | Cached archives: {cached_archives} | Manifest entries: {manifest_entries}")
        self._append_log(f"> Manifest saved: {manifest_path}")
        self._append_log("> Original audio was indexed only; no audio files were extracted or modified.")
        self.step_status.set(f"Current task: English voice manifest complete | {total} new entries")
        self.current_line.set("Current dialogue: English original audio manifest is ready.")
        self.action_button.configure(text="Build Dialogue Text Index", command=self._build_dialogue_text_index)

    def _set_target_language(self, language: str, dialog: tk.Toplevel) -> None:
        """Switch target language only after confirming workspace-only cache cleanup."""
        code = language.rsplit("(", 1)[1].rstrip(")").lower()
        database_path = WORK_ROOT / "voice_pipeline.db"
        old_codes: list[str] = []
        if database_path.exists():
            connection = sqlite3.connect(database_path)
            try:
                old_codes = [row[0] for row in connection.execute("SELECT DISTINCT language FROM official_subtitles") if row[0] != code]
            finally:
                connection.close()
        if old_codes:
            files_root = WORK_ROOT / "input" / "official_subtitles"
            message = ("Changing the target language will remove only local workspace subtitle caches and database entries for: "
                       + ", ".join(old_codes) + "\n\nGame files in Starfield will not be changed. Continue?")
            if not messagebox.askyesno("Confirm Workspace Cleanup", message, parent=dialog):
                self._append_log("> Target language change cancelled. Existing workspace cache was kept.")
                return
            connection = sqlite3.connect(database_path)
            try:
                placeholders = ",".join("?" for _ in old_codes)
                connection.execute(f"DELETE FROM official_subtitles WHERE language IN ({placeholders})", old_codes)
                connection.commit()
            finally:
                connection.close()
            for old_code in old_codes:
                cache_dir = files_root / old_code
                if cache_dir.is_dir():
                    shutil.rmtree(cache_dir)
                self._append_log(f"> Removed local subtitle cache: {old_code}")
        self.target_language.set(language)
        self._append_log(f"> Selected target voice-over language: {language}")
        self.current_line.set(f"Current dialogue: Target language selected ? {language}")
        self.action_button.configure(text="Build Target Dialogue Text Index", command=self._build_target_subtitle_index)
        dialog.destroy()


    def _open_language_picker(self, languages: list[str]) -> None:
        """Open a dedicated language chooser after discovery completes."""
        dialog = tk.Toplevel(self)
        dialog.title("Select Target Voice-over Language")
        dialog.transient(self)
        dialog.resizable(False, False)
        panel = ttk.Frame(dialog, padding=16)
        panel.grid(sticky="nsew")
        ttk.Label(panel, text="Target voice-over language").grid(row=0, column=0, sticky="w")
        selected = tk.StringVar(value=languages[0] if languages else "Language detection requires the internal BA2 reader")
        chooser = ttk.Combobox(panel, textvariable=selected, values=languages, state="readonly" if languages else "disabled", width=56)
        chooser.grid(row=1, column=0, sticky="ew", pady=(6, 10))
        if languages:
            ttk.Button(panel, text="Use Selected Voice-over Language", command=lambda: self._set_target_language(selected.get(), dialog)).grid(row=2, column=0, sticky="ew")
        else:
            ttk.Label(panel, text="Installed subtitle languages will be listed here after the internal BA2 reader has inspected the localization archives.", wraplength=440).grid(row=2, column=0, sticky="w")
            ttk.Button(panel, text="Close", command=dialog.destroy).grid(row=3, column=0, sticky="ew", pady=(12, 0))
        dialog.update_idletasks()
        self._refresh_progress_bars()
        x = self.winfo_rootx() + (self.winfo_width() - dialog.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dialog.winfo_reqheight()) // 2
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
        dialog.grab_set()

    @staticmethod
    def _usage_color(ratio: float) -> str:
        if ratio > 0.90:
            return "#d7191c"
        if ratio > 0.70:
            return "#ffb347"
        return "#00aa28"
    @staticmethod
    def _cpu_model() -> str:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except OSError:
            return "CPU model unavailable"

    @staticmethod
    def _system_memory() -> tuple[float, float]:
        memory = MEMORYSTATUSEX(); memory.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            return 0.0, 0.0
        return memory.ullTotalPhys / 1024 ** 3, (memory.ullTotalPhys - memory.ullAvailPhys) / 1024 ** 3
    def _update_cpu_bar(self) -> None:
        idle = FILETIME(); kernel = FILETIME(); user = FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return
        current = (filetime_value(idle), filetime_value(kernel) + filetime_value(user))
        ratio = 0.0
        if self._cpu_times is not None:
            idle_delta = current[0] - self._cpu_times[0]
            total_delta = current[1] - self._cpu_times[1]
            ratio = min(1.0, max(0.0, (total_delta - idle_delta) / total_delta)) if total_delta else 0.0
            self.cpu_bar.delete("all")
            self.cpu_bar.create_rectangle(0, 0, max(1, int(self.cpu_bar.winfo_width() * ratio)), 4, fill=self._usage_color(ratio), outline="")
        self._cpu_times = current
        total_ram, used_ram = self._system_memory()
        cpu_percent = ratio * 100 if self._cpu_times is not None else 0.0
        ram_suffix = f" | RAM: {total_ram:.1f} GB" if total_ram > 0 else ""
        self.cpu_status.set(f"CPU usage {cpu_percent:.0f}% - {self._cpu_model()}{ram_suffix}")
        if total_ram > 0:
            ram_ratio = min(1.0, max(0.0, used_ram / total_ram))
            self.ram_usage_status.set(f"RAM usage: {ram_ratio * 100:.0f}% | {used_ram:.1f} / {total_ram:.1f} GB")
            self._draw_usage_bar(self.ram_bar, ram_ratio)
        else:
            self.ram_usage_status.set("RAM usage: unavailable")
            self.ram_bar.delete("all")
    def _draw_usage_bar(self, canvas: tk.Canvas, ratio: float) -> None:
        canvas.delete("all")
        canvas.create_rectangle(0, 0, max(1, int(canvas.winfo_width() * ratio)), 4, fill=self._usage_color(ratio), outline="")

    def _refresh_gpu_status(self) -> None:
        if self._gpu_name is None:
            self.gpu_usage_status.set("GPU usage unavailable")

    def _detect_gpu(self) -> None:
        """Refresh VRAM telemetry once per second without loading AI models."""
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, check=True, timeout=5, creationflags=HIDDEN_PROCESS,
            )
            name, total_mb, used_mb = [item.strip() for item in result.stdout.splitlines()[0].split(",")]
            self._gpu_name = name
            self._gpu_total_mb = int(total_mb)
            self._gpu_used_mb = int(used_mb)
            ratio = min(1.0, max(0.0, self._gpu_used_mb / self._gpu_total_mb)) if self._gpu_total_mb else 0.0
            self.vram_usage_status.set(f"VRAM usage {ratio * 100:.0f}%")
            self._draw_usage_bar(self.vram_bar, ratio)
            self._refresh_gpu_status()
        except (FileNotFoundError, subprocess.SubprocessError, IndexError, ValueError):
            self._gpu_name = None
            self.vram_usage_status.set("VRAM usage: unavailable")
            self.vram_bar.delete("all")
            self._refresh_gpu_status()
        self.after(1000, self._detect_gpu)

    def _update_gpu_usage(self) -> None:
        """Refresh the latest persistent NVIDIA utilization sample every 500 ms."""
        if self._gpu_usage_process is None or self._gpu_usage_process.poll() is not None:
            self._start_gpu_usage_monitor()
        try:
            latest = None
            while True:
                latest = self._gpu_usage_updates.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            self._gpu_usage_percent = latest
            self.gpu_usage_status.set(f"GPU usage {self._gpu_usage_percent}%")
            self._draw_usage_bar(self.gpu_usage_bar, self._gpu_usage_percent / 100)
        elif self._gpu_usage_process is None:
            self.gpu_usage_bar.delete("all")
        self.after(500, self._update_gpu_usage)

    def _start_gpu_usage_monitor(self) -> None:
        """Use one nvidia-smi process, avoiding ten process launches per second."""
        try:
            process = subprocess.Popen(
                ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits", "--loop-ms=500"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                creationflags=HIDDEN_PROCESS,
            )
        except OSError:
            self._gpu_usage_process = None
            return
        self._gpu_usage_process = process
        threading.Thread(target=self._read_gpu_usage_monitor, args=(process,), daemon=True).start()

    def _read_gpu_usage_monitor(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is None:
            return
        for raw_value in process.stdout:
            try:
                value = max(0, min(100, int(raw_value.strip())))
            except ValueError:
                continue
            try:
                while True:
                    self._gpu_usage_updates.get_nowait()
            except queue.Empty:
                pass
            try:
                self._gpu_usage_updates.put_nowait(value)
            except queue.Full:
                pass

    def destroy(self) -> None:
        """Stop the hidden NVIDIA monitor when the GUI is closed."""
        process = self._gpu_usage_process
        self._gpu_usage_process = None
        if process is not None and process.poll() is None:
            process.terminate()
        super().destroy()

    def _schedule_cpu_bar(self) -> None:
        self._update_cpu_bar()
        self.after(3000, self._schedule_cpu_bar)

    def _refresh_disk_status(self) -> None:
        raw_path = self.game_path.get().strip()
        if not raw_path:
            self.disk_status.set("Disk space: Select a game folder")
            return
        try:
            folder = Path(raw_path)
            free = shutil.disk_usage(folder).free / 1024 ** 3
            valid = (folder / "Data").is_dir()
            self.disk_status.set(f"Disk space: {free:.1f} GB free | Data folder: {'Found' if valid else 'Not found'}")
        except OSError:
            self.disk_status.set("Disk space: unavailable | Data folder: Not found")

    def _schedule_disk_status(self) -> None:
        self._refresh_disk_status()
        self.after(5000, self._schedule_disk_status)

    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory(title="Select Starfield game folder")
        if not selected:
            return
        self.game_path.set(selected)
        self._refresh_disk_status()
    def _validate_reader(self) -> None:
        """Find a locally installed BA2 reader; never download or execute it here."""
        game_root = Path(self.game_path.get())
        if not (game_root / "Data").is_dir():
            messagebox.showerror("Invalid game folder", "Select the Starfield installation folder containing Data.")
            return
        internal_reader = Path(getattr(sys, "_MEIPASS", APP_ROOT)) / "archive_reader.exe" if getattr(sys, "frozen", False) else SOURCE_ROOT / "archive_reader" / "target" / "release" / "archive_reader.exe"
        candidates = [
            internal_reader,
            game_root / "Tools" / "Archive2" / "Archive2.exe",
            game_root / "Archive2.exe",
            Path(r"C:\Program Files (x86)\Steam\steamapps\common\Starfield Creation Kit\Tools\Archive2\Archive2.exe"),
        ]
        for name in ("Archive2.exe", "BAE.exe", "bsarch.exe"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
        readers = []
        for candidate in candidates:
            if candidate.is_file() and str(candidate) not in {str(item) for item in readers}:
                readers.append(candidate)
        report = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "available_readers": [str(item) for item in readers],
            "status": "ready" if readers else "reader_not_found",
            "required_capability": "Read BA2 contents and export selected voice/localization files to a temporary workspace.",
        }
        report_path = WORK_ROOT / "discovery" / "reader_detection.json"
        temporary = report_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
        temporary.replace(report_path)
        self.overall.configure(value=2)
        self.overall_status.set("Overall: Preflight — Validate local asset reader")
        if readers:
            self._append_log(f"> BA2 reader found: {readers[0]}")
            self._append_log("> Reader validated. The next step can build dialogue pairs.")
            self.step_status.set("Current task: Local reader available")
            self.current_line.set("Current dialogue: Reader ready. No assets were extracted.")
        else:
            self._append_log("> No supported local BA2 reader found.")
            self._append_log("> Install or select a local reader before building dialogue pairs.")
            self.step_status.set("Current task: Reader required")
            self.current_line.set("Current dialogue: Discovery complete. Reader installation required.")
        self._append_log(f"> Reader report saved: {report_path}")

    def _discover_mapping(self) -> None:
        """Discover the local sources required to map dialogue keys to audio and subtitles."""
        game_root = Path(self.game_path.get())
        data_root = game_root / "Data"
        if not data_root.is_dir():
            messagebox.showerror("Invalid game folder", "Select the Starfield installation folder containing Data.")
            return
        self.overall.configure(value=1)
        self.overall_status.set(self._stage_overall(1, "Discover dialogue mapping"))
        self._append_log("> Starting read-only dialogue source discovery...")
        self.step.configure(maximum=3, value=0)
        plugins = sorted(path for path in data_root.rglob("*") if path.suffix.lower() in {".esm", ".esp", ".esl"})
        self.step.configure(value=1); self.step_status.set("Current task: Discovering game plugins"); self._refresh_progress_bars()
        localization_files = sorted(path for path in data_root.rglob("*") if path.suffix.lower() in {".strings", ".dlstrings", ".ilstrings"})
        self.step.configure(value=2); self.step_status.set("Current task: Discovering loose localization files"); self._refresh_progress_bars()
        archives = sorted(data_root.rglob("*.ba2"))
        voice_archives = [path for path in archives if "voice" in path.name.lower()]
        def is_localization_archive(path: Path) -> bool:
            name = path.name.lower()
            return any(token in name for token in ("localization", "string", "interface")) or name.endswith(" - main.ba2")
        localization_archives = [path for path in archives if is_localization_archive(path)]
        self.step.configure(value=3); self.step_status.set("Current task: Reading installed subtitle languages"); self._refresh_progress_bars()
        reader_path = Path(getattr(sys, "_MEIPASS", APP_ROOT)) / "archive_reader.exe" if getattr(sys, "frozen", False) else SOURCE_ROOT / "archive_reader" / "target" / "release" / "archive_reader.exe"
        language_labels = {
            "de": "German (de)", "en": "English (en)", "es": "Spanish (es)", "fr": "French (fr)",
            "it": "Italian (it)", "ja": "Japanese (ja)", "pl": "Polish (pl)", "ptbr": "Portuguese, Brazil (ptbr)",
            "zhhans": "Chinese, Simplified (zhhans)",
        }
        language_codes: set[str] = set()
        reader_errors: list[str] = []
        if reader_path.is_file():
            for archive in localization_archives:
                result = subprocess.run([str(reader_path), "list", str(archive)], capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=HIDDEN_PROCESS)
                if result.returncode:
                    reader_errors.append(f"{archive.name}: {result.stderr.strip()}")
                    continue
                for line in result.stdout.splitlines():
                    lowered = line.lower().replace("\\", "/")
                    if lowered.startswith("strings/") and "." in lowered:
                        stem = lowered.rsplit("/", 1)[-1].split(".", 1)[0]
                        code = stem.rsplit("_", 1)[-1]
                        if code in language_labels:
                            language_codes.add(code)
        else:
            reader_errors.append("Internal BA2 reader executable is unavailable.")
        installed_languages = [language_labels[code] for code in sorted(language_codes)]
        for relative in ("discovery", "input/official_subtitles", "samples/original_en", "output/target_voice", "reports"):
            (WORK_ROOT / relative).mkdir(parents=True, exist_ok=True)
        report = {
            "game_path": str(game_root),
            "discovered_at": datetime.now(timezone.utc).isoformat(),
            "plugins": [str(path) for path in plugins],
            "loose_localization_files": [str(path) for path in localization_files],
            "voice_archives": [str(path) for path in voice_archives],
            "localization_archives": [str(path) for path in localization_archives],
            "installed_subtitle_languages": installed_languages,
            "reader_errors": reader_errors,
            "mapping_status": "reader_required",
            "next_requirement": "A local BA2/ESM reader is required before dialogue pairs can be created.",
        }
        report_path = WORK_ROOT / "discovery" / "dialogue_sources.json"
        temporary = report_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
        temporary.replace(report_path)
        self._append_log(f"> Plugins found: {len(plugins)}")
        self._append_log(f"> Loose localization files found: {len(localization_files)}")
        self._append_log(f"> Voice archive candidates: {len(voice_archives)}")
        self._append_log(f"> Localization archive candidates: {len(localization_archives)}")
        self._append_log(f"> Installed subtitle languages found: {len(installed_languages)}")
        for language in installed_languages:
            self._append_log(f"> Subtitle language: {language}")
        for error in reader_errors:
            self._append_log(f"> Reader warning: {error}")
        self._append_log(f"> Discovery report saved: {report_path}")
        self._append_log("> No assets were extracted, copied, hashed, or modified.")
        self.step_status.set("Current task: Discovery complete ? reader integration required")
        self.current_line.set("Current dialogue: Mapping sources found. No dialogue pairs created yet.")
        self._append_log("> Select the desired target voice-over language for generated audio in the next window.")
        self.action_button.configure(text="Validate Internal Reader", command=self._validate_reader)
        self._open_language_picker(installed_languages)


import xtranslator_mapper
xtranslator_mapper.install(GameDubberApp)

if __name__ == "__main__":
    enable_high_dpi()
    GameDubberApp().mainloop()
