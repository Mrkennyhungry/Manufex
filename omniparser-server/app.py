"""Manufex OmniParser service — self-hosted vision parsing for the desktop agent.

Exposes the contract that Manufex's `ioa_analyze` expects:

  POST /parse         multipart file=<jpeg>            → {"elements": [...]}
  POST /caption_icons multipart file=<jpeg>, boxes=json → {"ok": true, "captions": [...]}
  GET  /health                                          → {"ok": true, "device": ...}

Pipeline (no dependency on the internal parser_service):
  - icon detection : YOLO (weights: microsoft/OmniParser-v2.0/icon_detect, auto-downloaded)
  - text detection : RapidOCR (ONNX, models bundled with the package)
  - icon captioning: Florence-2 (optional, ENABLE_CAPTION=1, GPU recommended)

Auth: set OMNIPARSER_TOKEN to require a matching `X-Auth-Token` header on every
request. Leave it unset for a trusted/local deployment (no auth).

Run:
  pip install -r requirements.txt
  uvicorn app:app --host 0.0.0.0 --port 8077

Then in Manufex: Settings → Vision service →
  URL:   http://<this-host>:8077
  Token: the value of OMNIPARSER_TOKEN (or anything, if unset)
"""
from __future__ import annotations

import io
import json
import logging
import os
from functools import lru_cache

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("omniparser")

TOKEN = os.environ.get("OMNIPARSER_TOKEN", "").strip()
ENABLE_CAPTION = os.environ.get("ENABLE_CAPTION", "0") == "1"
WEIGHTS_DIR = os.environ.get("OMNIPARSER_WEIGHTS_DIR", "")  # optional local weights

app = FastAPI(title="Manufex OmniParser service", version="1.0.0")


# ── auth ──────────────────────────────────────────────────────────────────────
@app.middleware("http")
async def _auth(request: Request, call_next):
    if TOKEN and request.url.path != "/health":
        if request.headers.get("X-Auth-Token", "") != TOKEN:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


# ── models (lazy, loaded once) ────────────────────────────────────────────────
class Models:
    yolo = None          # ultralytics YOLO — icon/bbox detection
    ocr = None           # RapidOCR — text detection + recognition
    florence = None      # (optional) Florence-2 — icon captioning
    florence_proc = None
    device = "cpu"


def _download_weights() -> str:
    """Fetch microsoft/OmniParser-v2.0/icon_detect from HuggingFace (cached)."""
    from huggingface_hub import snapshot_download
    local = WEIGHTS_DIR or snapshot_download("microsoft/OmniParser-v2.0", allow_patterns=["icon_detect/*"])
    d = os.path.join(local, "icon_detect")
    return d if os.path.isdir(d) else local


def load_models() -> None:
    import torch
    Models.device = "cuda" if torch.cuda.is_available() else "cpu"
    log.info("device: %s", Models.device)

    from ultralytics import YOLO
    det_dir = _download_weights()
    det_pt = os.path.join(det_dir, "model.pt")
    if not os.path.isfile(det_pt):
        det_pt = os.path.join(det_dir, "model.safetensors")
    log.info("loading YOLO icon detector from %s", det_pt)
    Models.yolo = YOLO(det_pt)

    from rapidocr_onnxruntime import RapidOCR
    Models.ocr = RapidOCR()
    log.info("RapidOCR ready")

    if ENABLE_CAPTION:
        try:
            import torch as _t
            from transformers import AutoModelForCausalLM, AutoProcessor
            from huggingface_hub import snapshot_download
            cap_dir = snapshot_download("microsoft/OmniParser-v2.0", allow_patterns=["icon_caption_florence/*"])
            cap_dir = os.path.join(cap_dir, "icon_caption_florence")
            Models.florence_proc = AutoProcessor.from_pretrained(cap_dir, trust_remote_code=True)
            Models.florence = AutoModelForCausalLM.from_pretrained(
                cap_dir, trust_remote_code=True, torch_dtype=_t.float16
            ).to(Models.device).eval()
            log.info("Florence-2 captioner ready (%s)", Models.device)
        except Exception as exc:  # caption is optional — degrade to 404
            log.warning("caption disabled (%s)", exc)
            Models.florence = None
    else:
        log.info("caption disabled (set ENABLE_CAPTION=1 to enable, GPU recommended)")


@app.on_event("startup")
def _startup() -> None:
    load_models()


# ── helpers ───────────────────────────────────────────────────────────────────
def _read_jpeg(data: bytes) -> np.ndarray:
    import cv2
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(status_code=400, detail="invalid image")
    return img


def _run_yolo(img: np.ndarray) -> list[dict]:
    result = Models.yolo.predict(img, conf=0.05, imgsz=640, verbose=False)[0]
    h, w = img.shape[:2]
    boxes = []
    for b in result.boxes:
        x1, y1, x2, y2 = (float(v) for v in b.xyxy[0].tolist())
        boxes.append({
            "type": "icon",
            "text": "",
            "content": "",
            "bbox": [max(x1 / w, 0), max(y1 / h, 0), min(x2 / w, 1), min(y2 / h, 1)],
            "normalized": True,
        })
    return boxes


def _run_ocr(img: np.ndarray) -> list[dict]:
    h, w = img.shape[:2]
    out, _ = Models.ocr(img)
    boxes = []
    for item in (out or []):
        box, text, score = item
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        boxes.append({
            "type": "text",
            "text": str(text).strip(),
            "content": str(text).strip(),
            "bbox": [min(xs) / w, min(ys) / h, max(xs) / w, max(ys) / h],
            "normalized": True,
            "score": float(score),
        })
    return boxes


def _merge(elements: list[dict]) -> list[dict]:
    """Drop text boxes fully inside an icon box (icon captions win)."""
    icons = [e for e in elements if e["type"] == "icon"]
    kept = []
    for e in elements:
        if e["type"] == "text":
            x1, y1, x2, y2 = e["bbox"]
            inside = any(
                i["bbox"][0] <= x1 and i["bbox"][1] <= y1
                and i["bbox"][2] >= x2 and i["bbox"][3] >= y2
                for i in icons
            )
            if inside:
                continue
        kept.append(e)
    return kept


def _caption_crops(img: np.ndarray, boxes01: list[dict]) -> dict[int, str]:
    if Models.florence is None:
        return {}
    import torch
    from PIL import Image
    h, w = img.shape[:2]
    captions: dict[int, str] = {}
    for b in boxes01:
        x1, y1, x2, y2 = b["bbox"]
        crop = img[max(int(y1 * h) - 10, 0):min(int(y2 * h) + 10, h),
                   max(int(x1 * w) - 10, 0):min(int(x2 * w) + 10, w)]
        if crop.size == 0:
            continue
        pil = Image.fromarray(crop[:, :, ::-1])
        inputs = Models.florence_proc(
            text="<MORE_DETAILED_CAPTION>", images=pil, return_tensors="pt"
        ).to(Models.device)
        with torch.no_grad():
            ids = Models.florence.generate(
                input_ids=inputs["input_ids"],
                pixel_values=inputs["pixel_values"],
                max_new_tokens=40, num_beams=1, do_sample=False,
            )
        text = Models.florence_proc.batch_decode(ids, skip_special_tokens=True)[0].strip()
        captions[int(b["id"])] = text[:120]
    return captions


# ── endpoints ─────────────────────────────────────────────────────────────────
@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "manufex-omniparser",
        "device": Models.device,
        "caption": Models.florence is not None,
        "auth_required": bool(TOKEN),
    }


@app.post("/parse")
async def parse(file: UploadFile = File(...), engine: str = Form("omni")) -> dict:
    if Models.yolo is None or Models.ocr is None:
        raise HTTPException(status_code=503, detail="models still loading")
    img = _read_jpeg(await file.read())
    elements = _merge(_run_yolo(img) + _run_ocr(img))
    return {"elements": elements, "normalized": True, "engine": engine}


@app.post("/caption_icons")
async def caption_icons(file: UploadFile = File(...), boxes: str = Form(...)) -> dict:
    if Models.florence is None:
        # Manufex treats 404 as "caption not deployed" and degrades gracefully.
        raise HTTPException(status_code=404, detail="caption not enabled on this deployment")
    try:
        box_list = json.loads(boxes)
    except ValueError:
        raise HTTPException(status_code=400, detail="boxes must be a JSON array")
    img = _read_jpeg(await file.read())
    captions = _caption_crops(img, box_list)
    return {
        "ok": True,
        "captions": [{"id": k, "content": v, "cached": False} for k, v in captions.items()],
    }
