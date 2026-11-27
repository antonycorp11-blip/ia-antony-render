#!/usr/bin/env python3
"""
Build pipeline para renderização do ep01 (IA Antony) no GitHub Actions.

Modos:
  --preview  Renderiza de 0:00 a 2:10 (130s) da linha do tempo final (abertura completa + volta pra câmera espelhada).
  --full     Renderiza o vídeo completo.

Recursos:
  - Classificação automática de cenas (CAMERA_CHEIA vs TELA) em out/scene_map.json
  - Espelhamento automático: hflip total em CAMERA_CHEIA; crop+hflip+overlay só no círculo da webcam em TELA
  - Abertura cinética (0:00 até 1:51.0): fundo B-roll da TELA com zoom/blur/escurecimento + 3 relances curtos
    do rosto espelhado + overlay transparente gerado do HTML (abertura.mov qtrle ou sequência PNG)
  - Crop global das barras do macOS (44 px topo / 48 px base) mantendo 16:9
  - Zooms suaves, cartões e legendas ASS grandes, masterização -14 LUFS + SFX

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
import struct
import subprocess
import sys
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GH_REPO = "antonycorp11-blip/ia-antony-render"
DEFAULT_RELEASE = "media"
PREVIEW_LIMIT_S = 130.0  # 0:00 a 2:10
OPENING_END_S = 111.0  # 0:00 até 1:51.0 (fim da introdução, antes da Seção 00 EQUIPAMENTO)
# Relances curtos (1.9s cada) do rosto espelhado na abertura — sincronizados com abertura/index.html
OPENING_RELANCES: list[tuple[float, float]] = [
    (7.60, 9.55),   # "quase fez eu perder o cliente, viu?"
    (14.30, 16.20), # "Fala pessoal, meu nome é Aquilles Antony"
    (63.65, 65.55), # "E eu falei: cara, eu posso te ajudar"
]
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
# Caixa padrão do círculo da câmera em TELA (1920x1080, conferida em frames/f4.jpg)
DEFAULT_WEBCAM_BOX: dict[str, Any] = {
    "x": 0,
    "y": 0,
    "w": 344,
    "h": 314,
    "circular_mask": False,
    "source": "fallback",
}


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


def warn(msg: str) -> None:
    """Aviso visível no log e como anotação do GitHub Actions."""
    log(f"AVISO: {msg}")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::{msg}", flush=True)


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


def run_cmd_bytes(cmd: list[str]) -> bytes:
    """Executa comando retornando stdout em bytes crus (ex.: rawvideo rgb24 do ffmpeg)."""
    log("$ " + " ".join(str(c) for c in cmd))
    proc = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        if proc.stderr:
            print(proc.stderr.decode("utf-8", errors="replace"), file=sys.stderr)
        raise RuntimeError(f"Comando falhou (exit {proc.returncode}): {' '.join(cmd)}")
    return proc.stdout


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


def ensure_media(
    edl: dict[str, Any],
    used_parts: set[str],
    media_dir: Path,
    gh_repo: str,
    release: str,
    skip_download: bool,
    need_broll_p3: bool = True,
) -> tuple[dict[str, Path], Path | None]:
    """Baixa vídeos/vozes obrigatórios, p3_video.mp4 (para B-roll da abertura mesmo em --preview) e trilha opcional."""
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

    # Garante p3_video.mp4 para o B-roll da abertura (tabelas e relatório na parte 3) mesmo no modo --preview
    if need_broll_p3 and "p3" in edl["sources"] and "p3" not in used_parts:
        p3_vid = edl["sources"]["p3"]["video"]
        p3_dest = media_dir / p3_vid
        if (not p3_dest.exists() or p3_dest.stat().st_size == 0) and not skip_download:
            download_release_asset(gh_repo, release, p3_vid, p3_dest)
        if p3_dest.exists() and p3_dest.stat().st_size > 0:
            paths[p3_vid] = p3_dest
            log(f"Vídeo extra para B-roll da abertura disponível: {p3_dest}")
        else:
            warn(f"{p3_vid} indisponível para B-roll; usando trechos TELA disponíveis em p1.")

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


# ==================== CLASSIFICAÇÃO DE CENAS & ESPELHAMENTO ====================


def write_circle_mask_png(path: Path, w: int, h: int, cx: float, cy: float, r: float) -> Path:
    """Gera uma máscara PNG em escala de cinza (8-bit, borda suave 2px) sem dependências externas."""
    rows: list[bytes] = []
    r_inner = max(1.0, r - 1.5)
    r_outer = r + 1.5
    for y in range(h):
        dy = y - cy
        dy2 = dy * dy
        row = bytearray(w + 1)
        row[0] = 0  # PNG filter type None
        for x in range(w):
            dx = x - cx
            dist = math.sqrt(dx * dx + dy2)
            if dist <= r_inner:
                row[x + 1] = 255
            elif dist >= r_outer:
                row[x + 1] = 0
            else:
                row[x + 1] = int(round(255.0 * (r_outer - dist) / (r_outer - r_inner)))
        rows.append(bytes(row))

    raw = b"".join(rows)

    def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + chunk_type
            + data
            + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)  # 8-bit grayscale
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", ihdr)
        + png_chunk(b"IDAT", zlib.compress(raw, level=6))
        + png_chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes)
    return path


def detect_webcam_circle(video_path: Path, tela_segments: list[dict[str, Any]]) -> dict[str, Any]:
    """Detecta a caixa do círculo da webcam laranja no canto superior esquerdo em quadros TELA."""
    if not tela_segments:
        return dict(DEFAULT_WEBCAM_BOX)

    # Escolhe até 3 instantes representativos dentro dos maiores trechos TELA
    ranked = sorted(tela_segments, key=lambda s: float(s["end"]) - float(s["start"]), reverse=True)
    sample_times: list[float] = []
    for seg in ranked:
        s, e = float(seg["start"]), float(seg["end"])
        if e - s < 2.0:
            continue
        sample_times.append(s + (e - s) * 0.5)
        if len(sample_times) >= 3:
            break
    if not sample_times:
        seg = ranked[0]
        sample_times = [0.5 * (float(seg["start"]) + float(seg["end"]))]

    sw, sh = 640, 360
    scale_x = WIDTH / sw   # 3.0
    scale_y = HEIGHT / sh  # 3.0
    max_scan_x = 130  # 390 px em 1920
    max_scan_y = 120  # 360 px em 1080

    boxes: list[tuple[int, int, int]] = []  # (x0, x1, y1) em 1920x1080
    for ts in sample_times:
        try:
            raw = run_cmd_bytes(
                [
                    "ffmpeg",
                    "-v",
                    "warning",
                    "-ss",
                    f"{ts:.3f}",
                    "-i",
                    str(video_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={sw}:{sh}:flags=area",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgb24",
                    "-",
                ]
            )
        except RuntimeError:
            continue
        if len(raw) < sw * sh * 3:
            continue

        col_counts = [0] * max_scan_x
        row_counts = [0] * max_scan_y
        total_orange = 0
        for y in range(max_scan_y):
            row_off = y * sw * 3
            for x in range(max_scan_x):
                idx = row_off + x * 3
                r = raw[idx]
                g = raw[idx + 1]
                b = raw[idx + 2]
                if r >= 120 and (r - b) >= 42 and r > g:
                    col_counts[x] += 1
                    row_counts[y] += 1
                    total_orange += 1

        if total_orange < 180:
            continue
        xs = [x for x, c in enumerate(col_counts) if c >= 4]
        ys = [y for y, c in enumerate(row_counts) if c >= 4]
        if not xs or not ys:
            continue
        x0 = max(0, int(round(xs[0] * scale_x)) - 4)
        x1 = min(370, int(round((xs[-1] + 1) * scale_x)) + 6)
        y1 = min(334, int(round((ys[-1] + 1) * scale_y)) + 6)
        if (x1 - x0) >= 220 and y1 >= 220:
            boxes.append((x0, x1, y1))

    if not boxes:
        return dict(DEFAULT_WEBCAM_BOX)

    boxes.sort()
    mid = len(boxes) // 2
    x0 = boxes[mid][0] & ~1
    x1 = min(370, (boxes[mid][1] + 1) & ~1)
    y1 = min(334, (boxes[mid][2] + 1) & ~1)
    w = max(220, (x1 - x0) & ~1)
    h = max(220, y1 & ~1)
    # Quando x0 >= 8 o círculo não está cortado na esquerda (ex: f2.jpg): usa máscara circular para não espelhar texto de fundo
    return {
        "x": x0,
        "y": 0,
        "w": w,
        "h": h,
        "circular_mask": x0 >= 8,
        "source": "auto",
    }


def classify_video_scenes(video_path: Path, sample_interval_s: float = 2.0) -> dict[str, Any]:
    """Amostra 1 quadro a cada 2s em 64x36 e classifica CAMERA_CHEIA (fundo laranja) vs TELA (interface escura)."""
    duration = ffprobe_duration(video_path)
    w, h = 64, 36
    frame_bytes = w * h * 3
    fps_expr = f"1/{sample_interval_s:g}"

    raw = run_cmd_bytes(
        [
            "ffmpeg",
            "-v",
            "warning",
            "-i",
            str(video_path),
            "-vf",
            f"fps={fps_expr},scale={w}:{h}:flags=area",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ]
    )

    n_frames = len(raw) // frame_bytes
    if n_frames == 0:
        return {
            "duration_s": round(duration, 3),
            "sample_interval_s": sample_interval_s,
            "segments": [{"start": 0.0, "end": round(duration, 3), "scene": "CAMERA_CHEIA"}],
            "webcam_box": dict(DEFAULT_WEBCAM_BOX),
        }

    pixels = w * h
    samples: list[dict[str, Any]] = []
    for k in range(n_frames):
        base = k * frame_bytes
        chunk = raw[base : base + frame_bytes]
        avg_r = sum(chunk[0::3]) / pixels
        avg_g = sum(chunk[1::3]) / pixels
        avg_b = sum(chunk[2::3]) / pixels
        # CAMERA_CHEIA: parede laranja dominante (R alto e R-B alto); TELA: interface escura
        scene = "CAMERA_CHEIA" if (avg_r >= 85.0 and (avg_r - avg_b) >= 32.0) else "TELA"
        samples.append(
            {
                "t": round(k * sample_interval_s, 3),
                "scene": scene,
                "r": round(avg_r, 1),
                "g": round(avg_g, 1),
                "b": round(avg_b, 1),
            }
        )

    # Suavização de picos isolados de 1 amostra
    labels = [s["scene"] for s in samples]
    if len(labels) >= 3:
        smoothed = list(labels)
        for i in range(1, len(labels) - 1):
            if labels[i - 1] == labels[i + 1] and labels[i] != labels[i - 1]:
                smoothed[i] = labels[i - 1]
        for i, lbl in enumerate(smoothed):
            samples[i]["scene"] = lbl

    # Agrupa amostras contíguas em intervalos [start, end]
    segments: list[dict[str, Any]] = []
    cur_scene = samples[0]["scene"]
    cur_start = 0.0
    acc_r: list[float] = [samples[0]["r"]]
    acc_g: list[float] = [samples[0]["g"]]
    acc_b: list[float] = [samples[0]["b"]]

    for i in range(1, len(samples)):
        sc = samples[i]["scene"]
        if sc != cur_scene:
            # Fronteira estimada no meio entre a última amostra anterior e a atual
            boundary = round(max(cur_start, (samples[i - 1]["t"] + samples[i]["t"]) * 0.5), 3)
            segments.append(
                {
                    "start": round(cur_start, 3),
                    "end": boundary,
                    "scene": cur_scene,
                    "avg_rgb": [
                        round(sum(acc_r) / len(acc_r), 1),
                        round(sum(acc_g) / len(acc_g), 1),
                        round(sum(acc_b) / len(acc_b), 1),
                    ],
                }
            )
            cur_scene = sc
            cur_start = boundary
            acc_r, acc_g, acc_b = [samples[i]["r"]], [samples[i]["g"]], [samples[i]["b"]]
        else:
            acc_r.append(samples[i]["r"])
            acc_g.append(samples[i]["g"])
            acc_b.append(samples[i]["b"])

    segments.append(
        {
            "start": round(cur_start, 3),
            "end": round(duration, 3),
            "scene": cur_scene,
            "avg_rgb": [
                round(sum(acc_r) / len(acc_r), 1),
                round(sum(acc_g) / len(acc_g), 1),
                round(sum(acc_b) / len(acc_b), 1),
            ],
        }
    )

    tela_segs = [s for s in segments if s["scene"] == "TELA"]
    webcam_box = detect_webcam_circle(video_path, tela_segs)

    return {
        "video": video_path.name,
        "duration_s": round(duration, 3),
        "sample_interval_s": sample_interval_s,
        "samples_count": n_frames,
        "webcam_box": webcam_box,
        "segments": segments,
    }


def split_clips_by_scene_map(
    clips: list[dict[str, Any]],
    scene_parts: dict[str, Any],
) -> list[dict[str, Any]]:
    """Divide clipes do EDL se cruzarem fronteira CAMERA_CHEIA <-> TELA, anotando 'scene' em cada sub-clipe."""
    out: list[dict[str, Any]] = []
    for clip in clips:
        p = clip["part"]
        s = float(clip["start"])
        e = float(clip["end"])
        out_s = float(clip["out_start"])
        segs = scene_parts.get(p, {}).get("segments", [])
        if not segs:
            c = dict(clip)
            c["scene"] = "CAMERA_CHEIA"
            out.append(c)
            continue

        # Encontra todos os segmentos que se sobrepõem a [s, e]
        overlaps: list[tuple[float, float, str]] = []
        for seg in segs:
            ov_s = max(s, float(seg["start"]))
            ov_e = min(e, float(seg["end"]))
            if ov_e > ov_s + 0.001:
                overlaps.append((ov_s, ov_e, str(seg["scene"])))

        if not overlaps:
            c = dict(clip)
            c["scene"] = "CAMERA_CHEIA"
            out.append(c)
            continue

        # Evita micro-fatias menores que 0.25s nas bordas incorporando-as ao vizinho
        merged: list[list[Any]] = []
        for ov_s, ov_e, sc in overlaps:
            if merged and merged[-1][2] == sc:
                merged[-1][1] = ov_e
            else:
                merged.append([ov_s, ov_e, sc])

        if len(merged) > 1:
            cleaned: list[list[Any]] = []
            for item in merged:
                dur = item[1] - item[0]
                if dur < 0.25 and cleaned:
                    cleaned[-1][1] = item[1]
                else:
                    cleaned.append(item)
            if len(cleaned) > 1 and (cleaned[0][1] - cleaned[0][0]) < 0.25:
                cleaned[1][0] = cleaned[0][0]
                cleaned.pop(0)
            merged = cleaned

        # Garante cobertura exata [s, e] sem perder milissegundos
        merged[0][0] = s
        merged[-1][1] = e

        cursor = out_s
        for sub_idx, (sub_s, sub_e, sc) in enumerate(merged):
            dur = round(sub_e - sub_s, 6)
            if dur <= 0.01:
                continue
            c = dict(clip)
            c["start"] = round(sub_s, 6)
            c["end"] = round(sub_e, 6)
            c["out_start"] = round(cursor, 6)
            c["out_end"] = round(cursor + dur, 6)
            c["scene"] = sc
            c["sub_idx"] = sub_idx
            out.append(c)
            cursor += dur
    return out


def build_scene_map(
    edl: dict[str, Any],
    clips: list[dict[str, Any]],
    parts_to_scan: set[str],
    media_dir: Path,
    out_dir: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Classifica todas as partes disponíveis, salva out/scene_map.json e registra resumo no log."""
    scene_parts: dict[str, Any] = {}
    for p in sorted(parts_to_scan):
        vid_path = media_dir / edl["sources"][p]["video"]
        if not vid_path.exists() or vid_path.stat().st_size == 0:
            continue
        log(f"Classificando cenas de {p} ({vid_path.name})...")
        info = classify_video_scenes(vid_path, sample_interval_s=2.0)
        scene_parts[p] = info

        cam_s = sum(seg["end"] - seg["start"] for seg in info["segments"] if seg["scene"] == "CAMERA_CHEIA")
        tela_s = sum(seg["end"] - seg["start"] for seg in info["segments"] if seg["scene"] == "TELA")
        wb = info["webcam_box"]
        log(
            f"  -> {p}: {len(info['segments'])} trechos | "
            f"CAMERA_CHEIA={cam_s:.1f}s | TELA={tela_s:.1f}s | "
            f"webcam_box=x:{wb['x']},y:{wb['y']},w:{wb['w']},h:{wb['h']} (mask={wb['circular_mask']}, {wb['source']})"
        )
        for seg in info["segments"]:
            log(f"     [{seg['start']:6.1f}s - {seg['end']:6.1f}s] {seg['scene']:12s} rgb={seg.get('avg_rgb')}")

    annotated_clips = split_clips_by_scene_map(clips, scene_parts)
    scene_map = {
        "rule": {
            "sample_interval_s": 2.0,
            "sample_size": [64, 36],
            "camera_cheia_condition": "avg_R >= 85 and (avg_R - avg_B) >= 32",
            "camera_cheia_mirror": "hflip full frame",
            "tela_mirror": "crop webcam circle top-left + hflip (+ optional circular mask) + overlay",
        },
        "parts": scene_parts,
        "clips": [
            {
                "id": c["id"],
                "part": c["part"],
                "start": c["start"],
                "end": c["end"],
                "out_start": c["out_start"],
                "out_end": c["out_end"],
                "scene": c["scene"],
            }
            for c in annotated_clips
        ],
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    scene_map_path = out_dir / "scene_map.json"
    scene_map_path.write_text(json.dumps(scene_map, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"Mapa de cenas salvo em: {scene_map_path} ({len(annotated_clips)} sub-clipes anotados)")
    return scene_map, annotated_clips


# ==================== CORTE DE VOZ E VÍDEO ====================


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
    scene_map: dict[str, Any],
    media_dir: Path,
    work_dir: Path,
) -> Path:
    """Corta e concatena o vídeo mudo em 1080p30 CFR, aplicando espelhamento por cena + crop global do macOS."""
    video_out = work_dir / "video_cut.mp4"
    seg_dir = work_dir / "video_segs"
    seg_dir.mkdir(parents=True, exist_ok=True)

    used_parts = {c["part"] for c in clips}
    vid_durations: dict[str, float] = {}
    mask_paths: dict[str, Path] = {}
    for part_name in sorted(used_parts):
        cfg = edl["sources"][part_name]
        vid_durations[part_name] = ffprobe_duration(media_dir / cfg["video"])
        log(f"Duração real do vídeo {part_name}: {vid_durations[part_name]:.3f}s")

        wb = scene_map.get("parts", {}).get(part_name, {}).get("webcam_box", DEFAULT_WEBCAM_BOX)
        if wb.get("circular_mask"):
            bw, bh = int(wb["w"]), int(wb["h"])
            r = bw / 2.0
            cx = bw / 2.0
            cy = bh - r
            mask_paths[part_name] = write_circle_mask_png(
                seg_dir / f"cam_mask_{part_name}.png", bw, bh, cx, cy, r + 2.0
            )
            log(f"Máscara circular gerada para {part_name}: {mask_paths[part_name]} ({bw}x{bh})")

    batch_size = 6
    batch_files: list[Path] = []
    crop_scale = global_crop_scale_filter()

    for b_idx in range(0, len(clips), batch_size):
        batch = clips[b_idx : b_idx + batch_size]
        batch_mp4 = seg_dir / f"vbatch_{b_idx // batch_size:03d}.mp4"

        inputs: list[str] = []
        next_in_idx = 0
        filter_lines: list[str] = []
        concat_labels: list[str] = []

        for i, c in enumerate(batch):
            p = c["part"]
            scene = c.get("scene", "CAMERA_CHEIA")
            cfg = edl["sources"][p]
            vid_path = media_dir / cfg["video"]
            offset = float(cfg.get("video_offset_s", 0.0))
            real_dur = vid_durations[p]
            target_dur = max(0.04, float(c["end"]) - float(c["start"]))
            v_start = max(0.0, float(c["start"]) + offset)
            v_end = v_start + target_dur

            needs_freeze = v_end > real_dur - 0.02 and bool(cfg.get("freeze_last_frame", False))
            seek_start = min(v_start, max(0.0, real_dur - 0.1)) if needs_freeze else v_start

            vid_in = next_in_idx
            next_in_idx += 1
            if needs_freeze:
                inputs.extend(["-ss", f"{seek_start:.6f}", "-i", str(vid_path)])
                pad_s = max(0.2, (v_end - real_dur) + 0.5)
                base_prep = (
                    f"[{vid_in}:v]tpad=stop_mode=clone:stop_duration={pad_s:.3f},"
                    f"fps={FPS},trim=start=0:duration={target_dur:.6f},setpts=PTS-STARTPTS"
                )
            else:
                inputs.extend(["-ss", f"{seek_start:.6f}", "-t", f"{target_dur + 0.25:.6f}", "-i", str(vid_path)])
                base_prep = (
                    f"[{vid_in}:v]fps={FPS},trim=start=0:duration={target_dur:.6f},setpts=PTS-STARTPTS"
                )

            lbl = f"v{i}"
            if scene == "TELA":
                wb = scene_map.get("parts", {}).get(p, {}).get("webcam_box", DEFAULT_WEBCAM_BOX)
                bx, by, bw, bh = int(wb["x"]), int(wb["y"]), int(wb["w"]), int(wb["h"])
                filter_lines.append(f"{base_prep},scale={WIDTH}:{HEIGHT}:flags=lanczos,split=2[b{i}][c{i}]")
                if wb.get("circular_mask") and p in mask_paths:
                    mask_in = next_in_idx
                    next_in_idx += 1
                    inputs.extend(
                        [
                            "-loop",
                            "1",
                            "-framerate",
                            str(FPS),
                            "-t",
                            f"{target_dur:.6f}",
                            "-i",
                            str(mask_paths[p]),
                        ]
                    )
                    filter_lines.append(f"[c{i}]crop=w={bw}:h={bh}:x={bx}:y={by},hflip,format=rgba[cf{i}]")
                    filter_lines.append(f"[{mask_in}:v]format=gray[mk{i}]")
                    filter_lines.append(f"[cf{i}][mk{i}]alphamerge[cfa{i}]")
                    filter_lines.append(f"[b{i}][cfa{i}]overlay=x={bx}:y={by}:shortest=1,{crop_scale}[{lbl}]")
                else:
                    filter_lines.append(f"[c{i}]crop=w={bw}:h={bh}:x={bx}:y={by},hflip[cf{i}]")
                    filter_lines.append(f"[b{i}][cf{i}]overlay=x={bx}:y={by}:shortest=1,{crop_scale}[{lbl}]")
            else:
                # CAMERA_CHEIA: espelha o quadro inteiro
                filter_lines.append(f"{base_prep},hflip,{crop_scale}[{lbl}]")

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


# ==================== FUNDO DA ABERTURA (B-ROLL DA TELA + RELANCES) ====================


def collect_broll_candidates(
    scene_map: dict[str, Any],
    edl: dict[str, Any],
    media_dir: Path,
) -> list[tuple[Path, float]]:
    """Coleta instantes variados em trechos TELA (priorizando parte 3, depois parte 1) para o B-roll da abertura."""
    by_part: dict[str, list[tuple[Path, float]]] = {}
    for p in ("p3", "p1", "p2"):
        p_info = scene_map.get("parts", {}).get(p)
        if not p_info:
            continue
        vid_path = media_dir / edl["sources"][p]["video"]
        if not vid_path.exists():
            continue
        cands: list[tuple[Path, float]] = []
        for seg in p_info.get("segments", []):
            if seg.get("scene") != "TELA":
                continue
            s = float(seg["start"]) + 1.5
            e = float(seg["end"]) - 5.5
            if e <= s:
                continue
            t = s
            while t <= e:
                cands.append((vid_path, round(t, 2)))
                t += 7.5
        if cands:
            by_part[p] = cands

    # Intercala p3 (relatório de serviços/custos/valor) com p1 (resumo/receita/venda) priorizando p3
    ordered: list[tuple[Path, float]] = []
    p3_list = by_part.get("p3", [])
    p1_list = by_part.get("p1", [])
    p2_list = by_part.get("p2", [])
    i3 = i1 = 0
    while i3 < len(p3_list) or i1 < len(p1_list):
        for _ in range(2):
            if i3 < len(p3_list):
                ordered.append(p3_list[i3])
                i3 += 1
        if i1 < len(p1_list):
            ordered.append(p1_list[i1])
            i1 += 1
    ordered.extend(p2_list)
    return ordered


def plan_opening_segments(opening_dur: float) -> list[dict[str, Any]]:
    """Particiona [0, opening_dur] em cortes de B-roll (3–5s) intercalados pelos relances curtos do rosto."""
    relances = [
        (max(0.0, rs), min(opening_dur, re))
        for rs, re in OPENING_RELANCES
        if rs < opening_dur and re > rs + 0.1
    ]
    spans: list[tuple[float, float, str]] = []
    cur = 0.0
    for rs, re in relances:
        if rs > cur + 0.05:
            spans.append((cur, rs, "broll"))
        spans.append((rs, re, "relance"))
        cur = re
    if cur < opening_dur - 0.05:
        spans.append((cur, opening_dur, "broll"))

    raw_segs: list[tuple[float, float, str]] = []
    for s, e, kind in spans:
        if kind == "relance":
            raw_segs.append((s, e, "relance"))
        else:
            dur = e - s
            if dur <= 4.9:
                raw_segs.append((s, e, "broll"))
            else:
                n_shots = max(2, int(round(dur / 4.0)))
                step = dur / n_shots
                for k in range(n_shots):
                    ss = s + k * step
                    ee = e if k == n_shots - 1 else s + (k + 1) * step
                    raw_segs.append((ss, ee, "broll"))

    # Converte para contagem exata de quadros inteiros somando round(opening_dur * FPS)
    total_frames = int(round(opening_dur * FPS))
    planned: list[dict[str, Any]] = []
    frame_cursor = 0
    for idx, (s, e, kind) in enumerate(raw_segs):
        target_end_f = total_frames if idx == len(raw_segs) - 1 else int(round(e * FPS))
        n_frames = max(1, target_end_f - frame_cursor)
        planned.append(
            {
                "idx": idx,
                "kind": kind,
                "start": frame_cursor / float(FPS),
                "end": (frame_cursor + n_frames) / float(FPS),
                "n_frames": n_frames,
            }
        )
        frame_cursor += n_frames
    return planned


def render_opening_segment(
    seg: dict[str, Any],
    broll_source: tuple[Path, float] | None,
    video_cut: Path,
    seg_dir: Path,
    threads: int,
) -> Path:
    idx = seg["idx"]
    n_frames = seg["n_frames"]
    dur_s = n_frames / float(FPS)
    out_mp4 = seg_dir / f"open_{idx:03d}_{seg['kind']}.mp4"

    if seg["kind"] == "relance" or broll_source is None:
        # Mostra a câmera normal (já espelhada e com crop global) no tempo exato da fala
        run_cmd(
            [
                "ffmpeg",
                "-y",
                "-v",
                "warning",
                "-ss",
                f"{seg['start']:.6f}",
                "-i",
                str(video_cut),
                "-vf",
                f"fps={FPS},trim=start_frame=0:end_frame={n_frames},setpts=PTS-STARTPTS,setsar=1,format=yuv420p",
                "-frames:v",
                str(n_frames),
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
                "-threads",
                str(threads),
                str(out_mp4),
            ]
        )
        return out_mp4

    src_vid, src_t = broll_source
    # Alterna zoom lento push-in (1.00->1.07) e pull-out (1.07->1.00) focado na área do relatório (sem bolinha da câmera nem barras do Mac)
    zoom_in = (idx % 2 == 0)
    den = max(1, n_frames - 1)
    if zoom_in:
        z_expr = f"1.0000+0.0700*min(1,max(0,on-1)/{den})"
    else:
        z_expr = f"1.0700-0.0700*min(1,max(0,on-1)/{den})"

    # Crop 1504x846 (16:9 exato) começando em x=368, y=84: remove barras do Mac e a bolinha laranja da webcam à esquerda
    vf = (
        f"tpad=stop_mode=clone:stop_duration=2.0,"
        f"fps={FPS},trim=start_frame=0:end_frame={n_frames},setpts=PTS-STARTPTS,"
        f"crop=w=trunc((iw*1504/{WIDTH}+1)/2)*2:"
        f"h=trunc((ih*846/{HEIGHT}+1)/2)*2:"
        f"x=trunc((iw*368/{WIDTH}+1)/2)*2:"
        f"y=trunc((ih*84/{HEIGHT}+1)/2)*2,"
        f"scale={WIDTH}:{HEIGHT}:flags=lanczos,"
        f"zoompan=z='{z_expr}':x='(iw-iw/zoom)*0.5':y='(ih-ih/zoom)*0.5':d=1:s={WIDTH}x{HEIGHT}:fps={FPS},"
        f"setpts=PTS-STARTPTS,"
        f"gblur=sigma=4.5,eq=brightness=-0.14:contrast=1.06:saturation=0.85,vignette=PI/5,"
        f"setsar=1,format=yuv420p"
    )

    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-v",
            "warning",
            "-ss",
            f"{src_t:.3f}",
            "-t",
            f"{dur_s + 0.8:.3f}",
            "-i",
            str(src_vid),
            "-vf",
            vf,
            "-frames:v",
            str(n_frames),
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
            "-threads",
            str(threads),
            str(out_mp4),
        ]
    )
    return out_mp4


def build_opening_base_video(
    opening_dur: float,
    scene_map: dict[str, Any],
    edl: dict[str, Any],
    media_dir: Path,
    video_cut: Path,
    work_dir: Path,
    workers: int,
) -> Path:
    """Constrói o vídeo base da abertura (0:00 até opening_dur): B-roll da TELA + 3 relances curtos do rosto."""
    seg_dir = work_dir / "opening_segs"
    seg_dir.mkdir(parents=True, exist_ok=True)

    candidates = collect_broll_candidates(scene_map, edl, media_dir)
    if not candidates:
        warn("Nenhum trecho TELA encontrado no scene_map para o B-roll; usando câmera normal na abertura.")
    else:
        log(f"Candidatos de B-roll TELA disponíveis para a abertura: {len(candidates)}")

    segments = plan_opening_segments(opening_dur)
    log(
        f"Montando base da abertura ({opening_dur:.2f}s): {len(segments)} trechos "
        f"({sum(1 for s in segments if s['kind'] == 'broll')} B-roll TELA + "
        f"{sum(1 for s in segments if s['kind'] == 'relance')} relances de rosto)"
    )

    # Atribui fonte de B-roll distinta para cada corte de B-roll
    broll_idx = 0
    seg_sources: list[tuple[Path, float] | None] = []
    for seg in segments:
        if seg["kind"] == "broll" and candidates:
            seg_sources.append(candidates[broll_idx % len(candidates)])
            broll_idx += 1
        else:
            seg_sources.append(None)

    cpu_count = os.cpu_count() or 4
    threads_per_worker = max(1, cpu_count // max(1, workers))
    seg_files: list[Path] = [Path()] * len(segments)

    if workers <= 1:
        for i, seg in enumerate(segments):
            seg_files[i] = render_opening_segment(seg, seg_sources[i], video_cut, seg_dir, cpu_count)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(
                    render_opening_segment, seg, seg_sources[i], video_cut, seg_dir, threads_per_worker
                ): i
                for i, seg in enumerate(segments)
            }
            for fut in concurrent.futures.as_completed(futs):
                i = futs[fut]
                seg_files[i] = fut.result()

    concat_list = seg_dir / "opening_concat.txt"
    concat_list.write_text(
        "\n".join(f"file '{p.resolve()}'" for p in seg_files) + "\n",
        encoding="utf-8",
    )
    opening_base = work_dir / "opening_base.mp4"
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
            str(opening_base),
        ]
    )
    log(f"Base da abertura concluída: {opening_base} ({ffprobe_duration(opening_base):.3f}s)")
    return opening_base


# ==================== ASS, ZOOMS E COMPOSIÇÃO DE CHUNKS ====================


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
        text = re.sub(
            r"\\move\(\s*[^,]+,\s*[^,]+,\s*([^,]+),\s*([^,\)]+)(?:,[^\)]+)?\)",
            r"\\pos(\1,\2)",
            text,
        )
        text = re.sub(r"\\fsc[xy][0-9.]+", "", text)
        text = re.sub(r"\\t\([^\)]*\)", "", text)
        text = re.sub(r"\\fad\(\s*\d+\s*,\s*(\d+)\s*\)", r"\\fad(0,\1)", text)
    if clipped_end:
        text = re.sub(r"\\fad\(\s*(\d+)\s*,\s*\d+\s*\)", r"\\fad(\1,0)", text)
    return text


def slice_ass_for_window(ass_path: Path, out_ass: Path, win_start: float, win_end: float) -> int:
    """Gera um .ass deslocado para o intervalo [win_start, win_end] e retorna a contagem de eventos Dialogue."""
    lines = ass_path.read_text(encoding="utf-8").splitlines()
    out_lines: list[str] = []
    in_events = False
    dialogue_count = 0
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
            dialogue_count += 1
        else:
            out_lines.append(line)
    out_ass.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return dialogue_count


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


def render_opening_chunk(
    chunk_idx: int,
    opening_base: Path,
    abertura_overlay: Path | None,
    ass_path: Path,
    fonts_dir: Path | None,
    win_end: float,
    work_dir: Path,
    threads: int,
) -> Path:
    """Compõe o chunk da abertura (0:00 até opening_end): base B-roll/relances + overlay alfa do HTML."""
    chunk_dir = work_dir / f"chunk_{chunk_idx:03d}"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunk_ass = chunk_dir / "overlays.ass"
    dialogue_count = slice_ass_for_window(ass_path, chunk_ass, 0.0, win_end)

    inputs: list[str] = ["-i", str(opening_base)]
    filter_lines: list[str] = []

    if abertura_overlay is not None and abertura_overlay.exists():
        if abertura_overlay.is_dir():
            inputs.extend(["-framerate", str(FPS), "-i", str(abertura_overlay / "frame_%06d.png")])
        else:
            inputs.extend(["-i", str(abertura_overlay)])
        filter_lines.append(f"[1:v]fps={FPS},format=rgba[ov]")
        filter_lines.append("[0:v][ov]overlay=0:0:format=auto:eof_action=pass[vcomp]")
        cur_lbl = "[vcomp]"
    else:
        warn("Overlay da abertura (abertura.mov / PNGs) ausente; renderizando abertura só com B-roll + relances.")
        cur_lbl = "[0:v]"

    if dialogue_count > 0:
        ass_esc = escape_ffmpeg_path(chunk_ass)
        if fonts_dir and fonts_dir.exists():
            fonts_esc = escape_ffmpeg_path(fonts_dir)
            ass_filter = f"ass=filename='{ass_esc}':fontsdir='{fonts_esc}'"
        else:
            ass_filter = f"ass=filename='{ass_esc}'"
        filter_lines.append(f"{cur_lbl}{ass_filter},format=yuv420p[vout]")
    else:
        filter_lines.append(f"{cur_lbl}format=yuv420p[vout]")

    fc_file = chunk_dir / "vf.txt"
    fc_file.write_text(";\n".join(filter_lines), encoding="utf-8")

    out_chunk = chunk_dir / f"rendered_{chunk_idx:03d}.mp4"
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-v",
            "warning",
            *inputs,
            "-filter_complex_script",
            str(fc_file),
            "-map",
            "[vout]",
            "-t",
            f"{win_end:.6f}",
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
    dialogue_count = slice_ass_for_window(ass_path, chunk_ass, win_start, win_end)

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

    if dialogue_count > 0:
        ass_esc = escape_ffmpeg_path(chunk_ass)
        if fonts_dir and fonts_dir.exists():
            fonts_esc = escape_ffmpeg_path(fonts_dir)
            ass_filter = f"ass=filename='{ass_esc}':fontsdir='{fonts_esc}'"
        else:
            ass_filter = f"ass=filename='{ass_esc}'"
        filter_lines.append(f"[vzoom]{ass_filter},format=yuv420p[vout]")
    else:
        filter_lines.append("[vzoom]format=yuv420p[vout]")

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


# ==================== ÁUDIO & SFX ====================


def resolve_sfx_file(sfx_dir: Path, sound_name: str) -> Path | None:
    """Resolve arquivo de SFX em assets/sfx/ aceitando qualquer extensão de áudio comum e aliases."""
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
    elif sound_name in ("impact", "impact-bass"):
        candidates.extend(["impact-bass-1"])
    elif sound_name == "riser":
        candidates.extend(["riser-short", "riser_1"])

    exts = [".wav", ".mp3", ".ogg", ".flac", ".m4a"]
    files_by_stem = {p.stem.lower(): p for p in sfx_dir.iterdir() if p.is_file() and p.suffix.lower() in exts}
    for cand in candidates:
        if cand.lower() in files_by_stem:
            return files_by_stem[cand.lower()]
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
      3. SFX: posicionados com adelay, volume e trim opcional (usando asplit explícito por arquivo reutilizado)
      4. Mix final -> limiter de segurança -> AAC 192k 48kHz stereo
    """
    audio_cfg = plan.get("audio", {})
    hp_hz = audio_cfg.get("voice_highpass_hz", 80)
    target_i = audio_cfg.get("voice_lufs", -14.0)
    target_tp = audio_cfg.get("voice_true_peak_db", -1.5)
    music_db = audio_cfg.get("music_volume_db", -24.0)
    duck = audio_cfg.get("ducking", {})

    # Passo 1: Pré-processamento da voz
    voice_pre = work_dir / "voice_pre.wav"
    pre_chain = (
        f"afftdn=nr=14:nf=-44:tn=1,highpass=f={hp_hz},"
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

    # Passo 3: Renderiza faixa de SFX separada
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
            trim_s = ev.get("trim_s")
            extra = ""
            if trim_s is not None and float(trim_s) > 0.1:
                ts = float(trim_s)
                fo = min(0.12, ts * 0.25)
                extra = f"atrim=0:{ts:.4f},afade=t=out:st={max(0.0, ts - fo):.4f}:d={fo:.4f},"
            lbl = f"s{i}"
            sfx_filters.append(
                f"{src_lbl}aformat=sample_rates=48000:channel_layouts=stereo,"
                f"{extra}volume={gain_db:.1f}dB,adelay={delay_ms}|{delay_ms}[{lbl}]"
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


def choose_chunk_boundaries(
    final_dur: float,
    chunk_seconds: float,
    zooms: list[dict[str, Any]],
    opening_end: float,
) -> list[tuple[float, float]]:
    """Separa a abertura [0, opening_end] como chunk 0 e divide o restante evitando cortar no meio de zooms."""
    if opening_end >= final_dur - 0.5:
        return [(0.0, round(final_dur, 6))]

    boundaries: list[float] = [0.0, round(opening_end, 6)]
    t = opening_end + chunk_seconds
    while t < final_dur - 15.0:
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


def resolve_abertura_overlay(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit if explicit.exists() else None
    for cand in (
        REPO_ROOT / "work" / "abertura" / "abertura.mov",
        REPO_ROOT / "out" / "abertura.mov",
        REPO_ROOT / "abertura" / "abertura.mov",
    ):
        if cand.exists():
            return cand
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Renderiza o ep01 do canal IA Antony.")
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--preview", action="store_true", help="Renderiza de 0:00 a 2:10 do vídeo final.")
    mode_group.add_argument("--full", action="store_true", help="Renderiza o vídeo completo.")
    parser.add_argument("--repo", default=DEFAULT_GH_REPO, help="Repositório GitHub com a release de mídias.")
    parser.add_argument("--release", default=DEFAULT_RELEASE, help="Tag da release com as mídias.")
    parser.add_argument("--media-dir", type=Path, default=REPO_ROOT / "media")
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "out")
    parser.add_argument("--work-dir", type=Path, default=REPO_ROOT / "work")
    parser.add_argument("--fonts-dir", type=Path, default=REPO_ROOT / "assets" / "fonts")
    parser.add_argument("--sfx-dir", type=Path, default=REPO_ROOT / "assets" / "sfx")
    parser.add_argument(
        "--abertura-overlay",
        type=Path,
        default=None,
        help="Caminho para abertura.mov (qtrle com alfa) ou pasta de PNGs frame_000000.png.",
    )
    parser.add_argument("--chunk-seconds", type=float, default=120.0, help="Tamanho aproximado de cada pedaço de vídeo.")
    parser.add_argument("--workers", type=int, default=2, help="Quantidade de chunks de vídeo renderizados em paralelo.")
    parser.add_argument("--skip-download", action="store_true", help="Não baixa mídias da release; exige arquivos locais.")
    args = parser.parse_args()

    mode = "preview" if args.preview else "full"
    limit_s = PREVIEW_LIMIT_S if args.preview else None
    log(f"Iniciando build no modo --{mode} (limite={limit_s or 'completo'})")

    edl_path = REPO_ROOT / "edit" / "edl.json"
    plan_path = REPO_ROOT / "edit" / "plan.json"
    ass_path = REPO_ROOT / "edit" / "overlays.ass"

    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    # 1. Aplica recorte de preview (2:10 = 130s) se solicitado
    clips, final_dur = slice_clips_for_limit(edl["clips"], limit_s)
    used_parts = {c["part"] for c in clips}
    log(
        f"Clipes ativos: {len(clips)} (partes: {', '.join(sorted(used_parts))}) | "
        f"Duração final prevista: {final_dur:.2f}s ({final_dur/60:.2f} min)"
    )

    # 2. Garante mídias obrigatórias, p3_video.mp4 para B-roll da abertura e trilha opcional
    _, trilha_path = ensure_media(
        edl,
        used_parts,
        args.media_dir,
        args.repo,
        args.release,
        args.skip_download,
        need_broll_p3=True,
    )

    work_dir = args.work_dir / mode
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    # 3. Classifica automaticamente CAMERA_CHEIA vs TELA e salva out/scene_map.json
    parts_to_scan = set(used_parts)
    if "p3" in edl["sources"] and (args.media_dir / edl["sources"]["p3"]["video"]).exists():
        parts_to_scan.add("p3")
    scene_map, annotated_clips = build_scene_map(edl, clips, parts_to_scan, args.media_dir, args.out_dir)

    # 4. Corta voz (EDL original) e vídeo (com espelhamento automático por cena + crop global do Mac)
    voice_cut = cut_voice_edl(clips, edl, args.media_dir, work_dir)
    video_cut = cut_video_edl(annotated_clips, edl, scene_map, args.media_dir, work_dir)
    actual_voice_dur = ffprobe_duration(voice_cut)
    log(f"Voz cortada: {actual_voice_dur:.3f}s")

    # 5. Constrói vídeo base da abertura (0:00 até 1:51.0): B-roll da TELA + 3 relances do rosto espelhado
    opening_end = min(OPENING_END_S, actual_voice_dur)
    opening_base = build_opening_base_video(
        opening_end,
        scene_map,
        edl,
        args.media_dir,
        video_cut,
        work_dir,
        args.workers,
    )
    abertura_overlay = resolve_abertura_overlay(args.abertura_overlay)
    log(f"Overlay HTML da abertura: {abertura_overlay if abertura_overlay else 'NÃO ENCONTRADO'}")

    # 6. Divide em pedaços no tempo: chunk 0 = abertura [0, opening_end], chunks 1+ = vídeo espelhado + zooms + ASS
    zooms = [z for z in plan.get("zooms", []) if float(z["end"]) > opening_end]
    chunks = choose_chunk_boundaries(actual_voice_dur, args.chunk_seconds, zooms, opening_end)
    log(f"Renderizando {len(chunks)} pedaço(s) de vídeo (workers={args.workers})...")

    cpu_count = os.cpu_count() or 4
    threads_per_worker = max(1, cpu_count // max(1, min(args.workers, len(chunks))))
    fonts_dir = args.fonts_dir if args.fonts_dir.exists() else None

    def _run_chunk(idx: int, cs: float, ce: float, threads: int) -> Path:
        if idx == 0 and cs == 0.0 and ce <= opening_end + 0.05:
            return render_opening_chunk(
                idx, opening_base, abertura_overlay, ass_path, fonts_dir, ce, work_dir, threads
            )
        return render_video_chunk(idx, cs, ce, video_cut, zooms, ass_path, fonts_dir, work_dir, threads)

    rendered_chunks: list[Path] = [Path()] * len(chunks)
    if len(chunks) == 1 or args.workers <= 1:
        for idx, (cs, ce) in enumerate(chunks):
            rendered_chunks[idx] = _run_chunk(idx, cs, ce, cpu_count)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {
                pool.submit(_run_chunk, idx, cs, ce, threads_per_worker): idx
                for idx, (cs, ce) in enumerate(chunks)
            }
            for fut in concurrent.futures.as_completed(futs):
                idx = futs[fut]
                rendered_chunks[idx] = fut.result()

    # 7. Concatena os pedaços renderizados
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

    # 8. Masteriza e mixa voz + trilha opcional (-24 dB com ducking) + SFX
    audio_final = master_audio(
        voice_cut,
        trilha_path,
        plan,
        actual_voice_dur,
        args.sfx_dir,
        work_dir,
    )

    # 9. Mux final MP4 1080p30 h264 CRF 20 AAC 192k
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
