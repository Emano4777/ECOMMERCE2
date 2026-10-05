"""Prepare reviewable copies, without changing live code or credentials."""
import ast
from pathlib import Path

ROOTS={'ecommerce':Path('C:/Users/Public/dns-ecommerce'),
       'pedido':Path('C:/Users/windows/OneDrive/pedidoeletronico')}
FILES={'ecommerce':['app.py','scripts/classificar_tarjas_ecommerce.py','scripts/classificar_medicamentos.py',
                     'scripts/classificar_produtos.py','scripts/alimentar_sintomas_catalogo.py','cosmos_sync.py'],
       'pedido':['app.py','laboratorio_ean_classifier.py','normalizar_base_molecular.py','fix_labs_claude.py']}

def transform(source):
    tree=ast.parse(source);raw=source.encode('utf-8');lines=raw.splitlines(keepends=True)
    offsets=[0]
    for line in lines:offsets.append(offsets[-1]+len(line))
    edits=[]
    for n in ast.walk(tree):
        if isinstance(n,ast.Constant) and n.value=='https://api.anthropic.com/v1/messages':
            edits.append((offsets[n.lineno-1]+n.col_offset,offsets[n.end_lineno-1]+n.end_col_offset,b'_anthropic_budget_endpoint()'))
        if isinstance(n,ast.Call) and n.args:
            arg=n.args[0]
            if (isinstance(arg,ast.Name) and arg.id=='CLAUDE_API_URL') or (isinstance(arg,ast.Attribute) and arg.attr=='CLAUDE_API_URL'):
                edits.append((offsets[arg.lineno-1]+arg.col_offset,offsets[arg.end_lineno-1]+arg.end_col_offset,b'_anthropic_budget_endpoint()'))
        if isinstance(n,ast.Call) and ((isinstance(n.func,ast.Attribute) and n.func.attr=='Anthropic') or (isinstance(n.func,ast.Name) and n.func.id=='Anthropic')):
            if not any(k.arg=='base_url' for k in n.keywords):
                pos=offsets[n.end_lineno-1]+n.end_col_offset-1
                prefix=b', ' if (n.args or n.keywords) else b''
                edits.append((pos,pos,prefix+b'base_url=_anthropic_budget_endpoint(base=True), max_retries=0'))
    if not edits:return source,0
    # Insert after module docstring/future imports to preserve Python import rules.
    pos=0
    for n in tree.body:
        if isinstance(n,ast.Expr) and isinstance(n.value,ast.Constant) and isinstance(n.value.value,str):pos=offsets[n.end_lineno]
        elif isinstance(n,ast.ImportFrom) and n.module=='__future__':pos=offsets[n.end_lineno]
        else:break
    if 'from anthropic_endpoint import endpoint as _anthropic_budget_endpoint' not in source:
        edits.append((pos,pos,b'\nfrom anthropic_endpoint import endpoint as _anthropic_budget_endpoint\n'))
    for start,end,replacement in sorted(edits,reverse=True):raw=raw[:start]+replacement+raw[end:]
    result=raw.decode('utf-8');ast.parse(result)
    return result,len(edits)-1

if __name__=='__main__':
    root=Path(__file__).parent/'prepared'
    for app,files in FILES.items():
        for name in files:
            source=(ROOTS[app]/name).read_text(encoding='utf-8-sig')
            result,count=transform(source)
            if count:
                target=root/app/name;target.parent.mkdir(parents=True,exist_ok=True)
                target.write_text(result,encoding='utf-8',newline='\n')
                print(app,name,count)
