@echo off
cd /d C:\Users\emano\dns-ecommerce
python cosmos_imagens_diario.py --per-token-limit 25 >> cosmos_imagens_diario.log 2>&1
