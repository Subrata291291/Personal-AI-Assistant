import pathlib
p = pathlib.Path("backend/app/services/chat_service.py")
lines = p.read_text(encoding="utf-8").splitlines()
for i,l in enumerate(lines,1):
    if "assistant_message" in l and "save" in l.lower():
        print(i, l.strip()[:160])
