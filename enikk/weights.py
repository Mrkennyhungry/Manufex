"""Weights management for bundled and user directories."""
import logging
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def get_bundle_weights_dir() -> Path | None:
    """Get weights directory from bundled package.

    Returns None if no bundled weights found.
    """
    # PyInstaller frozen application
    if getattr(sys, 'frozen', False):
        # In frozen mode, data files are in sys._MEIPASS
        bundle_dir = Path(sys._MEIPASS) / 'weights'  # type: ignore[attr-defined]
        if bundle_dir.exists():
            return bundle_dir
    # Development mode: weights in package directory
    else:
        dev_dir = Path(__file__).parent.parent / 'weights'
        if dev_dir.exists():
            return dev_dir
    return None


def ensure_weights_ready(user_weights_dir: Path) -> None:
    """Ensure weights are available in user directory.

    If weights not found in user directory, copy from bundled package.

    Args:
        user_weights_dir: Target directory for weights (e.g., %LOCALAPPDATA%/Enikk/weights)
    """
    icon_detect_path = user_weights_dir / 'icon_detect' / 'model.onnx'
    rapidocr_det_path = user_weights_dir / 'rapidocr' / 'ch_PP-OCRv4_det_infer.onnx'

    # Weights already exist in user directory
    if icon_detect_path.exists() and rapidocr_det_path.exists():
        logger.debug(f"Weights already exist at {user_weights_dir}")
        return

    # Get bundled weights
    bundle_weights = get_bundle_weights_dir()
    if not bundle_weights:
        logger.warning("No bundled weights found, weights_dir must be configured manually")
        return

    # Copy weights from bundle to user directory
    try:
        logger.info(f"Copying weights from {bundle_weights} to {user_weights_dir}")
        user_weights_dir.mkdir(parents=True, exist_ok=True)

        # Copy only the necessary ONNX model files (not training artifacts)
        files_to_copy = [
            ('icon_detect', ['model.onnx']),
            ('rapidocr', ['ch_PP-OCRv4_det_infer.onnx', 'ch_PP-OCRv4_rec_infer.onnx', 'ch_ppocr_mobile_v2.0_cls_infer.onnx']),
        ]

        for subdir, files in files_to_copy:
            src_dir = bundle_weights / subdir
            dst_dir = user_weights_dir / subdir
            dst_dir.mkdir(exist_ok=True)

            for filename in files:
                src_file = src_dir / filename
                dst_file = dst_dir / filename
                if src_file.exists() and not dst_file.exists():
                    shutil.copy2(src_file, dst_file)
                    logger.debug(f"Copied {subdir}/{filename}")
                elif not src_file.exists():
                    logger.warning(f"Missing bundled weight: {subdir}/{filename}")

    except Exception as e:
        logger.error(f"Failed to copy weights: {e}")
