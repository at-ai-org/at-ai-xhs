import re, sys
from pathlib import Path

# 模板里可用的"局部/内置"名字（v-for 变量、$event 等）与全局
LOCALS = set()
GLOBALS = {
    'true','false','null','undefined','this','$event','$index','$refs','Math','Date','JSON',
    'Object','Array','String','Number','Boolean','parseInt','parseFloat','String','EventSource',
    'console','window','document','RegExp','Set','Map','Promise','isNaN','Intl','encodeURIComponent'
}

def strip_literals(expr):
    # 去掉 '...' 和 `...` 里的内容，避免把字符串当标识符
    expr = re.sub(r"'[^']*'", "''", expr)
    expr = re.sub(r'`[^`]*`', '``', expr)
    expr = re.sub(r'"[^"]*"', '""', expr)
    return expr


def check(path):
    src = Path(path).read_text(encoding='utf-8')
    tpl = src[src.index('<template>'):src.rindex('</template>')]
    script = src[src.index('<script setup>'):]

    defined = set(re.findall(r'(?:async\s+)?function\s+(\w+)', script))
    defined |= set(re.findall(r'(?:const|let|var)\s+(\w+)', script))
    for grp in re.findall(r'import\s+\{([^}]*)\}', script):
        defined |= {x.strip().split(' as ')[-1].strip() for x in grp.split(',') if x.strip()}
    defined |= set(re.findall(r'import\s+(\w+)\s+from', script))

    # v-for 引入的循环变量
    for m in re.findall(r'v-for="\(?([^")]*)\)?\s+in\s+', tpl):
        for name in m.split(','):
            name = name.strip()
            if name: LOCALS.add(name)
    # 插槽作用域变量
    for m in re.findall(r'#default="\{([^}]*)\}"', tpl):
        LOCALS.update(x.strip() for x in m.split(',') if x.strip())
    for m in re.findall(r'v-slot:[\w-]+="\{([^}]*)\}"', tpl):
        LOCALS.update(x.strip() for x in m.split(',') if x.strip())
    # 非解构插槽： #default="scope" / #default="scope, extra"
    for m in re.findall(r'#[\w-]+="([^"{},]+)"', tpl):
        LOCALS.update(x.strip() for x in m.split(',') if x.strip())
    # 模板里的内联箭头函数参数： @change="(v) => fn(v)" / @click="v => fn(v)"
    for params in re.findall(r'\(([^()]*)\)\s*=>', tpl) + re.findall(r'(?<![\w$.])([a-zA-Z_$][\w$]*)\s*=>', tpl):
        LOCALS.update(x.strip() for x in params.split(',') if x.strip())

    used = {}
    # 绑定 / 事件 / v-if 等表达式里的裸标识符（排除属性访问 .xxx）
    for attr, raw in re.findall(r'((?:[:@]|v-)[\w.:-]+)="([^"]*)"', tpl):
        expr = strip_literals(raw)
        expr = re.sub(r'([{,;]\s*)([a-zA-Z_$][\w$]*)\s*:', r'\1', expr)  # 去掉对象 key
        for ident in re.findall(r'(?<![.\w$])([a-zA-Z_$][\w$]*)', expr):
            if ident in GLOBALS or ident in ('in','of','new','typeof','return','if','else'):
                continue
            used.setdefault(ident, attr)
    # 插值 {{ }}
    for raw in re.findall(r'\{\{([^}]*)\}\}', tpl):
        expr = strip_literals(raw)
        expr = re.sub(r'([{,;]\s*)([a-zA-Z_$][\w$]*)\s*:', r'\1', expr)  # 去掉对象 key
        for ident in re.findall(r'(?<![.\w$])([a-zA-Z_$][\w$]*)', expr):
            if ident in GLOBALS: continue
            used.setdefault(ident, '{{ }}')

    missing = {k: v for k, v in used.items() if k not in defined and k not in LOCALS}
    print(f'--- {path} ---')
    if missing:
        for k, v in sorted(missing.items()):
            print(f'  ❌ {k}   (出现在 {v})')
    else:
        print('  ✅ 模板引用的名字都有定义')
    return missing

bad = False
for f in sys.argv[1:]:
    if check(f): bad = True
sys.exit(1 if bad else 0)
