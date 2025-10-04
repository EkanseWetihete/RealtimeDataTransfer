from fastapi import FastAPI, WebSocket
from fastapi.websockets import WebSocketDisconnect
import json
import asyncio
import os
import hashlib
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from contextlib import asynccontextmanager

# Global manager variable for lifespan context
manager = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan"""
    global manager
    # Startup
    manager = AssetManager()
    yield
    # Shutdown
    if manager:
        manager.cleanup()

app = FastAPI(lifespan=lifespan)

class FileWatchHandler(FileSystemEventHandler):
    def __init__(self, manager):
        self.manager = manager
        self.loop = None
        
    def set_loop(self, loop):
        """Set the event loop for cross-thread async operations"""
        self.loop = loop
        
    def on_moved(self, event):
        if self.loop and self.loop.is_running():
            src_rel_path = os.path.relpath(event.src_path, self.manager.data_dir)
            dest_rel_path = os.path.relpath(event.dest_path, self.manager.data_dir)
            print(f"[SERVER WATCHER] Move/Rename detected - From: {src_rel_path} To: {dest_rel_path}, Is Dir: {event.is_directory}")
            if event.is_directory:
                asyncio.run_coroutine_threadsafe(
                    self.manager.handle_directory_renamed(src_rel_path, dest_rel_path), 
                    self.loop
                )
            else:
                asyncio.run_coroutine_threadsafe(
                    self.manager.handle_file_renamed(src_rel_path, dest_rel_path), 
                    self.loop
                )
        
    def on_deleted(self, event):
        if self.loop and self.loop.is_running():
            rel_path = os.path.relpath(event.src_path, self.manager.data_dir)
            print(f"[SERVER WATCHER] Deletion detected - Path: {rel_path}, Is Dir: {event.is_directory}")
            
            # Check if this is a directory by looking at tracked files
            is_actually_directory = False
            if not event.is_directory:
                # Check if any tracked files have this path as a parent directory
                rel_path_normalized = os.path.normpath(rel_path)
                for filename in self.manager.file_hashes.keys():
                    filename_normalized = os.path.normpath(filename)
                    if filename_normalized.startswith(rel_path_normalized + os.sep):
                        is_actually_directory = True
                        print(f"[SERVER WATCHER] Detected as directory based on tracked files")
                        break
            
            if event.is_directory or is_actually_directory:
                asyncio.run_coroutine_threadsafe(
                    self.manager.handle_directory_deletion(rel_path), 
                    self.loop
                )
            else:
                asyncio.run_coroutine_threadsafe(
                    self.manager.handle_file_deletion(rel_path), 
                    self.loop
                )

class AssetManager:
    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)
        self.file_hashes: Dict[str, str] = {}
        self.active_connections: List[WebSocket] = []
        
        # Script file extensions to ignore
        self.ignored_extensions = {
            '.py', '.json', '.html', '.css', '.js', '.cs', '.ts', '.jsx', '.tsx',
            '.xml', '.yaml', '.yml', '.ini', '.cfg', '.config', '.gitignore',
            '.md', '.txt', '.log', '.bat', '.sh', '.cmd', '.ps1'
        }
        
        # File processing queues
        self.upload_queue = None
        self.broadcast_queue = None
        self.deletion_queue = None
        self.processing_uploads = False
        self.processing_broadcasts = False
        self.processing_deletions = False
        self.queue_tasks = []
        
        # Setup file watching
        self.observer = Observer()
        self.event_handler = FileWatchHandler(self)
        self.observer.schedule(self.event_handler, str(self.data_dir), recursive=True)
        self.observer.start()
        
        # Load existing files
        self._scan_existing_files()

    def _scan_existing_files(self):
        """Scan existing files and compute their hashes"""
        for file_path in self.data_dir.rglob("*"):
            if file_path.is_file() and not self._is_ignored_file(file_path):
                rel_path = file_path.relative_to(self.data_dir)
                self.file_hashes[str(rel_path)] = self._compute_hash(file_path)
    
    def _is_ignored_file(self, file_path: Path) -> bool:
        """Check if file should be ignored based on extension"""
        return file_path.suffix.lower() in self.ignored_extensions

    def _compute_hash(self, file_path: Path) -> str:
        """Compute SHA256 hash of file"""
        hash_sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_sha256.update(chunk)
        return hash_sha256.hexdigest()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        print(f"Client connected. Total connections: {len(self.active_connections)}")
        
        # Initialize queues and start processors if this is the first connection
        if self.upload_queue is None:
            await self._start_queue_processors()
        
        # Send initial sync data to new client
        await self._send_initial_sync(websocket)

    async def _start_queue_processors(self):
        """Start the queue processing tasks"""
        if self.upload_queue is None:
            self.upload_queue = asyncio.Queue()
            self.broadcast_queue = asyncio.Queue()
            self.deletion_queue = asyncio.Queue()
            
            # Set the event loop for the file watcher
            current_loop = asyncio.get_running_loop()
            self.event_handler.set_loop(current_loop)
            
            # Start queue processors
            upload_task = asyncio.create_task(self._process_upload_queue())
            broadcast_task = asyncio.create_task(self._process_broadcast_queue())
            deletion_task = asyncio.create_task(self._process_deletion_queue())
            
            self.queue_tasks = [upload_task, broadcast_task, deletion_task]
            print("Started queue processors")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            print(f"Client disconnected. Total connections: {len(self.active_connections)}")
    
    def is_connection_active(self, websocket: WebSocket) -> bool:
        """Check if WebSocket connection is still active"""
        try:
            # Check if the connection is in our active list and not closed
            return (websocket in self.active_connections and 
                    hasattr(websocket, 'client_state') and 
                    websocket.client_state.name != 'DISCONNECTED')
        except:
            return False

    async def _send_initial_sync(self, websocket: WebSocket):
        """Send all existing files to newly connected client"""
        try:
            # Send file list first
            file_list = list(self.file_hashes.keys())
            await websocket.send_text(json.dumps({
                "type": "initial_sync",
                "files": file_list
            }))
            
            # Send each file
            for rel_path in file_list:
                full_path = self.data_dir / rel_path
                if full_path.exists():
                    with open(full_path, "rb") as f:
                        file_data = f.read()
                    
                    message = {
                        "type": "file_update",
                        "filename": rel_path,
                        "hash": self.file_hashes[rel_path],
                        "size": len(file_data)
                    }
                    await websocket.send_text(json.dumps(message))
                    await websocket.send_bytes(file_data)
        except Exception as e:
            print(f"Error sending initial sync: {e}")

    async def handle_client_sync(self, websocket: WebSocket, client_files: Dict[str, str]):
        """Handle client sync - tell client to delete files server doesn't have"""
        try:
            files_to_delete = []
            directories_to_check = set()
            
            # Collect files to delete and directories that might need cleanup
            for filename in client_files.keys():
                if self._is_ignored_file(Path(filename)):
                    continue  # Skip script files
                    
                if filename not in self.file_hashes:
                    # Server doesn't have this file, tell client to delete it
                    files_to_delete.append(filename)
                    # Track directories for potential cleanup
                    parent_dir = str(Path(filename).parent)
                    if parent_dir != '.':
                        directories_to_check.add(parent_dir)
            
            # Send file deletion commands
            for filename in files_to_delete:
                await websocket.send_text(json.dumps({
                    "type": "file_delete",
                    "filename": filename
                }))
                print(f"Instructing client to delete: {filename}")
            
            # Check for empty directories that should be deleted
            directories_to_delete = []
            for directory in directories_to_check:
                should_delete_dir = True
                
                # Check if server has any files in this directory
                for server_file in self.file_hashes.keys():
                    if server_file.startswith(directory + "/") or server_file.startswith(directory + "\\"):
                        should_delete_dir = False
                        break
                
                if should_delete_dir:
                    directories_to_delete.append(directory)
            
            # Send directory deletion commands
            for directory in directories_to_delete:
                await websocket.send_text(json.dumps({
                    "type": "directory_delete",
                    "directory": directory
                }))
                print(f"Instructing client to delete directory: {directory}")
            
            print(f"Completed server-authoritative sync with client")
        except Exception as e:
            print(f"Error in client sync: {e}")

    async def save_file(self, filename: str, data: bytes, sender_ws: WebSocket = None):
        """Queue file for processing"""
        try:
            # Check if file should be ignored
            file_path = self.data_dir / filename
            if self._is_ignored_file(file_path):
                print(f"Ignoring script file: {filename}")
                return False
            
            # Ensure queue is initialized
            if self.upload_queue is None:
                await self._start_queue_processors()
            
            # Add to upload queue for processing
            await self.upload_queue.put((filename, data, sender_ws))
            print(f"Queued file for upload: {filename} ({len(data)} bytes)")
            
            return True
        except Exception as e:
            print(f"Error queueing file {filename}: {e}")
            return False

    async def _save_file_internal(self, filename: str, data: bytes, sender_ws: WebSocket = None):
        """Internal method to actually save file and queue broadcast"""
        try:
            file_path = self.data_dir / filename
            
            # Ensure directory exists
            file_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Write file
            with open(file_path, "wb") as f:
                f.write(data)
            
            # Update hash
            new_hash = self._compute_hash(file_path)
            self.file_hashes[filename] = new_hash
            
            print(f"Saved file: {filename} ({len(data)} bytes)")
            
            # Queue broadcast to all clients except sender
            await self.broadcast_queue.put(("file_update", filename, data, new_hash, sender_ws))
            
        except Exception as e:
            print(f"Error saving file {filename}: {e}")

    async def _delete_file_internal(self, filename: str, sender_ws: WebSocket = None):
        """Internal method to actually delete file and queue broadcast"""
        try:
            file_path = self.data_dir / filename
            
            # Remove from file tracking first
            if filename in self.file_hashes:
                del self.file_hashes[filename]
            
            # Delete the actual file if it exists
            if file_path.exists():
                os.remove(file_path)
            
            print(f"File deleted: {filename}")
            
            # Queue broadcast deletion to all clients except sender
            await self.broadcast_queue.put(("file_delete", filename, None, None, sender_ws))
            
        except Exception as e:
            print(f"Error deleting file {filename}: {e}")

    async def _delete_directory_internal(self, directory: str, sender_ws: WebSocket = None):
        """Internal method to recursively delete directory and queue broadcast"""
        try:
            import shutil
            dir_path = self.data_dir / directory
            
            if dir_path.exists() and dir_path.is_dir():
                # Collect all files in directory before deletion
                files_to_delete = []
                for root, dirs, files in os.walk(dir_path):
                    for file in files:
                        file_path = Path(root) / file
                        rel_path = str(file_path.relative_to(self.data_dir))
                        if not self._is_ignored_file(file_path):
                            files_to_delete.append(rel_path)
                
                # Delete all files first
                for file_rel_path in files_to_delete:
                    file_full_path = self.data_dir / file_rel_path
                    try:
                        if file_full_path.exists():
                            os.remove(file_full_path)
                            print(f"Deleted file: {file_rel_path}")
                            # Remove from hash tracking
                            if file_rel_path in self.file_hashes:
                                del self.file_hashes[file_rel_path]
                    except Exception as e:
                        print(f"Error deleting file {file_rel_path}: {e}")
                
                # Now clean up empty directories
                await self._recursive_cleanup(dir_path)
                print(f"Cleaned up directory: {directory}")
                
                # Queue broadcast deletion to all clients except sender
                await self.broadcast_queue.put(("directory_delete", directory, None, None, sender_ws))
            
        except Exception as e:
            print(f"Error deleting directory {directory}: {e}")

    async def _recursive_cleanup(self, dir_path: Path):
        """Recursively clean up directory, removing non-ignored files and empty directories"""
        if not dir_path.exists() or not dir_path.is_dir():
            return
            
        try:
            # Process all items in the directory
            for item in dir_path.iterdir():
                if item.is_file():
                    if not self._is_ignored_file(item):
                        # Remove non-ignored files
                        rel_path = item.relative_to(self.data_dir)
                        if str(rel_path) in self.file_hashes:
                            del self.file_hashes[str(rel_path)]
                        item.unlink()
                        print(f"Deleted file: {rel_path}")
                elif item.is_dir():
                    # Recursively process subdirectory
                    await self._recursive_cleanup(item)
            
            # Try to remove the directory if it's empty (will fail if it contains ignored files)
            try:
                if dir_path.exists():
                    dir_path.rmdir()  # Only removes if empty
                    rel_dir = dir_path.relative_to(self.data_dir)
                    print(f"Removed empty directory: {rel_dir}")
            except OSError:
                # Directory not empty (contains ignored files or subdirs with ignored files)
                pass
                
        except Exception as e:
            print(f"Error in recursive cleanup of {dir_path}: {e}")

    async def _broadcast_file_update_internal(self, filename: str, data: bytes, file_hash: str, sender_ws: WebSocket = None):
        """Internal method to broadcast file update to all clients except sender"""
        message = {
            "type": "file_update",
            "filename": filename,
            "hash": file_hash,
            "size": len(data)
        }
        
        disconnected = []
        for connection in self.active_connections[:]:  # Copy list to avoid modification during iteration
            if connection == sender_ws:
                continue
                
            try:
                # Check if connection is still active before sending
                if hasattr(connection, 'client_state') and connection.client_state.name == 'DISCONNECTED':
                    disconnected.append(connection)
                    continue
                    
                await connection.send_text(json.dumps(message))
                await connection.send_bytes(data)
                print(f"Broadcasted file to client: {filename}")
            except Exception as e:
                print(f"Error broadcasting to client: {e}")
                disconnected.append(connection)
        
        # Clean up disconnected clients
        for conn in disconnected:
            self.disconnect(conn)

    async def handle_file_deletion(self, filename: str):
        """Handle file deletion and queue broadcast to clients"""
        if filename in self.file_hashes:
            del self.file_hashes[filename]
            print(f"File deleted by file watcher: {filename}")
            
            # Ensure queue is initialized before using it
            if self.broadcast_queue is not None:
                # Queue broadcast deletion to all clients (no sender for file watcher events)
                await self.broadcast_queue.put(("file_delete", filename, None, None, None))

    async def handle_directory_deletion(self, directory: str):
        """Handle directory deletion and queue broadcast to clients"""
        print(f"Directory deleted by file watcher: {directory}")
        
        # Normalize the directory path for comparison
        directory_normalized = os.path.normpath(directory)
        
        # Remove all files in this directory from tracking
        files_to_remove = []
        for filename in list(self.file_hashes.keys()):
            filename_normalized = os.path.normpath(filename)
            # Check if the file is in the deleted directory
            if (filename_normalized.startswith(directory_normalized + os.sep) or 
                filename_normalized == directory_normalized):
                files_to_remove.append(filename)
        
        for filename in files_to_remove:
            del self.file_hashes[filename]
            print(f"Removed from tracking: {filename}")
        
        # Ensure queue is initialized before using it
        if self.broadcast_queue is not None:
            # Queue broadcast directory deletion to all clients (no sender for file watcher events)
            await self.broadcast_queue.put(("directory_delete", directory, None, None, None))
    
    async def handle_file_renamed(self, old_path: str, new_path: str):
        """Handle file rename"""
        print(f"File renamed by file watcher: {old_path} -> {new_path}")
        
        # Delete old file
        if old_path in self.file_hashes:
            await self.handle_file_deletion(old_path)
        
        # Add new file
        new_file_path = self.data_dir / new_path
        if new_file_path.exists() and not self._is_ignored_file(new_file_path):
            new_hash = self._compute_hash(new_file_path)
            self.file_hashes[new_path] = new_hash
            
            # Read and broadcast new file
            with open(new_file_path, "rb") as f:
                file_data = f.read()
            
            if self.broadcast_queue is not None:
                await self.broadcast_queue.put(("file_update", new_path, file_data, new_hash, None))
    
    async def handle_directory_renamed(self, old_path: str, new_path: str):
        """Handle directory rename"""
        print(f"Directory renamed by file watcher: {old_path} -> {new_path}")
        
        # Normalize paths
        old_path_normalized = os.path.normpath(old_path)
        new_path_normalized = os.path.normpath(new_path)
        
        # Find all files in old directory and rename them
        files_to_rename = {}
        for filename in list(self.file_hashes.keys()):
            filename_normalized = os.path.normpath(filename)
            if filename_normalized.startswith(old_path_normalized + os.sep):
                # Calculate new filename
                relative_part = filename_normalized[len(old_path_normalized) + len(os.sep):]
                new_filename = os.path.join(new_path, relative_part)
                files_to_rename[filename] = new_filename
        
        # Delete old directory
        await self.handle_directory_deletion(old_path)
        
        # Create new directory and add files
        new_dir_path = self.data_dir / new_path
        if new_dir_path.exists() and new_dir_path.is_dir():
            # Scan for files in new directory
            for root, dirs, files in os.walk(new_dir_path):
                for file in files:
                    file_path = Path(root) / file
                    if not self._is_ignored_file(file_path):
                        rel_path = str(file_path.relative_to(self.data_dir))
                        new_hash = self._compute_hash(file_path)
                        self.file_hashes[rel_path] = new_hash
                        
                        # Broadcast file
                        with open(file_path, "rb") as f:
                            file_data = f.read()
                        
                        if self.broadcast_queue is not None:
                            await self.broadcast_queue.put(("file_update", rel_path, file_data, new_hash, None))
            
            # If no files, broadcast empty directory creation
            if not any(new_dir_path.rglob("*")):
                if self.broadcast_queue is not None:
                    await self.broadcast_queue.put(("directory_create", new_path, None, None, None))

    async def delete_file(self, filename: str, sender_ws: WebSocket = None):
        """Queue file deletion"""
        try:
            # Check if file should be ignored
            file_path = self.data_dir / filename
            if self._is_ignored_file(file_path):
                print(f"Ignoring script file deletion: {filename}")
                return False
            
            # Ensure queue is initialized
            if self.deletion_queue is None:
                await self._start_queue_processors()
            
            # Add to deletion queue for processing
            await self.deletion_queue.put(("file", filename, sender_ws))
            print(f"Queued file for deletion: {filename}")
            
            return True
        except Exception as e:
            print(f"Error queueing file deletion {filename}: {e}")
            return False

    async def delete_directory(self, directory: str, sender_ws: WebSocket = None):
        """Queue directory deletion"""
        try:
            # Ensure queue is initialized
            if self.deletion_queue is None:
                await self._start_queue_processors()
            
            # Add to deletion queue for processing
            await self.deletion_queue.put(("directory", directory, sender_ws))
            print(f"Queued directory for deletion: {directory}")
            
            return True
        except Exception as e:
            print(f"Error queueing directory deletion {directory}: {e}")
            return False

    async def _broadcast_file_deletion_internal(self, filename: str, sender_ws: WebSocket = None):
        """Internal method to broadcast file deletion to all clients except sender"""
        message = {
            "type": "file_delete",
            "filename": filename
        }
        
        disconnected = []
        for connection in self.active_connections[:]:  # Copy list to avoid modification during iteration
            if connection == sender_ws:
                continue  # Don't send back to the client who requested deletion
                
            try:
                # Check if connection is still active before sending
                if hasattr(connection, 'client_state') and connection.client_state.name == 'DISCONNECTED':
                    disconnected.append(connection)
                    continue
                    
                await connection.send_text(json.dumps(message))
                print(f"Broadcasted deletion to client: {filename}")
            except Exception as e:
                print(f"Error broadcasting deletion to client: {e}")
                disconnected.append(connection)
        
        # Clean up disconnected clients
        for conn in disconnected:
            self.disconnect(conn)

    async def _broadcast_directory_deletion_internal(self, directory: str, sender_ws: WebSocket = None):
        """Internal method to broadcast directory deletion to all clients except sender"""
        message = {
            "type": "directory_delete",
            "directory": directory
        }
        
        disconnected = []
        for connection in self.active_connections[:]:  # Copy list to avoid modification during iteration
            if connection == sender_ws:
                continue  # Don't send back to the client who requested deletion
                
            try:
                # Check if connection is still active before sending
                if hasattr(connection, 'client_state') and connection.client_state.name == 'DISCONNECTED':
                    disconnected.append(connection)
                    continue
                    
                await connection.send_text(json.dumps(message))
                print(f"Broadcasted directory deletion to client: {directory}")
            except Exception as e:
                print(f"Error broadcasting directory deletion to client: {e}")
                disconnected.append(connection)
        
        # Clean up disconnected clients
        for conn in disconnected:
            self.disconnect(conn)
    
    async def _broadcast_directory_creation_internal(self, directory: str, sender_ws: WebSocket = None):
        """Internal method to broadcast directory creation to all clients except sender"""
        message = {
            "type": "directory_create",
            "directory": directory
        }
        
        disconnected = []
        for connection in self.active_connections[:]:
            if connection == sender_ws:
                continue
                
            try:
                if hasattr(connection, 'client_state') and connection.client_state.name == 'DISCONNECTED':
                    disconnected.append(connection)
                    continue
                    
                await connection.send_text(json.dumps(message))
                print(f"Broadcasted directory creation to client: {directory}")
            except Exception as e:
                print(f"Error broadcasting directory creation to client: {e}")
                disconnected.append(connection)
        
        for conn in disconnected:
            self.disconnect(conn)

    def get_file_list(self) -> Dict[str, str]:
        """Get list of all files with their hashes"""
        return self.file_hashes.copy()

    async def _process_upload_queue(self):
        """Process file uploads from queue sequentially"""
        self.processing_uploads = True
        while self.processing_uploads:
            try:
                # Get upload task from queue
                upload_data = await self.upload_queue.get()
                if upload_data is None:  # Shutdown signal
                    break
                    
                filename, data, sender_ws = upload_data
                
                # Process the file upload
                await self._save_file_internal(filename, data, sender_ws)
                
                # Mark task as done
                self.upload_queue.task_done()
                
            except Exception as e:
                print(f"Error processing upload queue: {e}")

    async def _process_broadcast_queue(self):
        """Process file broadcasts from queue sequentially"""
        self.processing_broadcasts = True
        while self.processing_broadcasts:
            try:
                # Get broadcast task from queue
                broadcast_data = await self.broadcast_queue.get()
                if broadcast_data is None:  # Shutdown signal
                    break
                    
                message_type, filename, data, file_hash, sender_ws = broadcast_data
                
                if message_type == "file_update":
                    await self._broadcast_file_update_internal(filename, data, file_hash, sender_ws)
                elif message_type == "file_delete":
                    await self._broadcast_file_deletion_internal(filename, sender_ws)
                elif message_type == "directory_delete":
                    await self._broadcast_directory_deletion_internal(filename, sender_ws)
                elif message_type == "directory_create":
                    await self._broadcast_directory_creation_internal(filename, sender_ws)
                
                # Mark task as done
                self.broadcast_queue.task_done()
                
            except Exception as e:
                print(f"Error processing broadcast queue: {e}")

    async def _process_deletion_queue(self):
        """Process file and directory deletions from queue sequentially"""
        self.processing_deletions = True
        while self.processing_deletions:
            try:
                # Get deletion task from queue
                deletion_data = await self.deletion_queue.get()
                if deletion_data is None:  # Shutdown signal
                    break
                    
                deletion_type, path, sender_ws = deletion_data
                
                if deletion_type == "file":
                    # Process file deletion
                    await self._delete_file_internal(path, sender_ws)
                elif deletion_type == "directory":
                    # Process directory deletion
                    await self._delete_directory_internal(path, sender_ws)
                
                # Mark task as done
                self.deletion_queue.task_done()
                
            except Exception as e:
                print(f"Error processing deletion queue: {e}")

    def cleanup(self):
        """Cleanup resources"""
        # Stop queue processing
        self.processing_uploads = False
        self.processing_broadcasts = False
        self.processing_deletions = False
        
        # Send shutdown signals to queues
        if self.upload_queue is not None:
            asyncio.create_task(self.upload_queue.put(None))
        if self.broadcast_queue is not None:
            asyncio.create_task(self.broadcast_queue.put(None))
        if self.deletion_queue is not None:
            asyncio.create_task(self.deletion_queue.put(None))
        
        # Cancel queue tasks
        for task in self.queue_tasks:
            if not task.cancelled():
                task.cancel()
        
        # Stop file observer
        self.observer.stop()
        self.observer.join()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Try to receive text message first (commands)
            try:
                text_data = await websocket.receive_text()
                command = json.loads(text_data)
                
                if command["type"] == "file_upload":
                    # Next message should be the file data
                    try:
                        file_data = await websocket.receive_bytes()
                        filename = command["filename"]
                        await manager.save_file(filename, file_data, websocket)
                    except Exception as e:
                        print(f"Error receiving file data for {command.get('filename', 'unknown')}: {e}")
                        continue
                    
                elif command["type"] == "file_delete":
                    filename = command["filename"]
                    success = await manager.delete_file(filename, websocket)
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "delete_response",
                            "filename": filename,
                            "success": success
                        }))
                    except Exception as e:
                        print(f"Error sending delete response: {e}")
                        
                elif command["type"] == "directory_delete":
                    directory = command["directory"]
                    success = await manager.delete_directory(directory, websocket)
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "delete_response",
                            "directory": directory,
                            "success": success
                        }))
                    except Exception as e:
                        print(f"Error sending directory delete response: {e}")
                        
                elif command["type"] == "directory_create":
                    directory = command["directory"]
                    # Create directory on server
                    dir_path = manager.data_dir / directory
                    dir_path.mkdir(parents=True, exist_ok=True)
                    print(f"Created empty directory: {directory}")
                    
                    # Broadcast to other clients
                    if manager.broadcast_queue is not None:
                        await manager.broadcast_queue.put(("directory_create", directory, None, None, websocket))
                    
                elif command["type"] == "request_sync":
                    await manager._send_initial_sync(websocket)
                
                elif command["type"] == "client_file_list":
                    # Client is sending their file list for server-authoritative sync
                    client_files = command["files"]
                    await manager.handle_client_sync(websocket, client_files)
                    
            except json.JSONDecodeError as e:
                print(f"JSON decode error: {e}")
                continue
            except WebSocketDisconnect:
                print("Client disconnected during message processing")
                break
            except Exception as e:
                print(f"Error processing command: {e}")
                continue
                
    except WebSocketDisconnect:
        print("Client disconnected")
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        manager.disconnect(websocket)

if __name__ == "__main__":
    import uvicorn
    import sys
    
    host = "0.0.0.0"
    port = 8002
    
    if len(sys.argv) > 1:
        host = sys.argv[1]
    if len(sys.argv) > 2:
        port = int(sys.argv[2])
    
    print(f"Starting server on {host}:{port}")
    uvicorn.run(app, host=host, port=port)