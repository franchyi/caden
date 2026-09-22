from pathlib import Path
from datetime import datetime


def get_safe_name(image_name: str) -> str:
    """Standardized function for output name from image name"""
    return image_name.split("/")[-1].replace(":", "_")


def prepare_output_dir(image_name: str, prefix: str) -> Path:
    """Create output directory for this run."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = get_safe_name(image_name)
    project_root = Path(__file__).resolve().parents[2]
    output_dir = project_root / "experiments-results" / prefix / f"{safe_name}_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir
