# TGFS-Manager

An unofficial TGFS Manager Bot for [TGFS](https://github.com/TheodoreKrypton/tgfs)

## Screenshots

### Start Command
<img src="https://i.imgur.com/OTEwzRx.png" alt="Start Command" width="400">

### File Upload/Forward
<img src="https://i.imgur.com/9lM29vd.png" alt="File Upload or Forward" width="400">

### Browse Command
<img src="https://i.imgur.com/LlNL5jf.png" alt="Browse Command - File List" width="400">
<img src="https://i.imgur.com/K1dtgBe.png" alt="Browse Command - Options" width="400">

### Index Channel Command
<img src="https://i.imgur.com/BX334BW.png" alt="Index Channel - Start" width="400">
<img src="https://i.imgur.com/MYXhFPA.png" alt="Index Channel - Processing" width="400">
<img src="https://i.imgur.com/iqWz56C.png" alt="Index Channel - Progress" width="400">
<img src="https://i.imgur.com/EKbLHye.png" alt="Index Channel - Status" width="400">
<img src="https://i.imgur.com/lnvmuIs.png" alt="Index Channel - Complete" width="400">
<img src="https://i.imgur.com/cksjS6H.png" alt="Index Channel - Final" width="400">

## Features
* Easily Import Files to TGFS
* Browse TGFS Files and Manage
* Index Entire Telegram Channels to TGFS with multiple bots
* Rclone Crypt Support
* Direct Download Link Upload to TGFS
* And many many more!

## Deployment Instructions

### Docker Deployment (Recommended)

#### Build the Docker Image
```bash
docker build -t tgfs-manager .
```

#### Run the Container
```bash
docker run \
  --name tgfs-manager \
  --restart unless-stopped \
  tgfs-manager
```

### Docker Compose Deployment (To Deploy TGFS Manager with wireguard VPN)

> Place your wg0.conf (wireguard conf from your vpn provider) in /wg/wg_confs/ folder as wg0.conf

> Now Run

```bash
docker compose up
```

### Direct Python Deployment

#### Prerequisites
- Python 3.8 or higher
- Git (optional)

#### Step 1: Setup Environment
```bash
# Create and navigate to project directory
mkdir tgfs-manager && cd tgfs-manager

# Create virtual environment (recommended)
python3 -m venv venv

# Activate virtual environment
# Linux/macOS:
source venv/bin/activate
# Windows:
# venv\Scripts\activate
```

#### Step 2: Install Dependencies
```bash
pip install -r requirements.txt
```

#### Step 3: Configure Settings
```bash
# Copy example configuration
cp settings.env.example settings.env

# Edit configuration file
nano settings.env  # Use your preferred editor
```

#### Step 4: Run the Bot
```bash
# Run directly
python3 bot.py

# Run with screen (detachable session) for 24/7 online bot
screen -S tgfs-manager python3 bot.py
# Detach: Ctrl+A then D
# Reattach: screen -r tgfs-manager
```