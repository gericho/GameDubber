"""Starfield voice mapper following xTranslator's FuzMap key."""
import json
import mmap
import os
import random
import re
import secrets
import subprocess
import sys
import shutil
import sqlite3
import struct
import tkinter as tk
import threading
import wave
import zlib
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk


def _find_local_python_runtime() -> Path:
    """Find a user-local Python runtime without embedding a user profile path."""
    candidates: list[Path] = []
    current = Path(sys.executable)
    if current.name.lower().startswith('python'):
        candidates.append(current)
    path_python = shutil.which('python')
    if path_python:
        candidates.append(Path(path_python))
    local_app_data = os.environ.get('LOCALAPPDATA', '')
    if local_app_data:
        candidates.extend(sorted(
            (Path(local_app_data) / 'Programs' / 'Python').glob('Python*/python.exe'),
            reverse=True,
        ))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return Path()


def _cuda_package_bin_paths() -> list[str]:
    """Locate NVIDIA pip-package DLL folders independently of the Windows user."""
    roots: list[Path] = [Path(sys.prefix), Path(sys.base_prefix)]
    local_app_data = os.environ.get('LOCALAPPDATA', '')
    if local_app_data:
        roots.extend((Path(local_app_data) / 'Programs' / 'Python').glob('Python*'))
    paths: list[str] = []
    for root in roots:
        nvidia_root = root / 'Lib' / 'site-packages' / 'nvidia'
        if not nvidia_root.is_dir():
            continue
        for bin_dir in nvidia_root.glob('*/bin'):
            value = str(bin_dir)
            if bin_dir.is_dir() and value not in paths:
                paths.append(value)
    return paths


def _masters(plugin: Path) -> list[str]:
    with plugin.open('rb') as f:
        head = f.read(24)
        if len(head) < 24 or head[:4] != b'TES4': return []
        data = f.read(struct.unpack_from('<I', head, 4)[0])
    ans=[]; p=0
    while p+6 <= len(data):
        tag=data[p:p+4]; n=struct.unpack_from('<H',data,p+4)[0]; p+=6
        if tag == b'XXXX' and n == 4 and p+10 <= len(data):
            n=struct.unpack_from('<I',data,p)[0]; p+=4; tag=data[p:p+4]; p+=6
        if p+n > len(data): break
        value=data[p:p+n]; p+=n
        if tag == b'MAST': ans.append(value.split(b'\0',1)[0].decode('utf-8','replace').lower())
    return ans


def iter_info(plugin: Path):
    """Yield (INFO, master plugin, response, TRDA audio id, NAM1 string id)."""
    masters=_masters(plugin)
    with plugin.open('rb') as f:
        data=mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ)
        try:
            off=0; limits=[len(data)]
            while off+24 <= len(data):
                while limits and off >= limits[-1]: limits.pop()
                if not limits: break
                sig=data[off:off+4]; size=struct.unpack_from('<I',data,off+4)[0]
                total=size if sig == b'GRUP' else 24+size
                if total < 24 or off+total > limits[-1]: break
                if sig == b'GRUP': limits.append(off+size); off+=24; continue
                if sig != b'INFO': off+=total; continue
                flags=struct.unpack_from('<I',data,off+8)[0]; form=struct.unpack_from('<I',data,off+12)[0]
                index=(form>>24)&255; master=masters[index-1] if 0<index<=len(masters) else plugin.name.lower()
                payload=data[off+24:off+total]
                if flags & 0x40000:
                    try: payload=zlib.decompress(payload[4:]) if len(payload)>=4 else b''
                    except zlib.error: payload=b''
                p=0; response=0; voice=''
                while p+6 <= len(payload):
                    tag=payload[p:p+4]; n=struct.unpack_from('<H',payload,p+4)[0]; p+=6
                    if tag == b'XXXX' and n == 4 and p+10 <= len(payload):
                        n=struct.unpack_from('<I',payload,p)[0]; p+=4; tag=payload[p:p+4]; p+=6
                    if p+n > len(payload): break
                    value=payload[p:p+n]; p+=n
                    if tag == b'TRDA' and len(value)>=8: voice=f"{struct.unpack_from('<I',value,4)[0]&0x00ffffff:08x}"
                    elif tag == b'NAM1' and len(value)==4:
                        response+=1
                        if voice: yield form,master,response,voice,struct.unpack_from('<I',value)[0]
                off+=total
        finally: data.close()


def _production_runs_under_output(root: Path) -> list[Path]:
    """Return production run folders which must never be silently discarded."""
    output = root / 'output'
    if not output.is_dir():
        return []
    return sorted((path for path in output.glob('*_voice/runs/run-*') if path.is_dir()), key=lambda path: path.name)


def _choose_reset_production_handling(self, runs: list[Path]) -> str:
    """Require an explicit archive/delete/cancel choice for production runs."""
    dialog = tk.Toplevel(self)
    dialog.title('Production sessions found')
    dialog.transient(self)
    dialog.resizable(False, False)
    answer = {'choice': 'cancel'}
    panel = ttk.Frame(dialog, padding=18)
    panel.grid(sticky='nsew')
    names = '\n'.join(f'• {path.name}' for path in runs[:5])
    if len(runs) > 5:
        names += f'\n• … and {len(runs) - 5} more'
    ttk.Label(
        panel,
        text=(
            f'{len(runs)} production session(s) were found:\n{names}\n\n'
            'Archive session: move the completed/paused run outside work so Reset cannot delete it.\n'
            'Delete session: permanently remove it together with the rest of the local pipeline data.'
        ),
        justify='left', wraplength=610,
    ).grid(row=0, column=0, columnspan=3, sticky='w')

    def choose(choice: str) -> None:
        answer['choice'] = choice
        dialog.destroy()

    ttk.Button(panel, text='Archive session', command=lambda: choose('archive')).grid(row=1, column=0, sticky='ew', padx=(0, 8), pady=(18, 0))
    ttk.Button(panel, text='Delete session', command=lambda: choose('delete')).grid(row=1, column=1, sticky='ew', padx=(0, 8), pady=(18, 0))
    ttk.Button(panel, text='Cancel reset', command=lambda: choose('cancel')).grid(row=1, column=2, sticky='ew', pady=(18, 0))
    for column in range(3):
        panel.columnconfigure(column, weight=1)
    dialog.protocol('WM_DELETE_WINDOW', lambda: choose('cancel'))
    dialog.update_idletasks()
    x = self.winfo_rootx() + max(0, (self.winfo_width() - dialog.winfo_reqwidth()) // 2)
    y = self.winfo_rooty() + max(0, (self.winfo_height() - dialog.winfo_reqheight()) // 2)
    dialog.geometry(f'+{x}+{y}')
    dialog.grab_set()
    dialog.wait_window()
    return str(answer['choice'])


def _archive_production_runs(root: Path, runs: list[Path]) -> list[Path]:
    """Move runs out of work and preserve their checkpoint/index information."""
    archive_root = root.parent / 'saved_production_sessions'
    archive_root.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    state_path = root / 'production_resume.json'
    try:
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.is_file() else {}
    except (OSError, ValueError, TypeError):
        state = {}
    saved_run = str(state.get('run_dir', ''))
    for run in runs:
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        destination = archive_root / f'{stamp}-{run.name}'
        serial = 2
        while destination.exists():
            destination = archive_root / f'{stamp}-{run.name}-{serial}'
            serial += 1
        shutil.move(str(run), str(destination))
        if saved_run and Path(saved_run) == run and state_path.is_file():
            shutil.move(str(state_path), str(destination / 'production_resume_saved.json'))
        saved.append(destination)
    if saved:
        index = archive_root / 'INDEX.md'
        with index.open('a', encoding='utf-8', newline='\n') as handle:
            for destination in saved:
                handle.write(f'\n- Archived by Reset Local Pipeline Data: `{destination.name}`\n')
    return saved


def reset(self):
    process = getattr(self, '_full_batch_process', None)
    is_active = bool(process is not None and process.poll() is None)
    if is_active:
        messagebox.showinfo('Reset unavailable', 'Stop the active production batch before resetting local pipeline data.', parent=self)
        return
    # A completed/failed child must never leave the reset button blocked.
    self._full_batch_running = False
    root = __import__('game_dubber_gui').WORK_ROOT
    named = ('voice_pipeline.db', 'voice_pipeline.db-shm', 'voice_pipeline.db-wal', 'discovery', 'input', 'manifests', 'samples', 'analysis', 'asr', 'output', 'production_resume.json')
    if not messagebox.askyesno('Reset local pipeline data', 'This removes all local pipeline data under work, including generated target-language WAV output and previous production runs.\n\nIt does not touch the game installation, work\\models, or work\\logs. Continue?', parent=self):
        self._append_log('> Clean rebuild cancelled; existing local pipeline data was kept.')
        return
    runs = _production_runs_under_output(root)
    if runs:
        handling = _choose_reset_production_handling(self, runs)
        if handling == 'cancel':
            self._append_log('> Local pipeline reset cancelled; production sessions were kept.')
            return
        if handling == 'archive':
            try:
                archived = _archive_production_runs(root, runs)
                self._append_log('> Production sessions archived outside work: ' + ', '.join(str(path) for path in archived))
            except OSError as error:
                messagebox.showerror('Archive failed', f'No pipeline data was reset.\n\nCould not archive production sessions:\n{error}', parent=self)
                return
    removed, failures = [], []
    for name in named:
        path = root / name
        try:
            if path.is_dir():
                shutil.rmtree(path)
                removed.append(name)
            elif path.is_file():
                path.unlink()
                removed.append(name)
        except OSError as error:
            failures.append(f'{name}: {error}')
    _clear_production_resume_state()
    if failures:
        detail = '\n'.join(failures)
        self._append_log('> Local pipeline reset incomplete | ' + detail)
        messagebox.showerror('Reset incomplete', f'Some local pipeline folders could not be removed:\n\n{detail}', parent=self)
        return
    self.target_language.set('')
    self.overall.configure(value=0)
    self.step.configure(value=0)
    self.overall_status.set('Overall: Ready to discover Starfield sources')
    self.step_status.set('Current task: Local pipeline reset complete')
    self.current_line.set('Current dialogue: No source files have been processed.')
    self.action_button.configure(text='Start discovery', command=self._discover_mapping, state='normal')
    self._append_log('> Local pipeline reset complete | Removed: ' + (', '.join(removed) if removed else 'no prior pipeline outputs'))
    self._append_log('> Preserved: game files (read only), work\\models, work\\logs.')

def build_dialogue(self):
    from game_dubber_gui import WORK_ROOT
    language=self.target_language.get().strip()
    if not language or '(' not in language: messagebox.showerror('Target language required','Select and index a target voice-over language first.'); return
    code=language.rsplit('(',1)[1].rstrip(')').lower(); report=WORK_ROOT/'discovery'/'dialogue_sources.json'
    if not report.is_file(): messagebox.showerror('Discovery required','Run dialogue discovery first.'); return
    plugins=[Path(x) for x in json.loads(report.read_text(encoding='utf-8')).get('plugins',[])]
    db=WORK_ROOT/'voice_pipeline.db'; manifest=WORK_ROOT/'manifests'/'dialogue_text_index.jsonl'
    self.overall.configure(value=4); self.overall_status.set(self._stage_overall(4,'Build xTranslator-compatible dialogue index')); self.step.configure(maximum=max(1,len(plugins)),value=0)
    self._append_log(f'> Building INFO/TRDA dialogue index from {len(plugins)} plugins...')
    con=sqlite3.connect(db)
    con.execute('CREATE TABLE IF NOT EXISTS dialogue_voice_records_v2 (source_plugin TEXT, source_revision TEXT, master_plugin TEXT, info_form_id TEXT, response_number INTEGER, voice_id TEXT, string_id INTEGER, target_language TEXT, english_text TEXT, target_text TEXT, mapping_status TEXT, PRIMARY KEY(source_plugin,source_revision,info_form_id,response_number,target_language))')
    def dialogue_texts(language: str) -> dict[tuple[str, int], str]:
        values: dict[tuple[str, int], str] = {}
        for internal_path, string_id, text in con.execute("SELECT internal_path,string_id,text FROM official_subtitles WHERE language=? AND file_kind='.ilstrings'", (language,)):
            resource_name = Path(internal_path).name.lower().rsplit('_', 1)[0]
            values[(resource_name, string_id)] = text
        return values
    target = dialogue_texts(code); english = dialogue_texts('en')
    count=missing=0
    try:
        for number,plugin in enumerate(plugins,1):
            if not plugin.is_file():
                self._append_log(f'> Skipped missing plugin: {plugin}')
                self.step.configure(value=number); self._refresh_progress_bars(); continue
            st=plugin.stat(); rev=f'{st.st_size:x}-{st.st_mtime_ns:x}'[-24:]; con.execute('DELETE FROM dialogue_voice_records_v2 WHERE source_plugin=?',(str(plugin),)); rows=[]
            for form,master,response,voice,sid in iter_info(plugin):
                resource_name = Path(master).stem.lower()
                text_key = (resource_name, sid)
                if text_key not in english or text_key not in target:
                    missing += 1; continue
                rows.append((str(plugin),rev,master,f'{form:08x}',response,voice,sid,code,english[text_key],target[text_key],'xtranslator_trda'))
            con.executemany('INSERT OR REPLACE INTO dialogue_voice_records_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?)',rows); con.commit(); count+=len(rows)
            self._append_log(f'> Indexed plugin: {plugin.name} | {len(rows)} TRDA-linked subtitle responses'); self.step.configure(value=number); self.step_status.set(self._stage_task(4,f'Reading {plugin.name} ({number} of {len(plugins)})')); self._refresh_progress_bars()
        tmp=manifest.with_suffix('.jsonl.tmp')
        with tmp.open('w',encoding='utf-8',newline='\n') as h:
            for r in con.execute('SELECT source_plugin,source_revision,master_plugin,info_form_id,response_number,voice_id,string_id,target_language,english_text,target_text,mapping_status FROM dialogue_voice_records_v2 WHERE target_language=? ORDER BY source_plugin,info_form_id,response_number',(code,)):
                h.write(json.dumps({'source_plugin':r[0],'source_revision':r[1],'master_plugin':r[2],'dialogue_id':r[3],'response_number':r[4],'voice_id':r[5],'string_id':r[6],'target_language':r[7],'english_subtitle':r[8],'official_subtitle':r[9],'mapping_status':r[10]},ensure_ascii=False)+'\n')
        tmp.replace(manifest)
    finally: con.close()
    self._append_log(f'> Dialogue index complete | TRDA-linked: {count} | Missing bilingual text: {missing}'); self._append_log(f'> Manifest saved: {manifest}'); self._append_log('> Audio key: TRDA voice ID + master plugin + response 1 (xTranslator FuzMap rule).')
    self.step_status.set(self._stage_task(4,f'complete | {count} TRDA-linked responses')); self.current_line.set('Current dialogue: English and target voice-over text is paired by StringID; audio map is ready.'); self.action_button.configure(text='Build xTranslator Voice Map',command=self._build_voice_map)


def build_map(self):
    from game_dubber_gui import WORK_ROOT
    language=self.target_language.get().strip()
    if not language or '(' not in language: messagebox.showerror('Target language required','Select a target voice-over language first.'); return
    code=language.rsplit('(',1)[1].rstrip(')').lower(); db=WORK_ROOT/'voice_pipeline.db'; manifest=WORK_ROOT/'manifests'/'voice_map.jsonl'; con=sqlite3.connect(db)
    self.overall.configure(value=5); self.overall_status.set(self._stage_overall(5,'Build xTranslator voice map'))
    con.execute('CREATE TABLE IF NOT EXISTS voice_map_entries_v2 (source_archive TEXT,source_audio_path TEXT,speaker_key TEXT,dialogue_plugin TEXT,dialogue_id TEXT,response_number INTEGER,voice_id TEXT,master_plugin TEXT,string_id INTEGER,target_language TEXT,english_text TEXT,target_text TEXT,mapping_status TEXT,mapping_confidence REAL,PRIMARY KEY(source_audio_path,dialogue_id,target_language))')
    total=con.execute('SELECT COUNT(*) FROM english_voice_assets').fetchone()[0]; self.step.configure(maximum=max(1,total),value=0); self.step_status.set(self._stage_task(5, f'Preparing {total} English voice assets')); self._refresh_progress_bars(); self._append_log(f'> Building xTranslator voice map from {total} English voice assets...')
    try:
        con.execute('DELETE FROM voice_map_entries_v2 WHERE target_language=?',(code,)); con.execute("INSERT OR REPLACE INTO voice_map_entries_v2 SELECT v.source_archive,v.internal_path,v.speaker_key,d.source_plugin,d.info_form_id,d.response_number,d.voice_id,d.master_plugin,d.string_id,d.target_language,d.english_text,d.target_text,'xtranslator_exact',1.0 FROM english_voice_assets v JOIN dialogue_voice_records_v2 d ON lower(substr(v.file_name,1,instr(v.file_name||'.','.')-1))=d.voice_id AND lower(v.game_plugin)=d.master_plugin WHERE d.target_language=?",(code,)); con.commit(); count=con.execute('SELECT COUNT(*) FROM voice_map_entries_v2 WHERE target_language=?',(code,)).fetchone()[0]; self.step.configure(value=total)
        tmp=manifest.with_suffix('.jsonl.tmp')
        with tmp.open('w',encoding='utf-8',newline='\n') as h:
            for r in con.execute('SELECT source_archive,source_audio_path,speaker_key,dialogue_plugin,dialogue_id,response_number,voice_id,master_plugin,string_id,target_language,english_text,target_text,mapping_status,mapping_confidence FROM voice_map_entries_v2 WHERE target_language=? ORDER BY source_audio_path',(code,)):
                h.write(json.dumps({'original_archive_path':r[0],'source_audio_path':r[1],'speaker_id':r[2],'dialogue_plugin':r[3],'dialogue_id':r[4],'response_number':r[5],'voice_id':r[6],'master_plugin':r[7],'string_id':r[8],'target_language':r[9],'english_subtitle':r[10],'official_subtitle':r[11],'mapping_status':r[12],'mapping_confidence':r[13]},ensure_ascii=False)+'\n')
        tmp.replace(manifest)
    finally: con.close()
    self._append_log(f'> xTranslator voice map complete | Exact FuzMap entries: {count}'); self._append_log(f'> Manifest saved: {manifest}'); self._append_log('> No filename-only FormID matches and no heuristic cross-products were used.')
    self.step_status.set(self._stage_task(5,f'complete | {count} exact FuzMap entries')); self.current_line.set('Current dialogue: Voice map is ready for validation samples.'); self.action_button.configure(text='Prepare Validation Samples',command=self._prepare_wav_test)





def prepare_initial_production_batch(self):
    """Create a small, reproducible local batch; no audio is extracted in this step."""
    from game_dubber_gui import WORK_ROOT
    source = WORK_ROOT / 'manifests' / 'voice_map.jsonl'
    destination = WORK_ROOT / 'manifests' / 'initial_production_batch.jsonl'
    if not source.is_file():
        messagebox.showerror('Voice map required', 'Build and validate the xTranslator voice map before preparing a production batch.', parent=self); return
    unique: list[dict[str, object]] = []
    seen: set[str] = set()
    for line in source.read_text(encoding='utf-8').splitlines():
        row = json.loads(line)
        path_key = str(row.get('source_audio_path', '')).lower()
        if row.get('mapping_status') == 'xtranslator_exact' and path_key and path_key not in seen:
            seen.add(path_key); unique.append(row)
    if not unique:
        messagebox.showerror('No validated audio', 'No exact voice mappings are available for the initial production batch.', parent=self); return
    seed = secrets.randbits(64)
    selected = random.Random(seed).sample(unique, min(100, len(unique)))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('w', encoding='utf-8', newline='\n') as handle:
        for row in selected:
            entry = {**row, 'batch_kind':'initial_production', 'batch_seed':seed, 'batch_status':'selected'}
            handle.write(json.dumps(entry, ensure_ascii=False)+'\n')
    self.overall.configure(value=8); self.overall_status.set(self._stage_overall(8, 'Prepare initial production batch'))
    self.step.configure(maximum=1, value=1); self.step_status.set(self._stage_task(8, 'initial production batch manifest ready'))
    self.current_line.set('Current dialogue: Initial production batch is ready for controlled extraction.')
    self._append_log(f'> Initial production batch manifest created | Seed: {seed}')
    self._append_log(f'> Manifest saved: {destination}')
    self._append_log('> No audio was extracted in this step.')
    self._refresh_progress_bars()
    self.action_button.configure(text='Extract Initial Production Audio', command=self._extract_initial_production_audio, state='normal')

def extract_initial_production_audio(self):
    """Extract only the selected production WEM files into the workspace."""
    from game_dubber_gui import WORK_ROOT, HIDDEN_PROCESS
    reader = self._reader_path()
    manifest = WORK_ROOT / 'manifests' / 'initial_production_batch.jsonl'
    if not reader.is_file() or not manifest.is_file():
        messagebox.showerror('Initial production batch required', 'Prepare the initial production batch before extracting its audio.', parent=self); return
    rows = [json.loads(line) for line in manifest.read_text(encoding='utf-8').splitlines() if line]
    self.overall.configure(value=8); self.overall_status.set(self._stage_overall(8, 'Extract initial production audio'))
    self.step.configure(maximum=max(1, len(rows)), value=0); self.step_status.set(self._stage_task(8, 'Extracting initial production audio'))
    self.action_button.configure(state='disabled'); self._refresh_progress_bars()
    self._append_log('> Extracting initial production audio into the local workspace...')
    extracted = cached = failed = 0
    for number, row in enumerate(rows, 1):
        internal_path = row['source_audio_path'].replace('\\', '/').lstrip('/')
        destination = WORK_ROOT / 'samples' / 'initial_production' / Path(*internal_path.split('/'))
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file() and destination.stat().st_size > 0:
            status = 'cached'; cached += 1
        else:
            result = subprocess.run([str(reader), 'extract', row['original_archive_path'], row['source_audio_path'], str(destination)], capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=HIDDEN_PROCESS)
            status = 'extracted' if result.returncode == 0 and destination.is_file() and destination.stat().st_size > 0 else 'failed'
            if status == 'extracted': extracted += 1
            else:
                failed += 1
                self._append_log(f"> Initial production extraction error: {internal_path}: {result.stderr.strip() or result.stdout.strip()}")
        row['workspace_wem_path'] = str(destination)
        row['batch_status'] = status
        self.step.configure(value=number); self.step_status.set(self._stage_task(8, 'Extracting initial production audio')); self._refresh_progress_bars()
    temporary = manifest.with_suffix('.jsonl.tmp')
    temporary.write_text(''.join(json.dumps(row, ensure_ascii=False)+'\n' for row in rows), encoding='utf-8')
    temporary.replace(manifest)
    self._append_log(f'> Initial production audio extraction complete | Extracted: {extracted} | Cached: {cached} | Failed: {failed}')
    self._append_log(f'> Manifest updated: {manifest}')
    self.step_status.set(self._stage_task(8, 'initial production audio extracted'))
    self.current_line.set('Current dialogue: Initial production audio is ready for CPU WAV conversion.')
    self.action_button.configure(text='Decode Initial Production Audio to WAV (CPU)', command=self._decode_initial_production_audio, state='normal')

def decode_initial_production_audio(self):
    """Decode extracted production WEM files locally, one at a time, using CPU only."""
    from game_dubber_gui import WORK_ROOT, HIDDEN_PROCESS
    decoder = self._decoder_path()
    manifest = WORK_ROOT / 'manifests' / 'initial_production_batch.jsonl'
    if not decoder.is_file() or not manifest.is_file():
        messagebox.showerror('Production audio unavailable', 'The bundled WEM decoder or initial production manifest is unavailable.', parent=self); return
    rows = [json.loads(line) for line in manifest.read_text(encoding='utf-8').splitlines() if line]
    ready_rows = [row for row in rows if Path(str(row.get('workspace_wem_path', ''))).is_file()]
    if not ready_rows:
        messagebox.showerror('Extracted audio required', 'Extract the initial production audio before converting it to WAV.', parent=self); return
    self.overall.configure(value=8); self.overall_status.set(self._stage_overall(8, 'Decode initial production audio to WAV'))
    self.step.configure(maximum=max(1, len(ready_rows)), value=0); self.step_status.set(self._stage_task(8, 'Decoding initial production audio on CPU'))
    self.current_line.set('Current dialogue: Preparing local WEM to WAV conversion.')
    self.action_button.configure(state='disabled'); self._refresh_progress_bars()
    self._append_log('> Decoding initial production audio to WAV on CPU only; CUDA models are not loaded.')
    decoded = cached = failed = 0
    for number, row in enumerate(ready_rows, 1):
        source = Path(str(row['workspace_wem_path']))
        output = source.with_suffix('.wav')
        self.step.configure(value=number - 1)
        self.step_status.set(self._stage_task(8, 'Decoding initial production audio on CPU'))
        self.current_line.set(f"Current dialogue: {row.get('dialogue_id', '')} | {str(row.get('official_subtitle', ''))[:90]}")
        self._refresh_progress_bars()
        try:
            if output.is_file() and output.stat().st_size > 0:
                cached += 1
            else:
                result = subprocess.run([str(decoder), '-o', str(output), str(source)], capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=HIDDEN_PROCESS)
                if result.returncode or not output.is_file() or output.stat().st_size == 0:
                    raise RuntimeError(result.stderr.strip() or result.stdout.strip() or 'decoder produced no WAV output')
                decoded += 1
            with wave.open(str(output), 'rb') as audio:
                row['duration_ms'] = round(audio.getnframes() * 1000 / audio.getframerate())
                row['sample_rate'] = audio.getframerate()
                row['channels'] = audio.getnchannels()
            row['workspace_wav_path'] = str(output); row['batch_status'] = 'wav_ready'
            self._append_log(f"> Decoded initial production WAV | {row['duration_ms']} ms")
        except Exception as error:
            row['batch_status'] = 'decode_failed'; row['decode_error'] = str(error); failed += 1
            self._append_log(f'> Initial production WAV decode failed: {source.name}: {error}')
        self.step.configure(value=number); self._refresh_progress_bars()
    temporary = manifest.with_suffix('.jsonl.tmp')
    temporary.write_text(''.join(json.dumps(row, ensure_ascii=False)+'\n' for row in rows), encoding='utf-8')
    temporary.replace(manifest)
    self._append_log(f'> Initial production WAV conversion complete | Decoded: {decoded} | Cached: {cached} | Failed: {failed}')
    self._append_log(f'> Manifest updated: {manifest}')
    self.step_status.set(self._stage_task(8, 'initial production WAV files ready'))
    self.current_line.set('Current dialogue: Initial production WAV files are ready for voice-reference analysis.')
    self.action_button.configure(text='Analyze Initial Production WAV References (CPU)', command=self._analyze_initial_production_audio, state='normal')

def analyze_initial_production_audio(self):
    """Measure local reference WAVs on CPU before any future synthesis step."""
    from game_dubber_gui import WORK_ROOT
    manifest = WORK_ROOT / 'manifests' / 'initial_production_batch.jsonl'
    if not manifest.is_file():
        messagebox.showerror('Production WAVs unavailable', 'The initial production manifest is unavailable.', parent=self); return
    rows = [json.loads(line) for line in manifest.read_text(encoding='utf-8').splitlines() if line]
    ready_rows = [row for row in rows if Path(str(row.get('workspace_wav_path', ''))).is_file()]
    if not ready_rows:
        messagebox.showerror('WAV files required', 'Convert the initial production WEM files to WAV before analyzing them.', parent=self); return
    self.overall.configure(value=8); self.overall_status.set(self._stage_overall(8, 'Analyze initial production WAV references'))
    self.step.configure(maximum=max(1, len(ready_rows)), value=0); self.step_status.set(self._stage_task(8, 'Analyzing initial production WAV references on CPU'))
    self.current_line.set('Current dialogue: Preparing local voice-reference analysis.')
    self.action_button.configure(state='disabled'); self._refresh_progress_bars()
    self._append_log('> Analyzing initial production WAV references on CPU only; CUDA models are not loaded.')
    analyzed = failed = 0
    for number, row in enumerate(ready_rows, 1):
        source = Path(str(row['workspace_wav_path']))
        self.step.configure(value=number - 1)
        self.step_status.set(self._stage_task(8, 'Analyzing initial production WAV references on CPU'))
        self.current_line.set(f"Current dialogue: {row.get('dialogue_id', '')} | {str(row.get('official_subtitle', ''))[:90]}")
        self._refresh_progress_bars()
        try:
            measurements = self._analyze_wav_file(source)
            row['audio_analysis'] = measurements
            row.update({key: measurements[key] for key in ('duration_ms', 'sample_rate', 'channels')})
            row['batch_status'] = 'reference_analyzed'
            analyzed += 1
            self._append_log(f"> Analyzed initial production reference | {measurements['duration_ms']} ms | RMS {measurements['rms_dbfs']} dBFS")
        except Exception as error:
            row['batch_status'] = 'analysis_failed'; row['analysis_error'] = str(error); failed += 1
            self._append_log(f'> Initial production reference analysis failed: {source.name}: {error}')
        self.step.configure(value=number); self._refresh_progress_bars()
    temporary = manifest.with_suffix('.jsonl.tmp')
    temporary.write_text(''.join(json.dumps(row, ensure_ascii=False)+'\n' for row in rows), encoding='utf-8')
    temporary.replace(manifest)
    self._append_log(f'> Initial production reference analysis complete | Analyzed: {analyzed} | Failed: {failed}')
    self._append_log(f'> Manifest updated: {manifest}')
    self.step_status.set(self._stage_task(8, 'initial production voice references analyzed'))
    self.current_line.set('Current dialogue: Voice-reference measurements are ready for controlled synthesis setup.')
    self.action_button.configure(text='Initial Production References Analyzed', state='disabled')
def show_asr_report(self):
    from game_dubber_gui import WORK_ROOT
    output = WORK_ROOT / 'asr' / 'audio_test_transcripts.jsonl'
    if not output.is_file():
        messagebox.showerror('ASR report unavailable', 'Run CUDA ASR validation before opening its report.', parent=self); return
    rows = [json.loads(line) for line in output.read_text(encoding='utf-8').splitlines() if line]
    complete = [row for row in rows if row.get('asr_status') == 'complete']
    scores = [float(row.get('english_overlap', 0)) for row in complete]
    average = sum(scores) / max(1, len(scores))
    matched = sum(score >= 0.55 for score in scores)
    review = sum(0.30 <= score < 0.55 for score in scores)
    mismatch = sum(score < 0.30 for score in scores)
    messagebox.showinfo('ASR matching report', f'CUDA ASR validation completed.\n\nMean English matching: {average * 100:.1f}%\nGood result (>= 55%): {matched}\nReview (30%–54%): {review}\nMismatch (< 30%): {mismatch}\n\nNote: Whisper is not infallible. This score is a tolerant lexical overlap; 55% or more is considered a good result, while lower scores require review rather than being automatic failures.\n\nDetailed report:\n{output}', parent=self)
    self.action_button.configure(text='Prepare Initial Production Batch', command=self._prepare_initial_production_batch, state='normal')
def run_asr_validation(self):
    from game_dubber_gui import WORK_ROOT, HIDDEN_PROCESS
    jobs = WORK_ROOT / 'manifests' / 'audio_test_jobs.jsonl'
    model = WORK_ROOT / 'models' / 'whisper-large-v3-turbo'
    output = WORK_ROOT / 'asr' / 'audio_test_transcripts.jsonl'
    script = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'asr_validation.py'
    python = _find_local_python_runtime()
    if not jobs.is_file() or not model.is_dir() or not script.is_file() or not python.is_file():
        messagebox.showerror('ASR validation unavailable', 'Required WAV jobs, Whisper model, validation script, or Python runtime is unavailable.'); return
    env = os.environ.copy()
    bins = _cuda_package_bin_paths()
    env['PATH'] = ';'.join(bins + [env.get('PATH', '')])
    self.overall.configure(value=7); self.overall_status.set(self._stage_overall(7, 'Validate WAV samples with CUDA ASR'))
    sample_total = sum(1 for line in jobs.read_text(encoding='utf-8').splitlines() if line); self.step.configure(maximum=max(1, sample_total), value=0); self.action_button.configure(state='disabled')
    self._append_log('> Starting CUDA ASR validation for validation samples...')
    process = subprocess.Popen([str(python), str(script), '--jobs', str(jobs), '--model', str(model), '--output', str(output)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', env=env, creationflags=HIDDEN_PROCESS)
    done = 0
    assert process.stdout is not None
    for line in process.stdout:
        clean = line.rstrip()
        if clean:
            self._append_log('> ' + clean.split(' target_text=', 1)[0])
            if clean.startswith('ASR '):
                done += 1; self.step.configure(value=done); self.step_status.set(self._stage_task(7, 'Validating audio samples with CUDA ASR')); self._refresh_progress_bars()
    code = process.wait()
    if process.stdout is not None:
        process.stdout.close()
    self._append_log('> CUDA ASR process ended; GPU memory released.')
    if code == 0 and output.is_file():
        rows = [json.loads(line) for line in output.read_text(encoding='utf-8').splitlines() if line]
        complete = [row for row in rows if row.get('asr_status') == 'complete']
        scores = [float(row.get('english_overlap', 0)) for row in complete]
        average = sum(scores) / max(1, len(scores))
        matched = sum(score >= 0.55 for score in scores)
        review = sum(0.30 <= score < 0.55 for score in scores)
        mismatch = sum(score < 0.30 for score in scores)
        self._append_log(f'> CUDA ASR validation complete | {len(complete)} of {len(rows)} | Mean English overlap: {average * 100:.1f}% | Match: {matched} | Review: {review} | Mismatch: {mismatch}')
        self.step_status.set(self._stage_task(7, 'ASR validation complete'))
        self.current_line.set(f'Current dialogue: ASR ready | mean English overlap {average * 100:.1f}%')
        self.action_button.configure(text='Show ASR Report', command=self._show_asr_report, state='normal')
    else:
        self._append_log(f'> CUDA ASR validation failed | exit code {code}')
        self.step_status.set(self._stage_task(7, 'ASR validation failed; batch remains blocked'))
        self.action_button.configure(text='ASR validation failed', state='disabled')
def install(cls):
    original=cls._build
    def build(self):
        original(self)
        if hasattr(self, 'reset_button'):
            self.reset_button.destroy()
        self.reset_button = ttk.Button(self.log.master,text='Reset local pipeline data',command=self._reset_pipeline_data,width=24)
        self.reset_button.grid(row=20,column=0,sticky='w')
    original_analyze = cls._analyze_audio_test
    def analyze(self):
        original_analyze(self)
        self.action_button.configure(text='Validate Audio Samples with ASR (CUDA)', command=self._run_asr_validation, state='normal')
    cls._build=build; cls._reset_pipeline_data=reset; cls._build_dialogue_text_index=build_dialogue; cls._build_voice_map=build_map; cls._analyze_audio_test=analyze; cls._run_asr_validation=run_asr_validation; cls._show_asr_report=show_asr_report; cls._prepare_initial_production_batch=prepare_initial_production_batch; cls._extract_initial_production_audio=extract_initial_production_audio; cls._decode_initial_production_audio=decode_initial_production_audio; cls._analyze_initial_production_audio=analyze_initial_production_audio
# Full, fresh production flow installed after the legacy incremental helpers above.
def _batch_unique_count(voice_map: Path) -> int:
    seen: set[str] = set()
    with voice_map.open(encoding='utf-8') as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            source = str(row.get('source_audio_path', '')).replace('\\', '/').lower()
            if row.get('mapping_status') == 'xtranslator_exact' and source:
                seen.add(source)
    return len(seen)


def _full_batch_space_estimate(root: Path, unique_count: int) -> tuple[int, int, int]:
    """Return Wwise Vorbis output estimate, required free space and free bytes."""
    # Starfield's Wwise Vorbis Quality Medium produced 64 kbps on the local
    # reference test. This includes headroom for longer translated lines.
    average_bytes = 32 * 1024
    output_estimate = unique_count * average_bytes
    # English WEM/WAV and target-language WAV are removed one item at a time.
    required_free = int(output_estimate * 1.20 + 2 * 1024 ** 3)
    free = shutil.disk_usage(root).free
    return output_estimate, required_free, free


def _start_full_voiceover_batch(self) -> None:
    from game_dubber_gui import WORK_ROOT, HIDDEN_PROCESS
    voice_map = WORK_ROOT / 'manifests' / 'voice_map.jsonl'
    language = self.target_language.get().strip()
    if not voice_map.is_file() or not language or '(' not in language:
        messagebox.showerror('Full batch unavailable', 'A validated voice map and a selected target voice-over language are required.', parent=self)
        return
    target_code = language.rsplit('(', 1)[1].rstrip(')').lower()
    unique_count = _batch_unique_count(voice_map)
    output_estimate, required_free, free = _full_batch_space_estimate(WORK_ROOT, unique_count)
    gb = 1024 ** 3
    self._append_log(f'> Full batch preflight | Exact unique voices: {unique_count:,} | Estimated final WAVs: {output_estimate / gb:.1f} GB | Required free space: {required_free / gb:.1f} GB | Available: {free / gb:.1f} GB')
    self.overall.configure(maximum=1, value=0)
    self.overall_status.set('Overall: Full voice-over batch preflight')
    self.step.configure(maximum=1, value=0)
    self.step_status.set('Current task: Checking local output capacity')
    self._refresh_progress_bars()
    if free < required_free:
        message = (f'Full generation is not started.\n\n'
                   f'Exact unique voices: {unique_count:,}\n'
                   f'Estimated final target-language WAVs: {output_estimate / gb:.1f} GB\n'
                   f'Required free workspace space: {required_free / gb:.1f} GB\n'
                   f'Available on the workspace drive: {free / gb:.1f} GB\n\n'
                   'Free additional disk space, then run the full batch again. No game files and no production audio were changed.')
        messagebox.showerror('Insufficient disk space', message, parent=self)
        self.step_status.set('Current task: Full batch blocked — insufficient disk space')
        self.current_line.set('Current dialogue: No production audio was extracted.')
        self.action_button.configure(text='Start Full Voice-over Batch', command=self._choose_generation_model, state='normal')
        return

    runtime = WORK_ROOT / 'runtimes' / 'voxcpm' / 'Scripts' / 'python.exe'
    model = WORK_ROOT / 'models' / 'VoxCPM2'
    bundle_root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    script = bundle_root / 'full_voxcpm2_batch.py'
    if not script.is_file():
        script = Path(__file__).resolve().parent / 'full_voxcpm2_batch.py'
    reader = self._reader_path()
    decoder = self._decoder_path()
    required_paths = (runtime, model, script, reader, decoder)
    missing = [str(path) for path in required_paths if not path.exists()]
    if missing:
        messagebox.showerror('Full batch unavailable', 'Required local components are missing:\n' + '\n'.join(missing), parent=self)
        return

    run_name = f'run-{datetime.now().strftime("%Y%m%d-%H%M%S")}-{target_code}'
    run_dir = WORK_ROOT / 'output' / 'target_voice' / 'runs' / run_name
    batch_log = WORK_ROOT / 'logs' / f'{run_name}.log'
    batch_log.parent.mkdir(parents=True, exist_ok=True)
    command = [str(runtime), '-u', str(script), '--voice-map', str(voice_map), '--run-dir', str(run_dir), '--reader', str(reader), '--decoder', str(decoder), '--model', str(model), '--database', str(WORK_ROOT / 'voice_pipeline.db'), '--language', target_code]
    self.overall.configure(maximum=max(1, unique_count), value=0)
    self.overall_status.set(f'Overall: Full {language} WAV generation — 0.0%')
    self.step.configure(maximum=3, value=0)
    self.step_status.set('Current task: Starting sequential English extraction, CPU conversion and VoxCPM2 synthesis')
    self.current_line.set('Current dialogue: Preparing fresh full-production run.')
    self.action_button.configure(state='disabled')
    self._append_log(f'> Starting full VoxCPM2 batch | Run: {run_dir}')
    self._append_log(f'> Detailed full-batch log: {batch_log}')
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', creationflags=HIDDEN_PROCESS)
    latest_number = 0
    assert process.stdout is not None
    with batch_log.open('w', encoding='utf-8', newline='\n') as raw_log:
        for line in process.stdout:
            clean = line.rstrip()
            if not clean:
                continue
            raw_log.write(f'[{datetime.now().astimezone().isoformat(timespec="seconds")}] {clean}\n')
            raw_log.flush()
            if clean.startswith('ITEM '):
                parts = clean.split()
                try:
                    progress = parts[1]
                    number, total = (int(value) for value in progress.split('/', 1))
                    stage = parts[2].split('=', 1)[1]
                    latest_number = number
                    self.step.configure(value={'extract': 1, 'decode_cpu': 2, 'voxcpm2': 3}.get(stage, 0))
                    if number == 1 or number == unique_count or number % 25 == 0:
                        labels = {'extract': 'Extracting English WEM', 'decode_cpu': 'Decoding English WEM to WAV on CPU', 'voxcpm2': 'Generating target WAV with VoxCPM2'}
                        self.step_status.set(f"Current task: {labels.get(stage, stage)} ({number:,} of {total:,})")
                        self.current_line.set(f'Current dialogue: Batch item {number:,} of {total:,}')
                        self._refresh_progress_bars()
                except (IndexError, ValueError):
                    pass
            elif clean.startswith('PROGRESS '):
                parts = clean.split()
                try:
                    number, total = (int(value) for value in parts[1].split('/', 1))
                    latest_number = number
                    self.overall.configure(maximum=max(1, total), value=number)
                    if number == 1 or number == total or number % 25 == 0:
                        self.overall_status.set(f'Overall: Full {language} WAV generation — {number / total * 100:.1f}% ({number:,} of {total:,})')
                        self._append_log('> ' + clean.split(' target_text=', 1)[0])
                        self._refresh_progress_bars()
                except (IndexError, ValueError):
                    self._append_log('> ' + clean.split(' target_text=', 1)[0])
            elif clean.startswith(('START ', 'MODEL ', 'DONE ')):
                self._append_log('> ' + clean.split(' target_text=', 1)[0])
            elif 'ERROR' in clean.upper() or 'Traceback' in clean:
                self._append_log('> ' + clean.split(' target_text=', 1)[0])
    exit_code = process.wait()
    process.stdout.close()
    if exit_code == 0:
        self.overall.configure(value=max(1, unique_count))
        self.step.configure(value=3)
        self.overall_status.set(f'Overall: Full {language} WAV generation complete')
        self.step_status.set('Current task: Primary production phase complete; follow-up phases are not started')
        self.current_line.set('Current dialogue: Full production run completed.')
        self._append_log(f'> Full VoxCPM2 batch complete | Run: {run_dir}')
        messagebox.showinfo('Full batch complete', f'Target-language WAV generation completed.\n\nRun folder:\n{run_dir}\n\nSQLite table: production_voice_outputs\nDetailed log:\n{batch_log}', parent=self)
        self.action_button.configure(text='Primary Production Phase Complete', state='disabled')
    else:
        self.overall.configure(value=latest_number)
        self.overall_status.set('Overall: Full voice-over batch ended with errors')
        self.step_status.set('Current task: Inspect the detailed batch log and SQLite results')
        self._append_log(f'> Full VoxCPM2 batch ended | Exit code: {exit_code} | Log: {batch_log}')
        messagebox.showerror('Full batch ended with errors', f'The batch did not complete cleanly.\n\nDetailed log:\n{batch_log}\n\nAny completed WAVs and per-line results are retained in the run folder.', parent=self)
        self.action_button.configure(text='Start New Full Voice-over Batch', command=self._choose_generation_model, state='normal')


def _show_asr_report_and_offer_full_batch(self) -> None:
    from game_dubber_gui import WORK_ROOT
    output = WORK_ROOT / 'asr' / 'audio_test_transcripts.jsonl'
    if not output.is_file():
        messagebox.showerror('ASR report unavailable', 'Run CUDA ASR validation before opening its report.', parent=self)
        return
    rows = [json.loads(line) for line in output.read_text(encoding='utf-8').splitlines() if line]
    complete = [row for row in rows if row.get('asr_status') == 'complete']
    scores = [float(row.get('english_overlap', 0)) for row in complete]
    average = sum(scores) / max(1, len(scores))
    matched = sum(score >= 0.55 for score in scores)
    review = sum(0.30 <= score < 0.55 for score in scores)
    mismatch = sum(score < 0.30 for score in scores)
    continue_batch = messagebox.askyesno(
        'ASR matching report',
        f'CUDA ASR validation completed.\n\nMean English matching: {average * 100:.1f}%\nGood result (>= 55%): {matched}\nReview (30%–54%): {review}\nMismatch (< 30%): {mismatch}\n\nWhisper is not infallible: 55% or more is considered a good validation result; lower scores need review rather than automatic rejection.\n\nDetailed report:\n{output}\n\nContinue with the fresh full voice-over batch (English extraction → CPU decode/normalization → selected-model WAV generation)?',
        parent=self,
    )
    if continue_batch:
        self._choose_generation_model()
    else:
        self.action_button.configure(text='Start Full Voice-over Batch', command=self._choose_generation_model, state='normal')


def _continue_automatic_setup_to_asr_report(self) -> None:
    from game_dubber_gui import WORK_ROOT
    if not getattr(self, '_automatic_setup_to_report', False):
        return
    self.action_button.configure(state='disabled')
    self._append_log('> Automatic setup continues: selected language to ASR validation report.')
    steps = (
        ('target subtitle index', self._build_target_subtitle_index, WORK_ROOT / 'voice_pipeline.db'),
        ('English voice manifest', self._build_english_voice_manifest, WORK_ROOT / 'manifests' / 'english_voice_manifest.jsonl'),
        ('xTranslator dialogue index', self._build_dialogue_text_index, WORK_ROOT / 'manifests' / 'dialogue_text_index.jsonl'),
        ('xTranslator voice map', self._build_voice_map, WORK_ROOT / 'manifests' / 'voice_map.jsonl'),
        ('validation sample extraction', self._prepare_wav_test, WORK_ROOT / 'manifests' / 'audio_test_jobs.jsonl'),
        ('validation sample decode', self._decode_audio_test, WORK_ROOT / 'manifests' / 'audio_test_jobs.jsonl'),
        ('validation sample analysis', self._analyze_audio_test, WORK_ROOT / 'analysis' / 'audio_test_analysis.jsonl'),
    )
    for label, action, expected in steps:
        action()
        if not expected.exists():
            self._automatic_setup_to_report = False
            self._append_log(f'> Automatic setup stopped after {label}: expected local output was not created.')
            return
    self._run_asr_validation()
    if (WORK_ROOT / 'asr' / 'audio_test_transcripts.jsonl').is_file():
        self._show_asr_report()
    self._automatic_setup_to_report = False


def _begin_full_voiceover_pipeline(self) -> None:
    self._automatic_setup_to_report = True
    self.action_button.configure(state='disabled')
    self._discover_mapping()


def install(cls):
    original_build = cls._build
    original_set_target_language = cls._set_target_language
    original_analyze = cls._analyze_audio_test

    def build(self):
        original_build(self)
        self.action_button.configure(text='Start full voice-over pipeline', command=self._begin_full_voiceover_pipeline)
        if hasattr(self, 'reset_button'):
            self.reset_button.destroy()
        self.reset_button = ttk.Button(self.log.master, text='Reset local pipeline data', command=self._reset_pipeline_data, width=24)
        self.reset_button.grid(row=20, column=0, sticky='w')

    def set_target_language(self, language, dialog):
        original_set_target_language(self, language, dialog)
        # A confirmed target language is the hand-off point for the whole
        # read-only setup. Never leave the user to discover a manual
        # lower-right button: continue directly through ASR and show its report.
        if self.target_language.get().strip() == language:
            self._automatic_setup_to_report = True
            self.after_idle(self._continue_automatic_setup_to_asr_report)

    def analyze(self):
        original_analyze(self)
        self.action_button.configure(text='Validate Audio Samples with ASR (CUDA)', command=self._run_asr_validation, state='normal')

    cls._build = build
    cls._set_target_language = set_target_language
    cls._reset_pipeline_data = reset
    cls._build_dialogue_text_index = build_dialogue
    cls._build_voice_map = build_map
    cls._analyze_audio_test = analyze
    cls._run_asr_validation = run_asr_validation
    cls._show_asr_report = _show_asr_report_and_offer_full_batch
    cls._prepare_initial_production_batch = prepare_initial_production_batch
    cls._extract_initial_production_audio = extract_initial_production_audio
    cls._decode_initial_production_audio = decode_initial_production_audio
    cls._analyze_initial_production_audio = analyze_initial_production_audio
    cls._begin_full_voiceover_pipeline = _begin_full_voiceover_pipeline
    cls._continue_automatic_setup_to_asr_report = _continue_automatic_setup_to_asr_report
    cls._start_full_voiceover_batch = _start_full_voiceover_batch
# Non-blocking production runner: the GPU subprocess never runs in Tk's UI thread.
def _start_full_voiceover_batch_async(self) -> None:
    from game_dubber_gui import WORK_ROOT, HIDDEN_PROCESS
    import queue
    import threading
    import time

    if getattr(self, '_full_batch_running', False):
        return
    voice_map = WORK_ROOT / 'manifests' / 'voice_map.jsonl'
    language = self.target_language.get().strip()
    if not voice_map.is_file() or not language or '(' not in language:
        messagebox.showerror('Full batch unavailable', 'A validated voice map and a selected target voice-over language are required.', parent=self)
        return
    target_code = language.rsplit('(', 1)[1].rstrip(')').lower()
    unique_count = _batch_unique_count(voice_map)
    output_estimate, required_free, free = _full_batch_space_estimate(WORK_ROOT, unique_count)
    gb = 1024 ** 3
    self._append_log(f'> Full batch preflight | Exact unique voices: {unique_count:,} | Estimated final WAVs: {output_estimate / gb:.1f} GB | Required free space: {required_free / gb:.1f} GB | Available: {free / gb:.1f} GB')
    if free < required_free:
        messagebox.showerror('Insufficient disk space', f'Full generation is not started.\n\nRequired: {required_free / gb:.1f} GB\nAvailable: {free / gb:.1f} GB', parent=self)
        self.step_status.set('Current task: Full batch blocked — insufficient disk space')
        self.action_button.configure(text='Start Full Voice-over Batch', command=self._choose_generation_model, state='normal')
        return

    runtime = WORK_ROOT / 'runtimes' / 'voxcpm' / 'Scripts' / 'python.exe'
    model = WORK_ROOT / 'models' / 'VoxCPM2'
    bundle_root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    script = bundle_root / 'full_voxcpm2_batch.py'
    if not script.is_file():
        script = Path(__file__).resolve().parent / 'full_voxcpm2_batch.py'
    reader = self._reader_path()
    decoder = self._decoder_path()
    missing = [str(path) for path in (runtime, model, script, reader, decoder) if not path.exists()]
    if missing:
        messagebox.showerror('Full batch unavailable', 'Required local components are missing:\n' + '\n'.join(missing), parent=self)
        return

    run_name = f'run-{datetime.now().strftime("%Y%m%d-%H%M%S")}-{target_code}'
    run_dir = WORK_ROOT / 'output' / 'target_voice' / 'runs' / run_name
    batch_log = WORK_ROOT / 'logs' / f'{run_name}.log'
    batch_log.parent.mkdir(parents=True, exist_ok=True)
    command = [str(runtime), '-u', str(script), '--voice-map', str(voice_map), '--run-dir', str(run_dir), '--reader', str(reader), '--decoder', str(decoder), '--model', str(model), '--database', str(WORK_ROOT / 'voice_pipeline.db'), '--language', target_code]
    self._full_batch_running = True
    self._full_batch_queue = queue.Queue()
    self._full_batch_context = {'language': language, 'unique_count': unique_count, 'run_dir': run_dir, 'batch_log': batch_log, 'latest_number': 0, 'started_monotonic': time.monotonic()}
    self.overall.configure(maximum=11, value=10)
    self.overall_status.set(f'Overall: Step 11 of 11 — Generate {language} WAVs with VoxCPM2')
    self.step.configure(maximum=max(1, unique_count), value=0)
    self.step_status.set(f'Current task: 0 / {unique_count:,} generated | {unique_count:,} remaining | ETA calculating...')
    self.current_line.set('Current dialogue: The interface remains available while the batch runs.')
    self.action_button.configure(state='disabled')
    self._append_log(f'> Starting non-blocking full VoxCPM2 batch | Run: {run_dir}')
    self._append_log(f'> Detailed full-batch log: {batch_log}')

    def worker() -> None:
        try:
            child_env = os.environ.copy()
            child_env['PYTHONUTF8'] = '1'
            child_env['PYTHONIOENCODING'] = 'utf-8'
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace', env=child_env, creationflags=HIDDEN_PROCESS)
            assert process.stdout is not None
            with batch_log.open('w', encoding='utf-8', newline='\n') as raw_log:
                for line in process.stdout:
                    clean = line.rstrip()
                    if clean:
                        raw_log.write(f'[{datetime.now().astimezone().isoformat(timespec="seconds")}] {clean}\n')
                        raw_log.flush()
                        self._full_batch_queue.put(('line', clean))
            code = process.wait()
            process.stdout.close()
            self._full_batch_queue.put(('done', code))
        except Exception as error:
            self._full_batch_queue.put(('error', str(error)))

    threading.Thread(target=worker, name='GameDubberFullBatch', daemon=True).start()
    self.after(100, self._poll_full_voiceover_batch)


def _poll_full_voiceover_batch(self) -> None:
    import queue
    import time
    import time
    if not getattr(self, '_full_batch_running', False):
        return
    context = self._full_batch_context
    finished = None
    try:
        while True:
            kind, value = self._full_batch_queue.get_nowait()
            if kind == 'line':
                clean = value
                if clean.startswith('ITEM '):
                    try:
                        number, total = (int(item) for item in clean.split()[1].split('/', 1))
                        stage = clean.split()[2].split('=', 1)[1]
                        context['latest_number'] = max(context.get('latest_number', 0), number - 1)
                        self.step.configure(maximum=max(1, total), value=context['latest_number'])
                        labels = {'extract': 'Extracting English WEM', 'decode_cpu': 'Decoding English WEM to WAV on CPU', 'voxcpm2': 'Generating target WAV with VoxCPM2'}
                        self.current_line.set(f"Current dialogue: {labels.get(stage, stage)} — item {number:,} of {total:,}")
                    except (IndexError, ValueError):
                        pass
                elif clean.startswith('PROGRESS '):
                    try:
                        fields = clean.split()
                        number, total = (int(item) for item in fields[1].split('/', 1))
                        context['latest_number'] = number
                        self.step.configure(maximum=max(1, total), value=number)
                        elapsed = max(0.001, time.monotonic() - context['started_monotonic'])
                        remaining_seconds = int(max(0, (total - number) / (number / elapsed)))
                        days, remainder = divmod(remaining_seconds, 86400)
                        hours, remainder = divmod(remainder, 3600)
                        minutes, seconds = divmod(remainder, 60)
                        eta = f'{days}d {hours:02}:{minutes:02}:{seconds:02}' if days else f'{hours:02}:{minutes:02}:{seconds:02}'
                        self.step_status.set(f'Current task: {number:,} / {total:,} generated | {total - number:,} remaining | ETA {eta}')
                        dialogue = next((field.split('=', 1)[1] for field in fields if field.startswith('dialogue=')), '')
                        target_text = ''
                        marker = ' target_text='
                        if marker in clean:
                            try:
                                target_text = str(json.loads(clean.split(marker, 1)[1]))
                            except (ValueError, TypeError):
                                target_text = ''
                        if target_text:
                            self.current_line.set(f'Current dialogue ({context["language"]}): {target_text}')
                        elif dialogue:
                            self.current_line.set(f'Current dialogue: {dialogue} | generated item {number:,} of {total:,}')
                        self._append_log('> ' + clean.split(' target_text=', 1)[0])
                    except (IndexError, ValueError, ZeroDivisionError):
                        self._append_log('> ' + clean.split(' target_text=', 1)[0])
                elif clean.startswith(('START ', 'MODEL ', 'DONE ')):
                    self._append_log('> ' + clean.split(' target_text=', 1)[0])
            elif kind == 'done':
                finished = int(value)
            elif kind == 'error':
                self._append_log('> Full batch worker error: ' + str(value))
                finished = -1
    except queue.Empty:
        pass
    self._refresh_progress_bars()
    if finished is None:
        self.after(150, self._poll_full_voiceover_batch)
        return
    self._full_batch_running = False
    if finished == 0:
        self.overall.configure(value=max(1, context['unique_count']))
        self.step.configure(value=3)
        self.overall_status.set(f"Overall: Full {context['language']} WAV generation complete")
        self.step_status.set('Current task: Primary production phase complete; follow-up phases are not started')
        self.current_line.set('Current dialogue: Full production run completed.')
        self._append_log(f"> Full VoxCPM2 batch complete | Run: {context['run_dir']}")
        messagebox.showinfo('Full batch complete', f"Target-language WAV generation completed.\n\nRun folder:\n{context['run_dir']}\n\nDetailed log:\n{context['batch_log']}", parent=self)
        self.action_button.configure(text='Full Voice-over Batch Complete', state='disabled')
    else:
        self.overall.configure(value=context.get('latest_number', 0))
        self.overall_status.set('Overall: Full voice-over batch ended with errors')
        self.step_status.set('Current task: Inspect detailed batch log and SQLite results')
        self._append_log(f"> Full VoxCPM2 batch ended | Exit code: {finished} | Log: {context['batch_log']}")
        messagebox.showerror('Full batch ended with errors', f"The batch ended with errors.\n\nDetailed log:\n{context['batch_log']}\n\nCompleted files, if any, remain in its run folder.", parent=self)
        self.action_button.configure(text='Start New Full Voice-over Batch', command=self._choose_generation_model, state='normal')


# Override the previous installer binding with the non-blocking runner.
_previous_install = install

def install(cls):
    _previous_install(cls)
    cls._start_full_voiceover_batch = _start_full_voiceover_batch_async
    cls._poll_full_voiceover_batch = _poll_full_voiceover_batch

# Generation-model adapters. Each entry explicitly selects its isolated local
# Python runtime and local model directory; Whisper deliberately remains ASR-only.
def _generation_engine_config(engine: str):
    from game_dubber_gui import WORK_ROOT
    configs = {
        'voxcpm2': (
            'VoxCPM2 (2.4× slower than XTTS v2)',
            WORK_ROOT / 'runtimes' / 'voxcpm' / 'Scripts' / 'python.exe',
            WORK_ROOT / 'models' / 'VoxCPM2',
        ),
        'qwen_1_7b': (
            'Qwen3-TTS 12Hz 1.7B Base (6.6× slower than XTTS v2)',
            WORK_ROOT / 'runtimes' / 'qwen-tts' / 'Scripts' / 'python.exe',
            WORK_ROOT / 'models' / 'Qwen3-TTS-12Hz-1.7B-Base',
        ),
        'qwen_0_6b': (
            'Qwen3-TTS 12Hz 0.6B Base (6.5× slower than XTTS v2)',
            WORK_ROOT / 'runtimes' / 'qwen-tts' / 'Scripts' / 'python.exe',
            WORK_ROOT / 'models' / 'Qwen3-TTS-12Hz-0.6B-Base',
        ),
        'cosyvoice3': (
            'CosyVoice 3 0.5B (2.6× slower than XTTS v2)',
            WORK_ROOT / 'runtimes' / 'cosyvoice3' / 'Scripts' / 'python.exe',
            WORK_ROOT / 'models' / 'CosyVoice3-0.5B-2512',
        ),
        'chatterbox_v3': (
            'Chatterbox TTS v3 (2.4× slower than XTTS v2)',
            WORK_ROOT / 'runtimes' / 'chatterbox' / 'Scripts' / 'python.exe',
            WORK_ROOT / 'models' / 'Chatterbox-v3',
        ),
        'xtts_v2': (
            'XTTS v2 — standard T 0.70 / p 0.88 / k 55',
            WORK_ROOT / 'runtimes' / 'xtts' / 'Scripts' / 'python.exe',
            WORK_ROOT / 'models' / 'XTTS-v2',
        ),
    }
    return configs.get(engine)


def _engine_supports_target_language(engine: str, target_code: str) -> bool:
    """Keep unsupported engine/language combinations out of the GUI chooser."""
    supported_codes = {'de', 'en', 'es', 'fr', 'it', 'ja', 'pl', 'ptbr', 'zhhans'}
    if target_code not in supported_codes:
        return False
    return engine in {'voxcpm2', 'qwen_0_6b', 'qwen_1_7b', 'cosyvoice3', 'chatterbox_v3', 'xtts_v2', 'whisper_asr'}


def _available_generation_models() -> list[tuple[str, str, str]]:
    entries: list[tuple[str, str, str]] = []
    # Tested models are presented in ascending mean synthesis time from the
    # shared ten-line benchmark (model loading and Wwise are excluded).
    for engine in ('xtts_v2', 'chatterbox_v3', 'voxcpm2', 'cosyvoice3', 'qwen_0_6b', 'qwen_1_7b'):
        config = _generation_engine_config(engine)
        assert config is not None
        name, runtime, model = config
        if runtime.is_file() and model.is_dir():
            entries.append((name, engine, 'production adapter available: fresh batch, reference format matching and SQLite metrics.'))
    from game_dubber_gui import WORK_ROOT
    if (WORK_ROOT / 'models' / 'whisper-large-v3-turbo').is_dir():
        entries.append(('Whisper large-v3-turbo — ASR only', 'whisper_asr', 'This is the English ASR validation model, not a speech-generation engine.'))
    return entries


def _clear_preview_audio_for_new_session(run_dir: Path) -> int:
    """Discard only disposable preview files before a new/resumed batch starts."""
    preview_root = run_dir / '_preview_audio'
    if not preview_root.exists():
        return 0
    player_pid = preview_root / '_player.pid'
    try:
        pid = int(player_pid.read_text(encoding='ascii').strip())
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, ValueError):
        pass
    disposable = [*preview_root.glob('*.wav'), *preview_root.glob('*.wav.done'),
                  preview_root / '_player.pid', preview_root / '_stop',
                  preview_root / '_preview_disabled', preview_root / '_preview_capability_ready']
    removed = 0
    for path in disposable:
        try:
            if path.is_file():
                path.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def _start_full_voiceover_batch_with_adapter(self, resume_run: Path | None = None) -> None:
    from game_dubber_gui import WORK_ROOT, HIDDEN_PROCESS
    import queue
    import threading
    import time

    if getattr(self, '_full_batch_running', False):
        return
    voice_map = WORK_ROOT / 'manifests' / 'voice_map.jsonl'
    language = self.target_language.get().strip()
    engine = getattr(self, '_selected_generation_model', 'xtts_v2')
    config = _generation_engine_config(engine)
    if not voice_map.is_file() or not language or '(' not in language or config is None:
        messagebox.showerror('Full batch unavailable', 'A validated voice map, target voice-over language and generation model are required.', parent=self)
        return
    engine_name, runtime, model = config
    voxcpm_steps = int(getattr(self, '_selected_voxcpm_steps', 6)) if engine == 'voxcpm2' else None
    if voxcpm_steps is not None and not 1 <= voxcpm_steps <= 20:
        voxcpm_steps = 6
    display_engine_name = (f'{engine_name} — diffusion {voxcpm_steps} steps'
                           if voxcpm_steps is not None else engine_name)
    target_code = language.rsplit('(', 1)[1].rstrip(')').lower()
    if not _engine_supports_target_language(engine, target_code):
        messagebox.showerror('Unsupported target language', f'{engine_name} does not support the selected target language: {language}.', parent=self)
        return
    unique_count = _batch_unique_count(voice_map)
    output_estimate, required_free, free = _full_batch_space_estimate(WORK_ROOT, unique_count)
    gb = 1024 ** 3
    self._append_log(f'> Full batch preflight | Engine: {display_engine_name} | Exact unique voices: {unique_count:,} | Estimated final Wwise Vorbis WEMs: {output_estimate / gb:.1f} GB | Required free space: {required_free / gb:.1f} GB | Available: {free / gb:.1f} GB')
    if free < required_free:
        messagebox.showerror('Insufficient disk space', f'Full generation is not started.\n\nRequired: {required_free / gb:.1f} GB\nAvailable: {free / gb:.1f} GB', parent=self)
        self.step_status.set('Current task: Full batch blocked — insufficient disk space')
        self.action_button.configure(text='Start Full Voice-over Batch', command=self._choose_generation_model, state='normal')
        return

    bundle_root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    script = bundle_root / 'full_voxcpm2_batch.py'
    if not script.is_file():
        script = Path(__file__).resolve().parent / 'full_voxcpm2_batch.py'
    reader = self._reader_path()
    decoder = self._decoder_path()
    wwise_console = getattr(self, '_wwise_console_path', None)
    if not isinstance(wwise_console, Path) or not wwise_console.is_file():
        wwise_console, detection = _find_starfield_wwise_console()
        self._wwise_console_path = wwise_console
        self._wwise_detection_message = detection
        self._append_log('> ' + detection)
    wwise_project = WORK_ROOT / 'wwise' / 'starfield_project' / 'Starfield.wproj'
    required_paths = (runtime, model, script, reader, decoder, wwise_project)
    missing = [str(path) for path in required_paths if not path.exists()]
    if wwise_console is None:
        missing.append(getattr(self, '_wwise_detection_message', 'Wwise 2021.1.10.7883'))
    if missing:
        messagebox.showerror('Full batch unavailable', 'Required local components are missing:\n' + '\n'.join(missing), parent=self)
        return

    resuming = resume_run is not None
    if resuming:
        run_dir = Path(resume_run)
        run_name = run_dir.name
    else:
        run_name = f'run-{datetime.now().strftime("%Y%m%d-%H%M%S")}-{target_code}-{engine}'
        run_dir = WORK_ROOT / 'output' / 'target_voice' / 'runs' / run_name
    cleared_preview_files = _clear_preview_audio_for_new_session(run_dir)
    batch_log = WORK_ROOT / 'logs' / f'{run_name}.log'
    batch_log.parent.mkdir(parents=True, exist_ok=True)
    command = [str(runtime), '-u', str(script), '--voice-map', str(voice_map), '--run-dir', str(run_dir), '--reader', str(reader), '--decoder', str(decoder), '--model', str(model), '--database', str(WORK_ROOT / 'voice_pipeline.db'), '--engine', engine, '--language', target_code, '--wwise-console', str(wwise_console), '--wwise-project', str(wwise_project)]
    preview_wavs = bool(getattr(self, 'preview_wav_playback_enabled', None) and self.preview_wav_playback_enabled.get())
    # Always enable the child-side preview capability: this lets the checkbox
    # start playing target WAVs even after a batch has already begun.
    command.append('--preview-wav-playback')
    # The GUI is the sole owner of the preview player.  Starting the child
    # idle avoids a second player racing the checkbox state at batch start.
    command.append('--preview-wav-initially-disabled')
    if voxcpm_steps is not None:
        command.extend(['--voxcpm-steps', str(voxcpm_steps)])
    if resuming:
        command.append('--resume')
    self._full_batch_running = True
    self._full_batch_queue = queue.Queue()
    resumed_count = _resume_completed_count(run_dir) if resuming else 0
    session_started_monotonic = time.monotonic()
    self._full_batch_context = {'language': language, 'engine': engine, 'engine_name': display_engine_name, 'unique_count': unique_count, 'run_dir': run_dir, 'batch_log': batch_log, 'latest_number': resumed_count, 'completed_wems': resumed_count, 'latest_attempt_number': resumed_count, 'started_monotonic': session_started_monotonic, 'eta_anchor_completed': resumed_count, 'eta_anchor_monotonic': session_started_monotonic, 'eta_text': 'calculating...', 'resuming': resuming, 'voxcpm_steps': voxcpm_steps, 'preview_wavs': preview_wavs, 'preview_player_started': False, 'game_path': self.game_path.get().strip(), 'asr_checked': 0, 'asr_total': resumed_count if resuming else 500}
    _write_production_run_state(run_dir, language, engine, display_engine_name, unique_count, 'active', voxcpm_steps, self.game_path.get().strip())
    self.overall.configure(maximum=11, value=10)
    self.overall_status.set(f'Overall: Step 11 of 11 — Generate {language} Wwise Vorbis WEMs with {engine_name}')
    self.step.configure(maximum=max(1, unique_count), value=0)
    self.step_status.set('Current task: Resuming production — waiting for the next manifest item...')
    self.current_line.set('Current dialogue: —')
    self.action_button.configure(state='disabled')
    if hasattr(self, 'reset_button'):
        self.reset_button.configure(state='disabled')
    self._append_log(f'> Starting non-blocking full batch with {display_engine_name} | Run: {run_dir}')
    if cleared_preview_files:
        self._append_log(f'> Cleared {cleared_preview_files} stale preview item(s); only WAVs generated in this session can be played.')
    self._append_log('> Background WAV preview: enabled (target language only; one next WAV is prepared during playback, then beep).' if preview_wavs else '> Background WAV preview: disabled.')
    self._append_log(f'> Detailed full-batch log: {batch_log}')

    def worker() -> None:
        try:
            # Keep child tracebacks in the run log and GUI terminal.  Hiding
            # stderr turned a concrete extraction error into an opaque code 1.
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', creationflags=HIDDEN_PROCESS)
            self._full_batch_process = process
            assert process.stdout is not None
            with batch_log.open('a' if resuming else 'w', encoding='utf-8', newline='\n') as raw_log:
                for line in process.stdout:
                    clean = line.rstrip()
                    if clean:
                        raw_log.write(f'[{datetime.now().astimezone().isoformat(timespec="seconds")}] {clean}\n')
                        raw_log.flush()
                        self._full_batch_queue.put(('line', clean))
            code = process.wait()
            process.stdout.close()
            self._full_batch_queue.put(('done', code))
        except Exception as error:
            self._full_batch_queue.put(('error', str(error)))

    threading.Thread(target=worker, name='GameDubberFullBatch', daemon=True).start()
    self.after(100, self._poll_full_voiceover_batch)
    # Apply the visible initial checkbox state too; waiting for the child-side
    # ready marker makes a checked box reliable without requiring a toggle.
    self.after(100, self._on_preview_wav_playback_changed)


def _poll_full_voiceover_batch_with_adapter(self) -> None:
    import queue
    import time
    if not getattr(self, '_full_batch_running', False):
        return
    context = self._full_batch_context
    finished = None
    try:
        while True:
            kind, value = self._full_batch_queue.get_nowait()
            if kind == 'line':
                clean = value
                if clean.startswith('ITEM '):
                    number = total = 0
                    stage = ''
                    try:
                        number, total = (int(item) for item in clean.split()[1].split('/', 1))
                        stage = clean.split()[2].split('=', 1)[1]
                        # ITEM is an attempted source index, never a completed
                        # WEM count.  Keep it only for dialogue/report tracking.
                        context['latest_attempt_number'] = number
                        if stage == 'extract':
                            label = 'Extracting English WEM'
                        elif stage == 'decode_cpu':
                            label = 'Decoding English WEM to WAV on CPU'
                        elif stage.startswith('generate_'):
                            label = f'Generating target WAV with {context["engine_name"]}'
                        elif stage == 'encode_wwise_wem':
                            label = 'Encoding and verifying Wwise Vorbis WEM'
                        else:
                            label = stage
                        completed = context.get('completed_wems', context.get('latest_number', 0))
                        context['latest_stage_label'] = label
                        summary = [f'Source WEM {number:,} / {total:,}']
                        prefetch = context.get('latest_prefetch')
                        if prefetch:
                            prefetch_number, prefetch_stage = prefetch
                            action = {'extract': 'extracting', 'decode_cpu': 'decoding'}.get(prefetch_stage, prefetch_stage)
                            summary.append(f'{action} next {prefetch_number:,}')
                        self._track_review_number(number)
                        self.step_status.set(
                            f'Current task: {label} — Source WEM {number:,} / {total:,} '
                            f'· ETA {context.get("eta_text", "calculating...")}'
                        )
                        # The technical ITEM line below is the single console
                        # record for this event.  Progress and dialogue still
                        # update here, but emitting an extra Pipeline summary
                        # made every item appear twice in the terminal.
                    except (IndexError, ValueError):
                        pass
                    # The child sends both paired subtitles with the
                    # generation event. Keep them together as unprefixed
                    # terminal lines: English in blue, target in white.
                    marker = ' target_subtitle='
                    if marker in clean:
                        event, raw_subtitle = clean.split(marker, 1)
                        english_subtitle = ''
                        english_marker = ' english_subtitle='
                        if english_marker in event:
                            event, raw_english = event.split(english_marker, 1)
                            try:
                                english_subtitle = str(json.loads(raw_english))
                            except (TypeError, ValueError):
                                english_subtitle = raw_english
                        try:
                            subtitle = str(json.loads(raw_subtitle))
                        except (TypeError, ValueError):
                            subtitle = raw_subtitle
                        action = 'Regenerating' if stage == 'generate_target' else 'Generating'
                        header = f'> {action} WEM {number:,} / {total:,}'
                        self._append_log(f'{header} | {english_subtitle}' if english_subtitle else header)
                        self._append_log('  ' + subtitle, 'target_dialogue')
                        # This dedicated field must never contain pipeline
                        # status, IDs, or the English source sentence.
                        # Keep it one physical line so the terminal always
                        # begins at the same position.
                        compact = ' '.join(subtitle.split())
                        if len(compact) > 150:
                            compact = compact[:147].rstrip() + '...'
                        self.current_line.set(f'Current dialogue ({context["language"]}): {compact}')
                    else:
                        # Extraction and CPU decoding are implementation
                        # details.  They remain in the run log but should not
                        # drown the dialogue-focused on-screen terminal.
                        if stage not in {'extract', 'decode_cpu'}:
                            self._append_log('> ' + clean)
                elif clean.startswith(('PREFETCH ', 'OUTPUT ')):
                    # Technical pipeline events are intentionally condensed
                    # into the next visible Pipeline summary line. The full
                    # chronological detail remains in the run-specific log.
                    try:
                        number = int(clean.split()[1].split('/', 1)[0])
                        stage = clean.split()[2].split('=', 1)[1]
                        if clean.startswith('PREFETCH '):
                            context['latest_prefetch'] = (number, stage)
                        else:
                            context['latest_output'] = (number, stage)
                    except (IndexError, ValueError):
                        pass
                elif clean.startswith('PROGRESS '):
                    try:
                        fields = clean.split()
                        number, total = (int(item) for item in fields[1].split('/', 1))
                        generated_field = next((field for field in fields if field.startswith('generated=')), None)
                        completed = int(generated_field.split('=', 1)[1]) if generated_field else context.get('completed_wems', 0)
                        context['completed_wems'] = completed
                        context['latest_number'] = completed
                        self._track_review_number(number)
                        self.step.configure(maximum=max(1, total), value=completed)
                        anchor_completed = int(context.get('eta_anchor_completed', 0))
                        completed_in_session = completed - anchor_completed
                        elapsed = max(0.001, time.monotonic() - context.get('eta_anchor_monotonic', context['started_monotonic']))
                        if completed_in_session > 0:
                            remaining_seconds = int(max(0, (total - completed) / (completed_in_session / elapsed)))
                            days, remainder = divmod(remaining_seconds, 86400)
                            hours, remainder = divmod(remainder, 3600)
                            minutes, seconds = divmod(remainder, 60)
                            eta = f'{days}d {hours:02}:{minutes:02}:{seconds:02}' if days else f'{hours:02}:{minutes:02}:{seconds:02}'
                            context['eta_text'] = eta
                        else:
                            eta = context.get('eta_text', 'calculating...')
                        source_item = context.get('latest_attempt_number', number)
                        stage_label = context.get('latest_stage_label', 'Generating target-language WEM')
                        self.step_status.set(
                            f'Current task: {stage_label} — Source WEM {source_item:,} / {total:,} '
                            f'· ETA {eta}'
                        )
                        dialogue = next((field.split('=', 1)[1] for field in fields if field.startswith('dialogue=')), '')
                        target_text = ''
                        marker = ' target_text='
                        if marker in clean:
                            try:
                                target_text = str(json.loads(clean.split(marker, 1)[1]))
                            except (ValueError, TypeError):
                                target_text = ''
                        if target_text:
                            compact = ' '.join(target_text.split())
                            if len(compact) > 150:
                                compact = compact[:147].rstrip() + '...'
                            self.current_line.set(f'Current dialogue ({context["language"]}): {compact}')
                        # The next ITEM line includes this completion count in
                        # the compact visible Pipeline summary.
                    except (IndexError, ValueError, ZeroDivisionError):
                        pass
                elif clean.startswith(('ASR ', 'VOX ASR ', 'VOX FALLBACK ', 'ASR GROUP ')):
                    if clean.startswith('ASR ') and ' expected=' in clean:
                        # The expected sentence belongs in the dedicated
                        # Current dialogue field.  Duplicating it in the ASR
                        # terminal made the transcript/result difficult to
                        # scan during large validation passes.
                        try:
                            asr_number, _asr_total = (int(value) for value in clean.split()[1].split('/', 1))
                            expected = str(json.loads(clean.split(' expected=', 1)[1]))
                            context.setdefault('asr_target_subtitles', {})[asr_number] = expected
                            compact = ' '.join(expected.split())
                            if len(compact) > 150:
                                compact = compact[:147].rstrip() + '...'
                            self.current_line.set(f'Current dialogue ({context["language"]}): {compact}')
                        except (TypeError, ValueError):
                            pass
                        continue
                    tag = 'asr_pass' if 'satisfactory=True' in clean else ('asr_fail' if 'satisfactory=False' in clean else None)
                    if clean.startswith('ASR ') and 'satisfactory=' in clean:
                        try:
                            asr_number, asr_total = (int(value) for value in clean.split()[1].split('/', 1))
                            target_subtitle = context.get('asr_target_subtitles', {}).pop(asr_number, '')
                            line = f'> ASR WEM validation {asr_number:,} / {asr_total:,}'
                            if target_subtitle:
                                line += f' | {target_subtitle}'
                            if tag == 'asr_fail':
                                line += ' — WILL BE REGENERATED'
                            self._append_log(line, tag)
                            self._track_review_number(asr_number)
                        except (IndexError, ValueError):
                            asr_number, asr_total = 0, int(context.get('unique_count', 0))
                            self._append_log('> ASR WEM validation', tag)
                        context['asr_checked'] = int(context.get('asr_checked', 0)) + 1
                        checked = context['asr_checked']; total_asr = max(1, int(context.get('asr_total', 500)))
                        self.step.configure(maximum=total_asr, value=min(checked, total_asr))
                        self.step_status.set(f'Current task: ASR verification — item {asr_number:,} / {asr_total:,} | group {checked:,} / {total_asr:,}')
                    else:
                        self._append_log('> ' + clean, tag)
                        self.step_status.set('Current task: Verifying generated WEMs with target-language ASR')
                elif clean.startswith('DEFERRED '):
                    # The XTTS duration gate is evaluated from decoded
                    # English audio.  It intentionally defers this source to
                    # a later secondary-model stage rather than spending a
                    # CUDA generation attempt on an unstable short reference.
                    self._append_log('> ' + clean)
                    self.step_status.set('Current task: Deferring a short English reference before XTTS generation')
                elif clean.startswith(('START ', 'MODEL ', 'DONE ', 'RESUME ', 'PAUSE ', 'PAUSED ', 'ERROR ', 'FINAL REPORT ')):
                    self._append_log('> ' + clean.split(' target_text=', 1)[0])
            elif kind == 'done':
                finished = int(value)
            elif kind == 'error':
                self._append_log('> Full batch worker error: ' + str(value))
                finished = -1
    except queue.Empty:
        pass
    self._refresh_progress_bars()
    if finished is None:
        self.after(50, self._poll_full_voiceover_batch)
        return
    self._full_batch_running = False
    if hasattr(self, 'reset_button'):
        self.reset_button.configure(state='normal')
    if finished == 3:
        # Exit code 3 is the pipeline's intentional cooperative pause, not a
        # failure.  The run folder and the per-line ASR checkpoints remain
        # available for the next launch.
        self.overall.configure(maximum=11, value=10)
        self.overall_status.set('Overall: Full voice-over batch paused safely')
        self.step_status.set('Current task: Restart GameDubber to resume from the first unverified ASR line')
        self.current_line.set('Current dialogue: Batch paused; generated WEMs and ASR checks were preserved.')
        self._append_log(f"> Full {context['engine_name']} batch paused safely | Run: {context['run_dir']}")
        messagebox.showinfo('Full batch paused', f"The batch was paused safely.\n\nGenerated files and ASR checkpoints were preserved. Restart GameDubber to resume from the first line that has not passed ASR validation.\n\nDetailed log:\n{context['batch_log']}", parent=self)
        self.action_button.configure(text='Restart GameDubber to Resume', state='disabled')
    elif finished == 0:
        self.overall.configure(maximum=11, value=11)
        self.step.configure(value=context['unique_count'])
        self.overall_status.set(f"Overall: Full {context['language']} Wwise Vorbis WEM generation complete with {context['engine_name']}")
        self.step_status.set('Current task: Primary production phase complete; follow-up phases are not started')
        self.current_line.set('Current dialogue: Full production run completed.')
        self._append_log(f"> Full {context['engine_name']} batch complete | Run: {context['run_dir']}")
        messagebox.showinfo('Primary production phase complete', f"Subtitle-driven voice generation and ASR validation completed with {context['engine_name']}.\n\nNo exception processing, original-English retention, Vox fallback, or BA2 packaging was started.\nDeferred rows, if any, were saved in the run folder for a later phase.\n\nRun folder:\n{context['run_dir']}\n\nDetailed log:\n{context['batch_log']}", parent=self)
        self.action_button.configure(text='Primary Production Phase Complete', state='disabled')
    else:
        self.overall.configure(maximum=11, value=10)
        self.overall_status.set('Overall: Full voice-over batch ended with errors')
        self.step_status.set('Current task: Inspect detailed batch log and SQLite results')
        self._append_log(f"> Full {context['engine_name']} batch ended | Exit code: {finished} | Log: {context['batch_log']}")
        messagebox.showerror('Full batch ended with errors', f"The batch ended with errors.\n\nDetailed log:\n{context['batch_log']}\n\nCompleted files, if any, remain in its run folder.", parent=self)
        completed = _resume_completed_count(Path(context['run_dir']))
        if completed and completed < int(context.get('unique_count', 0)):
            _write_production_resume_state(context, 'paused')
            self.action_button.configure(text='Restart GameDubber to Resume', state='disabled')
        else:
            self.action_button.configure(text='Start New Full Voice-over Batch', command=self._choose_generation_model, state='normal')


def _choose_generation_model(self) -> None:
    import tkinter as tk
    language = self.target_language.get().strip()
    target_code = language.rsplit('(', 1)[1].rstrip(')').lower() if '(' in language else ''
    entries = [entry for entry in _available_generation_models() if _engine_supports_target_language(entry[1], target_code)]
    generation_entries = [entry for entry in entries if entry[1] != 'whisper_asr']
    if not generation_entries:
        messagebox.showerror('No generation model', 'No complete local generation adapter was found.', parent=self)
        return
    labels = [entry[0] for entry in entries]
    details = {entry[0]: entry[2] for entry in entries}
    identifiers = {entry[0]: entry[1] for entry in entries}
    dialog = tk.Toplevel(self)
    dialog.title('Select Target Voice-over Model')
    dialog.transient(self)
    dialog.resizable(False, False)
    panel = ttk.Frame(dialog, padding=16)
    panel.grid(sticky='nsew')
    ttk.Label(panel, text='Target voice-over generation model').grid(row=0, column=0, sticky='w')
    default_model = next((label for label, engine, _detail in generation_entries if engine == 'xtts_v2'), generation_entries[0][0])
    selected = tk.StringVar(value=default_model)
    detail = tk.StringVar(value=details[selected.get()])
    chooser = ttk.Combobox(panel, textvariable=selected, values=labels, state='readonly', width=68)
    chooser.grid(row=1, column=0, sticky='ew', pady=(6, 10))
    ttk.Label(panel, textvariable=detail, justify='left', wraplength=550).grid(row=2, column=0, sticky='w', pady=(0, 12))
    def refresh_detail(*_args):
        detail.set(details[selected.get()])
    selected.trace_add('write', refresh_detail)
    def confirm() -> None:
        model_id = identifiers[selected.get()]
        if model_id == 'whisper_asr':
            messagebox.showinfo('ASR model', detail.get() + '\n\nChoose a voice-over generation model for the full batch.', parent=dialog)
            return
        self._selected_generation_model = model_id
        self._append_log(f'> Selected generation model: {selected.get()}')
        dialog.destroy()
        if model_id == 'voxcpm2':
            _choose_voxcpm_steps(self)
            return
        self._start_full_voiceover_batch()
    ttk.Button(panel, text='Use Selected Model', command=confirm).grid(row=3, column=0, sticky='ew')
    dialog.update_idletasks()
    x = self.winfo_rootx() + (self.winfo_width() - dialog.winfo_reqwidth()) // 2
    y = self.winfo_rooty() + (self.winfo_height() - dialog.winfo_reqheight()) // 2
    dialog.geometry(f'+{max(0, x)}+{max(0, y)}')
    dialog.grab_set()


def _choose_voxcpm_steps(self) -> None:
    import tkinter as tk
    dialog = tk.Toplevel(self)
    dialog.title('Select VoxCPM2 Diffusion Steps')
    dialog.transient(self)
    dialog.resizable(False, False)
    panel = ttk.Frame(dialog, padding=16)
    panel.grid(sticky='nsew')
    ttk.Label(panel, text='VoxCPM2 diffusion steps').grid(row=0, column=0, sticky='w')
    selected = tk.StringVar(value=str(getattr(self, '_selected_voxcpm_steps', 6)))
    chooser = ttk.Combobox(panel, textvariable=selected, values=('4', '5', '6', '7', '8', '10'), state='readonly', width=12)
    chooser.grid(row=1, column=0, sticky='w', pady=(6, 8))
    ttk.Label(panel, text='6 is the recommended balance of speed and quality.\nMore steps take longer; fewer steps are faster.', justify='left').grid(row=2, column=0, sticky='w', pady=(0, 12))
    def confirm() -> None:
        try:
            steps = int(selected.get())
        except ValueError:
            return
        self._selected_voxcpm_steps = steps
        self._append_log(f"> Selected VoxCPM2 diffusion steps: {steps}")
        dialog.destroy()
        self._start_full_voiceover_batch()

    ttk.Button(panel, text='Use Selected VoxCPM2 Steps', command=confirm).grid(row=3, column=0, sticky='ew')
    dialog.update_idletasks()
    x = self.winfo_rootx() + (self.winfo_width() - dialog.winfo_reqwidth()) // 2
    y = self.winfo_rooty() + (self.winfo_height() - dialog.winfo_reqheight()) // 2
    dialog.geometry(f'+{max(0, x)}+{max(0, y)}')
    dialog.grab_set()


_previous_model_install = install


def install(cls):
    _previous_model_install(cls)
    cls._choose_generation_model = _choose_generation_model
    cls._choose_voxcpm_steps = _choose_voxcpm_steps
    cls._start_full_voiceover_batch = _start_full_voiceover_batch_with_adapter
    cls._poll_full_voiceover_batch = _poll_full_voiceover_batch_with_adapter
# Resume state is deliberately outside the run folder, so the fresh child can
# still create that directory atomically on its first invocation.
def _production_resume_state_path() -> Path:
    from game_dubber_gui import WORK_ROOT
    return WORK_ROOT / 'production_resume.json'


def _write_production_run_state(run_dir: Path, language: str, engine: str, engine_name: str, total: int, status: str, voxcpm_steps: int | None = None, game_path: str = '') -> None:
    path = _production_resume_state_path()
    payload = {
        'status': status, 'run_dir': str(run_dir), 'language': language,
        'engine': engine, 'engine_name': engine_name, 'total': total,
        'updated_at': datetime.now(timezone.utc).isoformat(),
    }
    if engine == 'voxcpm2' and voxcpm_steps is not None:
        payload['voxcpm_steps'] = voxcpm_steps
    if game_path:
        payload['game_path'] = game_path
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def _write_production_resume_state(context: dict, status: str) -> None:
    _write_production_run_state(context['run_dir'], context['language'], context['engine'], context['engine_name'], int(context['unique_count']), status, context.get('voxcpm_steps'), str(context.get('game_path', '')))


def _clear_production_resume_state() -> None:
    _production_resume_state_path().unlink(missing_ok=True)


def _resume_completed_count(run_dir: Path) -> int:
    results = Path(run_dir) / 'results.jsonl'
    if not results.is_file():
        return 0
    completed: set[str] = set()
    with results.open(encoding='utf-8') as handle:
        for line in handle:
            try:
                row = json.loads(line)
                # Resume is based exclusively on the final production WEM.
                # A temporary WAV/Opus file is not a completed game asset.
                output = Path(str(row.get('output_wem_path') or ''))
                if row.get('status') == 'wem_generated' and output.is_file() and output.stat().st_size > 0:
                    completed.add(str(row.get('source_audio_path', '')))
            except (ValueError, TypeError):
                continue
    return len(completed)


def _find_starfield_wwise_console() -> tuple[Path | None, str]:
    """Find the exact Wwise build used by the local Starfield project."""
    executable = Path('Authoring') / 'x64' / 'Release' / 'bin' / 'WwiseConsole.exe'
    roots = (Path(r'C:\Audiokinetic'), Path(r'C:\Program Files\Audiokinetic'), Path(r'C:\Program Files (x86)\Audiokinetic'))
    candidates: list[Path] = []
    for root in roots:
        if root.is_dir():
            candidates.extend(root.glob('Wwise_2021.1.10*/' + str(executable)))
    seen: set[Path] = set()
    found_versions: list[str] = []
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        try:
            probe = subprocess.run([str(candidate), 'help'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=12, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            details = (probe.stdout or '') + (probe.stderr or '')
        except (OSError, subprocess.TimeoutExpired) as error:
            found_versions.append(f'{candidate}: unavailable ({error})')
            continue
        if 'v2021.1.10' in details and 'Build no.7883' in details:
            return candidate, 'Wwise 2021.1.10.7883 detected — Starfield-compatible Custom Vorbis encoder ready.'
        first_line = next((line.strip() for line in details.splitlines() if 'Wwise |' in line), candidate.name)
        found_versions.append(first_line)
    if found_versions:
        return None, 'Wwise found, but not the required Starfield build 2021.1.10.7883: ' + '; '.join(found_versions)
    return None, 'Wwise 2021.1.10.7883 was not found. Install Authoring + Windows through Audiokinetic Launcher.'


def _detect_wwise_installation(self) -> None:
    console, message = _find_starfield_wwise_console()
    self._wwise_console_path = console
    self._wwise_detection_message = message
    self._append_log('> ' + message)
    if console is None and not getattr(self, '_wwise_requirement_notice_shown', False):
        self._wwise_requirement_notice_shown = True
        messagebox.showwarning(
            'Required Wwise version not installed',
            'GameDubber requires the exact Wwise version used by the Starfield Creation Kit:\n\n'
            'Wwise Authoring 2021.1.10.7883\n'
            'Packages: Authoring + Microsoft / Windows\n\n'
            'Do not install a newer Wwise version for this pipeline.\n'
            'The Wwise Vorbis encoder must match Starfield to create compatible WEM files.',
            parent=self,
        )


def _resume_game_folder(state: dict) -> str:
    """Read the saved game folder, with discovery as a legacy-run fallback."""
    saved = str(state.get('game_path', '')).strip()
    if saved:
        return saved
    try:
        from game_dubber_gui import WORK_ROOT
        report = json.loads((WORK_ROOT / 'discovery' / 'dialogue_sources.json').read_text(encoding='utf-8'))
        return str(report.get('game_path', '')).strip()
    except (OSError, ValueError, TypeError):
        return ''


def _ask_production_resume(self, state: dict, completed: int, total: int, game_folder: str) -> bool:
    """Ask explicitly whether to continue now or close without changing state."""
    import tkinter as tk

    dialog = tk.Toplevel(self)
    dialog.withdraw()
    dialog.title('Resume production batch')
    dialog.transient(self)
    dialog.resizable(False, False)
    answer = {'proceed': False}

    panel = ttk.Frame(dialog, padding=18)
    panel.grid(sticky='nsew')
    ttk.Label(
        panel,
        text=(
            'A previous production batch was stopped.\n\n'
            f'Model: {state.get("engine_name", state.get("engine", ""))}\n'
            f'Language: {state.get("language", "")}\n'
            f'Game folder: {game_folder or "Not recorded"}\n'
            f'Verified WEMs already generated: {completed:,} of {total:,}\n\n'
            'Continue from the next unfinished item?'
        ),
        justify='left',
        wraplength=540,
    ).grid(row=0, column=0, columnspan=2, sticky='w')

    def choose(proceed: bool) -> None:
        answer['proceed'] = proceed
        dialog.destroy()

    ttk.Button(panel, text='Not now', command=lambda: choose(False)).grid(row=1, column=0, sticky='ew', padx=(0, 8), pady=(18, 0))
    ttk.Button(panel, text='Yes, proceed', command=lambda: choose(True)).grid(row=1, column=1, sticky='ew', pady=(18, 0))
    panel.columnconfigure(0, weight=1)
    panel.columnconfigure(1, weight=1)
    dialog.protocol('WM_DELETE_WINDOW', lambda: choose(False))
    dialog.update_idletasks()
    x = self.winfo_rootx() + (self.winfo_width() - dialog.winfo_reqwidth()) // 2
    y = self.winfo_rooty() + (self.winfo_height() - dialog.winfo_reqheight()) // 2
    dialog.geometry(f'+{max(0, x)}+{max(0, y)}')
    dialog.deiconify()
    dialog.lift()
    dialog.grab_set()
    dialog.wait_window()
    return bool(answer['proceed'])


def _offer_production_resume(self) -> None:
    path = _production_resume_state_path()
    if getattr(self, '_full_batch_running', False):
        return
    # Reading a large prior run can take a moment.  Make this explicit with a
    # passive notice; it intentionally has no action buttons.
    import tkinter as tk
    detecting = tk.Toplevel(self)
    self._resume_detection_dialog = detecting
    detecting.withdraw()
    detecting.title('Checking production checkpoint')
    detecting.transient(self)
    detecting.resizable(False, False)
    detecting.protocol('WM_DELETE_WINDOW', lambda: None)
    ttk.Label(detecting, text='Detecting previous production checkpoint…', padding=(22, 16)).pack()
    detecting.update_idletasks()
    x = self.winfo_rootx() + max(0, (self.winfo_width() - detecting.winfo_reqwidth()) // 2)
    y = self.winfo_rooty() + max(0, (self.winfo_height() - detecting.winfo_reqheight()) // 2)
    detecting.geometry(f'+{x}+{y}')
    detecting.deiconify()
    detecting.lift()
    detecting.update()
    try:
        _offer_production_resume_after_detection(self, path)
    finally:
        if detecting.winfo_exists():
            detecting.destroy()


def _offer_production_resume_after_detection(self, path: Path) -> None:
    """Resume discovery after its temporary, non-interactive status notice."""
    # Recover a run whose child process failed before the old GUI could retain
    # its resume-state file.  Only an incomplete run with real final WEMs is
    # eligible; completed runs are never offered as resumes.
    if not path.is_file():
        from game_dubber_gui import WORK_ROOT
        runs_root = WORK_ROOT / 'output' / 'target_voice' / 'runs'
        candidates = sorted((item for item in runs_root.glob('run-*') if item.is_dir() and (item / 'results.jsonl').is_file()), key=lambda item: item.stat().st_mtime, reverse=True) if runs_root.is_dir() else []
        if not candidates:
            return
        run_dir = candidates[0]
        total = _batch_unique_count(WORK_ROOT / 'manifests' / 'voice_map.jsonl') if (WORK_ROOT / 'manifests' / 'voice_map.jsonl').is_file() else 0
        completed = _resume_completed_count(run_dir)
        # Run IDs include timestamp components, for example
        # run-20260802-231206-it-xtts_v2.  The final two components are the
        # durable target-language and engine identifiers.
        suffix = run_dir.name.split('-')
        language = suffix[-2] if len(suffix) >= 3 else ''
        engine = suffix[-1] if len(suffix) >= 3 else ''
        if not completed or not total or completed >= total or _generation_engine_config(engine) is None:
            return
        game_folder = _resume_game_folder({})
        _write_production_run_state(run_dir, language, engine, engine, total, 'paused', game_path=game_folder)
        self._append_log(f'> Recovered incomplete production run for resume: {completed:,} final WEMs | {run_dir}')
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
        run_dir = Path(str(state['run_dir']))
        engine = str(state['engine'])
        language = str(state['language'])
    except (OSError, ValueError, KeyError, TypeError):
        _clear_production_resume_state()
        return
    if not run_dir.is_dir() or state.get('status') != 'paused' or _generation_engine_config(engine) is None:
        _clear_production_resume_state()
        return
    completed = _resume_completed_count(run_dir)
    total = int(state.get('total', 0))
    game_folder = _resume_game_folder(state)
    if game_folder:
        self.game_path.set(game_folder)
        self._refresh_disk_status()
    # The passive detection notice must be gone before the actionable resume
    # dialog is created; otherwise it can remain above it on Windows.
    notice = getattr(self, '_resume_detection_dialog', None)
    if notice is not None:
        try:
            if notice.winfo_exists():
                notice.destroy()
        except Exception:
            pass
        self._resume_detection_dialog = None
    if not _ask_production_resume(self, state, completed, total, game_folder):
        self._append_log('> Production resume deferred; checkpoint retained and the GUI remains available.')
        self.step_status.set('Current task: Resume deferred. Restart GameDubber to resume the process.')
        self.current_line.set('Current dialogue: Production batch is paused; use Reset local pipeline data to discard it.')
        self.action_button.configure(text='Restart GameDubber to resume the process', state='disabled')
        return
    # A recovered run stores the compact archive code (for example ``it``),
    # while the GUI validator requires the displayed ``Italian (it)`` form.
    language_labels = {
        'de': 'German (de)', 'en': 'English (en)', 'es': 'Spanish (es)',
        'fr': 'French (fr)', 'it': 'Italian (it)', 'ja': 'Japanese (ja)',
        'pl': 'Polish (pl)', 'ptbr': 'Portuguese, Brazil (ptbr)',
        'zhhans': 'Chinese, Simplified (zhhans)',
    }
    self.target_language.set(language if '(' in language else language_labels.get(language.lower(), language))
    self._selected_generation_model = engine
    if engine == 'voxcpm2':
        try:
            self._selected_voxcpm_steps = int(state.get('voxcpm_steps', 6))
        except (TypeError, ValueError):
            self._selected_voxcpm_steps = 6
    self._append_log(f'> Resuming production batch from {completed:,} verified final WEMs: {run_dir}')
    self._start_full_voiceover_batch(run_dir)


def _start_target_preview_player(preview_root: Path, runtime: Path) -> None:
    """Launch the detached, target-language-only preview player for the active run."""
    preview_root.mkdir(parents=True, exist_ok=True)
    bundle_root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    acknowledge_wav = bundle_root / 'tools' / 'acknowledge.wav'
    script = (
        "import os, sys, time, winsound\n"
        "from pathlib import Path\n"
        "root = Path(sys.argv[1])\n"
        "acknowledge = Path(sys.argv[2])\n"
        "disabled = root / '_preview_disabled'\n"
        "stop = root / '_stop'\n"
        "pid_file = root / '_player.pid'\n"
        "pid_file.write_text(str(os.getpid()), encoding='ascii')\n"
        "try:\n"
        "    while True:\n"
        "        if disabled.is_file(): break\n"
        "        items = sorted(root.glob('*.wav'))\n"
        "        if items:\n"
        "            item = items[0]\n"
        "            try:\n"
        "                if acknowledge.is_file(): winsound.PlaySound(str(acknowledge), winsound.SND_FILENAME)\n"
        "                winsound.PlaySound(str(item), winsound.SND_FILENAME)\n"
        "            except (OSError, RuntimeError): pass\n"
        "            finally:\n"
        "                item.unlink(missing_ok=True)\n"
        "                Path(str(item) + '.done').touch()\n"
        "            continue\n"
        "        if stop.is_file(): break\n"
        "        time.sleep(0.05)\n"
        "finally:\n"
        "    pid_file.unlink(missing_ok=True)\n"
        "    stop.unlink(missing_ok=True)\n"
    )
    subprocess.Popen(
        [str(runtime), '-u', '-c', script, str(preview_root), str(acknowledge_wav)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )


def _apply_preview_wav_choice(self, attempt: int = 0) -> None:
    """Apply the checkbox only after the child initialized its preview queue."""
    context = getattr(self, '_full_batch_context', None)
    if not context or not getattr(self, '_full_batch_running', False):
        return
    preview_root = Path(context['run_dir']) / '_preview_audio'
    ready_path = preview_root / '_preview_capability_ready'
    if not ready_path.is_file():
        if attempt < 300:
            self.after(100, lambda: _apply_preview_wav_choice(self, attempt + 1))
        return

    disabled_path = preview_root / '_preview_disabled'
    enabled = bool(self.preview_wav_playback_enabled.get())
    context['preview_wavs'] = enabled
    if enabled:
        disabled_path.unlink(missing_ok=True)
        config = _generation_engine_config(str(context.get('engine', 'voxcpm2')))
        if config is None or not config[1].is_file():
            self._append_log('> Target-language WAV preview could not be enabled: local Python runtime is unavailable.')
            return
        if not context.get('preview_player_started', False):
            _start_target_preview_player(preview_root, config[1])
            context['preview_player_started'] = True
            self._append_log('> Target-language WAV preview enabled for the active batch.')
        return

    disabled_path.touch()
    context['preview_player_started'] = False
    player_pid = preview_root / '_player.pid'
    try:
        pid = int(player_pid.read_text(encoding='ascii').strip())
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, ValueError):
        pass
    self._append_log('> Target-language WAV preview disabled; audio player terminated immediately.')


def _on_preview_wav_playback_changed(self, *_unused) -> None:
    """Apply a preview toggle without racing creation of a fresh run folder."""
    if getattr(self, '_full_batch_context', None):
        _apply_preview_wav_choice(self)


def _ask_stop_production_batch(self) -> bool:
    """Ask for a safe stop in a dialog centred over the GameDubber window."""
    answer = {'pause': False}
    dialog = tk.Toplevel(self)
    dialog.withdraw()
    dialog.title('Stop production batch?')
    dialog.transient(self)
    dialog.resizable(False, False)

    def close(pause: bool = False) -> None:
        answer['pause'] = pause
        dialog.destroy()

    dialog.protocol('WM_DELETE_WINDOW', close)
    frame = ttk.Frame(dialog, padding=24)
    frame.grid(sticky='nsew')
    ttk.Label(frame, text='Temporarily stop the production batch?', font=('Segoe UI', 11, 'bold')).grid(row=0, column=0, columnspan=2, sticky='w')
    ttk.Label(
        frame,
        text=('Yes: finish the current WEM, save the resume checkpoint, and close the app.\n'
              'No: keep the app open while the batch continues.'),
        justify='left', wraplength=500,
    ).grid(row=1, column=0, columnspan=2, sticky='w', pady=(12, 18))
    ttk.Button(frame, text='No, continue', command=close).grid(row=2, column=0, sticky='e', padx=(0, 8))
    ttk.Button(frame, text='Yes, pause safely', command=lambda: close(True)).grid(row=2, column=1, sticky='e')
    dialog.update_idletasks()
    owner_x, owner_y = self.winfo_rootx(), self.winfo_rooty()
    owner_w, owner_h = self.winfo_width(), self.winfo_height()
    dialog.geometry(f'+{owner_x + max(0, (owner_w - dialog.winfo_reqwidth()) // 2)}+{owner_y + max(0, (owner_h - dialog.winfo_reqheight()) // 2)}')
    dialog.deiconify()
    dialog.lift()
    dialog.grab_set()
    dialog.wait_window()
    return bool(answer['pause'])


def _handle_production_window_close(self) -> None:
    if not getattr(self, '_full_batch_running', False):
        self.destroy()
        return
    if getattr(self, '_closing_after_production_pause', False):
        return
    if not _ask_stop_production_batch(self):
        return
    context = getattr(self, '_full_batch_context', None)
    if not context:
        return
    self._closing_after_production_pause = True
    self._full_batch_running = False
    self.action_button.configure(state='disabled')
    # Persist the request before creating any window.  Even an unexpected
    # application termination can then be recovered as a resumable run.
    run_dir = Path(context['run_dir'])
    request_path = run_dir / '_pause_requested'
    request_path.touch(exist_ok=True)
    _write_production_resume_state(context, 'pause_requested')
    self.step_status.set('Current task: Waiting for the last active process to finish…')
    self.current_line.set('Current dialogue: Safe pause requested; no new dialogue line will start.')
    self._append_log('> Safe pause requested. Waiting for the last active generation/encoding process before closing.')
    dialog = tk.Toplevel(self)
    dialog.withdraw()
    dialog.title('Completing last task')
    dialog.transient(self)
    dialog.resizable(False, False)
    dialog.protocol('WM_DELETE_WINDOW', lambda: None)
    frame = ttk.Frame(dialog, padding=24)
    frame.grid(sticky='nsew')
    ttk.Label(frame, text='Completing last task…', font=('Segoe UI', 11, 'bold')).grid(row=0, column=0, sticky='w')
    ttk.Label(
        frame,
        text=('The current WEM is being written and verified.\n'
              'The resume checkpoint will then be saved and GameDubber will close automatically.'),
        justify='left',
        wraplength=430,
    ).grid(row=1, column=0, sticky='w', pady=(12, 0))
    dialog.update_idletasks()
    owner_x, owner_y = self.winfo_rootx(), self.winfo_rooty()
    owner_w, owner_h = self.winfo_width(), self.winfo_height()
    dialog.geometry(f'+{owner_x + max(0, (owner_w - dialog.winfo_reqwidth()) // 2)}+{owner_y + max(0, (owner_h - dialog.winfo_reqheight()) // 2)}')
    dialog.deiconify()
    dialog.lift()
    self._safe_pause_dialog = dialog
    self._wait_for_safe_production_pause()


def _wait_for_safe_production_pause(self) -> None:
    """Close only after the child exits at a WEM-safe cooperative boundary."""
    context = getattr(self, '_full_batch_context', None)
    if not context:
        self.destroy()
        return
    run_dir = Path(context['run_dir'])
    process = getattr(self, '_full_batch_process', None)
    if not run_dir.is_dir() or process is None:
        self.after(150, self._wait_for_safe_production_pause)
        return
    request_path = run_dir / '_pause_requested'
    if not request_path.exists():
        request_path.touch()
        _write_production_resume_state(context, 'pause_requested')
    if process.poll() is None:
        self.after(200, self._wait_for_safe_production_pause)
        return
    _write_production_resume_state(context, 'paused')
    self._append_log('> Last active process finished. Safe pause checkpoint saved; closing GameDubber.')
    dialog = getattr(self, '_safe_pause_dialog', None)
    if dialog is not None:
        try:
            dialog.destroy()
        except tk.TclError:
            pass
        self._safe_pause_dialog = None
    self.destroy()


_previous_resume_install = install


def install(cls):
    _previous_resume_install(cls)
    original_init = cls.__init__
    def resume_aware_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.protocol('WM_DELETE_WINDOW', self._handle_production_window_close)
        self.preview_wav_playback_enabled.trace_add('write', self._on_preview_wav_playback_changed)
        self.after(250, self._detect_wwise_installation)
        self.after(500, self._offer_production_resume)
    cls.__init__ = resume_aware_init
    original_poll = cls._poll_full_voiceover_batch
    def resume_aware_poll(self, *args, **kwargs):
        was_running = bool(getattr(self, '_full_batch_running', False))
        original_poll(self, *args, **kwargs)
        if was_running and not getattr(self, '_full_batch_running', False):
            context = getattr(self, '_full_batch_context', None)
            if context:
                # A resume is offered only after the user explicitly paused the app.
                # A failed/terminated child is not a resumable checkpoint.
                try:
                    paused = json.loads(_production_resume_state_path().read_text(encoding='utf-8')).get('status') == 'paused'
                except (OSError, ValueError, TypeError):
                    paused = False
                if not paused:
                    completed = _resume_completed_count(Path(context['run_dir']))
                    total = int(context.get('unique_count', 0))
                    if completed and total and completed < total:
                        _write_production_resume_state(context, 'paused')
                        self._append_log(f'> Batch stopped with an error; resumable checkpoint retained at {completed:,} final WEMs.')
                    else:
                        _clear_production_resume_state()
    cls._poll_full_voiceover_batch = resume_aware_poll
    cls._offer_production_resume = _offer_production_resume
    cls._handle_production_window_close = _handle_production_window_close
    cls._wait_for_safe_production_pause = _wait_for_safe_production_pause
    cls._on_preview_wav_playback_changed = _on_preview_wav_playback_changed
    cls._detect_wwise_installation = _detect_wwise_installation


def _edit_phonetic_dictionary(self) -> None:
    """Let the user select a generation backend and edit its JSON dictionary."""
    from game_dubber_gui import WORK_ROOT
    from phonetic_dictionaries import ensure_dictionary

    models = [entry for entry in _available_generation_models() if entry[1] != 'whisper_asr']
    if not models:
        messagebox.showerror('Phonetic dictionaries', 'No installed generation model is available.', parent=self)
        return
    window = tk.Toplevel(self)
    window.title('Select Model Dictionary')
    window.transient(self)
    window.resizable(False, False)
    ttk.Label(window, text='Choose the model dictionary to edit:').grid(row=0, column=0, padx=14, pady=(14, 6), sticky='w')
    names = [name for name, _engine, _description in models]
    selection = tk.StringVar(value=names[0])
    chooser = ttk.Combobox(window, values=names, state='readonly', textvariable=selection, width=54)
    chooser.grid(row=1, column=0, padx=14, pady=(0, 12), sticky='ew')

    def open_selected() -> None:
        index = names.index(selection.get())
        engine = models[index][1]
        path = ensure_dictionary(WORK_ROOT / 'phonetic_dictionaries', engine)
        try:
            subprocess.Popen(['notepad.exe', str(path)], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError as error:
            messagebox.showerror('Phonetic dictionaries', f'Could not open the Windows editor:\n{error}', parent=window)
            return
        self._append_log(f'> Opened editable phonetic dictionary: {path}')
        window.destroy()

    ttk.Button(window, text='Open Dictionary in Notepad', command=open_selected).grid(row=2, column=0, padx=14, pady=(0, 14), sticky='ew')
    # Tk's default placement is desktop-relative. Centre the compact chooser
    # over GameDubber instead, after the widgets have their real dimensions.
    window.update_idletasks()
    x = self.winfo_rootx() + max(0, (self.winfo_width() - window.winfo_reqwidth()) // 2)
    y = self.winfo_rooty() + max(0, (self.winfo_height() - window.winfo_reqheight()) // 2)
    window.geometry(f'+{x}+{y}')
    window.grab_set()
    chooser.focus_set()


_previous_phonetic_dictionary_install = install


def install(cls):
    _previous_phonetic_dictionary_install(cls)
    original_build = cls._build

    def build_with_phonetic_dictionary_button(self):
        original_build(self)
        from game_dubber_gui import WORK_ROOT
        from phonetic_dictionaries import ensure_dictionary
        for _name, engine, _description in _available_generation_models():
            if engine == 'whisper_asr':
                continue
            ensure_dictionary(WORK_ROOT / 'phonetic_dictionaries', engine)
        self.phonetic_dictionary_button = ttk.Button(
            self.log.master,
            text='Edit Phonetic Dictionary...',
            command=self._edit_phonetic_dictionary,
            width=0,
        )
        # Keep this utility separate from pipeline controls: it occupies only
        # its natural text width at the upper-right edge of the main panel.
        self.phonetic_dictionary_button.place(relx=1.0, x=0, y=0, anchor='ne')

    cls._build = build_with_phonetic_dictionary_button
    cls._edit_phonetic_dictionary = _edit_phonetic_dictionary


# Embedded real-time validation review --------------------------------------

def _review_run_directory(self) -> Path | None:
    context = getattr(self, '_full_batch_context', None)
    if context and Path(context.get('run_dir', '')).is_dir():
        return Path(context['run_dir'])
    from game_dubber_gui import WORK_ROOT
    runs = WORK_ROOT / 'output' / 'target_voice' / 'runs'
    candidates = [path for path in runs.glob('run-*') if path.is_dir()]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def _review_sync(self, run_dir: Path) -> dict:
    """Incrementally read append-only production and ASR journals."""
    cache = getattr(self, '_review_cache', None)
    if not isinstance(cache, dict) or cache.get('run_dir') != str(run_dir):
        cache = {'run_dir': str(run_dir), 'results_offset': 0, 'asr_offset': 0,
                 'rows': {}, 'asr': {}, 'manual': {}, 'manual_loaded': False, 'version': 0}
        self._review_cache = cache
    changed = False

    def consume(path: Path, offset_key: str, consume_row) -> None:
        if not path.is_file():
            return
        try:
            with path.open('r', encoding='utf-8', errors='replace') as handle:
                handle.seek(int(cache.get(offset_key, 0)))
                for line in handle:
                    try:
                        consume_row(json.loads(line))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                cache[offset_key] = handle.tell()
        except OSError:
            return

    def save_result(row: dict) -> None:
        nonlocal changed
        source = str(row.get('source_audio_path', ''))
        if source:
            changed = changed or cache['rows'].get(source) != row
            cache['rows'][source] = row

    def save_asr(row: dict) -> None:
        nonlocal changed
        source = str(row.get('source_audio_path', ''))
        if source:
            attempts = row.get('attempts') or []
            value = {
                'validated': bool(row.get('satisfactory')),
                'attempts': len(attempts),
            }
            changed = changed or cache['asr'].get(source) != value
            cache['asr'][source] = value

    consume(run_dir / 'results.jsonl', 'results_offset', save_result)
    consume(run_dir / 'asr_checkpoint_checks.jsonl', 'asr_offset', save_asr)
    if not cache['manual_loaded']:
        from game_dubber_gui import WORK_ROOT
        database = WORK_ROOT / 'voice_pipeline.db'
        try:
            connection = sqlite3.connect(database, timeout=0.2)
            connection.execute("""CREATE TABLE IF NOT EXISTS production_voice_review_overrides (
                run_id TEXT NOT NULL, source_audio_path TEXT NOT NULL,
                validated INTEGER NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (run_id, source_audio_path))""")
            for source, validated in connection.execute(
                'SELECT source_audio_path, validated FROM production_voice_review_overrides WHERE run_id=?',
                (run_dir.name,),
            ):
                cache['manual'][str(source)] = bool(validated)
            connection.commit(); connection.close()
            cache['manual_loaded'] = True
            changed = True
        except sqlite3.Error:
            pass
    if changed:
        cache['version'] = int(cache.get('version', 0)) + 1
    return cache


def _review_state(cache: dict, source: str, row: dict) -> tuple[str, str, int]:
    if row.get('status') == 'deferred_short_reference':
        return 'deferred', 'Deferred', 0
    wem = Path(str(row.get('output_wem_path', '')))
    available = row.get('status') == 'wem_generated' and wem.is_file()
    if not available:
        return 'available', 'WEM unavailable', 0
    if source in cache['manual']:
        return ('validated', 'Validated (manual)', 0) if cache['manual'][source] else ('not_validated', 'Not validated (manual)', 0)
    asr = cache['asr'].get(source)
    if asr:
        return ('validated', 'Validated', int(asr['attempts'])) if asr['validated'] else ('not_validated', 'Not validated', int(asr['attempts']))
    return 'not_validated', 'Not validated', 0


def _refresh_embedded_validation_report(self, schedule: bool = True) -> None:
    tree = getattr(self, 'review_tree', None)
    if tree is None or not tree.winfo_exists():
        return
    run_dir = _review_run_directory(self)
    if run_dir is None:
        self.review_status.set('Report: no production run available')
        if schedule:
            self.after(1000, self._refresh_embedded_validation_report)
        return
    cache = _review_sync(self, run_dir)
    prepared = []
    counts = {'available': 0, 'deferred': 0, 'validated': 0, 'not_validated': 0, 'rejected': 0}
    search_text = str(getattr(self, '_review_search_applied', '')).strip().casefold()
    for source, row in cache['rows'].items():
        state, label, attempts = _review_state(cache, source, row)
        counts[state] += 1
        if state == 'not_validated' and attempts >= 5:
            counts['rejected'] += 1
        if self.review_only_unvalidated.get() and state != 'not_validated':
            continue
        if search_text:
            searchable = ' '.join((
                str(row.get('official_subtitle', '')),
                str(row.get('english_subtitle', '')),
            )).casefold()
            if search_text not in searchable:
                continue
        prepared.append((int(row.get('number', 0)), source, row, state, label, attempts))
    prepared.sort(key=lambda item: (item[0] <= 0, item[0], item[1]))
    page_size = 1000
    pages = max(1, (len(prepared) + page_size - 1) // page_size)
    page = min(max(0, int(getattr(self, '_review_page', 0))), pages - 1)
    self._review_page = page
    render_key = (cache.get('version', 0), bool(self.review_only_unvalidated.get()), search_text, page)
    if render_key == getattr(self, '_review_render_key', None):
        if schedule:
            self.after(1000, self._refresh_embedded_validation_report)
        return
    self._review_render_key = render_key
    for item in tree.get_children():
        tree.delete(item)
    self._review_tree_rows = {}
    for number, source, row, state, label, attempts in prepared[page * page_size:(page + 1) * page_size]:
        item_id = f'r{number}_{zlib.crc32(source.encode("utf-8")) & 0xffffffff:08x}'
        english_duration = row.get('reference_duration_ms')
        tree.insert(
            '', 'end', iid=item_id,
            values=(
                f'{number:,}' if number else '—',
                str(row.get('official_subtitle', '')), label, f'{attempts}/5' if attempts else '—',
                f'{int(english_duration):,}' if isinstance(english_duration, (int, float)) else '—',
            ),
            tags=(state,),
        )
        self._review_tree_rows[item_id] = (source, row, state)
    self.review_previous_button.configure(state='normal' if page else 'disabled')
    self.review_next_button.configure(state='normal' if page + 1 < pages else 'disabled')
    self.review_jump_previous_button.configure(state='normal' if page >= 5 else 'disabled')
    self.review_jump_next_button.configure(state='normal' if page + 5 < pages else 'disabled')
    requires_review = max(0, counts['not_validated'] - counts['rejected'])
    search_suffix = f' | Search: {len(prepared):,} match(es)' if search_text else ''
    self.review_status.set(f'Validated: {counts["validated"]:,} | Deferred: {counts["deferred"]:,} | Requires review: {requires_review:,} | Rejected: {counts["rejected"]:,} | Page {page + 1}/{pages} ({len(prepared):,} journal entries){search_suffix}')
    if schedule:
        self.after(1000, self._refresh_embedded_validation_report)


def _change_review_page(self, delta: int) -> None:
    self._review_page = max(0, int(getattr(self, '_review_page', 0)) + delta)
    _refresh_embedded_validation_report(self, schedule=False)


def _schedule_review_search(self, *_args) -> None:
    """Debounce report filtering: wait one second after at least two characters."""
    pending = getattr(self, '_review_search_after_id', None)
    if pending is not None:
        try:
            self.after_cancel(pending)
        except tk.TclError:
            pass
        self._review_search_after_id = None
    query = str(self.review_search_text.get()).strip()
    if len(query) < 2:
        if getattr(self, '_review_search_applied', ''):
            self._review_search_applied = ''
            self._review_page = 0
            self._review_render_key = None
            _refresh_embedded_validation_report(self, schedule=False)
        return
    self._review_search_after_id = self.after(1000, lambda: _apply_review_search(self))


def _apply_review_search(self) -> None:
    self._review_search_after_id = None
    query = str(self.review_search_text.get()).strip()
    self._review_search_applied = query if len(query) >= 2 else ''
    self._review_page = 0
    self._review_render_key = None
    _refresh_embedded_validation_report(self, schedule=False)


def _focus_tracked_review_row(self, number: int) -> bool:
    """Select a visible tracked row and place it at the fifth position."""
    for item_id, (_source, row, _state) in getattr(self, '_review_tree_rows', {}).items():
        if int(row.get('number', 0)) == number:
            self.review_tree.selection_set(item_id)
            self.review_tree.focus(item_id)
            self.review_tree.update_idletasks()
            items = self.review_tree.get_children()
            try:
                item_index = items.index(item_id)
                # Treeview's fraction is based on the first visible row.
                # Put the tracked entry at row five (index four) wherever
                # possible; near the end Tk clamps naturally to the bottom.
                top_index = max(0, item_index - 4)
                self.review_tree.yview_moveto(top_index / max(1, len(items)))
            except (ValueError, tk.TclError):
                self.review_tree.see(item_id)
            return True
    return False


def _track_review_number(self, number: int) -> None:
    """Keep the embedded report focused on the active production/ASR row."""
    enabled = getattr(self, 'review_track_enabled', None)
    if enabled is None or not enabled.get() or number <= 0:
        return
    self._review_tracked_number = number
    # Tracking takes precedence over the review-only filter; otherwise a
    # freshly validated row could disappear at the exact moment it is worked.
    filter_changed = bool(self.review_only_unvalidated.get())
    if filter_changed:
        self.review_only_unvalidated.set(False)
    wanted_page = (number - 1) // 1000
    # Whisper can emit several events per second.  When the row already
    # belongs to the displayed page, moving selection/scrollbar is O(1);
    # rebuilding 1,000 Tk rows for every ASR line froze the interface.
    if not filter_changed and wanted_page == int(getattr(self, '_review_page', 0)) and _focus_tracked_review_row(self, number):
        return
    self._review_page = wanted_page
    self._review_render_key = None
    _refresh_embedded_validation_report(self, schedule=False)
    _focus_tracked_review_row(self, number)


def _review_click(self, event) -> None:
    tree = self.review_tree
    item_id = tree.identify_row(event.y)
    if not item_id or item_id not in getattr(self, '_review_tree_rows', {}):
        return
    source, row, state = self._review_tree_rows[item_id]
    if state != 'available':
        _play_review_wem(self, Path(str(row.get('output_wem_path', ''))), source)


def _review_context_menu(self, event) -> None:
    tree = self.review_tree
    item_id = tree.identify_row(event.y)
    if not item_id or item_id not in getattr(self, '_review_tree_rows', {}):
        return
    tree.selection_set(item_id)
    source, row, state = self._review_tree_rows[item_id]
    busy = bool(getattr(self, '_full_batch_running', False) or getattr(self, '_review_manual_running', False))
    menu = tk.Menu(self, tearoff=False)
    menu.add_command(label='Test with ASR', state='disabled' if busy or state == 'available' else 'normal', command=lambda: _test_review_asr(self, source, row))
    menu.add_separator()
    menu.add_command(label='●  Validate', foreground='#179b3a', state='disabled' if busy or state == 'available' else 'normal', command=lambda: _set_review_validation(self, source, True))
    menu.add_command(label='●  Reject', foreground='#d32121', state='disabled' if busy or state == 'available' else 'normal', command=lambda: _set_review_validation(self, source, False))
    menu.add_separator()
    menu.add_command(
        label='Listen to original English voice',
        state='disabled' if busy or state == 'available' else 'normal',
        command=lambda: _listen_to_original_english_voice(self, source, row),
    )
    menu.add_command(
        label='Use original English voice',
        state='disabled' if busy or state == 'available' else 'normal',
        command=lambda: _import_review_original_wem(self, source, row),
    )
    menu.add_separator()
    for backend, label in (
        ('translategemma', 'Transcribe, TranslateGemma and generate'),
        ('nllb', 'Transcribe, NLLB and generate'),
    ):
        translated_models = tk.Menu(menu, tearoff=False)
        for name, engine, _description in _available_generation_models():
            if engine != 'whisper_asr':
                translated_models.add_command(
                    label=name,
                    state='disabled' if busy or state == 'available' else 'normal',
                    command=lambda value=engine, translator=backend: _transcribe_translate_and_regenerate_review_row(
                        self, source, row, value, translator,
                    ),
                )
        menu.add_cascade(
            label=label, menu=translated_models,
            state='disabled' if busy or state == 'available' else 'normal',
        )
    menu.add_separator()
    models = tk.Menu(menu, tearoff=False)
    for name, engine, _description in _available_generation_models():
        if engine != 'whisper_asr':
            models.add_command(label=name, state='disabled' if busy or state == 'available' else 'normal', command=lambda value=engine: _regenerate_review_row(self, source, row, value))
    menu.add_cascade(label='Generate from subtitle', menu=models, state='disabled' if busy or state == 'available' else 'normal')
    menu.tk_popup(event.x_root, event.y_root)
    menu.grab_release()


def _set_review_validation(self, source: str, validated: bool) -> None:
    """Persist an explicit manual validation choice from the context menu."""
    run_dir = _review_run_directory(self)
    if run_dir is None or getattr(self, '_full_batch_running', False) or getattr(self, '_review_manual_running', False):
        return
    cache = _review_sync(self, run_dir)
    previous = cache['manual'].get(source)
    from game_dubber_gui import WORK_ROOT
    try:
        connection = sqlite3.connect(WORK_ROOT / 'voice_pipeline.db', timeout=0.5)
        connection.execute('INSERT OR REPLACE INTO production_voice_review_overrides VALUES (?, ?, ?, ?)', (run_dir.name, source, int(validated), datetime.now(timezone.utc).isoformat()))
        connection.commit(); connection.close()
        cache['manual'][source] = validated
        cache['version'] = int(cache.get('version', 0)) + 1
        self._review_override_history.append((run_dir.name, source, previous, validated))
        self._review_redo_history = []
        self.review_undo_button.configure(state='normal'); self.review_redo_button.configure(state='disabled')
        _refresh_embedded_validation_report(self, schedule=False)
    except sqlite3.Error as error:
        self._append_log(f'> Manual validation could not be saved: {error}')


def _manual_asr_match(expected_text: str, transcript: str) -> bool:
    """Dependency-free copy of the production ASR acceptance threshold."""
    def words(text: str) -> list[str]:
        folded = unicodedata.normalize('NFKD', str(text).lower())
        folded = ''.join(char for char in folded if not unicodedata.combining(char))
        return re.findall(r"[\w']+", folded, flags=re.UNICODE)
    expected, recognised = words(expected_text), words(transcript)
    if not expected:
        return bool(not recognised)
    previous = list(range(len(recognised) + 1))
    for index, token in enumerate(expected, 1):
        current = [index]
        for actual_index, actual in enumerate(recognised, 1):
            current.append(min(previous[actual_index] + 1, current[actual_index - 1] + 1, previous[actual_index - 1] + (token != actual)))
        previous = current
    distance = previous[-1] / len(expected)
    shared = sum(min(expected.count(token), recognised.count(token)) for token in set(expected))
    coverage = shared / len(expected)
    return bool(recognised) and distance <= 0.35 and coverage >= 0.70


def _listen_to_original_english_voice(self, source: str, row: dict) -> None:
    """Extract a disposable original WEM copy and play it without changing output."""
    if getattr(self, '_full_batch_running', False) or getattr(self, '_review_manual_running', False):
        return
    from game_dubber_gui import WORK_ROOT, HIDDEN_PROCESS
    reader, decoder = self._reader_path(), self._decoder_path()
    archive = Path(str(row.get('original_archive_path', '')))
    if not reader.is_file() or not decoder.is_file() or not archive.is_file():
        self._append_log('> Original English preview unavailable: archive reader, decoder, or source archive is missing.', 'asr_fail')
        return
    self._review_manual_running = True
    self._append_log(f'> Preparing original English voice preview: {source}')

    def worker() -> None:
        preview_root = WORK_ROOT / 'review_original_preview'
        staged = preview_root / f'{zlib.crc32(source.encode("utf-8")) & 0xffffffff:08x}.wem'
        try:
            preview_root.mkdir(parents=True, exist_ok=True)
            staged.unlink(missing_ok=True)
            extracted = subprocess.run(
                [str(reader), 'extract', str(archive), source, str(staged)],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                creationflags=HIDDEN_PROCESS,
            )
            if extracted.returncode or not staged.is_file() or staged.stat().st_size == 0:
                raise RuntimeError(extracted.stderr.strip() or extracted.stdout.strip() or 'archive reader produced no WEM')
            self.after(0, lambda: _play_review_wem(self, staged, source, cleanup_source=True))
            self.after(0, lambda: self._append_log('> Playing original English voice.'))
        except Exception as error:
            self.after(0, lambda value=str(error): self._append_log(f'> Original English preview ERROR | {value}', 'asr_fail'))
            staged.unlink(missing_ok=True)
        finally:
            self._review_manual_running = False

    threading.Thread(target=worker, name='review-original-english-preview', daemon=True).start()


def _import_review_original_wem(self, source: str, row: dict) -> None:
    """Replace one target WEM with its mapped English source and preview it."""
    if getattr(self, '_full_batch_running', False) or getattr(self, '_review_manual_running', False):
        return
    from game_dubber_gui import HIDDEN_PROCESS
    reader, decoder = self._reader_path(), self._decoder_path()
    archive = Path(str(row.get('original_archive_path', '')))
    output_wem = Path(str(row.get('output_wem_path', '')))
    if not reader.is_file() or not decoder.is_file() or not archive.is_file() or not output_wem.parent.is_dir():
        self._append_log('> Import original English WEM unavailable: archive reader, decoder, source archive, or output folder is missing.', 'asr_fail')
        return

    self._review_manual_running = True
    self._append_log(f'> Importing original English WEM: {source}')

    def worker() -> None:
        # Extract to a sibling staging file.  The archive reader never
        # overwrites and os.replace publishes only a complete WEM.
        staged = output_wem.with_name(f'{output_wem.stem}.original-import.tmp.wem')
        try:
            staged.unlink(missing_ok=True)
            extracted = subprocess.run(
                [str(reader), 'extract', str(archive), source, str(staged)],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                creationflags=HIDDEN_PROCESS,
            )
            if extracted.returncode or not staged.is_file() or staged.stat().st_size == 0:
                raise RuntimeError(extracted.stderr.strip() or extracted.stdout.strip() or 'archive reader produced no WEM')
            verified = subprocess.run(
                [str(decoder), '-m', str(staged)], capture_output=True, text=True,
                encoding='utf-8', errors='replace', creationflags=HIDDEN_PROCESS,
            )
            if verified.returncode or 'Audiokinetic Wwise RIFF header' not in verified.stdout:
                raise RuntimeError(verified.stderr.strip() or verified.stdout.strip() or 'original WEM verification failed')
            os.replace(staged, output_wem)
            self.after(0, lambda: _play_review_wem(self, output_wem, source))
            self.after(0, lambda: self._append_log('> Original English WEM imported; playing preview.'))
        except Exception as error:
            self.after(0, lambda: self._append_log(f'> Import original English WEM ERROR | {error}', 'asr_fail'))
        finally:
            staged.unlink(missing_ok=True)
            self._review_manual_running = False

    threading.Thread(target=worker, name='review-original-wem-import', daemon=True).start()


def _test_review_asr(self, source: str, row: dict) -> None:
    """Run a manual Whisper test without touching checkpoint or review state."""
    if getattr(self, '_full_batch_running', False) or getattr(self, '_review_manual_running', False):
        return
    wem = Path(str(row.get('output_wem_path', '')))
    if not wem.is_file():
        return
    from game_dubber_gui import WORK_ROOT, HIDDEN_PROCESS
    decoder = self._decoder_path(); model = WORK_ROOT / 'models' / 'whisper-large-v3-turbo'
    # The production ASR runs inside the local XTTS runtime.  It contains both
    # faster-whisper and soundfile; the system Python deliberately is not used.
    runtime = WORK_ROOT / 'runtimes' / 'xtts' / 'Scripts' / 'python.exe'
    if not decoder.is_file() or not model.is_dir() or not runtime.is_file():
        messagebox.showerror('ASR test unavailable', 'The local decoder, Whisper model, or Python runtime is unavailable.', parent=self); return
    # Marshal worker results through a queue.  Calling Tk methods directly
    # from the worker occasionally lost the final message on Windows.
    import queue
    result_queue = queue.Queue()
    self._review_manual_running = True
    self._append_log('> Manual ASR test started; validation state will not be changed.')
    # The normal ASR launcher adds the pip-installed NVIDIA DLL directories.
    # Do the same here: a bare Python process otherwise cannot load cublas64_12.
    asr_env = os.environ.copy()
    cuda_bins = _cuda_package_bin_paths()
    asr_env['PATH'] = ';'.join(cuda_bins + [asr_env.get('PATH', '')])
    temp = WORK_ROOT / 'review_preview' / f'asr_{zlib.crc32(source.encode("utf-8")) & 0xffffffff:08x}.wav'; temp.parent.mkdir(parents=True, exist_ok=True)
    code = """import json,sys
from faster_whisper import WhisperModel
model=WhisperModel(sys.argv[1],device='cuda',compute_type='int8_float16')
segments,_=model.transcribe(sys.argv[2],language=sys.argv[3],task='transcribe',beam_size=5,vad_filter=False)
print(json.dumps({'transcript':' '.join(s.text.strip() for s in segments).strip()},ensure_ascii=False))
"""
    language = str(row.get('target_language', ''))
    def worker() -> None:
        try:
            decoded = subprocess.run([str(decoder), '-o', str(temp), str(wem)], capture_output=True, creationflags=HIDDEN_PROCESS)
            if decoded.returncode or not temp.is_file():
                raise RuntimeError('WEM decode failed')
            result = subprocess.run([str(runtime), '-c', code, str(model), str(temp), language], capture_output=True, text=True, encoding='utf-8', errors='replace', env=asr_env, creationflags=HIDDEN_PROCESS, timeout=180)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or 'Whisper failed')
            transcript = json.loads(result.stdout.strip())['transcript']
            passed = _manual_asr_match(str(row.get('official_subtitle', '')), transcript)
            result_queue.put(('result', transcript, passed))
        except Exception as error:
            result_queue.put(('error', str(error), False))
        finally:
            temp.unlink(missing_ok=True)
            self._review_manual_running = False

    def receive_result() -> None:
        try:
            kind, payload, passed = result_queue.get_nowait()
        except queue.Empty:
            if getattr(self, '_review_manual_running', False):
                self.after(100, receive_result)
            return
        if kind == 'result':
            tag = 'asr_pass' if passed else 'asr_fail'
            outcome = 'PASS' if passed else 'FAIL'
            self._append_log(f'Manual ASR test {outcome} | transcript={json.dumps(payload, ensure_ascii=False)}', tag)
        else:
            self._append_log(f'Manual ASR test ERROR | {payload}', 'asr_fail')
    threading.Thread(target=worker, name='manual-asr-test', daemon=True).start()
    self.after(100, receive_result)


def _transcribe_translate_and_regenerate_review_row(
    self, source: str, row: dict, engine: str, translation_backend: str = 'translategemma',
) -> None:
    """Whisper English, translate on CUDA, then synthesize one target WEM."""
    if getattr(self, '_full_batch_running', False) or getattr(self, '_review_manual_running', False):
        return
    config = _generation_engine_config(engine)
    if config is None:
        return
    _display, runtime, model_path = config
    from game_dubber_gui import WORK_ROOT, HIDDEN_PROCESS
    reader, decoder = self._reader_path(), self._decoder_path()
    wwise_console = getattr(self, '_wwise_console_path', None)
    project = WORK_ROOT / 'wwise' / 'starfield_project' / 'Starfield.wproj'
    archive = Path(str(row.get('original_archive_path', '')))
    output_wem = Path(str(row.get('output_wem_path', '')))
    whisper_model = WORK_ROOT / 'models' / 'whisper-large-v3-turbo'
    asr_runtime = WORK_ROOT / 'runtimes' / 'voxcpm' / 'Scripts' / 'python.exe'
    nllb_model = WORK_ROOT / 'models' / 'NLLB-200-distilled-600M'
    translategemma_model = WORK_ROOT / 'models' / 'TranslateGemma-4B-Q4' / 'translategemma-4b-it.Q4_K_M.gguf'
    llama_cli = WORK_ROOT.parent / 'tools' / 'llama-cpp' / 'llama-cli.exe'
    script = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'manual_review_transcribe_translate_generate.py'
    asr_script = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'manual_review_transcribe_asr.py'
    if not all((model_path.is_dir(), runtime.is_file(), reader.is_file(), decoder.is_file(), archive.is_file(),
                output_wem.parent.is_dir(), whisper_model.is_dir(), asr_runtime.is_file(), script.is_file(), asr_script.is_file(),
                isinstance(wwise_console, Path) and wwise_console.is_file(), project.is_file())):
        self._append_log('> Transcribe, translate and generate unavailable: a local model, tool, archive, or Wwise component is missing.', 'asr_fail')
        return
    if translation_backend == 'translategemma':
        if not translategemma_model.is_file() or not llama_cli.is_file():
            self._append_log('> TranslateGemma unavailable: its Q4 model or CUDA llama.cpp runtime is missing.', 'asr_fail')
            return
        translator_label = 'TranslateGemma Q4 CUDA'
    elif translation_backend == 'nllb':
        if not nllb_model.is_dir():
            self._append_log('> NLLB unavailable: its local model is missing.', 'asr_fail')
            return
        translator_label = 'NLLB'
    else:
        self._append_log('> Transcribe, translate and generate unavailable: unknown translation backend.', 'asr_fail')
        return
    target_language = str(row.get('target_language', '')).strip().lower()
    if target_language not in {'de', 'en', 'es', 'fr', 'it', 'ja', 'pl', 'ptbr', 'zhhans'}:
        self._append_log('> Transcribe, translate and generate unavailable: selected target language is not supported.', 'asr_fail')
        return
    try:
        voxcpm_steps = int(row.get('voxcpm_steps') or getattr(self, '_selected_voxcpm_steps', 6))
    except (TypeError, ValueError):
        voxcpm_steps = 6
    self._review_manual_running = True
    self._append_log(f'> Transcribe, translate and generate started: English Whisper → {translator_label} → {engine}.')

    def worker() -> None:
        temp_root = WORK_ROOT / 'review_transcribe_translate' / f'{zlib.crc32(source.encode("utf-8")) & 0xffffffff:08x}'
        try:
            cuda_env = os.environ.copy()
            cuda_bins = _cuda_package_bin_paths()
            cuda_env['PATH'] = ';'.join(cuda_bins + [cuda_env.get('PATH', '')])
            command = [
                str(runtime), str(script), '--engine', engine, '--model', str(model_path), '--reader', str(reader),
                '--decoder', str(decoder), '--archive', str(archive), '--source', source,
                '--language', target_language, '--output-wem', str(output_wem),
                '--wwise-console', str(wwise_console), '--wwise-project', str(project),
                '--work-dir', str(temp_root), '--phonetic-dictionary-root', str(WORK_ROOT / 'phonetic_dictionaries'),
                '--whisper-model', str(whisper_model), '--asr-runtime', str(asr_runtime), '--asr-script', str(asr_script),
                '--translation-backend', translation_backend,
                '--nllb-model', str(nllb_model), '--translategemma-model', str(translategemma_model),
                '--llama-cli', str(llama_cli),
                '--voxcpm-steps', str(voxcpm_steps), '--number', str(int(row.get('number', 0))),
            ]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                       encoding='utf-8', errors='replace', env=cuda_env, creationflags=HIDDEN_PROCESS)
            assert process.stdout is not None
            for line in process.stdout:
                clean = line.rstrip()
                if clean:
                    self.after(0, lambda value=clean: self._append_log('> ' + value))
            if process.wait():
                raise RuntimeError('transcribe/translate/generate process exited with an error')
            self.after(0, lambda: _play_review_wem(self, output_wem, source))
            self.after(0, lambda: self._append_log('> Transcribe, translate and generate complete; playing the new target-language WEM.'))
        except Exception as error:
            self.after(0, lambda value=str(error): self._append_log(f'> Transcribe, translate and generate ERROR | {value}', 'asr_fail'))
        finally:
            try:
                shutil.rmtree(temp_root, ignore_errors=True)
            except OSError:
                pass
            self._review_manual_running = False

    threading.Thread(target=worker, name='review-transcribe-translate-generate', daemon=True).start()


def _regenerate_review_row(self, source: str, row: dict, engine: str) -> None:
    """Regenerate exactly one local output WEM and immediately preview it."""
    if getattr(self, '_full_batch_running', False) or getattr(self, '_review_manual_running', False):
        return
    config = _generation_engine_config(engine)
    if config is None:
        return
    _display, runtime, model_path = config
    from game_dubber_gui import WORK_ROOT, HIDDEN_PROCESS
    reader, decoder = self._reader_path(), self._decoder_path()
    wwise_console = getattr(self, '_wwise_console_path', None)
    if not isinstance(wwise_console, Path) or not wwise_console.is_file():
        wwise_console, _message = _find_starfield_wwise_console()
    project = WORK_ROOT / 'wwise' / 'starfield_project' / 'Starfield.wproj'
    output_wem = Path(str(row.get('output_wem_path', '')))
    archive = Path(str(row.get('original_archive_path', '')))
    if not all((model_path.is_dir(), reader.is_file(), decoder.is_file(), isinstance(wwise_console, Path) and wwise_console.is_file(), project.is_file(), archive.is_file(), output_wem.parent.is_dir())):
        messagebox.showerror('Manual regeneration unavailable', 'A required local model, archive, reader, decoder, or Wwise component is missing.', parent=self); return
    script = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'manual_review_regenerate.py'
    if not runtime.is_file() or not script.is_file():
        self._append_log('> Manual regeneration ERROR | selected model runtime or regeneration helper is unavailable.', 'asr_fail')
        return
    self._review_manual_running = True
    self._append_log(f'> Manual regeneration started: {source} with {engine}.')
    try:
        voxcpm_steps = int(row.get('voxcpm_steps') or getattr(self, '_selected_voxcpm_steps', 6))
    except (TypeError, ValueError):
        voxcpm_steps = 6
    def worker() -> None:
        temp_root = WORK_ROOT / 'review_manual' / f'{zlib.crc32(source.encode("utf-8")) & 0xffffffff:08x}'
        try:
            command = [
                str(runtime), str(script), '--engine', engine, '--model', str(model_path), '--reader', str(reader), '--decoder', str(decoder),
                '--archive', str(archive), '--source', source, '--target-text', str(row.get('synthesis_text') or row.get('official_subtitle') or ''),
                '--language', str(row.get('target_language', '')), '--output-wem', str(output_wem), '--wwise-console', str(wwise_console),
                '--wwise-project', str(project), '--work-dir', str(temp_root), '--phonetic-dictionary-root', str(WORK_ROOT / 'phonetic_dictionaries'),
                '--voxcpm-steps', str(voxcpm_steps), '--number', str(int(row.get('number', 0))),
            ]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', creationflags=HIDDEN_PROCESS)
            assert process.stdout is not None
            for line in process.stdout:
                clean = line.rstrip()
                if clean:
                    self.after(0, lambda value=clean: self._append_log('> ' + value))
            exit_code = process.wait()
            if exit_code:
                raise RuntimeError(f'generator process exited with code {exit_code}')
            self.after(0, lambda: _play_review_wem(self, output_wem, source))
            self.after(0, lambda: self._append_log('> Manual regeneration complete; playing the new target-language WEM.'))
        except Exception as error:
            self.after(0, lambda: self._append_log(f'> Manual regeneration ERROR | {error}', 'asr_fail'))
        finally:
            try: shutil.rmtree(temp_root, ignore_errors=True)
            except OSError: pass
            self._review_manual_running = False
    threading.Thread(target=worker, name='manual-wem-regeneration', daemon=True).start()


def _undo_review_override(self) -> None:
    """Restore the state that existed before the last manual dot click."""
    history = getattr(self, '_review_override_history', None)
    run_dir = _review_run_directory(self)
    if not isinstance(history, list) or not history or run_dir is None:
        return
    _run_name, source, previous_manual, new_value = history[-1]
    if _run_name != run_dir.name:
        return
    from game_dubber_gui import WORK_ROOT
    try:
        connection = sqlite3.connect(WORK_ROOT / 'voice_pipeline.db', timeout=0.5)
        if previous_manual is None:
            connection.execute('DELETE FROM production_voice_review_overrides WHERE run_id=? AND source_audio_path=?', (run_dir.name, source))
        else:
            connection.execute('INSERT OR REPLACE INTO production_voice_review_overrides VALUES (?, ?, ?, ?)', (
                run_dir.name, source, int(previous_manual), datetime.now(timezone.utc).isoformat(),
            ))
        connection.commit(); connection.close()
        cache = _review_sync(self, run_dir)
        if previous_manual is None:
            cache['manual'].pop(source, None)
        else:
            cache['manual'][source] = previous_manual
        cache['version'] = int(cache.get('version', 0)) + 1
        history.pop()
        redo_history = getattr(self, '_review_redo_history', None)
        if not isinstance(redo_history, list):
            redo_history = []
            self._review_redo_history = redo_history
        redo_history.append((_run_name, source, previous_manual, new_value))
        self.review_undo_button.configure(state='normal' if history else 'disabled')
        self.review_redo_button.configure(state='normal')
        _refresh_embedded_validation_report(self, schedule=False)
    except sqlite3.Error as error:
        self._append_log(f'> Report redo could not be saved: {error}')


def _redo_review_override(self) -> None:
    """Reapply the most recently undone manual review decision."""
    history = getattr(self, '_review_override_history', None)
    redo_history = getattr(self, '_review_redo_history', None)
    run_dir = _review_run_directory(self)
    if not isinstance(redo_history, list) or not redo_history or run_dir is None:
        return
    _run_name, source, previous_manual, new_value = redo_history[-1]
    if _run_name != run_dir.name:
        return
    from game_dubber_gui import WORK_ROOT
    try:
        connection = sqlite3.connect(WORK_ROOT / 'voice_pipeline.db', timeout=0.5)
        connection.execute('INSERT OR REPLACE INTO production_voice_review_overrides VALUES (?, ?, ?, ?)', (
            run_dir.name, source, int(new_value), datetime.now(timezone.utc).isoformat(),
        ))
        connection.commit(); connection.close()
        cache = _review_sync(self, run_dir)
        cache['manual'][source] = new_value
        cache['version'] = int(cache.get('version', 0)) + 1
        redo_history.pop()
        if not isinstance(history, list):
            history = []
            self._review_override_history = history
        history.append((_run_name, source, previous_manual, new_value))
        self.review_undo_button.configure(state='normal')
        self.review_redo_button.configure(state='normal' if redo_history else 'disabled')
        _refresh_embedded_validation_report(self, schedule=False)
    except sqlite3.Error as error:
        self._append_log(f'> Report redo could not be saved: {error}')


def _play_review_wem(self, wem: Path, source: str, cleanup_source: bool = False) -> None:
    if not wem.is_file():
        return
    from game_dubber_gui import WORK_ROOT, HIDDEN_PROCESS
    decoder = self._decoder_path()
    if not decoder.is_file():
        self._append_log('> Report preview unavailable: WEM decoder is missing.')
        return
    preview_dir = WORK_ROOT / 'review_preview'; preview_dir.mkdir(parents=True, exist_ok=True)
    wav = preview_dir / f'{zlib.crc32(source.encode("utf-8")) & 0xffffffff:08x}.wav'
    # Only one manual-review preview may exist.  A monotonically increasing
    # token also prevents an older decode worker from starting after the user
    # already selected a newer row.
    token = int(getattr(self, '_review_preview_token', 0)) + 1
    self._review_preview_token = token
    previous_player = getattr(self, '_review_player_process', None)
    if previous_player is not None and previous_player.poll() is None:
        try:
            previous_player.terminate()
        except OSError:
            pass
    def worker() -> None:
        result = subprocess.run([str(decoder), '-o', str(wav), str(wem)], capture_output=True, creationflags=HIDDEN_PROCESS)
        if cleanup_source:
            wem.unlink(missing_ok=True)
        if token != getattr(self, '_review_preview_token', 0):
            wav.unlink(missing_ok=True)
            return
        if result.returncode or not wav.is_file():
            self.after(0, lambda: self._append_log('> Report preview decode failed.'))
            return
        # In a frozen build sys.executable is GameDubber.exe.  Using it here
        # opened a second GUI instead of playing sound.  Always launch the
        # isolated Python runtime belonging to the active synthesis backend.
        context = getattr(self, '_full_batch_context', {}) or {}
        config = _generation_engine_config(str(context.get('engine', 'xtts_v2')))
        runtime = config[1] if config else None
        player_runtime = runtime.with_name('pythonw.exe') if isinstance(runtime, Path) and runtime.with_name('pythonw.exe').is_file() else runtime
        if player_runtime is None or not Path(player_runtime).is_file():
            self.after(0, lambda: self._append_log('> Report preview unavailable: local Python runtime is missing.'))
            return
        player = "import pathlib,sys,winsound; p=pathlib.Path(sys.argv[1]);\ntry: winsound.PlaySound(str(p), winsound.SND_FILENAME)\nfinally: p.unlink(missing_ok=True)"
        process = subprocess.Popen([str(player_runtime), '-c', player, str(wav)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=HIDDEN_PROCESS)
        if token != getattr(self, '_review_preview_token', 0):
            try:
                process.terminate()
            except OSError:
                pass
            return
        self._review_player_process = process
    threading.Thread(target=worker, name='review-wem-preview', daemon=True).start()


_previous_embedded_review_install = install


def install(cls):
    _previous_embedded_review_install(cls)
    original_build = cls._build
    def build_with_embedded_review(self):
        original_build(self)
        self._review_page = 0
        self._review_cache = None
        self._review_override_history = []
        self._review_redo_history = []
        self._review_tracked_number = None
        self._review_search_applied = ''
        self._review_search_after_id = None
        self.review_only_unvalidated.trace_add('write', lambda *_args: (_change_review_page(self, -int(getattr(self, '_review_page', 0)))))
        self.review_search_text.trace_add('write', self._schedule_review_search)
        self.review_tree.bind('<ButtonRelease-1>', self._review_click)
        self.review_tree.bind('<Button-3>', self._review_context_menu)
        self.after(500, self._refresh_embedded_validation_report)
    cls._build = build_with_embedded_review
    cls._refresh_embedded_validation_report = _refresh_embedded_validation_report
    cls._change_review_page = _change_review_page
    cls._schedule_review_search = _schedule_review_search
    cls._track_review_number = _track_review_number
    cls._review_click = _review_click
    cls._review_context_menu = _review_context_menu
    cls._undo_review_override = _undo_review_override
    cls._redo_review_override = _redo_review_override
