cd "C:\Users\thene\projects\HASHI"
& .\.venv\Scripts\Activate.ps1
pyinstaller main.py -y -F -n hashi-zero --add-data "agent_seeds;agent_seeds" --add-data "skills;skills" --add-data "scripts;scripts" --add-data "agents.json;." --add-data "secrets.json;." --add-data "wa_session;wa_session" --runtime-tmpdir _MEI