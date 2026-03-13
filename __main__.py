"""
HASHI CLI entry point for pip-installed package.
Allows running: python -m hashi
"""

def main():
    import sys
    import os
    from pathlib import Path
    
    # Set up paths
    hashi_root = Path(__file__).parent.absolute()
    sys.path.insert(0, str(hashi_root))
    os.chdir(hashi_root)
    
    # Import and run main
    import main as hashi_main
    
    # The main.py module-level code will execute when imported,
    # but we need to trigger its __main__ block manually
    import argparse
    import asyncio
    from orchestrator.pathing import build_bridge_paths
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--agents", nargs="*", help="Only start the specified agents")
    parser.add_argument("--api-gateway", action="store_true", help="Enable the local OpenAI-compatible API gateway")
    parser.add_argument("--bridge-home", help="Override the bridge home directory")
    args = parser.parse_args()
    
    selected_agents = set(args.agents) if args.agents else None
    paths = build_bridge_paths(hashi_root, bridge_home=args.bridge_home)
    
    orchestrator = hashi_main.UniversalOrchestrator(
        paths=paths,
        selected_agents=selected_agents,
        enable_api_gateway=args.api_gateway
    )
    lock = hashi_main.InstanceLock(paths.lock_path)
    
    try:
        lock.acquire()
        hashi_main.main_logger.info(
            f"Process bootstrap: pid={os.getpid()} ppid={os.getppid()} "
            f"exe={sys.executable} cwd={Path.cwd()} "
            f"code_root={paths.code_root} bridge_home={paths.bridge_home} "
            f"config={paths.config_path}"
        )
        asyncio.run(orchestrator.run())
    except KeyboardInterrupt:
        hashi_main.main_logger.info("KeyboardInterrupt received. Exiting.")
    except Exception as e:
        import traceback
        hashi_main.main_logger.critical(f"Fatal crash: {e}\n{traceback.format_exc()}")
    finally:
        lock.release()
    
    os._exit(0)

if __name__ == "__main__":
    main()
