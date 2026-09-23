"""Export YOLO .pt model to ONNX format.

Usage:
    python scripts/export_yolo_onnx.py weights/icon_detect/model.pt

Output: model.onnx in the same directory as model.pt.
Requires ultralytics (install temporarily for export only).
"""
import sys
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <model.pt>")
        sys.exit(1)

    pt_path = Path(sys.argv[1]).resolve()
    if not pt_path.exists():
        print(f"Error: {pt_path} not found")
        sys.exit(1)

    try:
        from ultralytics import YOLO
    except ImportError:
        print("Error: ultralytics not installed. Run: pip install ultralytics")
        sys.exit(1)

    model = YOLO(str(pt_path))
    onnx_path = pt_path.parent / "model.onnx"

    print(f"Exporting {pt_path} -> {onnx_path}")
    model.export(
        format="onnx",
        imgsz=640,
        opset=17,
        simplify=True,
        dynamic=False,
    )

    # ultralytics exports to model.onnx in the working directory or next to .pt
    exported = pt_path.with_suffix(".onnx")
    if exported.exists() and exported != onnx_path:
        exported.rename(onnx_path)

    print(f"Done: {onnx_path}")
    print(f"Size: {onnx_path.stat().st_size / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    main()
