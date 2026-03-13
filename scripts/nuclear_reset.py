import os
import shutil
import json
from pathlib import Path

def nuclear_reset():
    # Navigate to project root (parent of scripts/)
    project_root = Path(__file__).parent.parent
    print(f"=== HASHI NUCLEAR RESET ===")
    print(f"Target: {project_root}")
    print("-" * 30)

    # 1. Files to delete
    files_to_remove = [
        "agents.json",
        "onboarding_state.json",
        ".bridge_u_last_agents.txt",
        ".bridge_u_lang.txt",
        ".bridge_u_f.pid",
        ".bridge_u_f.lock",
        "scheduler_state.json",
        ".verbose",
        ".think"
    ]


    for f_name in files_to_remove:
        f_path = project_root / f_name
        if f_path.exists():
            try:
                f_path.unlink()
                print(f"✓ Deleted: {f_name}")
            except Exception as e:
                print(f"✗ Failed to delete {f_name}: {e}")

    # 2. Reset secrets.json to clean state
    secrets_path = project_root / "secrets.json"
    try:
        with open(secrets_path, 'w', encoding='utf-8') as f:
            json.dump({}, f)
        print("✓ Reset: secrets.json (emptied)")
    except Exception as e:
        print(f"✗ Failed to reset secrets.json: {e}")

    # 3. Clear all workspaces (The "History" wipe)
    workspaces_dir = project_root / "workspaces"
    if workspaces_dir.exists():
        try:
            # Delete all subdirectories and files in workspaces
            for item in workspaces_dir.iterdir():
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            print("✓ Cleared: All agent workspaces and chat histories")
        except Exception as e:
            print(f"✗ Error clearing workspaces: {e}")
    else:
        workspaces_dir.mkdir(parents=True, exist_ok=True)
        print("• Workspaces directory created")

    # 4. Clear logs, media, state, and transport sessions (Keep agent_seeds safe!)
    for folder in ["logs", "media", "state", "wa_session"]:

        path = project_root / folder
        if path.exists():
            try:
                shutil.rmtree(path)
                # Re-create these essential directories as empty
                if folder != "wa_session":
                    path.mkdir(parents=True, exist_ok=True)
                print(f"✓ Cleared: {folder}/")
            except Exception as e:
                print(f"✗ Error clearing {folder}: {e}")



    print("-" * 30)
    print("NUCLEAR RESET COMPLETE.")
    print("The instance is now brand new. Run 'onboard.bat' to start fresh.")

if __name__ == "__main__":
    confirm = input("WARNING: This will delete ALL agents, settings, and chat history.\nType 'RESET' to confirm: ").strip()
    if confirm == "RESET":
        nuclear_reset()
    else:
        print("Reset aborted.")
