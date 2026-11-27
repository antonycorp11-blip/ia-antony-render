#!/usr/bin/env python3
"""
Renderiza uma fatia da abertura HTML (repo/abertura/index.html) com Playwright + Chromium
em PNG transparente (omitBackground=True) chamando window.seek(t) de forma 100% determinística,
e empacota a fatia em um .mov lossless com canal alfa (qtrle / argb) pronto para concatenação
e overlay no scripts/build.py.

Exemplo (fatia 0 de 6):
  python3 scripts/render_abertura.py --slice 0 --total-slices 6 --duration 111.0 --fps 30 \
    --out out/abertura_slice_0.mov
"""

from __future__ import annotations

import argparse
import math
import shutil
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
WIDTH = 1920
HEIGHT = 1080


def log(msg: str) -> None:
    print(f"[abertura] {msg}", flush=True)


def run_cmd(cmd: list[str]) -> None:
    log("$ " + " ".join(str(c) for c in cmd))
    proc = subprocess.run(cmd, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"Comando falhou (exit {proc.returncode}): {' '.join(cmd)}")


def compute_frame_range(total_frames: int, slice_idx: int, total_slices: int) -> tuple[int, int]:
    if slice_idx < 0 or slice_idx >= total_slices:
        raise ValueError(f"slice={slice_idx} fora do intervalo [0, {total_slices - 1}]")
    base = total_frames // total_slices
    rem = total_frames % total_slices
    start = slice_idx * base + min(slice_idx, rem)
    count = base + (1 if slice_idx < rem else 0)
    return start, start + count


def main() -> None:
    parser = argparse.ArgumentParser(description="Renderiza abertura HTML com alfa via Playwright.")
    parser.add_argument("--html", type=Path, default=REPO_ROOT / "abertura" / "index.html")
    parser.add_argument("--duration", type=float, default=111.0, help="Duração da abertura em segundos.")
    parser.add_argument("--fps", type=int, default=30, help="Quadros por segundo.")
    parser.add_argument("--slice", type=int, default=0, help="Índice desta fatia (0-based).")
    parser.add_argument("--total-slices", type=int, default=1, help="Quantidade total de fatias paralelas.")
    parser.add_argument("--out", type=Path, required=True, help="Arquivo .mov qtrle de saída.")
    parser.add_argument("--frames-dir", type=Path, default=None, help="Diretório temporário de PNGs.")
    parser.add_argument("--keep-pngs", action="store_true", help="Mantém os PNGs após gerar o .mov.")
    args = parser.parse_args()

    html_path = args.html.resolve()
    if not html_path.exists():
        raise SystemExit(f"ERRO: arquivo HTML não encontrado: {html_path}")

    total_frames = int(round(args.duration * args.fps))
    start_f, end_f = compute_frame_range(total_frames, args.slice, args.total_slices)
    count = end_f - start_f

    frames_dir = args.frames_dir or (REPO_ROOT / "work" / f"abertura_frames_s{args.slice}")
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)

    log(
        f"Fatia {args.slice + 1}/{args.total_slices}: quadros [{start_f}, {end_f}) "
        f"({count} quadros @ {args.fps}fps = {count / args.fps:.2f}s)"
    )

    t0 = time.monotonic()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--allow-file-access-from-files",
                "--disable-web-security",
                "--font-render-hinting=none",
                "--disable-gpu-sandbox",
                "--no-sandbox",
            ],
        )
        context = browser.new_context(
            viewport={"width": WIDTH, "height": HEIGHT},
            device_scale_factor=1,
        )
        page = context.new_page()
        page.goto(html_path.as_uri(), wait_until="load")

        # Garante que todas as fontes locais (@font-face) terminaram de carregar
        loaded_fonts = page.evaluate(
            """async () => {
                await document.fonts.ready;
                return Array.from(document.fonts)
                    .filter(f => f.status === 'loaded')
                    .map(f => `${f.family}:${f.weight}`);
            }"""
        )
        log(f"Fontes carregadas no Chromium: {sorted(set(loaded_fonts))}")

        has_seek = page.evaluate("() => typeof window.seek === 'function'")
        if not has_seek:
            raise RuntimeError("window.seek(t) não encontrado em abertura/index.html")

        for idx, frame_num in enumerate(range(start_f, end_f)):
            t = frame_num / float(args.fps)
            page.evaluate("(t) => window.seek(t)", t)
            png_path = frames_dir / f"frame_{frame_num:06d}.png"
            page.screenshot(
                path=str(png_path),
                type="png",
                omit_background=True,
                animations="disabled",
            )
            if (idx + 1) % 100 == 0 or (idx + 1) == count:
                elapsed = time.monotonic() - t0
                fps_eff = (idx + 1) / max(0.001, elapsed)
                log(f"  progresso fatia {args.slice}: {idx + 1}/{count} ({fps_eff:.1f} quadros/s)")

        browser.close()

    # Empacota a sequência PNG transparente em .mov lossless RGBA (qtrle / argb)
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-v",
            "warning",
            "-framerate",
            str(args.fps),
            "-start_number",
            str(start_f),
            "-i",
            str(frames_dir / "frame_%06d.png"),
            "-frames:v",
            str(count),
            "-c:v",
            "qtrle",
            "-pix_fmt",
            "argb",
            "-r",
            str(args.fps),
            "-video_track_timescale",
            "30000",
            str(args.out),
        ]
    )

    if not args.keep_pngs:
        shutil.rmtree(frames_dir, ignore_errors=True)

    size_mb = args.out.stat().st_size / (1024 * 1024)
    total_elapsed = time.monotonic() - t0
    log(f"Fatia {args.slice} concluída: {args.out} ({size_mb:.1f} MB em {total_elapsed:.1f}s)")


if __name__ == "__main__":
    main()
