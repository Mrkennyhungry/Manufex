# Manufex OmniParser service — GPU image.
# Build:  docker build -t manufex/omniparser .
# Run:    docker run --gpus all -p 8077:8077 -e OMNIPARSER_TOKEN=<secret> manufex/omniparser
#
# For a CPU-only deployment use the pip instructions in README.md instead.

FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/models

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.10 python3-pip python3.10-dev libgl1 libglib2.0-0 curl \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.10 /usr/local/bin/python

WORKDIR /srv

COPY requirements.txt .
# Torch with CUDA 12.1 support
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cu121
RUN pip install --no-cache-dir -r requirements.txt transformers>=4.44

COPY app.py .

# Pre-download model weights at build time (~1GB, cached in the image).
# Set HF_TOKEN in the build environment if the repo ever requires auth.
RUN python -c "from huggingface_hub import snapshot_download; \
    snapshot_download('microsoft/OmniParser-v2.0', \
    allow_patterns=['icon_detect/*','icon_caption_florence/*'])"

EXPOSE 8077
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8077"]
