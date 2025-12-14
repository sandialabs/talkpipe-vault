#!/usr/bin/env python3
"""Quick test to verify watchdog glob pattern parsing"""
import os
import tempfile
import time
from pathlib import Path

# Create a test directory with some .txt files
with tempfile.TemporaryDirectory() as tmpdir:
    print(f"Test directory: {tmpdir}")

    # Create some test files
    for i in range(3):
        test_file = Path(tmpdir) / f"test{i}.txt"
        test_file.write_text(f"Test content {i}")
        print(f"Created: {test_file}")

    # Now test the watchdog with glob pattern
    from talkpipe_vault.watchdog import file_watcher

    glob_path = os.path.join(tmpdir, "*.txt")
    print(f"\nWatching with glob pattern: {glob_path}")
    print("Waiting for file events (will timeout after creating a new file)...")

    # Start watching in background
    import threading
    events_received = []

    def watch():
        try:
            for event in file_watcher(path=glob_path, max_events=1):
                events_received.append(event)
                print(f"Event received: {event}")
        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()

    watcher_thread = threading.Thread(target=watch, daemon=True)
    watcher_thread.start()

    # Wait a moment for watcher to initialize
    time.sleep(2)

    # Create a new file to trigger an event
    new_file = Path(tmpdir) / "new_test.txt"
    print(f"\nCreating new file: {new_file}")
    new_file.write_text("New test content")

    # Wait for event to be processed
    watcher_thread.join(timeout=5)

    if events_received:
        print(f"\nSuccess! Received {len(events_received)} event(s)")
        for event in events_received:
            print(f"  {event}")
    else:
        print("\nNo events received - check if pattern matching is working")
