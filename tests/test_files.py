"""test_files.py — compressed audit. pytest + CLI. 28 formats."""
import configparser, csv, importlib, json, re, sys, tempfile, threading, traceback
import xml.etree.ElementTree as ET
from pathlib import Path

_R = Path(__file__).resolve().parent.parent
_P = _R / "crawler_toolkit"
if str(_R) not in sys.path: sys.path.insert(0, str(_R))
_TAG = re.compile(rb"<([a-zA-Z][a-zA-Z0-9]*)\b")

TEXT = set(".txt .md .rst .tex .py .js .ts .css .sql .sh .bat".split())
INI  = set(".ini .cfg .conf".split())
ENV  = set(".env .properties".split())
HTML = set(".html .htm".split())
ALL  = frozenset(TEXT | INI | ENV | HTML |
                 {".json",".jsonl",".ndjson",".yaml",".yml",".toml",
                  ".csv",".tsv",".xml",".log"})
assert len(ALL) >= 23, f"need ≥23, have {len(ALL)}"

_F = {**{e: "x\n" for e in TEXT}, **{e: "[s]\nk=v\n" for e in INI},
      **{e: "K=v\n" for e in ENV},
      **{e: "<html><body><p>x</p></body></html>" for e in HTML},
      ".json": '{"a":1}', ".jsonl": '{"a":1}\n{"a":2}\n', ".ndjson": '{"a":1}\n',
      ".yaml": "a: 1\n", ".yml": "a: 1\n", ".toml": "a = 1\n", ".xml": "<r><a/></r>",
      ".csv": "a,b\n1,2\n", ".tsv": "a\tb\n1\t2\n", ".log": "INFO x\nERROR y\n"}


def _r(p, x, ok=True, err=None):
    try: n = p.stat().st_size
    except OSError: n = 0
    return {"bytes": n, "ok": ok, "parsed": x, "error": err}


def load(path):
    p = Path(path); e = p.suffix.lower()
    try:
        if e in TEXT or e not in ALL:
            t = p.read_text("utf-8", "replace")
            return _r(p, {"text": t, "lines": t.count("\n") + 1})
        if e == ".json":
            return _r(p, json.loads(p.read_text("utf-8")))
        if e in (".jsonl", ".ndjson"):
            rs, es = [], []
            for i, ln in enumerate(p.read_text("utf-8").splitlines(), 1):
                if ln.strip():
                    try: rs.append(json.loads(ln))
                    except Exception as x: es.append(f"{i}:{x}")
            return _r(p, {"rows": rs, "errs": es}, not es, ";".join(es) or None)
        if e in (".yaml", ".yml"):
            return _r(p, __import__("yaml").safe_load(p.read_text("utf-8")))
        if e == ".toml":
            import tomllib
            with p.open("rb") as f: return _r(p, tomllib.load(f))
        if e in INI:
            c = configparser.ConfigParser(); c.read(p, encoding="utf-8")
            return _r(p, {s: dict(c[s]) for s in c.sections()})
        if e in ENV:
            d, es = {}, []
            for i, ln in enumerate(p.read_text("utf-8").splitlines(), 1):
                s = ln.strip()
                if not s or s.startswith("#"): continue
                if "=" not in s: es.append(str(i)); continue
                k, _, v = s.partition("=")
                d[k.strip()] = v.strip().strip('"').strip("'")
            return _r(p, d, not es, ";".join(es) or None)
        if e in (".csv", ".tsv"):
            with p.open("r", encoding="utf-8", newline="") as f:
                r = csv.DictReader(f, delimiter="\t" if e == ".tsv" else ",",
                                   strict=True)
                rows = list(r)
            return _r(p, {"rows": rows, "fields": r.fieldnames})
        if e == ".xml":
            root = ET.parse(p).getroot()
            return _r(p, {"root": root.tag, "children": len(root)})
        if e in HTML:
            t = p.read_text("utf-8", "replace")
            try:
                from selectolax.parser import HTMLParser
                tree = HTMLParser(t)
                return _r(p, {"len": len(t),
                              "body_children": len(tree.css("body > *")) if tree.body else 0})
            except Exception:
                return _r(p, {"len": len(t), "tags": len(_TAG.findall(t.encode()))})
        if e == ".log":
            t = p.read_text("utf-8", "replace"); u = t.upper()
            return _r(p, {"lines": t.count("\n") + 1,
                          "levels": {l: u.count(l) for l in
                                     ("DEBUG","INFO","WARN","WARNING","ERROR","FATAL","CRITICAL","TRACE")}})
    except Exception as x:
        return _r(p, None, False, f"{type(x).__name__}: {x}")
    return _r(p, {})


def discover():
    if not _P.is_dir(): return []
    s = set()
    for x in _P.rglob("*.py"):
        ps = list(x.relative_to(_R).with_suffix("").parts)
        if ps[-1] == "__init__": ps = ps[:-1]
        if ps: s.add(".".join(ps))
    return sorted(s)


def check_imports():
    mods = discover(); f, ex = {}, {}
    for m in mods:
        try: importlib.import_module(m)
        except Exception as e: f[m] = f"{type(e).__name__}: {e}"; ex[m] = e
    if mods and not f:
        return {"ok": True, "modules": mods, "failures": {}, "debug": ""}
    dbg = f"pkg={_P} exists={_P.is_dir()}"
    if f:
        dbg = (f"py={sys.version.split()[0]} pkg={_P} "
               f"init={(_P/'__init__.py').is_file()}\n")
        dbg += "".join(
            f"\n{m}: {''.join(traceback.format_exception(type(e), e, e.__traceback__))}\n"
            for m, e in ex.items())
    return {"ok": False, "modules": mods, "failures": f, "debug": dbg}


def check_defaults():
    from crawler_toolkit.imports_integration import ToolkitConfig as C
    c = C()
    K = [
        ("rate>0", c.default_rate > 0), ("burst", c.default_burst >= 1),
        ("min<=rate", c.min_rate <= c.default_rate), ("max>=rate", c.max_rate >= c.default_rate),
        ("max>min", c.max_rate > c.min_rate), ("retry", c.max_retry_after > 0),
        ("climb-off", not getattr(c, "allow_rate_climb", False)),
        ("level", c.log_level.upper() in {"TRACE","DEBUG","INFO","WARN","ERROR","FATAL"}),
        ("dedup", c.log_dedup_max > 0), ("ring", c.log_ring_size > 0),
        ("parser", c.parser.lower() in {"lexbor","modest"}),
        ("challenge", len(c.challenge_markers) > 0),
        ("security", len(c.security_headers) > 0),
        ("trackers", len(c.tracker_markers) > 0), ("ads", len(c.ad_markers) > 0),
        ("warn<fail", c.response_time_warn_ms < c.response_time_fail_ms),
        ("size", c.content_size_min < c.content_size_max),
    ]
    return {"ok": all(o for _, o in K), "checks": K}


def test_pkg(): assert _P.is_dir() and (_P / "__init__.py").is_file(), f"pkg broken: {_P}"


def test_imports():
    r = check_imports(); assert r["ok"], f"{r['failures']}\n{r['debug']}"


def test_defaults():
    r = check_defaults(); assert r["ok"], [n for n, o in r["checks"] if not o]


def test_count(): assert len(ALL) >= 23, f"have {len(ALL)}"


def test_formats():
    with tempfile.TemporaryDirectory() as td:
        for e in sorted(ALL):
            p = Path(td) / f"s{e}"; p.write_text(_F[e], encoding="utf-8")
            r = load(p); assert r["ok"], f"{e}: {r['error']}"


def test_edges():
    with tempfile.TemporaryDirectory() as td:
        tp = Path(td)
        for e in sorted(ALL):
            (tp / f"e{e}").write_text("", encoding="utf-8")
            load(tp / f"e{e}")
        for e, c in ((".json", "{bad"), (".xml", "<a>"),
                     (".toml", "[[["), (".csv", 'a,b\n"u')):
            p = tp / f"m{e}"; p.write_text(c, encoding="utf-8")
            assert not load(p)["ok"], f"{e} should fail on malformed"


def test_concurrent():
    with tempfile.TemporaryDirectory() as td:
        tp = Path(td); ps = []
        for i, e in enumerate(sorted(ALL)):
            p = tp / f"c{i}{e}"; p.write_text(_F[e], encoding="utf-8"); ps.append(p)
        errs = []
        def w(chunk):
            for p in chunk:
                try: load(p)
                except Exception as x: errs.append((p.name, str(x)))
        ts = [threading.Thread(target=w, args=(ps[i::8],)) for i in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
        assert not errs, errs


def test_volume():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "big.jsonl"
        p.write_text("\n".join(json.dumps({"i": i}) for i in range(5000)),
                     encoding="utf-8")
        r = load(p)
    assert r["ok"], r["error"]
    assert len(r["parsed"]["rows"]) == 5000


_T = [test_pkg, test_imports, test_defaults, test_count,
      test_formats, test_edges, test_concurrent, test_volume]


def main():
    print(f"root={_R}\npkg ={_P}  init={(_P/'__init__.py').is_file()}  formats={len(ALL)}")
    f = 0
    for t in _T:
        try: t(); print(f"  PASS {t.__name__}")
        except AssertionError as e: f += 1; print(f"  FAIL {t.__name__}: {e}")
        except Exception as e: f += 1; print(f"  ERR  {t.__name__}: {type(e).__name__}: {e}")
    print(f"exit={f}")
    return 1 if f else 0


if __name__ == "__main__":
    raise SystemExit(main())