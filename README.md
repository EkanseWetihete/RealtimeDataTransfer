# Hello ladies and gentleman!

I have created a realtime data transfering between all clients and a server!
This script was made specifically to use for Unity collaboration. However, you can also use it on any other project or whatever else you can think of. It is not difficult to use, just click on "client.bat" or "server.bat" and it will be connected.
Read down below for more information how to download or use it.

## How does it work:
1. Anything in the data directory sends data through websockets to server and from server to all clients.
2. It only saves into "Data" directory in the same location as python script.
3. Server will 100% be syncronising with your files so, if you upload files into "data" while you are not connected, it will delete them afterwards if you connect to the server to syncronize with the server. (which means, you should save file backups outside just in case)
4. You will potentially need portforward or zerotier localhost servers for it to work. (recommended to use 0.0.0.0)
5. To change IP or port, you can always edit .bat files.

## To install python, you basically need:
1. go to microsoft store
2. download latest version you see of python.
3. open cmd (or powershell recommended since cmd for some reason doesnt show installation progress for me)
4. type ``pip install websockets>=12.0 watchdog>=3.0.0 multipart psutil``
5. Done.
  
## Basically what will be needed:
1. Create Assets -> TransferredFiles
2. Upload a file Assets -> TutorialInfo -> Scripts -> Editor -> RealtimeAssetWatcher.cs (Potentially as long as the parent directory is named "Editor" it should work so we dont need "tutorial" one but didnt test myself.)
3. Fully restart Unity. (When you open it, there should in the Console say "[Asset Watcher] succesfully refreshed")
4. Extract Client.rar (all client directory files) into TransferredFiles Unity directory.
5. Inside unity you can click on "client - UnityEdition.py" outside of it. both, should technically work but the regular client.bat works only without admin privileges, idk how will it work on win11 if you have it tho but i guess we will see.
