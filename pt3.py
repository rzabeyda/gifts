import ast
m = open("main.py", encoding="utf-8").read()
def rep(a, b):
    global m
    assert m.count(a) == 1, a[:60]
    m = m.replace(a, b)
rep('"sortBy": "price", "sortOrder": "asc", "page": 1, "limit": 40}', '"sortBy": "price", "sortOrder": "asc", "page": 1, "limit": 20}  # peek отвечает 403 на limit > 20')
rep('"items": items[:8], "total"', '"items": items[:20], "total"')
ast.parse(m)
assert b"\x00" not in m.encode()
open("main.py", "w", encoding="utf-8", newline="\n").write(m)
s = open("web/index.html", encoding="utf-8").read()
a = "const lots = (live?.items || []).slice(0, 5);"
assert s.count(a) == 1
s = s.replace(a, "const lots = (live?.items || []).slice(0, 20);")
open("web/index.html", "w", encoding="utf-8", newline="\n").write(s)
print("ok")
