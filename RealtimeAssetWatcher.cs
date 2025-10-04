#if UNITY_EDITOR
using System.IO;
using UnityEngine;
using UnityEditor;
using System;

[InitializeOnLoad]
public static class RealtimeAssetWatcher
{
    private static FileSystemWatcher watcher;
    // <--- THIS IS THE ONLY LINE YOU NEED TO CHANGE --->
    private const string WatchFolderName = "TransferredFiles"; 
    // <------------------------------------------------>
    private static string assetFolderPath = Path.Combine(Application.dataPath, WatchFolderName);
    
    // Static constructor runs when the Editor loads.
    static RealtimeAssetWatcher()
    {
        EditorApplication.update += Initialize;
    }

    private static void Initialize()
    {
        EditorApplication.update -= Initialize;
        InitializeWatcher();
    }

    private static void InitializeWatcher()
    {
        try
        {
            // Ensure the directory we are watching exists
            if (!Directory.Exists(assetFolderPath))
            {
                Directory.CreateDirectory(assetFolderPath);
                Debug.Log($"[Asset Watcher] Created directory: {assetFolderPath}");
            }
            
            // Cleanup existing watcher if any
            if (watcher != null)
            {
                watcher.EnableRaisingEvents = false;
                watcher.Dispose();
            }
            
            watcher = new FileSystemWatcher(assetFolderPath);
            watcher.IncludeSubdirectories = true;
            
            // Watch for all types of changes
            watcher.NotifyFilter = NotifyFilters.FileName | 
                                   NotifyFilters.DirectoryName | 
                                   NotifyFilters.LastWrite |
                                   NotifyFilters.Size;

            // Increase buffer size to prevent missing events
            watcher.InternalBufferSize = 65536;

            watcher.Created += OnAssetChanged;
            watcher.Deleted += OnAssetChanged;
            watcher.Renamed += OnAssetRenamed;
            watcher.Changed += OnAssetChanged;
            watcher.Error += OnWatcherError;

            watcher.EnableRaisingEvents = true;
            Debug.Log($"[Asset Watcher] ✓ Successfully monitoring: {assetFolderPath}");
            Debug.Log($"[Asset Watcher] ✓ IncludeSubdirectories: {watcher.IncludeSubdirectories}");
        }
        catch (Exception ex)
        {
            Debug.LogError($"[Asset Watcher] Failed to initialize: {ex.Message}\n{ex.StackTrace}");
        }
    }
    
    private static void OnWatcherError(object sender, ErrorEventArgs e)
    {
        Debug.LogError($"[Asset Watcher] ERROR: {e.GetException()}");
        // Try to reinitialize
        InitializeWatcher();
    }
    
    private static string GetRelativeAssetPath(string fullPath)
    {
        // Converts the full system path back to a Unity-style path starting with "Assets/..."
        string relativePath = "Assets" + fullPath.Replace(Application.dataPath, "").Replace('\\', '/');
        return relativePath;
    }

    private static void OnAssetChanged(object sender, FileSystemEventArgs e)
    {
        // Ignore meta files and temp files
        if (e.FullPath.EndsWith(".meta") || e.FullPath.Contains("~"))
        {
            return;
        }

        Debug.Log($"[Asset Watcher] 🔔 FileSystemWatcher triggered: {e.ChangeType} - {e.Name}");
        
        // Must dispatch the AssetDatabase call to the main Unity thread.
        EditorApplication.delayCall += () =>
        {
            try
            {
                string relativePath = GetRelativeAssetPath(e.FullPath);
                
                Debug.Log($"[Asset Watcher] 🔄 Attempting to import: {relativePath}");
                
                // Forces Unity to re-scan and import the file.
                AssetDatabase.ImportAsset(relativePath, ImportAssetOptions.ForceUpdate);
                AssetDatabase.Refresh(ImportAssetOptions.ForceUpdate);
                
                Debug.Log($"[Asset Watcher] ✓ Successfully refreshed: {e.ChangeType} - {relativePath}");
            }
            catch (Exception ex)
            {
                Debug.LogError($"[Asset Watcher] Failed to import asset: {ex.Message}");
            }
        };
    }

    private static void OnAssetRenamed(object sender, RenamedEventArgs e)
    {
        // Ignore meta files
        if (e.FullPath.EndsWith(".meta"))
        {
            return;
        }

        Debug.Log($"[Asset Watcher] 🔔 Rename detected: {e.OldName} → {e.Name}");
        
        EditorApplication.delayCall += () =>
        {
            try
            {
                string oldRelativePath = GetRelativeAssetPath(e.OldFullPath);
                string newRelativePath = GetRelativeAssetPath(e.FullPath);

                AssetDatabase.ImportAsset(oldRelativePath, ImportAssetOptions.ForceUpdate); 
                AssetDatabase.ImportAsset(newRelativePath, ImportAssetOptions.ForceUpdate);
                AssetDatabase.Refresh(ImportAssetOptions.ForceUpdate);

                Debug.Log($"[Asset Watcher] ✓ Successfully handled rename: {e.OldName} → {e.Name}");
            }
            catch (Exception ex)
            {
                Debug.LogError($"[Asset Watcher] Failed to handle rename: {ex.Message}");
            }
        };
    }
}
#endif