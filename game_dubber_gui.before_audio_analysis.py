from __future__ import annotations

import shutil
import subprocess
import sqlite3
import struct
import tkinter as tk
import ctypes
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_ROOT = Path(__file__).resolve().parent


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
        self.title("GameDubber")
        self.geometry("840x660")
        self.update_idletasks()
        window_width = self.winfo_width()
        window_height = self.winfo_height()
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        position_x = max(0, (screen_width - window_width) // 2)
        position_y = max(0, (screen_height - window_height) // 2)
        self.geometry(f"{window_width}x{window_height}+{position_x}+{position_y}")
        self.minsize(760, 560)
        self.game_path = tk.StringVar()
        self.target_language = tk.StringVar()
        self.gpu_status = tk.StringVar(value="GPU: Checking CUDA availability...")
        self.disk_status = tk.StringVar(value="Disk space: Select a game folder")
        self.overall_status = tk.StringVar(value="Overall: Ready")
        self.step_status = tk.StringVar(value="Current task: No task running")
        self.current_line = tk.StringVar(value="Current dialogue: —")
        self._build()
        self._detect_gpu()

    def _build(self) -> None:
        panel = ttk.Frame(self, padding=16)
        panel.grid(sticky="nsew")
        self.columnconfigure(0, weight=1); self.rowconfigure(0, weight=1)
        panel.columnconfigure(1, weight=1); panel.rowconfigure(9, weight=1)

        ttk.Label(panel, text="Game folder").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(panel, textvariable=self.game_path).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(panel, text="Browse...", command=self._choose_folder).grid(row=0, column=2)
        ttk.Label(panel, textvariable=self.gpu_status).grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 2))
        ttk.Label(panel, textvariable=self.disk_status).grid(row=2, column=0, columnspan=3, sticky="w", pady=2)

        ttk.Separator(panel).grid(row=3, column=0, columnspan=3, sticky="ew", pady=10)
        ttk.Label(panel, textvariable=self.overall_status).grid(row=4, column=0, columnspan=3, sticky="w")
        self.overall = ttk.Progressbar(panel, mode="determinate", maximum=4, value=0)
        self.overall.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(2, 8))
        ttk.Label(panel, textvariable=self.step_status).grid(row=6, column=0, columnspan=3, sticky="w")
        self.step = ttk.Progressbar(panel, mode="determinate", maximum=100, value=0)
        self.step.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(2, 8))
        ttk.Label(panel, textvariable=self.current_line).grid(row=8, column=0, columnspan=3, sticky="nw")

        ttk.Button(panel, text="Build Index", command=self._build_index).grid(row=9, column=0, columnspan=3, sticky="ew", pady=(12, 0))

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
            self.step_status.set(self._stage_task(6, f"Decoding sample {number} of {len(jobs)}"))
            self.current_line.set(f"Current dialogue: {job['dialogue_id']} | {job['official_subtitle'][:90]}")
            self.update_idletasks()
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
                self._append_log(f"> Decoded sample {number} of {len(jobs)} | {job['duration_ms']} ms")
            except Exception as error:
                job["status"] = "decode_failed"; job["decode_error"] = str(error)
                self._append_log(f"> Decode failed for sample {number}: {error}")
            self.step.configure(value=number)
            self.update_idletasks()
        jobs_path.write_text(''.join(json.dumps(job, ensure_ascii=False) + "\n" for job in jobs), encoding="utf-8")
        self._append_log(f"> WAV decoding complete | Ready: {ready} of {len(jobs)}")
        self.step_status.set(self._stage_task(6, f"complete | {ready} WAV samples ready"))
        self.current_line.set("Current dialogue: English WAV samples are ready for CPU audio analysis.")
        self.action_button.configure(text="Analyze 20 WAV Samples (CPU)", command=self._analyze_audio_test, state="normal")
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
        self.overall_status.set(self._stage_overall(7, "Analyze 20-line audio test"))
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
                self.step_status.set(self._stage_task(7, f"Analyzing sample {number} of {len(ready_jobs)}"))
                self.current_line.set(f"Current dialogue: {job['dialogue_id']} | {job['official_subtitle'][:90]}")
                self.update_idletasks()
                try:
                    metrics = self._analyze_wav_file(Path(job["workspace_wav_path"]))
                    row = {**job, **metrics, "status": "analysis_ready"}
                    connection.execute("INSERT OR REPLACE INTO audio_test_analysis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                        job["source_audio_path"], metrics["duration_ms"], metrics["sample_rate"], metrics["channels"],
                        metrics["rms_dbfs"], metrics["peak_dbfs"], metrics["leading_silence_ms"], metrics["trailing_silence_ms"],
                        metrics["internal_pause_count"], metrics["energy"], datetime.now(timezone.utc).isoformat()))
                    report.append(row); job.update(metrics); job["status"] = "analysis_ready"; completed += 1
                    self._append_log(f"> Analyzed sample {number} of {len(ready_jobs)} | {metrics['duration_ms']} ms | RMS {metrics['rms_dbfs']} dBFS")
                except Exception as error:
                    job["analysis_error"] = str(error)
                    self._append_log(f"> Analysis failed for sample {number}: {error}")
                self.step.configure(value=number)
                self.update_idletasks()
            connection.commit()
        finally:
            connection.close()
        (WORK_ROOT / "analysis").mkdir(parents=True, exist_ok=True)
        (WORK_ROOT / "analysis" / "audio_test_analysis.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in report), encoding="utf-8")
        jobs_path.write_text("".join(json.dumps(job, ensure_ascii=False) + "\n" for job in jobs), encoding="utf-8")
        self._append_log(f"> CPU audio analysis complete | Ready: {completed} of {len(ready_jobs)}")
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
            messagebox.showerror("Target language required", "Select a verified target subtitle language first.")
            return
        code = language.rsplit("(", 1)[1].rstrip(")").lower()
        reader = self._reader_path()
        report_path = WORK_ROOT / "discovery" / "dialogue_sources.json"
        if not reader.is_file() or not report_path.is_file():
            messagebox.showerror("Discovery required", "Run dialogue discovery and validate the internal reader first.")
            return
        report = json.loads(report_path.read_text(encoding="utf-8"))
        archives = [Path(item) for item in report["localization_archives"]]
        suffixes = (f"_{code}.strings", f"_{code}.dlstrings", f"_{code}.ilstrings")
        self.overall.configure(value=2)
        self.overall_status.set("Overall: Step 2 of 4 ? Build target subtitle index")
        self._append_log(f"> Building subtitle index for: {language}")
        self.step.configure(maximum=max(1, len(archives)), value=0)
        database_path = WORK_ROOT / "voice_pipeline.db"
        connection = sqlite3.connect(database_path)
        connection.execute("""CREATE TABLE IF NOT EXISTS official_subtitles (
            source_archive TEXT NOT NULL, source_revision TEXT NOT NULL, internal_path TEXT NOT NULL,
            language TEXT NOT NULL, file_kind TEXT NOT NULL, string_id INTEGER NOT NULL, text TEXT NOT NULL,
            workspace_path TEXT NOT NULL, content_sha256 TEXT NOT NULL,
            PRIMARY KEY (source_archive, source_revision, internal_path, string_id))""")
        extracted = skipped = indexed = 0
        try:
            for number, archive in enumerate(archives, 1):
                stats = archive.stat()
                revision = f"{stats.st_size:x}-{stats.st_mtime_ns:x}"[-24:]
                result = subprocess.run([str(reader), "list", str(archive)], capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=HIDDEN_PROCESS)
                if result.returncode:
                    self._append_log(f"> Reader error: {archive.name}: {result.stderr.strip()}")
                    continue
                entries = [line for line in result.stdout.splitlines() if line.lower().replace("\\", "/").endswith(suffixes)]
                self._append_log(f"> {archive.name}: {len(entries)} {code} subtitle files")
                for internal_path in entries:
                    destination = WORK_ROOT / "input" / "official_subtitles" / code / revision / archive.stem / Path(*internal_path.split("/"))
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
                    rows = [(str(archive), revision, internal_path, code, destination.suffix.lower(), string_id, text, str(destination), digest)
                            for string_id, text in self._parse_localized_strings(destination)]
                    connection.executemany("INSERT OR REPLACE INTO official_subtitles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
                    indexed += len(rows)
                connection.commit()
                self.step.configure(value=number)
                self.step_status.set(f"Current task: Indexing {archive.name} ({number} of {len(archives)})")
                self.update_idletasks()
        finally:
            connection.close()
        self._append_log(f"> Subtitle index complete | Extracted: {extracted} | Cached: {skipped} | Text entries: {indexed}")
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
            messagebox.showerror("Target language required", "Select and index a target subtitle language first.")
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
                self.update_idletasks()
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
            messagebox.showerror("Target language required", "Select a target subtitle language first.")
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
        self.current_line.set("Current dialogue: Voice map is ready for a 20-line audio test.")
        self.action_button.configure(text="Prepare 20-Line Audio Test: next integration", state="disabled")
    def _build_english_voice_manifest(self) -> None:
        """Index only original English WEM paths; do not extract audio in this stage."""
        report_path = WORK_ROOT / "discovery" / "dialogue_sources.json"
        reader = self._reader_path()
        if not report_path.is_file() or not reader.is_file():
            messagebox.showerror("Discovery required", "Run dialogue discovery and validate the internal reader first.")
            return
        report = json.loads(report_path.read_text(encoding="utf-8"))
        def is_english_voice(path: Path) -> bool:
            name = path.name.lower()
            return "voices_en" in name or name.startswith("starfield - voices")
        archives = [Path(item) for item in report["voice_archives"] if is_english_voice(Path(item))]
        database_path = WORK_ROOT / "voice_pipeline.db"
        manifest_dir = WORK_ROOT / "manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_dir / "english_voice_manifest.jsonl"
        temporary_path = manifest_path.with_suffix(".jsonl.tmp")
        self.overall.configure(value=3)
        self.overall_status.set("Overall: Step 3 of 4 ? Build English voice manifest")
        self.step.configure(maximum=max(1, len(archives)), value=0)
        self._append_log(f"> Building English voice manifest from {len(archives)} archives...")
        connection = sqlite3.connect(database_path)
        connection.execute("""CREATE TABLE IF NOT EXISTS english_voice_assets (
            source_archive TEXT NOT NULL, source_revision TEXT NOT NULL, internal_path TEXT NOT NULL,
            game_plugin TEXT NOT NULL, speaker_key TEXT NOT NULL, file_name TEXT NOT NULL,
            audio_format TEXT NOT NULL, source_audio_language TEXT NOT NULL, status TEXT NOT NULL,
            indexed_at TEXT NOT NULL, PRIMARY KEY (source_archive, source_revision, internal_path))""")
        total = cached_archives = 0
        try:
            with temporary_path.open("w", encoding="utf-8", newline="\n") as handle:
                for number, archive in enumerate(archives, 1):
                    stats = archive.stat()
                    revision = f"{stats.st_size:x}-{stats.st_mtime_ns:x}"[-24:]
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
                    self.update_idletasks()
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
        self._append_log(f"> Selected target subtitle language: {language}")
        self.current_line.set(f"Current dialogue: Target language selected ? {language}")
        self.action_button.configure(text="Build Target Subtitle Index", command=self._build_target_subtitle_index)
        dialog.destroy()


    def _open_language_picker(self, languages: list[str]) -> None:
        """Open a dedicated language chooser after discovery completes."""
        dialog = tk.Toplevel(self)
        dialog.title("Select Target Subtitle Language")
        dialog.transient(self)
        dialog.resizable(False, False)
        panel = ttk.Frame(dialog, padding=16)
        panel.grid(sticky="nsew")
        ttk.Label(panel, text="Target subtitle language").grid(row=0, column=0, sticky="w")
        selected = tk.StringVar(value=languages[0] if languages else "Language detection requires the internal BA2 reader")
        chooser = ttk.Combobox(panel, textvariable=selected, values=languages, state="readonly" if languages else "disabled", width=56)
        chooser.grid(row=1, column=0, sticky="ew", pady=(6, 10))
        if languages:
            ttk.Button(panel, text="Use Selected Language", command=lambda: self._set_target_language(selected.get(), dialog)).grid(row=2, column=0, sticky="ew")
        else:
            ttk.Label(panel, text="Installed subtitle languages will be listed here after the internal BA2 reader has inspected the localization archives.", wraplength=440).grid(row=2, column=0, sticky="w")
            ttk.Button(panel, text="Close", command=dialog.destroy).grid(row=3, column=0, sticky="ew", pady=(12, 0))
        dialog.update_idletasks()
        self.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dialog.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dialog.winfo_reqheight()) // 2
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
        dialog.grab_set()

    def _detect_gpu(self) -> None:
        """Read NVIDIA driver telemetry without loading PyTorch or any AI model."""
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, check=True, timeout=5, creationflags=HIDDEN_PROCESS,
            )
            name, total_mb, used_mb = [item.strip() for item in result.stdout.splitlines()[0].split(",")]
            total_gb, used_gb = int(total_mb) / 1024, int(used_mb) / 1024
            minimum = "OK" if total_gb >= 8 else "Below 8 GB minimum"
            self.gpu_status.set(
                f"GPU: {name} | Total: {total_gb:.1f} GB | In use: {used_gb:.2f} GB | "
                f"GameDubber allocated: 0.00 GB | {minimum}"
            )
        except (FileNotFoundError, subprocess.SubprocessError, IndexError, ValueError):
            self.gpu_status.set("GPU: NVIDIA CUDA driver not detected (minimum: 8 GB VRAM)")

    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory(title="Select Starfield game folder")
        if not selected: return
        self.game_path.set(selected)
        folder = Path(selected)
        free = shutil.disk_usage(folder).free / 1024 ** 3
        valid = (folder / "Data").is_dir()
        self.disk_status.set(f"Disk space: {free:.1f} GB free | Data folder: {'Found' if valid else 'Not found'}")

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
        self.overall_status.set("Overall: Step 2 of 4 ? Validate local asset reader")
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
        self.overall_status.set("Overall: Step 1 of 4 ? Discover dialogue mapping")
        self._append_log("> Starting read-only dialogue source discovery...")
        self.step.configure(maximum=3, value=0)
        plugins = sorted(path for path in data_root.rglob("*") if path.suffix.lower() in {".esm", ".esp", ".esl"})
        self.step.configure(value=1); self.step_status.set("Current task: Discovering game plugins"); self.update_idletasks()
        localization_files = sorted(path for path in data_root.rglob("*") if path.suffix.lower() in {".strings", ".dlstrings", ".ilstrings"})
        self.step.configure(value=2); self.step_status.set("Current task: Discovering loose localization files"); self.update_idletasks()
        archives = sorted(data_root.rglob("*.ba2"))
        voice_archives = [path for path in archives if "voice" in path.name.lower()]
        localization_archives = [path for path in archives if any(token in path.name.lower() for token in ("localization", "string", "interface"))]
        self.step.configure(value=3); self.step_status.set("Current task: Reading installed subtitle languages"); self.update_idletasks()
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
        for relative in ("discovery", "input/official_subtitles", "samples/original_en", "output/italian_voice", "reports"):
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
        self._append_log("> Select the desired target subtitle language in the next window.")
        self.action_button.configure(text="Validate Internal Reader", command=self._validate_reader)
        self._open_language_picker(installed_languages)


if __name__ == "__main__":
    enable_high_dpi()
    GameDubberApp().mainloop()
