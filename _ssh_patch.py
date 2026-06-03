"""Patch: restaura backup, remove BOM e insere from __future__."""
import os

path     = "/root/scripts/pedidoeletronico/dns_ecommerce/app.py"
bak_path = path + ".bak"
marker   = "from __future__ import annotations"

# Restaura backup original se existir
if os.path.exists(bak_path):
    with open(bak_path, "rb") as f:
        raw = f.read()
    print("Restaurado do backup")
else:
    with open(path, "rb") as f:
        raw = f.read()

# Remove BOM
bom = b"\xef\xbb\xbf"
if raw.startswith(bom):
    raw = raw[3:]
    print("BOM removido")

content = raw.decode("utf-8", errors="replace")

if marker in content:
    # Já tem __future__ sem BOM? Apenas reescreve sem BOM
    print("Ja tem __future__. Reescrevendo sem BOM.")
    with open(path, "wb") as f:
        f.write(content.encode("utf-8"))
    print("OK")
else:
    lines = content.split("\n")
    # Insere após docstring de módulo (se houver)
    insert_at = 0
    in_ds = False
    ds_char = None
    for i, line in enumerate(lines):
        s = line.strip()
        if i == 0 and (s.startswith('"""') or s.startswith("'''")):
            ds_char = s[:3]
            in_ds = True
            if s.count(ds_char) >= 2 and len(s) > 3:
                in_ds = False
            continue
        if in_ds:
            if ds_char and ds_char in s:
                in_ds = False
            continue
        if s and not s.startswith("#"):
            insert_at = i
            break
    lines.insert(insert_at, marker)
    new_content = "\n".join(lines)
    with open(path, "wb") as f:
        f.write(new_content.encode("utf-8"))
    print(f"PATCH OK: __future__ inserido na linha {insert_at}")

# Verifica
with open(path, "rb") as f:
    first_bytes = f.read(100)
print(f"Primeiros bytes: {first_bytes[:60]!r}")
