FROM python:3.10-slim

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    wget \
    tar \
    ca-certificates \
    libicu-dev \
    libssl-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install high-performance binary N_m3u8DL-RE
RUN wget https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v0.6.0-beta/N_m3u8DL-RE_v0.6.0-beta_linux-x64_20260629.tar.gz -O re.tar.gz \
    && tar -xvf re.tar.gz \
    && mv N_m3u8DL-RE /usr/local/bin/ \
    && chmod +x /usr/local/bin/N_m3u8DL-RE \
    && rm -rf re.tar.gz

ENV DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 \
    TERM=xterm-256color \
    PYTHONUNBUFFERED=1

# Fail the build if the video downloader can't run
RUN N_m3u8DL-RE --version

WORKDIR /app

# Install Python modules
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy source repository
COPY . .

# Run application
CMD ["python", "allen2.py"]
