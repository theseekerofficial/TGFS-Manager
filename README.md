# TGFS-Manager

An unofficial TGFS Manager Bot for [TGFS](https://github.com/TheodoreKrypton/tgfs)

## Screenshots

### Start Command
<img src="https://i.ibb.co/Sb39fKg/image-2025-09-03-132636924.png" alt="Start Command" width="400">

### File Upload/Forward
<img src="https://i.ibb.co/mV6YWVT2/image-2025-09-03-133615185.png" alt="File Upload or Forward" width="400">

### Browse Command
<img src="https://i.ibb.co/B59z01fn/image-2025-09-03-133049039.png" alt="Browse Command - File List" width="400">
<img src="https://i.ibb.co/k26qDZWr/image-2025-09-03-133231261.png" alt="Browse Command - Options" width="400">

### Index Channel Command
<img src="https://i.ibb.co/zWTtSGdM/image-2025-09-03-133717820.png" alt="Index Channel - Start" width="400">
<img src="https://i.ibb.co/TxK3gpNk/image-2025-09-03-133829956.png" alt="Index Channel - Processing" width="400">
<img src="https://i.ibb.co/Kx8jbsxd/image-2025-09-03-133953505.png" alt="Index Channel - Progress" width="400">
<img src="https://i.ibb.co/DPLdT1VF/image-2025-09-03-134056366.png" alt="Index Channel - Status" width="400">
<img src="https://i.ibb.co/LLSP48q/image-2025-09-03-134136866.png" alt="Index Channel - Complete" width="400">
<img src="https://i.ibb.co/MkY1vs5s/image-2025-09-03-134305283.png" alt="Index Channel - Final" width="400">

## Deployment Instructions

### Docker Deployment (Recommended)

#### Build the Docker Image
```bash
docker build -t tgfs-manager .
```

#### Run the Container
```bash
docker run -d \
  --name tgfs-manager \
  --restart unless-stopped \
  tgfs-manager
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