"""Routes only Anthropic calls to the shared HostGator budget gateway."""
import inspect
import os
import re
from pathlib import Path
DEFAULT_APP = 'ecommerce'

def endpoint(base=False):
    frame=inspect.currentframe().f_back
    names=[]
    while frame and len(names)<2:
        name=frame.f_code.co_name
        if name!='<module>': names.append(name)
        filename=Path(frame.f_code.co_filename).stem
        if not names: names.append(filename)
        frame=frame.f_back
    routine=re.sub(r'[^A-Za-z0-9_.-]','_',os.getenv('ANTHROPIC_APP',DEFAULT_APP)+'.'+'.'.join(names))[:180]
    root=os.getenv('ANTHROPIC_BUDGET_URL','http://127.0.0.1:8769').rstrip('/')
    return root+'/'+routine+('/' if base else '/v1/messages')
