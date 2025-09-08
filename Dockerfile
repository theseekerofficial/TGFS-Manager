FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
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
COPY bot.py .
COPY settings.env .

# Copy rclone.conf if available (optional)
COPY rclone.conf /app/rclone.conf 2>/dev/null || echo "rclone.conf not found, skipping copy"

# Run the bot
CMD ["python3", "bot.py"]