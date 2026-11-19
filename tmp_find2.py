import pathlib
p = pathlib.Path("backend/app/services/chat_service.py")
lines = p.read_text(encoding="utf-8").splitlines()
for i,l in enumerate(lines,1):
    if "create_message" in l or "save_message" in l:
        print(i, l.strip()[:160])
