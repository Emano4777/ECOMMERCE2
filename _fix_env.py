"""Corrige .env do servidor: conserta linhas mescladas e atualiza OCR key."""
import re

path     = "/root/scripts/pedidoeletronico/.env"
key_name = "OCR_SPACE_API_KEY"
key_val  = "K85942195888957"

with open(path, "r", encoding="utf-8", errors="replace") as f:
    content = f.read()

# Corrige linhas mescladas (sem newline antes de uma chave de env conhecida)
content = re.sub(r'([^\n])(OCR_SPACE_API_KEY=)', r'\1\n\2', content)

lines = content.splitlines()
found = False
new_lines = []
for line in lines:
    if line.startswith(key_name + "="):
        new_lines.append(f"{key_name}={key_val}")
        found = True
    else:
        new_lines.append(line)
if not found:
    new_lines.append(f"{key_name}={key_val}")

with open(path, "w", encoding="utf-8") as f:
    f.write("\n".join(new_lines) + "\n")

print("OK")
# Mostra resultado
with open(path, "r") as f:
    for line in f:
        if "OCR" in line or "COSMOS_TOKEN=" in line:
            print(repr(line.rstrip()))
