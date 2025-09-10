FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies including aria2
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    unzip \
    aria2 \
    && rm -rf /var/lib/apt/lists/*

# Install rclone using official install script
RUN curl https://rclone.org/install.sh | bash

# Verify rclone installation
RUN rclone version

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY *.py .
COPY settings.env .

# Copy rclone.conf
COPY rclone.conf /app/rclone.conf

# Create downloads directory
RUN mkdir -p /app/downloads

# Run both aria2 RPC and bot together
CMD ["bash", "-c", "aria2c --enable-rpc --rpc-listen-all=true --rpc-allow-origin-all --disable-ipv6 --rpc-secret='Kv4crmwrfbfVGxrsHqKk9APBjWRv101MSZjgKcyBqCRUkNfrpRn8d1ZSFaEFV15B' --rpc-listen-port=6800 --quiet=true & python3 bot.py"]