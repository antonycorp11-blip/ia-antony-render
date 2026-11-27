import json, subprocess, numpy as np, os
from scipy.signal import fftconvolve
from faster_whisper import WhisperModel
parts=["p1","p2","p3"]
def load(path, sr=8000, t=180):
    raw=subprocess.run(["ffmpeg","-v","error","-i",path,"-t",str(t),"-ac","1","-ar",str(sr),"-f","f32le","-"],capture_output=True).stdout
    return np.frombuffer(raw,np.float32)
sync={p:{"voice_offset_in_video_s":0.0,"note":"audio do video e mudo; gravados juntos"} for p in parts}
json.dump(sync,open("out/sync.json","w"),indent=1)
m=WhisperModel("large-v3",device="cpu",compute_type="int8",cpu_threads=os.cpu_count())
for p in parts:
    segs,info=m.transcribe(f"media/{p}_voz.wav",language="pt",word_timestamps=True,vad_filter=False,beam_size=5)
    out=[]
    for s in segs:
        out.append({"start":s.start,"end":s.end,"text":s.text,"words":[{"w":w.word,"s":round(w.start,3),"e":round(w.end,3),"p":round(w.probability,3)} for w in s.words]})
    json.dump(out,open(f"out/{p}.json","w"),ensure_ascii=False,indent=0)
    with open(f"out/{p}.txt","w") as f:
        for s in out: f.write(f"[{s['start']:7.2f}-{s['end']:7.2f}] {s['text'].strip()}\n")
    print(p,"ok",len(out))
