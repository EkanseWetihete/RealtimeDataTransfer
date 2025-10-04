# Real-Time File Sync - Client/Server Synchronization Tool

A real-time file synchronization system that keeps clients and servers in sync via WebSockets. Perfect for team collaboration, asset sharing, and multi-machine development workflows.

## Features

- **Real-time synchronization** between server and multiple clients
- **Automatic file watching** - detects creates, renames, and deletions
- **WebSocket-based** reliable communication with reconnection handling
- **Git-friendly** - ignores source code extensions (.cs, .py, .txt, etc.)
- **Simple deployment** - just run the batch files
- **Unity integration** support

## How It Works

1. **Server** hosts the central data repository
2. **Clients** connect and maintain synchronized copies
3. Any changes in the `Data` directory are automatically synced across all connected devices
4. Server acts as the single source of truth

## Quick Start

### Prerequisites
- Python 3.7+
- Required packages:
```pip install websockets>=12.0 watchdog>=3.0.0 multipart psutil fastapi>=0.104.1 uvicorn[standard]>=0.24.0```

## Basic Usage
1. Server: Run server.bat
2. Clients: Run client.bat
3. Place files in the Data directory to sync

## Configuration
- Edit .bat files to change IP/port settings
- Recommended: Use 0.0.0.0 for local network access
- For remote access: Port forwarding or ZeroTier required

## Unity Integration
1. Create Assets/TransferredFiles/ in your Unity project
2. Place RealtimeAssetWatcher.cs in an Editor folder (Create it doesn't exist)
3. Extract client files to TransferredFiles/
4. Restart Unity - look for [Asset Watcher] successfully refreshed in Console
5. Upload all client files into "/TransferedFiles" directory.
6. Run client - UnityEdition.py
Note: Unity integration is functional but considered experimental and may not fully work for development.

## ⚠️ Important Notes
1. Server is authoritative: Local files not on server will be deleted on sync
2. Always backup important files outside the Data directory
3. Designed for trusted networks - no built-in security
4. Tested with 2-4+ clients depending on network quality

## Use Cases
1. Team asset sharing - textures, configs, prefabs
2. Multi-machine development - keep desktop/laptop in sync
3. Small team collaboration - shared working files
4. Rapid prototyping - instant file sharing between team members
5. Source control availability.

## Technical Details
- Protocol: WebSockets
- Architecture: Client-server with queue system
- File handling: Monitors filesystem events
- Error handling: Automatic reconnection and error recovery
