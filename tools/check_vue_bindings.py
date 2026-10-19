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


def prop_names(script):
    """defineProps 声明的 prop 名。

    它是编译宏、不是普通变量，所以上面那几条正则看不见它；不认的话，任何带 props 的
    组件都会被误报成"模板引用了未定义的 xxx"（Sparkline、SessionRenewToggle 这些老组件
    一直是这样）。两种写法都认：对象式 defineProps({ a: {...} }) 和数组式 defineProps(['a'])。
    """
    names = set()
    m = re.search(r'defineProps\s*\(\s*\{', script)
    if m:
        # 括号配平找出对象字面量的范围，再只看第 0 层的 key（嵌套的是 prop 的配置，不是 prop）
        depth, i = 1, m.end()
        while i < len(script) and depth:
            depth += {'{': 1, '}': -1}.get(script[i], 0)
            i += 1
        for line in script[m.end():i - 1].splitlines():
            stripped = line.strip()
            if depth == 0:
                km = re.match(r'(\w+)\s*:', stripped)
                if km:
                    names.add(km.group(1))
            depth += stripped.count('{') - stripped.count('}')
    for arr in re.findall(r'defineProps\s*\(\s*\[([^\]]*)\]', script):
        names |= {x.strip().strip('\'"') for x in arr.split(',') if x.strip()}
    return names


def check(path):
    src = Path(path).read_text(encoding='utf-8')
    tpl = src[src.index('<template>'):src.rindex('</template>')]
    script = src[src.index('<script setup>'):]

    defined = set(re.findall(r'(?:async\s+)?function\s+(\w+)', script))
    defined |= set(re.findall(r'(?:const|let|var)\s+(\w+)', script))
    for grp in re.findall(r'import\s+\{([^}]*)\}', script):
        defined |= {x.strip().split(' as ')[-1].strip() for x in grp.split(',') if x.strip()}
    defined |= set(re.findall(r'import\s+(\w+)\s+from', script))
    defined |= prop_names(script)

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
