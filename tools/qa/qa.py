#!/usr/bin/env python3
"""Prueba QA en el emulador: entra a la app con un catálogo enorme y mide qué tan rápida es al cambiar de sección.

Uso: qa.py CARPETA_DE_SALIDA   (lee la configuración de la app en tools/qa/app.json)

Mide con las marcas que deja la propia app en el registro (etiqueta QA, solo en la compilación de prueba):
  ABRIR / NAV  -> se pidió abrir una pantalla y cuándo se dibujó
  LISTO        -> la sección ya tiene su contenido en pantalla
  LAG          -> el hilo principal se quedó trabado N ms (y en qué código)
  CATALOGO     -> cuánto tardó en cargar cada lista
Además revisa cierres (FATAL EXCEPTION), "no responde" (ANR), memoria y deja capturas de cada pantalla.
"""
import json
import os
import re
import subprocess
import sys
import time
import traceback
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1] if len(sys.argv) > 1 else "qa-out"
os.makedirs(OUT, exist_ok=True)
APP = json.load(open(os.path.join(HERE, "app.json"), encoding="utf-8"))
PKG = APP["pkg"]
ACT = APP["activity"]
W, H = 1920, 1080

REPORT = []
NAVS = []      # (nombre, ms_internos, ms_totales)
SHOTS = [0]
RESULTS = {"navs": [], "lags": [], "crashes": 0, "anrs": 0, "fails": 0}


# ----------------------------------------------------------------------------- utilidades

def log(kind, text):
    REPORT.append(f"- **{kind}** {text}")
    print(f"[{kind}] {text}", flush=True)
    if kind == "FALLO":
        RESULTS["fails"] += 1
    write_report()


def write_report():
    with open(os.path.join(OUT, "report.md"), "w", encoding="utf-8") as f:
        f.write(f"# Prueba QA de {APP['name']}\n\n" + "\n".join(REPORT) + "\n")


def adb(*args, timeout=60, binary=False):
    try:
        r = subprocess.run(["adb", *args], capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return b"" if binary else ""
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def sh(cmd, timeout=60):
    return adb("shell", cmd, timeout=timeout)


def now_dev():
    """Milisegundos desde el arranque del emulador (el mismo reloj que usa la app en sus marcas)."""
    out = sh("cat /proc/uptime")
    try:
        return int(float(out.split()[0]) * 1000)
    except (ValueError, IndexError):
        return 0


def dump():
    for _ in range(3):
        sh("rm -f /sdcard/ui.xml")
        sh("uiautomator dump /sdcard/ui.xml >/dev/null 2>&1", timeout=40)
        xml = sh("cat /sdcard/ui.xml 2>/dev/null")
        if "<hierarchy" in xml:
            try:
                root = ET.fromstring(xml[xml.index("<"):])
            except ET.ParseError:
                time.sleep(0.5)
                continue
            out = []
            for n in root.iter("node"):
                m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", n.get("bounds", ""))
                if not m:
                    continue
                out.append({
                    "text": n.get("text", ""), "desc": n.get("content-desc", ""), "cls": n.get("class", ""),
                    "clickable": n.get("clickable") == "true", "focused": n.get("focused") == "true",
                    "box": tuple(map(int, m.groups())),
                })
            return out
        time.sleep(0.7)
    return []


def texts(nodes):
    seen, out = set(), []
    for n in nodes:
        for t in (n["text"], n["desc"]):
            t = t.strip()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def find(nodes, labels, exact=False):
    labels = [labels] if isinstance(labels, str) else labels
    for label in labels:
        low = label.lower()
        for n in nodes:
            for t in (n["text"], n["desc"]):
                tl = t.strip().lower()
                if (tl == low) if exact else (low in tl):
                    return n
    return None


def center(n):
    x1, y1, x2, y2 = n["box"]
    return (x1 + x2) // 2, (y1 + y2) // 2


def tap_node(n, wait=0.8):
    x, y = center(n)
    sh(f"input tap {x} {y}")
    time.sleep(wait)


def keys(*codes, wait=0.5):
    sh("input keyevent " + " ".join(str(c) for c in codes))
    time.sleep(wait)


def shot(name):
    SHOTS[0] += 1
    png = adb("exec-out", "screencap", "-p", binary=True, timeout=40)
    if png:
        with open(os.path.join(OUT, f"{SHOTS[0]:02d}_{name}.png"), "wb") as f:
            f.write(png)


def qa_lines(since=0):
    """Marcas de la app (etiqueta QA) desde el instante 'since' (ms del emulador)."""
    out = adb("logcat", "-d", "-v", "brief", "-s", "QA:I", timeout=60)
    res = []
    for line in out.splitlines():
        m = re.search(r"t=(\d+) (.*)$", line)
        if m and int(m.group(1)) >= since:
            res.append((int(m.group(1)), m.group(2).strip()))
    return res


def wait_qa(pattern, since, timeout=90):
    end = time.time() + timeout
    rx = re.compile(pattern)
    while time.time() < end:
        for t, msg in qa_lines(since):
            if rx.search(msg):
                return t, msg
        time.sleep(0.4)
    return None, None


def lags_between(t0, t1):
    res = []
    for t, msg in qa_lines(t0):
        if t > t1:
            break
        m = re.match(r"LAG (\d+) ms :: (.*)", msg)
        if m:
            res.append((t, int(m.group(1)), m.group(2)))
    return res


def app_alive():
    return sh(f"pidof {PKG}").strip() != ""


def in_front():
    out = sh("dumpsys activity activities | grep -m1 -E 'mResumedActivity|topResumedActivity'")
    return PKG in out


def start_app(extra=""):
    sh(f"am start -n {PKG}/{ACT} {extra}")


def qa_open(screen):
    """Abre una pantalla directamente (solo en la compilación de prueba) por si no se encuentra el botón."""
    sh(f"am start -n {PKG}/{ACT} --activity-single-top --es qa_open {screen}")


CRASH_SEEN = [0]


def check_crash(step_name):
    out = adb("logcat", "-d", "-b", "crash", "-v", "time", timeout=60)
    n = out.count("FATAL EXCEPTION")
    if n > CRASH_SEEN[0]:
        lines = out.splitlines()
        idx = [i for i, l in enumerate(lines) if "FATAL EXCEPTION" in l]
        trace = lines[idx[CRASH_SEEN[0]]: idx[CRASH_SEEN[0]] + 25]
        log("FALLO", f"La app se CERRÓ durante «{step_name}»: " + " / ".join(re.sub(r"^\S+ \S+ ", "", l).strip()[:200] for l in trace[:9]))
        with open(os.path.join(OUT, f"cierre_{n}.txt"), "w") as f:
            f.write("\n".join(lines[idx[CRASH_SEEN[0]]:idx[CRASH_SEEN[0]] + 80]))
        CRASH_SEEN[0] = n
        RESULTS["crashes"] += 1
    main = adb("logcat", "-d", "-b", "main,system", "-v", "time", timeout=60)
    anrs = len(re.findall(r"ANR in " + re.escape(PKG), main))
    if anrs > RESULTS["anrs"]:
        log("FALLO", f"Android avisó que la app NO RESPONDE (ANR) durante «{step_name}»")
        RESULTS["anrs"] = anrs


def step(name):
    def deco(fn):
        def run(*a, **k):
            print(f"\n=== {name}", flush=True)
            try:
                return fn(*a, **k)
            except Exception as e:
                log("FALLO", f"{name}: error en la prueba {e!r}")
                traceback.print_exc()
            finally:
                try:
                    check_crash(name)
                    if not app_alive() or not in_front():
                        log("AVISO", f"Después de «{name}» la app no estaba en pantalla; se vuelve a abrir")
                        start_app()
                        time.sleep(6)
                except Exception:
                    pass
        return run
    return deco


def go_home():
    for _ in range(8):
        nodes = dump()
        if find(nodes, APP["home_markers"]):
            return nodes
        if find(nodes, ["Más tarde", "Ahora no"]):
            tap_node(find(nodes, ["Más tarde", "Ahora no"]))
            continue
        if find(nodes, APP.get("exit_dialog", ["¿Salir"])):
            keys(4, wait=0.8)
            continue
        keys(4, wait=0.9)
        if not in_front():
            start_app()
            time.sleep(5)
    return dump()


def record_nav(label, t0, t_ready, msg):
    internal = None
    m = re.search(r"a (\d+) ms de abrir", msg or "")
    if m:
        internal = int(m.group(1))
    total = (t_ready - t0) if t_ready else None
    lags = lags_between(t0, (t_ready or t0) + 1500)
    worst = max((l[1] for l in lags), default=0)
    RESULTS["navs"].append({"seccion": label, "ms_app": internal, "ms_total": total, "peor_trabon_ms": worst})
    kind = "OK"
    if total is None:
        kind = "FALLO"
    elif (internal or total) > APP.get("max_nav_ms", 1500) or worst > 700:
        kind = "LENTO"
    log(kind, f"Abrir {label}: contenido en pantalla a {internal if internal is not None else '?'} ms de pulsar "
              f"({total if total is not None else 'nunca'} ms con la espera del control); peor trabón {worst} ms")
    for t, d, where in lags[:3]:
        log("TRABON", f"{d} ms al abrir {label} :: {where[:300]}")


def open_section(key, label_list, shot_name=None):
    """Desde el inicio, pulsa la sección y espera a que tenga contenido."""
    nodes = go_home()
    target = find(nodes, label_list, exact=True) or find(nodes, label_list)
    t0 = now_dev()
    if target:
        tap_node(target, wait=0)
    else:
        log("AVISO", f"No encontré el botón «{label_list[0]}» en el inicio; se abre directo")
        qa_open(APP["screens"][key])
    t, msg = wait_qa(r"^LISTO ", t0, timeout=120)
    record_nav(label_list[0], t0, t, msg)
    time.sleep(1.0)
    if shot_name:
        shot(shot_name)
    return t is not None


def stress_section(key, rounds=1, take_shots=False):
    """Cambia de categoría varias veces y recorre la lista/cuadrícula con el control."""
    t0 = now_dev()
    nodes = dump()
    left = [n for n in nodes if n["text"].strip() and n["box"][2] < W * 0.36 and n["box"][1] > H * 0.12
            and n["box"][3] - n["box"][1] < 140]
    picks = left[3:12:2] if len(left) > 6 else left[:4]
    for n in picks:
        tap_node(n, wait=0.6)
    if take_shots and picks:
        shot(f"{key}_categoria")
    # recorrer la lista con el control: derecha (al contenido) y muchas veces abajo
    keys(22, wait=0.4)
    for _ in range(rounds):
        keys(*([20] * 25), wait=0.3)
    keys(*([22] * 4), wait=0.3)
    keys(*([20] * 15), wait=0.6)
    if take_shots:
        shot(f"{key}_recorrido")
    t1 = now_dev()
    lags = lags_between(t0, t1 + 500)
    worst = max((l[1] for l in lags), default=0)
    total = sum(l[1] for l in lags)
    RESULTS["lags"].append({"seccion": key, "trabones": len(lags), "peor_ms": worst, "total_ms": total})
    kind = "OK" if worst <= 500 and total <= 3000 else "LENTO"
    log(kind, f"Moverse dentro de {key} (cambiar categorías y bajar por la lista): {len(lags)} trabones, "
              f"peor {worst} ms, total {total} ms en {round((t1 - t0) / 1000, 1)} s")
    for t, d, where in sorted(lags, key=lambda l: -l[1])[:3]:
        log("TRABON", f"{d} ms en {key} :: {where[:300]}")


# ---------------------------------------------------------------------------------- pasos

@step("preparar")
def prepare():
    sh(f"wm size {W}x{H}")
    sh("wm density 320")
    for k in ("window_animation_scale", "transition_animation_scale", "animator_duration_scale"):
        sh(f"settings put global {k} 0")
    sh("settings put secure show_ime_with_hard_keyboard 0")
    sh(f"pm clear {PKG}")
    sh(f"pm grant {PKG} android.permission.POST_NOTIFICATIONS")
    adb("logcat", "-b", "all", "-c")
    sh("logcat -G 16M")
    start_app()
    time.sleep(7)
    shot("arranque")
    log("INFO", "Pantalla " + sh("wm size").strip().replace("\n", " ") + " · " + sh("wm density").strip().replace("\n", " "))


@step("iniciar sesión")
def login():
    nodes = []
    for _ in range(20):
        nodes = dump()
        if len([n for n in nodes if n["cls"].endswith("EditText")]) >= 2:
            break
        if find(nodes, APP["home_markers"]):
            log("INFO", "Ya había sesión")
            return True
        time.sleep(1.5)
    shot("login")
    edits = [n for n in nodes if n["cls"].endswith("EditText")]
    if len(edits) < 2:
        log("FALLO", f"No aparecieron los campos de usuario y contraseña (textos: {texts(nodes)[:12]})")
        return False
    tap_node(edits[0], 0.6)
    sh("input text demo")
    tap_node(edits[1], 0.6)
    sh("input text demo")
    time.sleep(0.4)
    keys(66, wait=0.3)  # Enter = Entrar
    nodes = dump()
    button = find(nodes, APP.get("login_button", ["Entrar"]), exact=True)
    if button and find(nodes, ["Usuario"]):
        tap_node(button, 0.3)
    t0 = time.time()
    while time.time() - t0 < 90:
        nodes = dump()
        if find(nodes, APP["home_markers"]):
            log("OK", f"Entró a la app en {round(time.time() - t0, 1)} s")
            time.sleep(1.5)
            shot("inicio_recien_entrado")
            return True
        time.sleep(1)
    log("FALLO", "Después de poner usuario y contraseña no apareció el inicio")
    shot("login_fallo")
    return False


@step("entrar a Películas mientras cargan las listas")
def early_movies():
    open_section("movies", APP["labels"]["movies"], "peliculas_primera_vez")
    keys(4, wait=1.0)


@step("esperar a que carguen las tres listas")
def wait_catalogs():
    t0 = time.time()
    got = set()
    while time.time() - t0 < 240 and len(got) < 3:
        for t, msg in qa_lines(0):
            m = re.match(r"CATALOGO (LIVE|MOVIES|SERIES) (\d+) elementos, (\d+) categorías en (\d+) ms", msg)
            if m and m.group(1) not in got:
                got.add(m.group(1))
                log("INFO", f"Lista {m.group(1)}: {m.group(2)} elementos y {m.group(3)} categorías en {m.group(4)} ms")
        time.sleep(2)
    if len(got) < 3:
        log("AVISO", f"Solo cargaron {sorted(got)} en 4 minutos")
    perf = [m for _, m in qa_lines(0) if "lista " in m]
    for line in adb("logcat", "-d", "-s", "UPTV:D", timeout=30).splitlines():
        if "lista " in line:
            log("INFO", "Medición interna: " + line[line.find("lista "):][:200])


@step("cambiar de sección varias veces")
def sections():
    for r in range(3):
        for key in ("live", "movies", "series"):
            ok = open_section(key, APP["labels"][key], f"{key}" if r == 0 else None)
            if ok:
                stress_section(key, rounds=1 if r else 2, take_shots=(r == 0))
            keys(4, wait=1.0)


@step("ficha de película y reproducir")
def movie_detail():
    open_section("movies", APP["labels"]["movies"])
    keys(22, wait=0.4)
    t0 = now_dev()
    keys(23, wait=0.2)
    t, msg = wait_qa(r"^NAV MovieDetails", t0, timeout=30)
    time.sleep(2.5)
    shot("ficha_pelicula")
    lags = lags_between(t0, now_dev())
    log("OK" if t else "FALLO", f"Ficha de película {'abrió en ' + str(t - t0) + ' ms' if t else 'no abrió'}; "
                                f"trabones: {[l[1] for l in lags][:6]}")
    nodes = dump()
    play = find(nodes, APP.get("play_labels", ["Reproducir", "Ver ahora", "Ver película", "▶"]))
    if play:
        tap_node(play, wait=8)
        shot("reproduciendo_pelicula")
        if PKG in sh("dumpsys activity activities | grep -m1 -E 'mResumedActivity|topResumedActivity'") and "Player" in sh("dumpsys activity activities | grep -m1 -E 'mResumedActivity|topResumedActivity'"):
            log("OK", "La película se abrió en el reproductor")
        keys(4, wait=1.5)
        nodes = dump()
        if find(nodes, ["¿Salir", "Salir del video", "Seguir viendo"]):
            keys(4, wait=1)
    else:
        log("AVISO", "No encontré el botón para reproducir en la ficha: " + " | ".join(texts(nodes)[:15]))
    keys(4, wait=1)


@step("ficha de serie y episodios")
def series_detail():
    open_section("series", APP["labels"]["series"])
    keys(22, wait=0.4)
    t0 = now_dev()
    keys(23, wait=0.2)
    t, msg = wait_qa(r"^NAV SeriesDetails", t0, timeout=30)
    time.sleep(3)
    shot("ficha_serie")
    keys(20, 20, wait=1.0)
    shot("ficha_serie_episodios")
    lags = lags_between(t0, now_dev())
    nodes = dump()
    eps = [x for x in texts(nodes) if re.search(r"(?i)episodio|cap[ií]tulo|E\d+", x)]
    log("OK" if t else "FALLO", f"Ficha de serie {'abrió en ' + str(t - t0) + ' ms' if t else 'no abrió'}; "
                                f"episodios visibles: {len(eps)}; trabones: {[l[1] for l in lags][:6]}")
    keys(4, wait=1)


@step("canal en vivo")
def live_channel():
    open_section("live", APP["labels"]["live"])
    keys(22, wait=0.4)
    keys(20, 20, wait=0.4)
    keys(23, wait=6)
    shot("vivo_vista_previa")
    keys(23, wait=7)
    shot("vivo_pantalla_completa")
    top = sh("dumpsys activity activities | grep -m1 -E 'mResumedActivity|topResumedActivity'")
    log("INFO", "Pantalla al reproducir en vivo: " + top.strip()[-90:])
    keys(4, wait=1.5)
    nodes = dump()
    if find(nodes, ["¿Salir", "Salir del video", "Seguir viendo"]):
        keys(4, wait=1)
    keys(4, wait=1)


@step("buscar")
def search():
    nodes = go_home()
    target = find(nodes, APP["labels"]["search"], exact=True) or find(nodes, APP["labels"]["search"])
    if not target:
        log("AVISO", "No encontré Buscar en el inicio")
        return
    tap_node(target, 1.5)
    nodes = dump()
    edits = [n for n in nodes if n["cls"].endswith("EditText")]
    if edits:
        tap_node(edits[0], 0.5)
    t0 = now_dev()
    sh("input text fuego")
    time.sleep(3)
    shot("busqueda")
    lags = lags_between(t0, now_dev())
    worst = max((l[1] for l in lags), default=0)
    log("OK" if worst < 700 else "LENTO", f"Buscar «fuego»: peor trabón {worst} ms; textos: {' | '.join(texts(dump())[:10])[:300]}")
    keys(4, wait=0.8)
    keys(4, wait=0.8)


@step("ajustes")
def settings():
    nodes = go_home()
    target = find(nodes, APP["labels"]["settings"], exact=True) or find(nodes, APP["labels"]["settings"])
    if target:
        tap_node(target, 2)
    else:
        qa_open(APP["screens"]["settings"])
        time.sleep(2)
    shot("ajustes")
    keys(4, wait=1)


@step("volver a abrir la app con la sesión guardada")
def reopen():
    sh(f"am force-stop {PKG}")
    time.sleep(2)
    t_start = now_dev()
    start_app()
    t0 = time.time()
    while time.time() - t0 < 60:
        nodes = dump()
        if find(nodes, APP["home_markers"]):
            break
        time.sleep(0.5)
    log("INFO", f"Con sesión guardada, el inicio apareció en {round(time.time() - t0, 1)} s")
    shot("inicio_al_reabrir")
    open_section("movies", APP["labels"]["movies"], "peliculas_al_reabrir")
    keys(4, wait=1)
    open_section("live", APP["labels"]["live"])
    keys(4, wait=1)


@step("memoria y cuadros")
def memory():
    mem = sh(f"dumpsys meminfo {PKG}")
    with open(os.path.join(OUT, "meminfo.txt"), "w") as f:
        f.write(mem)
    java = re.search(r"Java Heap:\s+(\d+)", mem)
    total = re.search(r"TOTAL(?: PSS)?:\s+(\d+)", mem)
    log("INFO", f"Memoria: Java {int(java.group(1)) // 1024 if java else '?'} MB, total {int(total.group(1)) // 1024 if total else '?'} MB")
    gfx = sh(f"dumpsys gfxinfo {PKG}")
    with open(os.path.join(OUT, "gfxinfo.txt"), "w") as f:
        f.write(gfx)
    jank = re.search(r"Janky frames: (\d+) \(([\d.]+)%\)", gfx)
    p90 = re.search(r"90th percentile: (\d+)ms", gfx)
    p99 = re.search(r"99th percentile: (\d+)ms", gfx)
    if jank:
        log("INFO", f"Cuadros lentos: {jank.group(1)} ({jank.group(2)}%), percentil 90: {p90.group(1) if p90 else '?'} ms, 99: {p99.group(1) if p99 else '?'} ms")


def final():
    lines = qa_lines(0)
    with open(os.path.join(OUT, "qa-marcas.txt"), "w") as f:
        f.write("\n".join(f"{t} {m}" for t, m in lines))
    allog = adb("logcat", "-d", "-v", "time", timeout=120)
    with open(os.path.join(OUT, "logcat.txt"), "w") as f:
        f.write(allog)
    choreo = re.findall(r"Skipped (\d+) frames", allog)
    if choreo:
        log("INFO", f"Android avisó de cuadros saltados {len(choreo)} veces (máximo {max(map(int, choreo))} cuadros seguidos)")
    lags = [(t, int(m.group(1)), m.group(2)) for t, msg in lines for m in [re.match(r"LAG (\d+) ms :: (.*)", msg)] if m]
    if lags:
        big = sorted(lags, key=lambda l: -l[1])[:8]
        log("INFO", f"Trabones del hilo principal en toda la prueba: {len(lags)}, total {sum(l[1] for l in lags)} ms")
        for t, d, where in big:
            log("TRABON", f"{d} ms :: {where[:400]}")
    navs = RESULTS["navs"]
    if navs:
        def avg(sec):
            vals = [n["ms_app"] for n in navs if n["seccion"].lower().startswith(sec) and n["ms_app"] is not None]
            return round(sum(vals) / len(vals)) if vals else None
        log("RESUMEN", "Promedio para abrir: " + ", ".join(f"{n}: {avg(n.lower()[:4])} ms" for n in
                                                           (APP['labels']['live'][0], APP['labels']['movies'][0], APP['labels']['series'][0])))
    log("RESUMEN", f"{RESULTS['fails']} fallos, {RESULTS['crashes']} cierres, {RESULTS['anrs']} ANR")
    with open(os.path.join(OUT, "resultados.json"), "w") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=1)


def main():
    prepare()
    if login():
        early_movies()
        wait_catalogs()
        sections()
        movie_detail()
        series_detail()
        live_channel()
        search()
        settings()
        reopen()
        memory()
    final()


if __name__ == "__main__":
    main()
