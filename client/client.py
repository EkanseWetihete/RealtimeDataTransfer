import asyncio
import websockets
import json
import os
import sys
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class ClientFileWatcher(FileSystemEventHandler):
    def __init__(self, client):
        self.client = client
        self.loop = None
    
    def set_loop(self, loop):
        self.loop = loop
    
    def on_created(self, event):
        if self.loop and self.loop.is_running():
            rel_path = os.path.relpath(event.src_path, self.client.data_dir)
            print(f"[WATCHER] Creation detected - Path: {rel_path}, Is Dir: {event.is_directory}")
            if event.is_directory:
                print(f"Directory created: {rel_path}")
                asyncio.run_coroutine_threadsafe(
                    self.client.handle_local_directory_created(rel_path), self.loop
                )
            else:
                asyncio.run_coroutine_threadsafe(
                    self.client.handle_local_file_created(rel_path), self.loop
                )
    
    def on_modified(self, event):
        if not event.is_directory and self.loop and self.loop.is_running():
            rel_path = os.path.relpath(event.src_path, self.client.data_dir)
            asyncio.run_coroutine_threadsafe(
                self.client.handle_local_file_modified(rel_path), self.loop
            )
    
    def on_moved(self, event):
        if self.loop and self.loop.is_running():
            src_rel_path = os.path.relpath(event.src_path, self.client.data_dir)
            dest_rel_path = os.path.relpath(event.dest_path, self.client.data_dir)
            print(f"[WATCHER] Move/Rename detected - From: {src_rel_path} To: {dest_rel_path}, Is Dir: {event.is_directory}")
            if event.is_directory:
                asyncio.run_coroutine_threadsafe(
                    self.client.handle_local_directory_renamed(src_rel_path, dest_rel_path), self.loop
                )
            else:
                asyncio.run_coroutine_threadsafe(
                    self.client.handle_local_file_renamed(src_rel_path, dest_rel_path), self.loop
                )
    
    def on_deleted(self, event):
        if self.loop and self.loop.is_running():
            rel_path = os.path.relpath(event.src_path, self.client.data_dir)
            print(f"[WATCHER] Deletion detected - Path: {rel_path}, Is Dir: {event.is_directory}")
            
            # Check if this is a directory by looking at tracked files
            is_actually_directory = False
            if not event.is_directory:
                # Check if any tracked files have this path as a parent directory
                rel_path_normalized = os.path.normpath(rel_path)
                for filename in self.client.file_hashes.keys():
                    filename_normalized = os.path.normpath(filename)
                    if filename_normalized.startswith(rel_path_normalized + os.sep):
                        is_actually_directory = True
                        print(f"[WATCHER] Detected as directory based on tracked files")
                        break
            
            if event.is_directory or is_actually_directory:
                print(f"Directory deleted: {rel_path}")
                asyncio.run_coroutine_threadsafe(
                    self.client.handle_local_directory_deletion(rel_path), self.loop
                )
            else:
                asyncio.run_coroutine_threadsafe(
                    self.client.handle_local_file_deletion(rel_path), self.loop
                )

class AssetClient:
    def __init__(self, data_dir: str = "data", host: str = "localhost", port: int = 8002):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)
        self.host = host
        self.port = port
        self.websocket = None
        self.running = False
        self.file_hashes = {}  # Track local file hashes to avoid unnecessary uploads
        self.syncing_files = set()  # Track files currently being synced to avoid loops
        self.upload_queue = asyncio.Queue()  # Queue for file uploads to prevent race conditions
        self.deletion_queue = asyncio.Queue()  # Queue for file deletions to prevent race conditions
        self.upload_lock = asyncio.Lock()  # Lock for upload operations
        self.deletion_lock = asyncio.Lock()  # Lock for deletion operations
        
        # Setup file watching
        self.observer = Observer()
        self.event_handler = ClientFileWatcher(self)
        self.observer.schedule(self.event_handler, str(self.data_dir), recursive=True)
        
        # Scan existing files
        self._scan_existing_files()
    
    def _scan_existing_files(self):
        """Scan existing files and compute their hashes"""
        for file_path in self.data_dir.rglob("*"):
            if file_path.is_file() and not self._is_ignored_file(file_path):
                rel_path = file_path.relative_to(self.data_dir)
                self.file_hashes[str(rel_path)] = self._compute_hash(file_path)
    
    def _compute_hash(self, file_path: Path) -> str:
        """Compute SHA256 hash of file"""
        import hashlib
        hash_sha256 = hashlib.sha256()
        try:
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_sha256.update(chunk)
            return hash_sha256.hexdigest()
        except:
            return ""
    
    def _is_ignored_file(self, file_path: Path) -> bool:
        """Check if file should be ignored based on extension"""
        ignored_extensions = {
            '.py', '.json', '.html', '.css', '.js', '.cs', '.ts', '.jsx', '.tsx',
            '.xml', '.yaml', '.yml', '.ini', '.cfg', '.config', '.gitignore',
            '.md', '.txt', '.log', '.bat', '.sh', '.cmd', '.ps1'
        }
        return file_path.suffix.lower() in ignored_extensions

    async def connect(self, retry_attempts: int = 3):
        """Connect to the asset server with retry logic"""
        uri = f"ws://{self.host}:{self.port}/ws"
        for attempt in range(retry_attempts):
            try:
                self.websocket = await websockets.connect(uri)
                self.running = True
                # Set the event loop for the file watcher
                self.event_handler.set_loop(asyncio.get_running_loop())
                self.observer.start()
                
                # Start deletion queue processor
                self.deletion_task = asyncio.create_task(self._process_deletion_queue())
                
                print(f"Connected to server at {uri}")
                
                # Send client file list for server sync
                await self._send_client_file_list()
                
                return True
            except Exception as e:
                print(f"Connection attempt {attempt + 1} failed: {e}")
                if attempt < retry_attempts - 1:
                    await asyncio.sleep(2)  # Wait 2 seconds before retry
                    
        print("Failed to connect after all retry attempts")
        return False
    
    async def reconnect(self):
        """Attempt to reconnect to the server"""
        uri = f"ws://{self.host}:{self.port}/ws"
        print("Attempting to reconnect...")
        self.running = False
        if self.websocket:
            try:
                await self.websocket.close()
            except:
                pass
        
        # Try to reconnect every 3 seconds
        while not self.running:
            try:
                self.websocket = await websockets.connect(uri)
                self.running = True
                self.event_handler.set_loop(asyncio.get_running_loop())
                
                # Start deletion queue processor
                self.deletion_task = asyncio.create_task(self._process_deletion_queue())
                
                print(f"Reconnected to server at {uri}")
                
                # Send client file list for server sync
                await self._send_client_file_list()
                return True
                
            except Exception as e:
                print(f"Reconnection failed: {e}. Retrying in 3 seconds...")
                await asyncio.sleep(3)
    
    async def disconnect(self):
        """Disconnect from server"""
        self.running = False
        
        # Cancel deletion task
        if hasattr(self, 'deletion_task') and not self.deletion_task.cancelled():
            self.deletion_task.cancel()
            try:
                await self.deletion_task
            except asyncio.CancelledError:
                pass
        
        if self.websocket:
            await self.websocket.close()
        self.observer.stop()
        self.observer.join()
    
    async def upload_file(self, filename: str, filepath: str = None):
        """Upload a file to the server with queue management"""
        if not self.websocket or not self.running:
            print("Not connected to server")
            return False
        
        async with self.upload_lock:  # Prevent concurrent uploads
            try:
                if filepath is None:
                    filepath = self.data_dir / filename
                
                with open(filepath, "rb") as f:
                    file_data = f.read()
                
                # Send upload command
                command = {
                    "type": "file_upload",
                    "filename": filename
                }
                await self.websocket.send(json.dumps(command))
                await asyncio.sleep(0.01)  # Small delay to ensure message order
                await self.websocket.send(file_data)
                
                print(f"Uploaded file: {filename} ({len(file_data)} bytes)")
                return True
                
            except websockets.exceptions.ConnectionClosed:
                print(f"Connection lost while uploading {filename}")
                asyncio.create_task(self._handle_connection_lost())
                return False
            except Exception as e:
                print(f"Error uploading file {filename}: {e}")
                if "no close frame received" in str(e) or "connection closed" in str(e):
                    asyncio.create_task(self._handle_connection_lost())
                return False
    
    async def _handle_connection_lost(self):
        """Handle lost connection"""
        was_running = self.running
        self.running = False
        print("Connection lost, attempting to reconnect...")
        await self.reconnect()
    
    async def delete_file(self, filename: str):
        """Queue file deletion request to server"""
        if not self.websocket or not self.running:
            print("Not connected to server")
            return False
        
        async with self.deletion_lock:  # Prevent concurrent deletions
            try:
                command = {
                    "type": "file_delete",
                    "filename": filename
                }
                await self.websocket.send(json.dumps(command))
                print(f"Requested deletion of: {filename}")
                return True
            except websockets.exceptions.ConnectionClosed:
                print(f"Connection lost while deleting {filename}")
                asyncio.create_task(self._handle_connection_lost())
                return False
            except Exception as e:
                print(f"Error deleting file {filename}: {e}")
                if "no close frame received" in str(e) or "connection closed" in str(e):
                    asyncio.create_task(self._handle_connection_lost())
                return False
    
    async def request_sync(self):
        """Request full sync from server"""
        if not self.websocket:
            print("Not connected to server")
            return False
        
        try:
            command = {"type": "request_sync"}
            await self.websocket.send(json.dumps(command))
            return True
        except Exception as e:
            print(f"Error requesting sync: {e}")
            return False
    
    async def _remove_from_syncing(self, filename: str):
        """Remove filename from syncing set after a delay"""
        await asyncio.sleep(1)  # Wait for file operations to complete
        self.syncing_files.discard(filename)

    async def _process_deletion_queue(self):
        """Process file and directory deletions from queue with rate limiting"""
        while self.running:
            try:
                # Get deletion from queue with timeout to allow graceful shutdown
                deletion_data = await asyncio.wait_for(self.deletion_queue.get(), timeout=1.0)
                
                deletion_type, path = deletion_data
                
                if deletion_type == "file":
                    await self._process_file_deletion(path)
                elif deletion_type == "directory":
                    await self._process_directory_deletion(path)
                
                # Mark task as done
                self.deletion_queue.task_done()
                
                # Small delay to prevent overwhelming the filesystem
                await asyncio.sleep(0.01)
                
            except asyncio.TimeoutError:
                # Continue loop, allows graceful shutdown check
                continue
            except Exception as e:
                print(f"Error processing deletion queue: {e}")
                await asyncio.sleep(0.1)  # Brief pause on error

    async def _process_file_deletion(self, filename: str):
        """Process individual file deletion"""
        file_path = self.data_dir / filename
        
        # Mark as syncing to avoid triggering file watcher
        self.syncing_files.add(filename)
        
        try:
            if file_path.exists():
                os.remove(file_path)
                print(f"Deleted file: {filename}")
            
            # Remove from local hash tracking
            if filename in self.file_hashes:
                del self.file_hashes[filename]
        
        finally:
            # Remove from syncing set after a delay
            asyncio.create_task(self._remove_from_syncing(filename))

    async def _process_directory_deletion(self, directory: str):
        """Process directory deletion recursively"""
        dir_path = self.data_dir / directory
        
        # Mark directory files as syncing and collect all files to delete
        files_in_dir = []
        if dir_path.exists() and dir_path.is_dir():
            for root, dirs, files in os.walk(dir_path):
                for file in files:
                    file_path = Path(root) / file
                    rel_path = str(file_path.relative_to(self.data_dir))
                    files_in_dir.append(rel_path)
                    self.syncing_files.add(rel_path)
        
        try:
            # Delete all files first
            for file_rel_path in files_in_dir:
                file_full_path = self.data_dir / file_rel_path
                try:
                    if file_full_path.exists() and not self._is_ignored_file(file_full_path):
                        os.remove(file_full_path)
                        print(f"Deleted file: {file_rel_path}")
                        # Remove from local hash tracking
                        if file_rel_path in self.file_hashes:
                            del self.file_hashes[file_rel_path]
                except Exception as e:
                    print(f"Error deleting file {file_rel_path}: {e}")
            
            # Now clean up empty directories
            if dir_path.exists() and dir_path.is_dir():
                await self._recursive_cleanup(dir_path)
                print(f"Cleaned up directory: {directory}")
        
        finally:
            # Remove files from syncing set
            for file_rel_path in files_in_dir:
                asyncio.create_task(self._remove_from_syncing(file_rel_path))

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
    
    async def _send_client_file_list(self):
        """Send client file list to server for bidirectional sync"""
        try:
            # Filter out ignored files
            filtered_hashes = {}
            for filename, file_hash in self.file_hashes.items():
                file_path = self.data_dir / filename
                if not self._is_ignored_file(file_path):
                    filtered_hashes[filename] = file_hash
            
            command = {
                "type": "client_file_list",
                "files": filtered_hashes
            }
            await self.websocket.send(json.dumps(command))
            print(f"Sent client file list for server sync: {len(filtered_hashes)} files")
        except Exception as e:
            print(f"Error sending client file list: {e}")

    async def handle_local_file_created(self, filename: str):
        """Handle new file created in local data directory"""
        if filename in self.syncing_files:
            return  # Skip files we're currently syncing from server
        
        file_path = self.data_dir / filename
        if not file_path.exists() or self._is_ignored_file(file_path):
            return
        
        new_hash = self._compute_hash(file_path)
        if filename not in self.file_hashes or self.file_hashes[filename] != new_hash:
            print(f"New file detected: {filename}")
            self.file_hashes[filename] = new_hash
            await asyncio.sleep(0.3)  # Longer delay to ensure file is fully written and prevent spam
            await self.upload_file(filename)
    
    async def handle_local_directory_created(self, directory: str):
        """Handle new directory created in local data directory"""
        if directory in self.syncing_files:
            return  # Skip directories we're currently syncing from server
        
        print(f"Directory created locally: {directory}")
        
        # Wait a bit for files to be copied into the directory
        await asyncio.sleep(0.5)
        
        # Check if directory still exists (might have been renamed quickly)
        dir_path = self.data_dir / directory
        if not dir_path.exists() or not dir_path.is_dir():
            print(f"Directory no longer exists: {directory}")
            return
        
        # Upload all files in the new directory
        has_files = False
        if dir_path.exists() and dir_path.is_dir():
            for root, dirs, files in os.walk(dir_path):
                for file in files:
                    file_path = Path(root) / file
                    if not self._is_ignored_file(file_path):
                        has_files = True
                        rel_path = str(file_path.relative_to(self.data_dir))
                        if rel_path not in self.syncing_files:
                            new_hash = self._compute_hash(file_path)
                            self.file_hashes[rel_path] = new_hash
                            print(f"New file in directory: {rel_path}")
                            await self.upload_file(rel_path)
        
        # If no files were found, send empty directory creation to server
        if not has_files:
            print(f"Empty directory created: {directory}")
            await self.create_directory(directory)
    
    async def create_directory(self, directory: str):
        """Send directory creation request to server"""
        if not self.websocket or not self.running:
            print("Not connected to server")
            return False
        
        try:
            command = {
                "type": "directory_create",
                "directory": directory
            }
            await self.websocket.send(json.dumps(command))
            print(f"Requested creation of directory: {directory}")
            return True
        except Exception as e:
            print(f"Error creating directory {directory}: {e}")
            return False
    
    async def handle_local_file_renamed(self, old_path: str, new_path: str):
        """Handle file rename"""
        print(f"File renamed: {old_path} -> {new_path}")
        
        # Delete old file from server
        if old_path in self.file_hashes:
            del self.file_hashes[old_path]
            await self.delete_file(old_path)
        
        # Upload new file to server
        await self.handle_local_file_created(new_path)
    
    async def handle_local_directory_renamed(self, old_path: str, new_path: str):
        """Handle directory rename"""
        print(f"Directory renamed: {old_path} -> {new_path}")
        
        # Normalize paths for comparison
        old_path_normalized = os.path.normpath(old_path)
        new_path_normalized = os.path.normpath(new_path)
        
        # Find all files in the old directory
        files_to_rename = []
        for filename in list(self.file_hashes.keys()):
            filename_normalized = os.path.normpath(filename)
            if (filename_normalized.startswith(old_path_normalized + os.sep) or 
                filename_normalized == old_path_normalized):
                files_to_rename.append(filename)
        
        # Delete old directory from server
        if files_to_rename:
            for filename in files_to_rename:
                del self.file_hashes[filename]
                await self.delete_file(filename)
            await self.delete_directory(old_path)
        else:
            # Empty directory rename
            await self.delete_directory(old_path)
        
        # Create new directory with files
        await self.handle_local_directory_created(new_path)
    
    async def handle_local_file_modified(self, filename: str):
        """Handle file modification in local data directory"""
        if filename in self.syncing_files:
            return  # Skip files we're currently syncing from server
        
        file_path = self.data_dir / filename
        if not file_path.exists() or self._is_ignored_file(file_path):
            return
        
        new_hash = self._compute_hash(file_path)
        if filename not in self.file_hashes or self.file_hashes[filename] != new_hash:
            print(f"File modified: {filename}")
            self.file_hashes[filename] = new_hash
            await asyncio.sleep(0.3)  # Longer delay to ensure file is fully written and prevent spam
            await self.upload_file(filename)
    
    async def handle_local_file_deletion(self, filename: str):
        """Handle local file deletion - notify server"""
        if filename in self.syncing_files:
            return  # Skip files we're currently syncing from server
        
        # Check if it's an ignored file
        file_path = self.data_dir / filename
        if self._is_ignored_file(file_path):
            return
        
        if filename in self.file_hashes:
            del self.file_hashes[filename]
            print(f"File deleted locally: {filename}")
            await self.delete_file(filename)

    async def handle_local_directory_deletion(self, directory: str):
        """Handle local directory deletion - notify server"""
        if directory in self.syncing_files:
            return  # Skip directories we're currently syncing from server
            
        print(f"Directory deleted locally: {directory}")
        
        # Normalize the directory path for comparison
        directory_normalized = os.path.normpath(directory)
        
        # Remove all files in this directory from local tracking
        files_to_remove = []
        for filename in list(self.file_hashes.keys()):
            filename_normalized = os.path.normpath(filename)
            # Check if the file is in the deleted directory
            if (filename_normalized.startswith(directory_normalized + os.sep) or 
                filename_normalized == directory_normalized):
                files_to_remove.append(filename)
        
        for filename in files_to_remove:
            del self.file_hashes[filename]
            print(f"Removed from local tracking: {filename}")
        
        # Notify server about directory deletion
        await self.delete_directory(directory)

    async def delete_directory(self, directory: str):
        """Queue directory deletion request to server"""
        if not self.websocket or not self.running:
            print("Not connected to server")
            return False
        
        async with self.deletion_lock:  # Prevent concurrent deletions
            try:
                command = {
                    "type": "directory_delete",
                    "directory": directory
                }
                await self.websocket.send(json.dumps(command))
                print(f"Requested deletion of directory: {directory}")
                return True
            except websockets.exceptions.ConnectionClosed:
                print(f"Connection lost while deleting directory {directory}")
                asyncio.create_task(self._handle_connection_lost())
                return False
            except Exception as e:
                print(f"Error deleting directory {directory}: {e}")
                if "no close frame received" in str(e) or "connection closed" in str(e):
                    asyncio.create_task(self._handle_connection_lost())
                return False
    
    async def listen(self):
        """Listen for messages from server"""
        if not self.websocket:
            return
        
        try:
            while self.running:
                # Receive text message (command)
                message_text = await self.websocket.recv()
                message = json.loads(message_text)
                
                if message["type"] == "initial_sync":
                    print(f"Receiving initial sync: {len(message['files'])} files")
                    
                elif message["type"] == "file_update":
                    # Next message is the file data
                    file_data = await self.websocket.recv()
                    filename = message["filename"]
                    server_hash = message["hash"]
                    
                    # Check if we need to update this file
                    file_path = self.data_dir / filename
                    current_hash = ""
                    if file_path.exists():
                        current_hash = self._compute_hash(file_path)
                    
                    # Only update if hash is different
                    if current_hash != server_hash:
                        # Mark as syncing to avoid triggering file watcher
                        self.syncing_files.add(filename)
                        
                        try:
                            # Save file to local data directory
                            file_path.parent.mkdir(parents=True, exist_ok=True)
                            
                            with open(file_path, "wb") as f:
                                f.write(file_data)
                            
                            # Update local hash
                            self.file_hashes[filename] = server_hash
                            print(f"Received file: {filename} ({len(file_data)} bytes)")
                        
                        finally:
                            # Remove from syncing set after a delay
                            asyncio.create_task(self._remove_from_syncing(filename))
                    
                elif message["type"] == "file_delete":
                    filename = message["filename"]
                    # Queue the deletion to process it smoothly
                    await self.deletion_queue.put(("file", filename))
                    
                elif message["type"] == "directory_delete":
                    directory = message["directory"]
                    # Queue the directory deletion to process it smoothly
                    await self.deletion_queue.put(("directory", directory))
                    
                elif message["type"] == "directory_create":
                    directory = message["directory"]
                    # Create the directory locally
                    dir_path = self.data_dir / directory
                    dir_path.mkdir(parents=True, exist_ok=True)
                    print(f"Created empty directory: {directory}")
                    
                elif message["type"] == "delete_response":
                    # Handle both file and directory delete responses
                    if "filename" in message:
                        filename = message["filename"]
                        success = message["success"]
                        print(f"Delete request for {filename}: {'success' if success else 'failed'}")
                    elif "directory" in message:
                        directory = message["directory"]
                        success = message["success"]
                        print(f"Delete request for directory {directory}: {'success' if success else 'failed'}")
        
        except websockets.exceptions.ConnectionClosed:
            print("Connection to server closed")
            # Don't call reconnect here, let the main loop handle it
        except Exception as e:
            print(f"Error in listen loop: {e}")
            # Don't call reconnect here, let the main loop handle it
        finally:
            # Mark as disconnected so main loop can reconnect
            if self.running:
                self.running = False
                print("Listen loop ended, will reconnect...")

async def test_client(host: str = "localhost", port: int = 8002):
    """Test the enhanced client functionality with auto-reconnect"""
    client = AssetClient(host=host, port=port)
    
    try:
        # Connect to server
        if not await client.connect():
            print("Initial connection failed, will retry automatically...")
            await client.reconnect()
        
        # Keep trying to listen and reconnect if needed
        while True:
            try:
                # Start listening
                listen_task = asyncio.create_task(client.listen())
                
                # Wait a moment for initial sync
                await asyncio.sleep(2)
                
                # Keep running
                print("Client running... Press Ctrl+C to stop")
                await listen_task
                
                # If we get here, the listen loop ended (connection lost)
                # Always try to reconnect unless explicitly stopped
                print("Listen loop ended, reconnecting...")
                await client.reconnect()
                    
            except KeyboardInterrupt:
                print("\nShutting down client...")
                client.running = False
                break
            except Exception as e:
                print(f"Client error: {e}")
                # Always try to reconnect on any error
                await asyncio.sleep(1)  # Brief pause before retry
                if not client.running:
                    client.running = True  # Reset flag to allow reconnection
                await client.reconnect()
        
    finally:
        await client.disconnect()

# Run the test
if __name__ == "__main__":
    host = "localhost"
    port = 8002
    
    if len(sys.argv) > 1:
        host = sys.argv[1]
    if len(sys.argv) > 2:
        port = int(sys.argv[2])
    
    print(f"Starting client with host={host}, port={port}")
    asyncio.run(test_client(host, port))