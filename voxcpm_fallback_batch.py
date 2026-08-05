"""VoxCPM2 fallback with grouped ASR validation."""
from __future__ import annotations
import argparse, gc, json, shutil, traceback
from datetime import datetime, timezone
from pathlib import Path
import torch
from faster_whisper import WhisperModel
from full_voxcpm2_batch import (asr_language_code, configure_phonetic_dictionary_root, evaluate_asr_match,
    extract_and_decode_english_wav, generate_with_engine, load_engine, normalize_and_encode_generated_wem,
    stable_seed, synthesis_text)

def main() -> int:
    p=argparse.ArgumentParser(); p.add_argument('--jobs',type=Path,required=True); p.add_argument('--results',type=Path,required=True)
    p.add_argument('--reader',type=Path,required=True); p.add_argument('--decoder',type=Path,required=True); p.add_argument('--model',type=Path,required=True); p.add_argument('--asr-model',type=Path,required=True)
    p.add_argument('--language',required=True); p.add_argument('--wwise-console',type=Path,required=True); p.add_argument('--wwise-project',type=Path,required=True); p.add_argument('--dictionary-root',type=Path,required=True)
    a=p.parse_args(); configure_phonetic_dictionary_root(a.dictionary_root); jobs=[json.loads(x) for x in a.jobs.read_text(encoding='utf-8').splitlines() if x]; root=a.results.parent/'_voxcpm_fallback_temp'; records=[]
    for i,row in enumerate(jobs,1):
        internal=str(row['source_audio_path']); rel=Path(*internal.replace('\\','/').split('/'))
        records.append({'row':row,'number':i,'output':Path(row['output_wem_path']),'wem':root/'en'/rel,'wav':(root/'en'/rel).with_suffix('.wav'),'target':(root/'target'/rel).with_suffix('.wav'),'result':dict(row),'history':[]})
    def generate(items,attempt):
        model=load_engine('voxcpm2',a.model)
        try:
            for x in items:
                r=x['row']; x['result'].update({'generation_engine':'voxcpm2_fallback','started_at':datetime.now(timezone.utc).isoformat()})
                extract_and_decode_english_wav(a.reader,a.decoder,Path(r['original_archive_path']),r['source_audio_path'],x['wem'],x['wav'],x['number'],len(records),False)
                audio,rate=generate_with_engine('voxcpm2',model,synthesis_text(str(r['official_subtitle']),a.language,'voxcpm2'),x['wav'],root,a.language,6,stable_seed(r['source_audio_path'])+attempt-1)
                x['result'].update(normalize_and_encode_generated_wem(audio,rate,x['wav'],x['target'],x['output'],a.wwise_console,a.wwise_project,a.decoder,x['number'],len(records)))
                x['wem'].unlink(missing_ok=True); x['wav'].unlink(missing_ok=True); x['target'].unlink(missing_ok=True)
        finally:
            if hasattr(model,'close'): model.close()
            del model; gc.collect(); torch.cuda.empty_cache()
    pending=records
    for attempt in range(1,6):
        if attempt==1: generate(pending,attempt)
        print(f'VOX ASR GROUP attempt={attempt}/5 items={len(pending)}',flush=True); asr=WhisperModel(str(a.asr_model),device='cuda',compute_type='int8_float16'); failed=[]
        try:
            for x in pending:
                decoded=__import__('subprocess').run([str(a.decoder),'-o',str(x['target']),str(x['output'])],capture_output=True,text=True,encoding='utf-8',errors='replace')
                if decoded.returncode or not x['target'].is_file(): check={'satisfactory':False,'asr_transcript':''}
                else:
                    seg,_=asr.transcribe(str(x['target']),language=asr_language_code(a.language),task='transcribe',beam_size=5,vad_filter=False); text=' '.join(s.text.strip() for s in seg).strip(); check=evaluate_asr_match(str(x['row']['official_subtitle']),text); check['asr_transcript']=text
                check['attempt']=attempt; x['history'].append(check); x['target'].unlink(missing_ok=True); print(f"VOX ASR {x['number']}/{len(records)} expected={json.dumps(x['row']['official_subtitle'],ensure_ascii=False)} transcript={json.dumps(check.get('asr_transcript',''),ensure_ascii=False)} satisfactory={check['satisfactory']}",flush=True)
                if not check['satisfactory']: failed.append(x)
        finally:
            del asr; gc.collect(); torch.cuda.empty_cache()
        pending=failed
        if not pending or attempt==5: break
        generate(pending,attempt+1)
    a.results.parent.mkdir(parents=True,exist_ok=True)
    with a.results.open('w',encoding='utf-8',newline='\n') as out:
        for x in records:
            x['result']['voxcpm_asr_attempts']=x['history']; x['result']['status']='voxcpm_fallback_generated' if x['history'] and x['history'][-1]['satisfactory'] else 'failed'; x['result']['finished_at']=datetime.now(timezone.utc).isoformat(); out.write(json.dumps(x['result'],ensure_ascii=False)+'\n')
    shutil.rmtree(root,ignore_errors=True); return 0
if __name__=='__main__': raise SystemExit(main())
