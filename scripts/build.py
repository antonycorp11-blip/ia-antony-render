#!/usr/bin/env python3
"""
Build pipeline para renderização do ep01 (IA Antony) no GitHub Actions.

Modos:
  --preview  Renderiza apenas de 0:00 a 1:30 (90s) da linha do tempo final.
  --full     Renderiza o vídeo completo.

Requisitos de ambiente:
  - ffmpeg / ffprobe no PATH
  - Fontes Montserrat / Inter em assets/fonts/ (ou instaladas no sistema)
  - Mídias em media/ ou na release 'media' do repo GitHub configurado
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GH_REPO = "antonycorp11-blip/ia-antony-render"
DEFAULT_RELEASE = "media"
PREVIEW_LIMIT_S = 90.0
FPS = 30
WIDTH = 1920
HEIGHT = 1080
# Crop global para remover barra de menu/título (44 px topo) e Dock do macOS (48 px base), mantendo 16:9
CROP_TOP = 44
CROP_BOTTOM = 48
CROP_H = HEIGHT - CROP_TOP - CROP_BOTTOM  # 988
CROP_W = (round(CROP_H * WIDTH / HEIGHT) // 2) * 2  # 1756
CROP_X = (WIDTH - CROP_W) // 2  # 82
CROP_Y = CROP_TOP  # 44


def global_crop_scale_filter() -> str:
    """Remove 44 px do topo e 48 px da base (laterais proporcionais em 16:9) e escala para 1920x1080."""
    return (
        f"crop=w=trunc((iw*{CROP_W}/{WIDTH}+1)/2)*2:"
        f"h=trunc((ih*{CROP_H}/{HEIGHT}+1)/2)*2:"
        f"x=trunc((iw*{CROP_X}/{WIDTH}+1)/2)*2:"
        f"y=trunc((ih*{CROP_Y}/{HEIGHT}+1)/2)*2,"
        f"scale={WIDTH}:{HEIGHT}:flags=lanczos,setsar=1,format=yuv420p"
    )


def remap_zoom_center_after_crop(cx: float, cy: float) -> tuple[float, float]:
    """Remapeia (cx, cy) normalizados do quadro 1920x1080 original para o quadro pós-crop."""
    new_cx = (cx * WIDTH - CROP_X) / CROP_W
    new_cy = (cy * HEIGHT - CROP_Y) / CROP_H
    return (min(1.0, max(0.0, new_cx)), min(1.0, max(0.0, new_cy)))


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def run_cmd(cmd: list[str], capture: bool = False) -> subprocess.CompletedProcess[str]:
    log("$ " + " ".join(str(c) for c in cmd))
    proc = subprocess.run(
        cmd,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if proc.returncode != 0:
        if capture:
            if proc.stdout:
                print(proc.stdout, file=sys.stderr)
            if proc.stderr:
                print(proc.stderr, file=sys.stderr)
        raise RuntimeError(f"Comando falhou (exit {proc.returncode}): {' '.join(cmd)}")
    return proc


def ffprobe_duration(path: Path) -> float:
    proc = run_cmd(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture=True,
    )
    return float(proc.stdout.strip())


def download_release_asset(gh_repo: str, release: str, filename: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return True

    # 1) Tenta via gh CLI se disponível
    if shutil.which("gh"):
        try:
            run_cmd(
                [
                    "gh",
                    "release",
                    "download",
                    release,
                    "--repo",
                    gh_repo,
                    "--pattern",
                    filename,
                    "--dir",
                    str(dest.parent),
                    "--clobber",
                ]
            )
            if dest.exists() and dest.stat().st_size > 0:
                return True
        except RuntimeError:
            log(f"Aviso: gh release download não encontrou {filename}; tentando URL direta...")

    # 2) Fallback por URL pública da release do GitHub
    url = f"https://github.com/{gh_repo}/releases/download/{release}/{filename}"
    log(f"Baixando {url} -> {dest}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ia-antony-build/1.0"})
        with urllib.request.urlopen(req, timeout=180) as resp, open(dest, "wb") as out:
            shutil.copyfileobj(resp, out)
        return dest.exists() and dest.stat().st_size > 0
    except (urllib.error.URLError, OSError) as exc:
        log(f"Falha ao baixar {filename}: {exc}")
        if dest.exists() and dest.stat().st_size == 0:
            dest.unlink()
        return False


def warn(msg: str) -> None:
    """Aviso visível no log e como anotação do GitHub Actions."""
    log(f"AVISO: {msg}")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::{msg}", flush=True)


def ensure_media(
    edl: dict[str, Any],
    used_parts: set[str],
    media_dir: Path,
    gh_repo: str,
    release: str,
    skip_download: bool,
) -> tuple[dict[str, Path], Path | None]:
    """Baixa vídeos/vozes (obrigatórios para as partes ativas) e a trilha (OPCIONAL por enquanto).

    Retorna (paths, trilha_ou_None). Se a trilha faltar, o build segue sem música e avisa no log.
    """
    media_dir.mkdir(parents=True, exist_ok=True)
    needed: list[str] = []
    for part_name, part_cfg in edl["sources"].items():
        if part_name not in used_parts:
            continue
        needed.append(part_cfg["video"])
        needed.append(part_cfg["voice"])

    paths: dict[str, Path] = {}
    for fname in needed:
        dest = media_dir / fname
        if not dest.exists() or dest.stat().st_size == 0:
            if not skip_download:
                download_release_asset(gh_repo, release, fname, dest)
        paths[fname] = dest

    missing = [f for f in needed if not (media_dir / f).exists() or (media_dir / f).stat().st_size == 0]
    if missing:
        raise SystemExit(
            f"\nERRO FATAL: mídias obrigatórias ausentes em {media_dir}: {', '.join(missing)}\n"
            f"Verifique se todos os arquivos existem na release '{release}' de '{gh_repo}'.\n"
        )

    # Trilha: opcional por enquanto
    trilha = media_dir / "trilha.mp3"
    if (not trilha.exists() or trilha.stat().st_size == 0) and not skip_download:
        download_release_asset(gh_repo, release, "trilha.mp3", trilha)
    if not trilha.exists() or trilha.stat().st_size == 0:
        warn(
            "trilha de fundo ausente (media/trilha.mp3 / release "
            f"'{release}'). Seguindo SEM trilha: só voz + SFX."
        )
        return paths, None
    log(f"Trilha encontrada: {trilha}")
    return paths, trilha


def slice_clips_for_limit(clips: list[dict[str, Any]], limit_s: float | None) -> tuple[list[dict[str, Any]], float]:
    out: list[dict[str, Any]] = []
    cursor = 0.0
    for clip in clips:
        src_start = float(clip["start"])
        src_end = float(clip["end"])
        dur = round(src_end - src_start, 6)
        if dur <= 0:
            continue
        if limit_s is not None and cursor >= limit_s:
            break
        if limit_s is not None and cursor + dur > limit_s:
            keep = round(limit_s - cursor, 6)
            if keep <= 0.02:
                break
            c = dict(clip)
            c["end"] = round(src_start + keep, 6)
            c["out_start"] = round(cursor, 6)
            c["out_end"] = round(cursor + keep, 6)
            out.append(c)
            cursor += keep
            break
        c = dict(clip)
        c["out_start"] = round(cursor, 6)
        c["out_end"] = round(cursor + dur, 6)
        out.append(c)
        cursor += dur
    return out, round(cursor, 6)


def cut_voice_edl(
    clips: list[dict[str, Any]],
    edl: dict[str, Any],
    media_dir: Path,
    work_dir: Path,
) -> Path:
    """Corta e concatena a voz limpa dos arquivos _voz.wav em ordem livre, com micro-fade de 4ms."""
    voice_out = work_dir / "voice_cut.wav"
    seg_dir = work_dir / "voice_segs"
    seg_dir.mkdir(parents=True, exist_ok=True)

    # Um input com seek rápido (-ss/-t) por clipe suporta ordem livre de trechos sem buffer de split
    batch_size = 20
    batch_files: list[Path] = []
    for b_idx in range(0, len(clips), batch_size):
        batch = clips[b_idx : b_idx + batch_size]
        batch_wav = seg_dir / f"batch_{b_idx // batch_size:03d}.wav"

        inputs: list[str] = []
        filter_lines: list[str] = []
        concat_labels: list[str] = []
        for i, c in enumerate(batch):
            wav_path = media_dir / edl["sources"][c["part"]]["voice"]
            s = max(0.0, float(c["start"]))
            e = float(c["end"])
            dur = max(0.02, e - s)
            inputs.extend(["-ss", f"{s:.6f}", "-t", f"{dur:.6f}", "-i", str(wav_path)])

            fade = min(0.004, dur / 4.0)
            fade_out_st = max(0.0, dur - fade)
            lbl = f"a{i}"
            filter_lines.append(
                f"[{i}:a]asetpts=PTS-STARTPTS,"
                f"afade=t=in:st=0:d={fade:.6f},afade=t=out:st={fade_out_st:.6f}:d={fade:.6f}[{lbl}]"
            )
            concat_labels.append(f"[{lbl}]")

        if len(concat_labels) == 1:
            filter_lines.append(f"{concat_labels[0]}anull[aout]")
        else:
            filter_lines.append(f"{''.join(concat_labels)}concat=n={len(batch)}:v=0:a=1[aout]")
        fc_path = seg_dir / f"batch_{b_idx // batch_size:03d}.txt"
        fc_path.write_text(";\n".join(filter_lines), encoding="utf-8")

        run_cmd(
            [
                "ffmpeg",
                "-y",
                "-v",
                "warning",
                *inputs,
                "-filter_complex_script",
                str(fc_path),
                "-map",
                "[aout]",
                "-ar",
                "48000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(batch_wav),
            ]
        )
        batch_files.append(batch_wav)

    concat_list = seg_dir / "voice_concat.txt"
    concat_list.write_text(
        "\n".join(f"file '{f.resolve()}'" for f in batch_files) + "\n",
        encoding="utf-8",
    )
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-v",
            "warning",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c:a",
            "pcm_s16le",
            str(voice_out),
        ]
    )
    return voice_out


def cut_video_edl(
    clips: list[dict[str, Any]],
    edl: dict[str, Any],
    media_dir: Path,
    work_dir: Path,
) -> Path:
    """Corta e concatena o vídeo mudo em 1080p30 CFR, em ordem livre, respeitando offset e freeze de p2."""
    video_out = work_dir / "video_cut.mp4"
    seg_dir = work_dir / "video_segs"
    seg_dir.mkdir(parents=True, exist_ok=True)

    # Descobre duração real apenas das partes ativas nos clipes (ex: p2 é 2,5s menor que a voz)
    used_parts = {c["part"] for c in clips}
    vid_durations: dict[str, float] = {}
    for part_name in sorted(used_parts):
        cfg = edl["sources"][part_name]
        vid_durations[part_name] = ffprobe_duration(media_dir / cfg["video"])
        log(f"Duração real do vídeo {part_name}: {vid_durations[part_name]:.3f}s")

    # Um input com fast-seek (-ss) por clipe evita decodificar partes inteiras e suporta ordem livre sem OOM
    batch_size = 8
    batch_files: list[Path] = []
    for b_idx in range(0, len(clips), batch_size):
        batch = clips[b_idx : b_idx + batch_size]
        batch_mp4 = seg_dir / f"vbatch_{b_idx // batch_size:03d}.mp4"

        inputs: list[str] = []
        filter_lines: list[str] = []
        concat_labels: list[str] = []
        for i, c in enumerate(batch):
            p = c["part"]
            cfg = edl["sources"][p]
            vid_path = media_dir / cfg["video"]
            offset = float(cfg.get("video_offset_s", 0.0))
            real_dur = vid_durations[p]
            target_dur = max(0.04, float(c["end"]) - float(c["start"]))
            v_start = max(0.0, float(c["start"]) + offset)
            v_end = v_start + target_dur

            needs_freeze = v_end > real_dur - 0.02 and bool(cfg.get("freeze_last_frame", False))
            seek_start = min(v_start, max(0.0, real_dur - 0.1)) if needs_freeze else v_start
            if needs_freeze:
                inputs.extend(["-ss", f"{seek_start:.6f}", "-i", str(vid_path)])
            else:
                inputs.extend(["-ss", f"{seek_start:.6f}", "-t", f"{target_dur + 0.25:.6f}", "-i", str(vid_path)])

            lbl = f"v{i}"
            crop_scale = global_crop_scale_filter()
            # Se ultrapassar o fim do vídeo (caso p2), congela o último quadro
            if needs_freeze:
                pad_s = max(0.2, (v_end - real_dur) + 0.5)
                chain = (
                    f"[{i}:v]tpad=stop_mode=clone:stop_duration={pad_s:.3f},"
                    f"fps={FPS},trim=start=0:duration={target_dur:.6f},setpts=PTS-STARTPTS,"
                    f"{crop_scale}[{lbl}]"
                )
            else:
                chain = (
                    f"[{i}:v]fps={FPS},trim=start=0:duration={target_dur:.6f},setpts=PTS-STARTPTS,"
                    f"{crop_scale}[{lbl}]"
                )
            filter_lines.append(chain)
            concat_labels.append(f"[{lbl}]")

        if len(concat_labels) == 1:
            filter_lines.append(f"{concat_labels[0]}null[vout]")
        else:
            filter_lines.append(f"{''.join(concat_labels)}concat=n={len(batch)}:v=1:a=0[vout]")
        fc_path = seg_dir / f"vbatch_{b_idx // batch_size:03d}.txt"
        fc_path.write_text(";\n".join(filter_lines), encoding="utf-8")

        run_cmd(
            [
                "ffmpeg",
                "-y",
                "-v",
                "warning",
                *inputs,
                "-filter_complex_script",
                str(fc_path),
                "-map",
                "[vout]",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "16",
                "-profile:v",
                "high",
                "-level",
                "4.1",
                "-g",
                str(FPS),
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(FPS),
                "-video_track_timescale",
                "30000",
                str(batch_mp4),
            ]
        )
        batch_files.append(batch_mp4)

    concat_list = seg_dir / "video_concat.txt"
    concat_list.write_text(
        "\n".join(f"file '{f.resolve()}'" for f in batch_files) + "\n",
        encoding="utf-8",
    )
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-v",
            "warning",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c:v",
            "copy",
            str(video_out),
        ]
    )
    return video_out


def ass_time_to_seconds(ts: str) -> float:
    h, m, s = ts.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def seconds_to_ass_time(sec: float) -> str:
    sec = max(0.0, sec)
    cs_total = int(round(sec * 100))
    cs = cs_total % 100
    s_total = cs_total // 100
    s = s_total % 60
    m_total = s_total // 60
    m = m_total % 60
    h = m_total // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _adjust_ass_tags_for_slice(text: str, clipped_start: bool, clipped_end: bool) -> str:
    """Ajusta tags de animação (\move, \fad, \t) quando um cartão contínuo cruza a fronteira de um chunk."""
    if clipped_start:
        # Substitui \move(x1,y1,x2,y2[,t1,t2]) pela posição final \pos(x2,y2)
        text = re.sub(
            r"\\move\(\s*[^,]+,\s*[^,]+,\s*([^,]+),\s*([^,\)]+)(?:,[^\)]+)?\)",
            r"\\pos(\1,\2)",
            text,
        )
        # Remove animações de entrada \fscx..\fscy..\t(...)
        text = re.sub(r"\\fsc[xy][0-9.]+", "", text)
        text = re.sub(r"\\t\([^\)]*\)", "", text)
        # Zera fade-in já concluído
        text = re.sub(r"\\fad\(\s*\d+\s*,\s*(\d+)\s*\)", r"\\fad(0,\1)", text)
    if clipped_end:
        # Zera fade-out que só ocorrerá no próximo chunk
        text = re.sub(r"\\fad\(\s*(\d+)\s*,\s*\d+\s*\)", r"\\fad(\1,0)", text)
    return text


def slice_ass_for_window(ass_path: Path, out_ass: Path, win_start: float, win_end: float) -> None:
    """Gera um .ass deslocado para o intervalo [win_start, win_end] sem quebrar cabeçalho, estilos ou cartões contínuos."""
    lines = ass_path.read_text(encoding="utf-8").splitlines()
    out_lines: list[str] = []
    in_events = False
    for line in lines:
        if line.strip().lower() == "[events]":
            in_events = True
            out_lines.append(line)
            continue
        if in_events and line.startswith("Dialogue:"):
            parts = line.split(",", 9)
            if len(parts) < 10:
                continue
            st = ass_time_to_seconds(parts[1])
            en = ass_time_to_seconds(parts[2])
            if en <= win_start or st >= win_end:
                continue
            clipped_start = st < win_start - 0.005
            clipped_end = en > win_end + 0.005
            new_st = max(0.0, st - win_start)
            new_en = min(win_end - win_start, en - win_start)
            if new_en - new_st < 0.02:
                continue
            parts[1] = seconds_to_ass_time(new_st)
            parts[2] = seconds_to_ass_time(new_en)
            if clipped_start or clipped_end:
                parts[9] = _adjust_ass_tags_for_slice(parts[9], clipped_start, clipped_end)
            out_lines.append(",".join(parts))
        else:
            out_lines.append(line)
    out_ass.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def build_zoom_intervals(zooms: list[dict[str, Any]], win_start: float, win_end: float) -> list[dict[str, Any]]:
    """Constrói partição contígua do intervalo [win_start, win_end] em trechos normais e com zoom."""
    clipped: list[dict[str, Any]] = []
    for z in sorted(zooms, key=lambda item: float(item["start"])):
        zs = max(win_start, float(z["start"]))
        ze = min(win_end, float(z["end"]))
        if ze - zs < 0.2:
            continue
        clipped.append(
            {
                "start": zs,
                "end": ze,
                "from_scale": float(z.get("from_scale", 1.0)),
                "to_scale": float(z.get("to_scale", 1.08)),
                "cx": float(z.get("cx", 0.5)),
                "cy": float(z.get("cy", 0.38)),
                "ease_out_s": float(z.get("ease_out_s", 0.45)),
            }
        )

    parts: list[dict[str, Any]] = []
    cur = win_start
    for z in clipped:
        zs = z["start"]
        if zs > cur + 0.04:
            parts.append({"start": cur, "end": zs, "zoom": None})
        elif zs > cur:
            zs = cur
        if z["end"] > zs + 0.04:
            parts.append({"start": zs, "end": z["end"], "zoom": z})
            cur = z["end"]
    if cur < win_end - 0.04:
        parts.append({"start": cur, "end": win_end, "zoom": None})
    if not parts:
        parts.append({"start": win_start, "end": win_end, "zoom": None})
    return parts


def escape_ffmpeg_path(p: Path) -> str:
    s = str(p.resolve())
    return s.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def render_video_chunk(
    chunk_idx: int,
    win_start: float,
    win_end: float,
    video_cut: Path,
    zooms: list[dict[str, Any]],
    ass_path: Path,
    fonts_dir: Path | None,
    work_dir: Path,
    threads: int,
) -> Path:
    """Aplica zooms (supersampled crop/scale via zoompan apenas nos trechos com zoom) + queima overlays.ass."""
    chunk_dir = work_dir / f"chunk_{chunk_idx:03d}"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunk_ass = chunk_dir / "overlays.ass"
    slice_ass_for_window(ass_path, chunk_ass, win_start, win_end)

    parts = build_zoom_intervals(zooms, win_start, win_end)
    filter_lines: list[str] = []
    concat_labels: list[str] = []

    if len(parts) > 1:
        split_outs = "".join(f"[in{i}]" for i in range(len(parts)))
        filter_lines.append(f"[0:v]split={len(parts)}{split_outs}")

    for i, part in enumerate(parts):
        in_lbl = f"[in{i}]" if len(parts) > 1 else "[0:v]"
        rel_s = part["start"] - win_start
        rel_dur = max(0.04, part["end"] - part["start"])
        lbl = f"p{i}"
        z = part["zoom"]
        if z is None:
            filter_lines.append(
                f"{in_lbl}trim=start={rel_s:.6f}:duration={rel_dur:.6f},setpts=PTS-STARTPTS,"
                f"fps={FPS},scale={WIDTH}:{HEIGHT}:flags=lanczos,setsar=1,format=yuv420p[{lbl}]"
            )
        else:
            s0 = max(1.0, z["from_scale"])
            s1 = max(1.0, z["to_scale"])
            cx, cy = remap_zoom_center_after_crop(z["cx"], z["cy"])
            total_f = max(2, int(round(rel_dur * FPS)))
            ease_f = int(round(z["ease_out_s"] * FPS))
            # Dentro de aspas simples no zoompan, vírgulas NÃO devem levar barra invertida
            if ease_f > 0 and total_f > ease_f + 4:
                ramp_f = total_f - ease_f
                z_expr = (
                    f"if(lt(max(0,on-1),{ramp_f}),"
                    f"{s0:.4f}+({s1 - s0:.4f})*max(0,on-1)/{ramp_f},"
                    f"{s1:.4f}-({s1 - 1.0:.4f})*min(1,(max(0,on-1)-{ramp_f})/{ease_f}))"
                )
            else:
                z_expr = f"{s0:.4f}+({s1 - s0:.4f})*min(1,max(0,on-1)/{total_f})"
            x_expr = f"(iw-iw/zoom)*{cx:.4f}"
            y_expr = f"(ih-ih/zoom)*{cy:.4f}"
            # Supersampling 2560x1440 elimina tremulação subpixel no zoom lento
            filter_lines.append(
                f"{in_lbl}trim=start={rel_s:.6f}:duration={rel_dur:.6f},setpts=PTS-STARTPTS,"
                f"fps={FPS},scale=2560:1440:flags=lanczos,"
                f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}':d=1:s={WIDTH}x{HEIGHT}:fps={FPS},"
                f"setpts=PTS-STARTPTS,setsar=1,format=yuv420p[{lbl}]"
            )
        concat_labels.append(f"[{lbl}]")

    if len(concat_labels) == 1:
        filter_lines.append(f"{concat_labels[0]}null[vzoom]")
    else:
        filter_lines.append(f"{''.join(concat_labels)}concat=n={len(concat_labels)}:v=1:a=0[vzoom]")

    ass_esc = escape_ffmpeg_path(chunk_ass)
    if fonts_dir and fonts_dir.exists():
        fonts_esc = escape_ffmpeg_path(fonts_dir)
        ass_filter = f"ass=filename='{ass_esc}':fontsdir='{fonts_esc}'"
    else:
        ass_filter = f"ass=filename='{ass_esc}'"

    filter_lines.append(f"[vzoom]{ass_filter},format=yuv420p[vout]")

    fc_file = chunk_dir / "vf.txt"
    fc_file.write_text(";\n".join(filter_lines), encoding="utf-8")

    out_chunk = chunk_dir / f"rendered_{chunk_idx:03d}.mp4"
    win_dur = max(0.05, win_end - win_start)
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-v",
            "warning",
            "-ss",
            f"{win_start:.6f}",
            "-t",
            f"{win_dur:.6f}",
            "-i",
            str(video_cut),
            "-filter_complex_script",
            str(fc_file),
            "-map",
            "[vout]",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-profile:v",
            "high",
            "-level",
            "4.1",
            "-g",
            str(FPS * 2),
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(FPS),
            "-video_track_timescale",
            "30000",
            "-threads",
            str(threads),
            str(out_chunk),
        ]
    )
    return out_chunk


def resolve_sfx_file(sfx_dir: Path, sound_name: str) -> Path | None:
    """Resolve arquivo de SFX em assets/sfx/ aceitando qualquer extensão de áudio comum e alias chime/ding."""
    if not sfx_dir.exists():
        return None
    candidates = [sound_name]
    if sound_name == "chime":
        candidates.extend(["ding", "chime-soft", "bell"])
    elif sound_name == "ding":
        candidates.extend(["chime"])
    elif sound_name == "whoosh-short":
        candidates.extend(["whoosh", "whoosh_short"])
    elif sound_name == "click-soft":
        candidates.extend(["click", "click_soft"])

    exts = [".wav", ".mp3", ".ogg", ".flac", ".m4a"]
    files_by_stem = {p.stem.lower(): p for p in sfx_dir.iterdir() if p.is_file() and p.suffix.lower() in exts}
    for cand in candidates:
        if cand.lower() in files_by_stem:
            return files_by_stem[cand.lower()]
    # Match parcial como último recurso
    for cand in candidates:
        for stem, path in files_by_stem.items():
            if cand.lower() in stem:
                return path
    return None


def master_audio(
    voice_cut: Path,
    trilha_path: Path | None,
    plan: dict[str, Any],
    final_dur: float,
    sfx_dir: Path,
    work_dir: Path,
) -> Path:
    """
    Processa e mixa o áudio final:
      1. Voz: highpass 80Hz -> de-ess leve (6.8kHz) -> compressão suave -> loudnorm 2-pass (-14 LUFS)
      2. Trilha (OPCIONAL): se presente, loop -> corte -> fade in/out -> -24 dB -> sidechain ducking pela voz
      3. SFX: posicionados com adelay e volume discreto (usando asplit explícito por arquivo reutilizado)
      4. Mix final -> limiter de segurança -> AAC 192k 48kHz stereo
    """
    audio_cfg = plan.get("audio", {})
    hp_hz = audio_cfg.get("voice_highpass_hz", 80)
    target_i = audio_cfg.get("voice_lufs", -14.0)
    target_tp = audio_cfg.get("voice_true_peak_db", -1.5)
    music_db = audio_cfg.get("music_volume_db", -24.0)
    duck = audio_cfg.get("ducking", {})

    # Passo 1: Pré-processamento da voz (highpass + de-ess leve + compressão)
    voice_pre = work_dir / "voice_pre.wav"
    pre_chain = (
        f"highpass=f={hp_hz},"
        "equalizer=f=6800:t=q:w=2.0:g=-2.5,"
        "acompressor=threshold=-18dB:ratio=3:attack=10:release=120:makeup=2,"
        "alimiter=limit=0.95"
    )
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-v",
            "warning",
            "-i",
            str(voice_cut),
            "-af",
            pre_chain,
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(voice_pre),
        ]
    )

    # Passo 2: Loudnorm 2-pass (-14 LUFS) na voz
    measure_proc = run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(voice_pre),
            "-af",
            f"loudnorm=I={target_i}:TP={target_tp}:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ],
        capture=True,
    )
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", measure_proc.stderr, re.DOTALL)
    voice_norm = work_dir / "voice_norm.wav"
    if m:
        stats = json.loads(m.group(0))
        if stats.get("input_i") not in (None, "-inf"):
            ln_filter = (
                f"loudnorm=I={target_i}:TP={target_tp}:LRA=11:"
                f"measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
                f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:"
                f"offset={stats['target_offset']}:linear=true"
            )
        else:
            log("Aviso: loudnorm pass 1 retornou -inf; usando loudnorm 1-pass.")
            ln_filter = f"loudnorm=I={target_i}:TP={target_tp}:LRA=11"
    else:
        log("Aviso: não foi possível ler JSON do loudnorm pass 1; usando loudnorm 1-pass.")
        ln_filter = f"loudnorm=I={target_i}:TP={target_tp}:LRA=11"

    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-v",
            "warning",
            "-i",
            str(voice_pre),
            "-af",
            ln_filter,
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(voice_norm),
        ]
    )

    # Passo 3: Renderiza faixa de SFX separada (se houver eventos dentro de final_dur)
    sfx_events = [e for e in plan.get("sfx", []) if 0.0 <= float(e["time"]) < final_dur - 0.15]
    sfx_track: Path | None = None
    resolved_events: list[tuple[dict[str, Any], Path]] = []
    for ev in sfx_events:
        p = resolve_sfx_file(sfx_dir, str(ev["sound"]))
        if p is None:
            log(f"Aviso: SFX '{ev['sound']}' não encontrado em {sfx_dir}; ignorando.")
            continue
        resolved_events.append((ev, p))

    if resolved_events:
        sfx_track = work_dir / "sfx_mix.wav"
        unique_paths: list[Path] = []
        path_to_idx: dict[Path, int] = {}
        sfx_inputs: list[str] = [
            "-f",
            "lavfi",
            "-t",
            f"{final_dur:.6f}",
            "-i",
            "anullsrc=r=48000:cl=stereo",
        ]
        for _, p in resolved_events:
            if p not in path_to_idx:
                path_to_idx[p] = len(unique_paths) + 1
                unique_paths.append(p)
                sfx_inputs.extend(["-i", str(p)])

        use_counts: dict[int, int] = {}
        for _, p in resolved_events:
            idx = path_to_idx[p]
            use_counts[idx] = use_counts.get(idx, 0) + 1

        sfx_filters: list[str] = []
        for idx, count in sorted(use_counts.items()):
            if count > 1:
                outs = "".join(f"[u{idx}_{k}]" for k in range(count))
                sfx_filters.append(f"[{idx}:a]asplit={count}{outs}")

        used_cursor: dict[int, int] = {}
        mix_labels: list[str] = ["[0:a]"]
        for i, (ev, p) in enumerate(resolved_events):
            in_idx = path_to_idx[p]
            if use_counts[in_idx] > 1:
                k = used_cursor.get(in_idx, 0)
                used_cursor[in_idx] = k + 1
                src_lbl = f"[u{in_idx}_{k}]"
            else:
                src_lbl = f"[{in_idx}:a]"
            delay_ms = int(round(float(ev["time"]) * 1000))
            gain_db = float(ev.get("gain_db", -14.0))
            lbl = f"s{i}"
            sfx_filters.append(
                f"{src_lbl}aformat=sample_rates=48000:channel_layouts=stereo,"
                f"volume={gain_db:.1f}dB,adelay={delay_ms}|{delay_ms}[{lbl}]"
            )
            mix_labels.append(f"[{lbl}]")

        sfx_filters.append(
            f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:duration=first:dropout_transition=0:normalize=0,"
            f"atrim=0:{final_dur:.6f}[sout]"
        )
        sfx_fc = work_dir / "sfx_fc.txt"
        sfx_fc.write_text(";\n".join(sfx_filters), encoding="utf-8")
        run_cmd(
            [
                "ffmpeg",
                "-y",
                "-v",
                "warning",
                *sfx_inputs,
                "-filter_complex_script",
                str(sfx_fc),
                "-map",
                "[sout]",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-c:a",
                "pcm_s16le",
                str(sfx_track),
            ]
        )

    # Passo 4: Mix final (voz + trilha opcional com sidechain ducking + SFX)
    final_audio = work_dir / "audio_final.m4a"
    has_music = trilha_path is not None and trilha_path.exists() and trilha_path.stat().st_size > 0

    if has_music and trilha_path is not None:
        fade_out_st = max(0.0, final_dur - 2.5)
        sc_thresh = duck.get("threshold", 0.025)
        sc_ratio = duck.get("ratio", 4.0)
        sc_attack = duck.get("attack_ms", 30)
        sc_release = duck.get("release_ms", 350)

        mix_inputs = [
            "-i",
            str(voice_norm),
            "-stream_loop",
            "-1",
            "-i",
            str(trilha_path),
        ]
        filter_parts = [
            "[0:a]asplit=2[v_main][v_key]",
            (
                f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                f"atrim=0:{final_dur:.6f},asetpts=PTS-STARTPTS,"
                f"afade=t=in:st=0:d=1.5,afade=t=out:st={fade_out_st:.6f}:d=2.5,"
                f"volume={music_db:.1f}dB[bgm_raw]"
            ),
            (
                f"[bgm_raw][v_key]sidechaincompress=threshold={sc_thresh}:ratio={sc_ratio}:"
                f"attack={sc_attack}:release={sc_release}:makeup=1[bgm_duck]"
            ),
        ]
        if sfx_track is not None:
            mix_inputs.extend(["-i", str(sfx_track)])
            filter_parts.append(
                "[v_main][bgm_duck][2:a]amix=inputs=3:duration=first:dropout_transition=2:normalize=0,"
                "alimiter=limit=0.95[aout]"
            )
        else:
            filter_parts.append(
                "[v_main][bgm_duck]amix=inputs=2:duration=first:dropout_transition=2:normalize=0,"
                "alimiter=limit=0.95[aout]"
            )
    else:
        log("Mixando áudio SEM trilha de fundo (apenas voz + SFX).")
        mix_inputs = ["-i", str(voice_norm)]
        if sfx_track is not None:
            mix_inputs.extend(["-i", str(sfx_track)])
            filter_parts = [
                "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=2:normalize=0,"
                "alimiter=limit=0.95[aout]"
            ]
        else:
            filter_parts = ["[0:a]alimiter=limit=0.95[aout]"]

    mix_fc = work_dir / "mix_fc.txt"
    mix_fc.write_text(";\n".join(filter_parts), encoding="utf-8")
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-v",
            "warning",
            *mix_inputs,
            "-filter_complex_script",
            str(mix_fc),
            "-map",
            "[aout]",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(final_audio),
        ]
    )
    return final_audio


def choose_chunk_boundaries(final_dur: float, chunk_seconds: float, zooms: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """Divide o tempo final em pedaços de ~chunk_seconds evitando cortar no meio de um zoom."""
    if final_dur <= chunk_seconds + 15.0:
        return [(0.0, final_dur)]

    boundaries: list[float] = [0.0]
    t = chunk_seconds
    while t < final_dur - 15.0:
        # Se t cai dentro de um zoom, empurra para o fim daquele zoom
        adjusted = t
        for z in zooms:
            if float(z["start"]) < adjusted < float(z["end"]):
                adjusted = float(z["end"])
                break
        if adjusted <= boundaries[-1] + 5.0:
            adjusted = t
        if adjusted >= final_dur - 5.0:
            break
        boundaries.append(round(adjusted, 6))
        t = adjusted + chunk_seconds
    boundaries.append(round(final_dur, 6))
    return [(boundaries[i], boundaries[i + 1]) for i in range(len(boundaries) - 1)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Renderiza o ep01 do canal IA Antony.")
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--preview", action="store_true", help="Renderiza de 0:00 a 1:30 do vídeo final.")
    mode_group.add_argument("--full", action="store_true", help="Renderiza o vídeo completo.")
    parser.add_argument("--repo", default=DEFAULT_GH_REPO, help="Repositório GitHub com a release de mídias.")
    parser.add_argument("--release", default=DEFAULT_RELEASE, help="Tag da release com as mídias.")
    parser.add_argument("--media-dir", type=Path, default=REPO_ROOT / "media")
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "out")
    parser.add_argument("--work-dir", type=Path, default=REPO_ROOT / "work")
    parser.add_argument("--fonts-dir", type=Path, default=REPO_ROOT / "assets" / "fonts")
    parser.add_argument("--sfx-dir", type=Path, default=REPO_ROOT / "assets" / "sfx")
    parser.add_argument("--chunk-seconds", type=float, default=120.0, help="Tamanho aproximado de cada pedaço de vídeo.")
    parser.add_argument("--workers", type=int, default=2, help="Quantidade de chunks de vídeo renderizados em paralelo.")
    parser.add_argument("--skip-download", action="store_true", help="Não baixa mídias da release; exige arquivos locais.")
    args = parser.parse_args()

    mode = "preview" if args.preview else "full"
    limit_s = PREVIEW_LIMIT_S if args.preview else None
    log(f"Iniciando build no modo --{mode}")

    edl_path = REPO_ROOT / "edit" / "edl.json"
    plan_path = REPO_ROOT / "edit" / "plan.json"
    ass_path = REPO_ROOT / "edit" / "overlays.ass"

    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    # 1. Aplica recorte de preview (90s) se solicitado
    clips, final_dur = slice_clips_for_limit(edl["clips"], limit_s)
    used_parts = {c["part"] for c in clips}
    log(
        f"Clipes ativos: {len(clips)} (partes: {', '.join(sorted(used_parts))}) | "
        f"Duração final prevista: {final_dur:.2f}s ({final_dur/60:.2f} min)"
    )

    # 2. Garante mídias obrigatórias e trilha opcional (segue sem ela com aviso se faltar)
    _, trilha_path = ensure_media(edl, used_parts, args.media_dir, args.repo, args.release, args.skip_download)

    work_dir = args.work_dir / mode
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    # 3. Corta voz e vídeo pelo EDL
    voice_cut = cut_voice_edl(clips, edl, args.media_dir, work_dir)
    video_cut = cut_video_edl(clips, edl, args.media_dir, work_dir)
    actual_voice_dur = ffprobe_duration(voice_cut)
    log(f"Voz cortada: {actual_voice_dur:.3f}s")

    # 4. Divide em pedaços no tempo para aplicar zooms + queimar overlays.ass em paralelo
    zooms = plan.get("zooms", [])
    chunks = choose_chunk_boundaries(actual_voice_dur, args.chunk_seconds, zooms)
    log(f"Renderizando {len(chunks)} pedaço(s) de vídeo (workers={args.workers})...")

    cpu_count = os.cpu_count() or 4
    threads_per_worker = max(1, cpu_count // max(1, min(args.workers, len(chunks))))
    fonts_dir = args.fonts_dir if args.fonts_dir.exists() else None

    rendered_chunks: list[Path] = [Path()] * len(chunks)
    if len(chunks) == 1 or args.workers <= 1:
        for idx, (cs, ce) in enumerate(chunks):
            rendered_chunks[idx] = render_video_chunk(
                idx, cs, ce, video_cut, zooms, ass_path, fonts_dir, work_dir, cpu_count
            )
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {
                pool.submit(
                    render_video_chunk,
                    idx,
                    cs,
                    ce,
                    video_cut,
                    zooms,
                    ass_path,
                    fonts_dir,
                    work_dir,
                    threads_per_worker,
                ): idx
                for idx, (cs, ce) in enumerate(chunks)
            }
            for fut in concurrent.futures.as_completed(futs):
                idx = futs[fut]
                rendered_chunks[idx] = fut.result()

    # 5. Concatena os pedaços renderizados
    video_final = work_dir / "video_rendered.mp4"
    if len(rendered_chunks) == 1:
        shutil.copy2(rendered_chunks[0], video_final)
    else:
        concat_txt = work_dir / "final_chunks.txt"
        concat_txt.write_text(
            "\n".join(f"file '{p.resolve()}'" for p in rendered_chunks) + "\n",
            encoding="utf-8",
        )
        run_cmd(
            [
                "ffmpeg",
                "-y",
                "-v",
                "warning",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_txt),
                "-c:v",
                "copy",
                str(video_final),
            ]
        )

    # 6. Masteriza e mixa voz + trilha opcional (-24 dB com ducking) + SFX
    audio_final = master_audio(
        voice_cut,
        trilha_path,
        plan,
        actual_voice_dur,
        args.sfx_dir,
        work_dir,
    )

    # 7. Mux final MP4 1080p30 h264 CRF 20 AAC 192k
    out_mp4 = args.out_dir / f"ep01_{mode}.mp4"
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-v",
            "warning",
            "-i",
            str(video_final),
            "-i",
            str(audio_final),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-shortest",
            "-movflags",
            "+faststart",
            str(out_mp4),
        ]
    )

    final_dur_check = ffprobe_duration(out_mp4)
    size_mb = out_mp4.stat().st_size / (1024 * 1024)
    log(f"CONCLUÍDO: {out_mp4} ({final_dur_check:.2f}s, {size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
