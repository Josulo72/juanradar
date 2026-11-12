"""RADAR AUTÓNOMOS · programa local (Argentina).

Un solo programa, en tu computadora, sin cuentas externas:
  1. Enciende el motor de búsqueda (carpeta «motor») y busca en internet negocios de una sola
     persona: monotributistas, autónomos, profesionales independientes. Guarda SOLO los datos
     de contacto profesional que ellos mismos publican en su web.
  2. Sirve el panel (http://127.0.0.1:8765): mapa, lista, fichas, etapas y notas.
  3. Si conectas tu correo en Ajustes, envía la secuencia de 3 emails y detecta respuestas y bajas.

Uso:
  python radar.py            abre el panel y deja el robot trabajando
  python radar.py --probar   hace 2 búsquedas y muestra lo que encuentra, sin guardar nada
  python radar.py --buscar   un ciclo de búsqueda y termina (sin panel)
"""
import csv, email, email.policy, html, email.utils, imaplib, io, json, os, random, re, smtplib, subprocess, sys, threading, time
import unicodedata, urllib.parse, urllib.request, webbrowser, zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DATOS = os.environ.get("RADAR_DATOS") or os.path.join(HERE, "datos")
F_CONTACTOS = os.path.join(DATOS, "autonomos.json")
F_AJUSTES = os.path.join(DATOS, "ajustes.json")
F_ESTADO = os.path.join(DATOS, "estado.json")
F_LOG = os.path.join(DATOS, "radar.log")
F_PANEL = os.path.join(HERE, "panel.html")
MOTOR_DIR = os.path.join(HERE, "motor")
MOTOR = os.environ.get("RADAR_MOTOR", "http://127.0.0.1:8787")
PUERTO = int(os.environ.get("RADAR_PUERTO", "8765"))

ETAPAS = ["Nueva", "Email 1", "Email 2", "Email 3", "Llamar", "Respondió", "Cotización", "Cliente", "Descartada", "Baja"]
SUFIJOS = ["contacto whatsapp", "turnos", "matriculado", "particular"]
TEXTO_BAJA = "Si preferís no recibir más correos míos, respondé a este mensaje con la palabra BAJA y no te escribo más."

# Portales, redes, directorios y medios: no son la web propia de un autónomo.
EXCL = ("facebook.", "instagram.", "linkedin.", "twitter.", "x.com", "youtube.", "tiktok.", "google.", "wikipedia.",
        "mercadolibre.", "mercadoshops.", "doctoralia", "paginasamarillas", "guiaoleo", "zonaprop", "argenprop",
        "clarin.", "lanacion.", "infobae.", "pagina12.", "perfil.com", "gob.ar", "gov.ar", "edu.ar", "computrabajo",
        "indeed.", "bumeran", "zonajobs", "workana", "freelancer.", "fiverr.", "homesolution", "timbrit", "zolvers",
        "iguanafix", "topdoctors", "cylex", "yelp.", "tripadvisor.", "pinterest.", "bing.", "duckduckgo.", "yahoo.",
        "amazon.", "apple.com", "wordpress.com", "blogspot.", "medium.com", "reddit.", "quora.", "psychologytoday",
        "psicologiaymente", "psicologiahoy", "psicologos.com", "buscopsi", "letbookly", "firmania", "argentino.com.ar",
        "padronprofesional", "clasificados", "olx.", "alamaula", "booking.", "osde.", "swissmedical", "galeno.",
        "medife.", "sancorsalud", "omint.", "prepagas", "linktr.ee", "calendly.", "t.me", "wa.me", "whatsapp.",
        "nutreando", "nutrifinder", "mundodietetica", "directorio", "guia", "listado", "buscador", "superprof", "tusclases", "habitissimo", "cronoshare")
TLD_FUERA = (".es", ".mx", ".cl", ".co", ".pe", ".uy", ".ve", ".ec", ".br", ".us", ".uk")

S_TEL = re.compile(r"(?<![\d/=.\-])(?:\+?54[\s.\-]*)?(?:9[\s.\-]*)?\(?0?\d{2,4}\)?[\s.\-]*(?:15[\s.\-]*)?\d{3,4}[\s.\-]?\d{4}(?![\d\-])")
S_MAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
BAD_MAIL = re.compile(r"\.(png|jpe?g|webp|gif|svg)$|sentry|wixpress|example|dominio|domain\.com|email\.com|tucorreo|yourdomain|@2x", re.I)
S_SOLO = re.compile(r"\b(soy|mi nombre es|me llamo|me dedico|me recib[ií]|mi consultorio|mi estudio|mi taller|mi trabajo|"
                    r"mis clientes|mis pacientes|sobre m[ií]|atiendo|trabajo por mi cuenta)\b", re.I)
S_MATRICULA = re.compile(r"\bmatr[ií]cula\b|\bM\.?\s?[PN]\.?\s*:?\s*\d", re.I)
S_INDEP = re.compile(r"\bmonotribut\w+|\bfreelance\b|\bindependiente\b|\baut[oó]nom[oa]\b", re.I)
S_TITULO = re.compile(r"\b(?:Lic|Dra?|Ing|Arq|Cdor|Cra|Abog|Mgter|Prof|Odont|Klgo|Klga)\.\s+"
                      r"([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+(?:de\s+|del\s+)?[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+){1})")
S_MELLAMO = re.compile(r"\b(?:[Ss]oy|[Mm]i nombre es|[Mm]e llamo)\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+){1})")
S_EQUIPO = re.compile(r"\b(nuestro equipo|nuestros profesionales|nuestro staff|sucursales|somos una empresa|somos un equipo|"
                      r"nuestros (?:t[eé]cnicos|profesores|abogados|especialistas|m[eé]dicos|contadores|docentes)|S\.A\.|S\.R\.L\.|S\.A\.S\.)", re.I)

AJUSTES_DEF = {
    "mi_nombre": "[Tu nombre]",
    "empresa": "[Prepaga o productora que representás]",
    "oferta": "[Una o dos frases con lo que ofrecés. Ej.: Asesoro a monotributistas y autónomos para elegir un plan de salud a su medida, comparando coberturas y cuotas.]",
    "firma": "[Tu nombre]\n[Matrícula o cargo] · [Prepaga]\n[Teléfono]",
    "tono": "Cercano",
    "plantilla_whatsapp": "Hola {Nombre}, ¿cómo estás? Soy {MiNombre}, asesor de {Empresa}. Encontré tu página buscando {Oficio} en {Lugar}. Trabajo con planes de salud para gente que trabaja por su cuenta. ¿Te puedo pasar una cotización sin compromiso?",
    "envios_automaticos": False,
    "limite_diario": 30,
    "dias_recordatorio_1": 4, "dias_recordatorio_2": 7, "dias_llamada": 5,
    "buscar_cada_horas": 2, "por_ciclo": 10, "incluir_sin_confirmar": False,
    "correo": {"usuario": "", "clave": "", "smtp_host": "smtp.gmail.com", "smtp_puerto": 465,
               "imap_host": "imap.gmail.com", "imap_puerto": 993},
    "zonas": [
        {"localidad": "Buenos Aires", "provincia": "Ciudad Autónoma de Buenos Aires", "alias": ["CABA", "Capital Federal"]},
        {"localidad": "La Plata", "provincia": "Buenos Aires"}, {"localidad": "Mar del Plata", "provincia": "Buenos Aires"},
        {"localidad": "Bahía Blanca", "provincia": "Buenos Aires"}, {"localidad": "Córdoba", "provincia": "Córdoba"},
        {"localidad": "Rosario", "provincia": "Santa Fe"}, {"localidad": "Santa Fe", "provincia": "Santa Fe"},
        {"localidad": "Mendoza", "provincia": "Mendoza"}, {"localidad": "San Miguel de Tucumán", "provincia": "Tucumán", "alias": ["Tucumán"]},
        {"localidad": "Salta", "provincia": "Salta"}, {"localidad": "Neuquén", "provincia": "Neuquén"},
        {"localidad": "Paraná", "provincia": "Entre Ríos"},
    ],
    "rubros": {
        "Salud": ["nutricionista", "psicóloga", "kinesiólogo", "odontólogo consultorio particular", "fonoaudióloga"],
        "Profesionales": ["contador público independiente", "abogada estudio jurídico", "arquitecto independiente", "traductora pública"],
        "Oficios": ["electricista matriculado", "gasista matriculado", "cerrajero", "técnico de aire acondicionado", "carpintero"],
        "Creativos y digital": ["fotógrafo", "diseñadora gráfica freelance", "desarrollador web freelance", "community manager freelance"],
        "Bienestar": ["masajista", "profesora de yoga", "entrenador personal", "maquilladora profesional"],
        "Educación": ["profesor particular", "profesora de inglés particular"],
    },
    "plantillas": None,
}

TONOS = {
    "Cercano": [
        ["Cobertura médica para quienes trabajan por su cuenta",
         "{Saludo}\n\nSoy {MiNombre}, asesor de {Empresa}. {Gancho}\n\nTe escribo porque encontré tu página buscando {Oficio} en {Lugar}. {Oferta}\n\nSi te interesa, te paso una cotización sin compromiso: me respondés este correo y lo vemos en cinco minutos.\n\n{Cierre}\n\n{Firma}\n\n{Baja}"],
        ["Re: Cobertura médica para quienes trabajan por su cuenta",
         "{Saludo}\n\nTe escribo de nuevo por si mi correo anterior se perdió entre tantos. Sigo a disposición para pasarte una cotización de plan de salud pensada para alguien que trabaja por su cuenta.\n\nCon que me respondas este correo alcanza.\n\n{Cierre}\n\n{Firma}\n\n{Baja}"],
        ["Último correo · {Empresa}",
         "{Saludo}\n\nEste es mi último mensaje, no quiero llenarte la casilla. Si ahora no es el momento, no pasa nada.\n\nSi más adelante querés revisar tu cobertura médica, me escribís y lo vemos.\n\n{Cierre}\n\n{Firma}\n\n{Baja}"]],
    "Formal": [
        ["Propuesta de cobertura médica para profesionales independientes",
         "{Saludo}\n\nMi nombre es {MiNombre} y soy asesor de {Empresa}. {Gancho}\n\nMe pongo en contacto porque encontré su página al buscar {Oficio} en {Lugar}. {Oferta}\n\nSi lo desea, puedo enviarle una cotización sin compromiso. Basta con responder a este correo.\n\n{Cierre}\n\n{Firma}\n\n{Baja}"],
        ["Re: Propuesta de cobertura médica para profesionales independientes",
         "{Saludo}\n\nRetomo mi mensaje anterior por si no hubiera llegado a su atención. Quedo a disposición para enviarle una cotización de plan de salud acorde a su actividad independiente.\n\n{Cierre}\n\n{Firma}\n\n{Baja}"],
        ["Cierre de contacto · {Empresa}",
         "{Saludo}\n\nCon este mensaje cierro el contacto para no importunarlo. Si en el futuro desea revisar su cobertura médica, quedo a su disposición.\n\n{Cierre}\n\n{Firma}\n\n{Baja}"]],
    "Breve": [
        ["Plan de salud para independientes",
         "{Saludo}\n\nSoy {MiNombre}, de {Empresa}. Encontré tu página buscando {Oficio} en {Lugar}. {Oferta}\n\n¿Te paso una cotización sin compromiso?\n\n{Cierre}\n\n{Firma}\n\n{Baja}"],
        ["Re: Plan de salud para independientes",
         "{Saludo}\n\nTe escribo de nuevo por si se perdió mi correo. ¿Te paso la cotización?\n\n{Cierre}\n\n{Firma}\n\n{Baja}"],
        ["Último mensaje · {Empresa}",
         "{Saludo}\n\nÚltimo correo para no molestar. Si más adelante querés ver opciones de cobertura, acá estoy.\n\n{Cierre}\n\n{Firma}\n\n{Baja}"]],
}
VARIANTES = {
    "saludo": {"Cercano": ["Hola, {Nombre}:", "Hola {Nombre}, ¿cómo estás?", "Buen día, {Nombre}:"],
               "Formal": ["Estimado/a {Nombre}:", "Buen día, {Nombre}:"],
               "Breve": ["Hola, {Nombre}:", "Buen día, {Nombre}:"]},
    "cierre": {"Cercano": ["Un saludo y gracias por tu tiempo,", "Gracias por leerme,", "Saludos,"],
               "Formal": ["Atentamente,", "Reciba un cordial saludo,"],
               "Breve": ["Gracias,", "Saludos,"]},
    "gancho": {
        "Salud": ["Quienes cuidan la salud de otros suelen dejar la propia para después.", "Atender pacientes por cuenta propia implica que, si uno se enferma, la agenda se frena."],
        "Oficios": ["En un oficio, el cuerpo es la herramienta de trabajo: conviene tenerlo bien cubierto.", "Cuando uno trabaja por su cuenta, un imprevisto de salud es también un imprevisto de ingresos."],
        "Profesionales": ["Los profesionales independientes resuelven todo solos, y la cobertura médica suele quedar para lo último.", "Trabajar de forma independiente tiene muchas ventajas; la cobertura de salud hay que armarla uno mismo."],
        "_": ["Quien trabaja por su cuenta no tiene una empresa atrás que le resuelva la cobertura médica.", "Cuando uno es su propio jefe, la cobertura de salud depende de uno."],
    },
}

LOCK = threading.RLock()
LOG = deque(maxlen=60)
ESTADO_VIVO = {"buscando": False, "enviando": False}
PROCESOS = {"motor": None, "servidor": None}


# ----------------------------------------------------------------------------- utilidades
def log(msg):
    linea = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    LOG.append(linea)
    try:
        print(linea, flush=True)
    except Exception:
        pass
    try:
        os.makedirs(DATOS, exist_ok=True)
        if os.path.exists(F_LOG) and os.path.getsize(F_LOG) > 1_000_000:
            os.replace(F_LOG, F_LOG + ".anterior")
        with open(F_LOG, "a", encoding="utf-8") as fh:
            fh.write(linea + "\n")
    except OSError:
        pass


def leer_json(path, defecto):
    if not os.path.exists(path):
        return defecto
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as e:
        log(f"AVISO: no pude leer {os.path.basename(path)} ({e})")
        return defecto


def guardar_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def ajustes():
    aj = json.loads(json.dumps(AJUSTES_DEF))
    guardado = leer_json(F_AJUSTES, {})
    for k, v in guardado.items():
        if k == "correo" and isinstance(v, dict):
            aj["correo"].update(v)
        else:
            aj[k] = v
    if not aj.get("plantillas"):
        aj["plantillas"] = [list(p) for p in TONOS.get(aj["tono"], TONOS["Cercano"])]
    return aj


def contactos():
    return leer_json(F_CONTACTOS, [])


def norm(s):
    s = unicodedata.normalize("NFD", str(s or ""))
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower().strip()


def dominio(url):
    d = urllib.parse.urlparse(url if "://" in url else "https://" + url).netloc.lower()
    return d[4:] if d.startswith("www.") else d


def ahora():
    return datetime.now().replace(microsecond=0)


# ----------------------------------------------------------------------------- motor de búsqueda (carpeta «motor»)
def motor_post(path, body, timeout=120):
    req = urllib.request.Request(MOTOR + "/v1" + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def motor_vivo():
    try:
        with urllib.request.urlopen(MOTOR + "/docs", timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


def arrancar_motor():
    if motor_vivo():
        return True
    py = os.path.join(MOTOR_DIR, ".venv", "Scripts", "python.exe")
    if not os.path.exists(py):
        log("El motor no está instalado todavía: ejecuta INSTALAR.bat una vez.")
        return False
    log("Enciendo el motor de búsqueda…")
    puerto = urllib.parse.urlparse(MOTOR).port or 8787
    env = dict(os.environ, PYTHONIOENCODING="utf-8", SP_PORT=str(puerto))
    PROCESOS["motor"] = subprocess.Popen([py, "-m", "scraperpro", "serve"], cwd=MOTOR_DIR, env=env,
                     creationflags=0x08000000 if os.name == "nt" else 0,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        time.sleep(2)
        if motor_vivo():
            return True
    log("El motor no arrancó. Prueba a ejecutar INSTALAR.bat de nuevo.")
    return False


def buscar(q):
    try:
        d = motor_post("/search", {"query": q, "limit": 10}, 90).get("data", {})
        res = d.get("web", d) if isinstance(d, dict) else d
        return [x.get("url", "") for x in (res or []) if isinstance(x, dict) and x.get("url")]
    except Exception as e:
        log(f"  ! la búsqueda falló: {str(e)[:80]}")
        return []


def leer_pagina(url):
    try:
        d = motor_post("/scrape", {"url": url, "formats": ["markdown", "links", "structuredData"], "onlyMainContent": False}, 120)
    except Exception:
        return None
    if d.get("success") is False:
        return None
    p = d.get("data", d)
    md = p.get("markdown") or ""
    if not md:
        return None
    hrefs = [l if isinstance(l, str) else (l.get("url", "") if isinstance(l, dict) else "") for l in (p.get("links") or [])]
    hrefs += re.findall(r"\]\(([^)\s]+)", md)
    texto = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", md)
    texto = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", texto)
    texto = re.sub(r"\s+", " ", re.sub(r"[*#>_`|]", " ", texto))
    return {"url": url, "texto": texto, "hrefs": hrefs, "ld": p.get("structuredData") or [], "meta": p.get("metadata") or {}}


# ----------------------------------------------------------------------------- teléfonos argentinos
def tel_ar(raw):
    """Normaliza a los 10 dígitos nacionales. Devuelve (digitos, es_movil, traia_prefijo_54)."""
    d = re.sub(r"\D", "", raw or "")
    if d.startswith("00"):
        d = d[2:]
    con54 = d.startswith("54") and len(d) >= 12
    if con54:
        d = d[2:]
    movil = False
    if d.startswith("9") and len(d) == 11:
        d, movil = d[1:], True
    if d.startswith("0"):
        d = d[1:]
    if len(d) == 12:                       # código de área + 15 + número
        for i in (2, 3, 4):
            if d[i:i + 2] == "15":
                d, movil = d[:i] + d[i + 2:], True
                break
    if len(d) != 10 or d[0] == "0":
        return "", False, False
    return d, movil, con54


def bonito(d, movil):
    cuerpo = f"{d[:2]} {d[2:6]} {d[6:]}" if d.startswith("11") else f"{d[:3]} {d[3:6]} {d[6:]}"
    return ("+54 9 " if movil else "+54 ") + cuerpo


def _walk(o):
    if isinstance(o, dict):
        yield o
        for x in o.values():
            yield from _walk(x)
    elif isinstance(o, list):
        for x in o:
            yield from _walk(x)


def _tipos(n):
    t = n.get("@type", [])
    return [t] if isinstance(t, str) else [x for x in (t or []) if isinstance(x, str)]


# ----------------------------------------------------------------------------- análisis de una web
def analizar(url, rubro, oficio, zona, incluir_sin_confirmar=False):
    """Devuelve (contacto | None, motivo)."""
    dom = urllib.parse.urlparse(url).netloc.lower()
    home = f"https://{dom}/"
    p1 = leer_pagina(home)
    if not p1:
        return None, "web no accesible"
    paginas, vistos = [p1], {home.rstrip("/")}
    extra = [url] + [urllib.parse.urljoin(home, l) for l in p1["hrefs"] if re.search(r"contact|sobre|quien|acerca|about", l, re.I)]
    for l in extra:
        l = l.split("#")[0]
        if urllib.parse.urlparse(l).netloc.lower() != dom or l.rstrip("/") in vistos or len(paginas) >= 3:
            continue
        vistos.add(l.rstrip("/"))
        p = leer_pagina(l)
        if p:
            paginas.append(p)

    texto = " ".join(p["texto"] for p in paginas)
    hrefs = [l for p in paginas for l in p["hrefs"]]
    nodos = [n for p in paginas for n in _walk(p["ld"])]
    meta = p1["meta"]
    tn = norm(texto + " " + str(meta.get("title") or "") + " " + str(meta.get("description") or ""))

    alias = [zona["localidad"], zona["provincia"]] + list(zona.get("alias") or [])
    if not any(norm(a) and re.search(r"\b" + re.escape(norm(a)) + r"\b", tn) for a in alias):
        return None, "no menciona la zona buscada"

    # teléfonos, WhatsApp y email: solo los que la web publica
    moviles, fijos, prefijo54 = [], [], False
    for n in nodos:
        if isinstance(n.get("telephone"), str):
            d, mv, c54 = tel_ar(n["telephone"])
            if d:
                (moviles if mv else fijos).append(d)
                prefijo54 |= c54
    for l in hrefs:
        if re.search(r"wa\.me/|whatsapp\.com/send", l):
            d, _, c54 = tel_ar(re.split(r"wa\.me/|phone=", l)[-1].split("?")[0].split("&")[0])
            if d and c54:
                moviles.insert(0, d)
                prefijo54 = True
        elif l.lower().startswith("tel:"):
            d, mv, c54 = tel_ar(urllib.parse.unquote(l[4:]))
            if d:
                (moviles if mv else fijos).append(d)
                prefijo54 |= c54
    for m in S_TEL.finditer(texto):
        bruto = m.group(0)
        d, mv, c54 = tel_ar(bruto)
        if d and (c54 or bruto.lstrip("( ").startswith("0") or mv):
            (moviles if mv else fijos).append(d)
            prefijo54 |= c54
    moviles = list(dict.fromkeys(moviles))
    fijos = [d for d in dict.fromkeys(fijos) if d not in moviles]

    if not (dom.endswith(".ar") or prefijo54 or re.search(r"\bargentina\b", tn)):
        return None, "no hay señales de que sea de Argentina"

    mails = [n["email"].replace("mailto:", "") for n in nodos if isinstance(n.get("email"), str)]
    mails += [urllib.parse.unquote(l[7:].split("?")[0]) for l in hrefs if l.lower().startswith("mailto:")]
    mails += S_MAIL.findall(texto)
    mails = list(dict.fromkeys(m.strip().strip(".").lower() for m in mails
                               if S_MAIL.fullmatch(m.strip().strip(".")) and not BAD_MAIL.search(m)))
    raiz = dom.replace("www.", "").split(".")[0]
    mails.sort(key=lambda m: 0 if raiz in m else 1)

    persona_ld = next((n for n in nodos if "Person" in _tipos(n) and isinstance(n.get("name"), str)), None)
    org = next((n for n in nodos if isinstance(n.get("name"), str) and (n.get("telephone") or n.get("address"))
                and "Person" not in _tipos(n)), {})
    nombre = org.get("name") or meta.get("og:site_name") or meta.get("ogSiteName") or \
        re.split(r"\s[|»\-–—:·]\s", str(meta.get("title") or ""))[0]
    nombre = re.sub(r"\s+", " ", html.unescape(str(nombre))).strip()[:80]
    persona = ""
    if persona_ld:
        persona = persona_ld["name"].strip()
    else:
        m = S_TITULO.search(texto) or S_MELLAMO.search(texto)
        if m:
            persona = re.sub(r"(\s+(Ver|Más|Mas|Contacto|Turnos|Inicio|Sobre|Leer|Soy|Es|En|Y))+$", "", m.group(1).strip())
    # solo vale si parece nombre y apellido de una persona
    if not re.fullmatch(r"[A-ZÁÉÍÓÚÑ][\wáéíóúñü.'-]+(\s+(de|del|la)?\s*[A-ZÁÉÍÓÚÑ][\wáéíóúñü.'-]+){1,3}", persona) \
            or re.search(r"\d|Responsable|Inscripto|Monotribut|Estudio|Consultorio", persona):
        persona = ""
    desc = org.get("description") if isinstance(org.get("description"), str) else ""
    desc = desc or str(meta.get("description") or meta.get("og:description") or "")
    desc = re.sub(r"\s+", " ", html.unescape(desc)).strip()[:220]

    # ¿una sola persona?  Estimación a partir de lo que dice la propia web.
    n_solo, n_equipo = len(S_SOLO.findall(texto)), len(S_EQUIPO.findall(texto))
    if re.search(r"(&|\by)\s+asoc(iad[oa]s\b|\.)|\bS\.?A\.?$|\bS\.?R\.?L\.?$", nombre, re.I):
        return None, "el nombre indica una sociedad o un estudio con socios"
    titulo = bool(S_TITULO.search(texto))
    senales = []
    if persona_ld:
        senales.append("la web se declara como persona")
    if titulo:
        senales.append("firma con título profesional")
    if n_solo:
        senales.append(f"habla en primera persona ({n_solo})")
    if S_MATRICULA.search(texto):
        senales.append("menciona matrícula")
    if S_INDEP.search(texto):
        senales.append("se describe como independiente")
    if n_equipo >= 2 and n_equipo > n_solo and not persona_ld:
        return None, "parece una empresa con equipo"
    fuertes = bool(persona_ld) + titulo + (n_solo >= 2) + bool(S_MATRICULA.search(texto))
    nivel = "Probable" if (fuertes and n_solo >= n_equipo - 1) else ("Posible" if senales else "Sin confirmar")
    if nivel == "Sin confirmar" and not incluir_sin_confirmar:
        return None, "sin señales de ser una sola persona"
    if not nombre or not (moviles or fijos or mails):
        return None, "sin nombre o sin ningún dato de contacto"

    direccion = ""
    addr = next((n for n in nodos if "PostalAddress" in _tipos(n)), None)
    if addr:
        direccion = ", ".join(str(x) for x in (addr.get("streetAddress"), addr.get("addressLocality"), addr.get("addressRegion")) if x)
    notas = "Datos tomados de su propia web. «Unipersonal» es una estimación: confirmar antes de contactar."
    if len(moviles) > 1 or len(fijos) > 1:
        notas = "Otros teléfonos en la web: " + ", ".join([bonito(d, True) for d in moviles[1:2]] + [bonito(d, False) for d in fijos[1:2]]) + ". " + notas
    return {
        "clave": dominio(home), "provincia": zona["provincia"], "localidad": zona["localidad"], "rubro": rubro, "oficio": oficio,
        "nombre": nombre, "persona": persona[:60], "actividad": desc, "unipersonal": nivel, "senales": "; ".join(senales),
        "telefono": bonito(fijos[0], False) if fijos else "", "whatsapp": bonito(moviles[0], True) if moviles else "",
        "email": mails[0] if mails else "", "web": home.rstrip("/"), "direccion": direccion[:160], "notas": notas,
        "fuentes": [p["url"] for p in paginas], "alta": date.today().isoformat(),
        "etapa": "Nueva", "paso": 0, "ultimo_envio": "", "proximo_envio": "", "notas_crm": "",
    }, "ok"


# ----------------------------------------------------------------------------- ciclo de búsqueda
def lista_consultas(aj):
    por_rubro = [[(r, o) for o in ofs] for r, ofs in aj["rubros"].items()]
    oficios = []
    for i in range(max((len(x) for x in por_rubro), default=0)):
        oficios += [x[i] for x in por_rubro if i < len(x)]
    return [(r, o, z) for (r, o) in oficios for z in aj["zonas"]]     # cada oficio recorre todas las zonas: reparte provincias


def ciclo_busqueda(probar=False):
    if ESTADO_VIVO["buscando"]:
        return
    ESTADO_VIVO["buscando"] = True
    try:
        aj = ajustes()
        todas = lista_consultas(aj)
        if not todas:
            log("No hay zonas u oficios en Ajustes. No busco.")
            return
        if not arrancar_motor():
            return
        estado = leer_json(F_ESTADO, {})
        i0 = int(estado.get("i") or 0)
        vistos = set(estado.get("vistos") or [])       # webs ya descartadas: no se vuelven a visitar
        claves = set() if probar else {c["clave"] for c in contactos()}
        encontrados = []
        n = 2 if probar else int(aj.get("por_ciclo") or 10)
        nuevas = descartes = fallos = hechas = 0
        log(f"Buscando: {n} búsquedas de {len(todas)} posibles (voy por la {i0 % len(todas) + 1})")
        for k in range(n):
            rubro, oficio, zona = todas[(i0 + k) % len(todas)]
            vuelta = (i0 + k) // len(todas)
            q = f"{oficio} {zona['localidad']} Argentina {SUFIJOS[vuelta % len(SUFIJOS)]}"
            urls = buscar(q)
            hechas += 1
            if not urls:
                fallos += 1
                log(f"  «{q}»: el buscador no devolvió nada")
                if fallos >= 3:
                    log("El buscador no responde ahora; sigo en el próximo ciclo.")
                    break
                time.sleep(random.uniform(20, 35))
                continue
            fallos = 0
            cand, doms = [], set()
            for u in urls:
                d = dominio(u)
                if d and d not in claves and d not in vistos and d not in doms and not d.endswith(TLD_FUERA) \
                        and not any(b in d for b in EXCL):
                    doms.add(d)
                    cand.append(u)
            cand = cand[:6]
            with ThreadPoolExecutor(3) as ex:
                res = list(ex.map(lambda u: analizar(u, rubro, oficio, zona, bool(aj.get("incluir_sin_confirmar"))), cand))
            for u, (r, motivo) in zip(cand, res):
                if r and r["clave"] not in claves:
                    claves.add(r["clave"])
                    nuevas += 1
                    encontrados.append(r)
                    log(f"  + {r['nombre']} · {oficio} · {zona['localidad']} · {r['unipersonal']}")
                    if not probar:
                        with LOCK:
                            todos = contactos()
                            todos.append(r)
                            guardar_json(F_CONTACTOS, todos)
                else:
                    descartes += 1
                    vistos.add(dominio(u))
                    if probar:
                        log(f"  - {dominio(u)}: {motivo}")
            if probar:
                log(f"  «{q}»: {len(urls)} resultados, {len(cand)} webs revisadas")
            if k < n - 1:
                time.sleep(random.uniform(8, 15))      # pausa para no saturar al buscador
        if not probar:
            with LOCK:
                estado = leer_json(F_ESTADO, {})
                estado.update({"i": i0 + hechas, "vistos": sorted(vistos)[-5000:], "ultima_busqueda": ahora().isoformat()})
                guardar_json(F_ESTADO, estado)
        log(f"Búsqueda terminada: {nuevas} nuevos, {descartes} webs descartadas.")
        if probar:
            for r in encontrados:
                print(json.dumps({k: r[k] for k in ("nombre", "persona", "oficio", "localidad", "unipersonal", "senales",
                                                    "whatsapp", "telefono", "email", "web")}, ensure_ascii=False, indent=1))
    except Exception as e:
        log(f"ERROR en la búsqueda: {e}")
    finally:
        ESTADO_VIVO["buscando"] = False


# ----------------------------------------------------------------------------- mensajes
def _elige(lista, c, sal):
    return lista[zlib.crc32((c.get("clave", "") + sal).encode("utf-8")) % len(lista)]


def rellenar(txt, c, aj, paso=0):
    tono = aj["tono"] if aj.get("tono") in TONOS else "Cercano"
    limpia = lambda s: str(s or "").strip().strip("[]").strip()
    nombre = (c.get("persona") or "").split(" ")[0] or c.get("nombre") or ""
    gancho = VARIANTES["gancho"].get(c.get("rubro")) or VARIANTES["gancho"]["_"]
    out = str(txt)
    for k, v in (("{Saludo}", _elige(VARIANTES["saludo"][tono], c, f"s{paso}")), ("{Cierre}", _elige(VARIANTES["cierre"][tono], c, f"c{paso}")),
                 ("{Gancho}", _elige(gancho, c, "g")), ("{Nombre}", nombre), ("{Negocio}", c.get("nombre") or ""),
                 ("{Oficio}", c.get("oficio") or "tu rubro"), ("{Lugar}", c.get("localidad") or c.get("provincia") or "tu zona"),
                 ("{MiNombre}", limpia(aj.get("mi_nombre"))), ("{Empresa}", limpia(aj.get("empresa"))),
                 ("{Oferta}", limpia(aj.get("oferta"))), ("{Firma}", str(aj.get("firma") or "")), ("{Baja}", TEXTO_BAJA)):
        out = out.replace(k, v)
    return out


def enlace_whatsapp(c, aj):
    d = re.sub(r"\D", "", c.get("whatsapp") or "")
    if d.startswith("549") and len(d) == 13:
        n = d
    elif d.startswith("54") and len(d) == 12:
        n = "549" + d[2:]
    elif len(d) == 10:
        n = "549" + d
    else:
        return ""
    return "https://wa.me/" + n + "?text=" + urllib.parse.quote(rellenar(aj.get("plantilla_whatsapp") or "", c, aj))


def falta_rellenar(aj):
    falta = [n for k, n in (("mi_nombre", "Tu nombre"), ("empresa", "Prepaga que representás"), ("oferta", "Qué ofrecés"), ("firma", "Firma"))
             if "[" in str(aj.get(k) or "") or not str(aj.get(k) or "").strip()]
    for i, p in enumerate(aj["plantillas"]):
        if "[" in p[0] + p[1] or "{Baja}" not in p[1]:
            falta.append(f"Email {i + 1} (no borres {{Baja}})")
    return falta


# ----------------------------------------------------------------------------- correo (SMTP / IMAP)
def _smtp(co):
    puerto = int(co.get("smtp_puerto") or 465)
    if puerto == 465:
        s = smtplib.SMTP_SSL(co["smtp_host"], puerto, timeout=30)
    else:
        s = smtplib.SMTP(co["smtp_host"], puerto, timeout=30)
        s.starttls()
    s.login(co["usuario"], co["clave"])
    return s


def construir_email(aj, destino, asunto, cuerpo):
    co = aj["correo"]
    m = EmailMessage()
    m["From"] = email.utils.formataddr((str(aj.get("mi_nombre") or "").strip("[]"), co["usuario"]))
    m["To"] = destino
    m["Subject"] = asunto
    m["Date"] = email.utils.formatdate(localtime=True)
    m["Message-ID"] = email.utils.make_msgid(domain=co["usuario"].split("@")[-1] or "localhost")
    m["List-Unsubscribe"] = f"<mailto:{co['usuario']}?subject=BAJA>"
    m.set_content(cuerpo)
    return m


def correo_listo(aj):
    co = aj["correo"]
    return bool(co.get("usuario") and co.get("clave") and co.get("smtp_host"))


def enviados_hoy():
    return int((leer_json(F_ESTADO, {}).get("enviados") or {}).get(date.today().isoformat(), 0))


def texto_propio(cuerpo):
    """Lo que escribió la persona, sin el mensaje citado debajo."""
    return re.split(r"\n>|\nEl .{5,80} escribió:|\nOn .{5,80} wrote:", cuerpo or "")[0]


def es_baja(asunto, cuerpo):
    """Pide la baja si lo dice el asunto (enlace «darse de baja» del correo) o lo que escribió la persona."""
    asunto = re.sub(r"^\s*((re|rv|fwd?)\s*:\s*)+", "", asunto or "", flags=re.I)
    return bool(re.fullmatch(r"\W*baja\W*", asunto, re.I) or re.search(r"\bbaja\b", texto_propio(cuerpo), re.I))


def revisar_respuestas(aj):
    co = aj["correo"]
    if not co.get("imap_host"):
        return 0
    cambios = 0
    im = imaplib.IMAP4_SSL(co["imap_host"], int(co.get("imap_puerto") or 993), timeout=30)
    try:
        im.login(co["usuario"], co["clave"])
        im.select("INBOX", readonly=True)
        with LOCK:
            todos = contactos()
            for c in todos:
                if c.get("etapa") not in ("Email 1", "Email 2", "Email 3", "Llamar") or not c.get("email") or not c.get("primer_envio"):
                    continue
                desde = datetime.fromisoformat(c["primer_envio"]).strftime("%d-%b-%Y")
                ok, ids = im.search(None, "FROM", '"%s"' % c["email"].replace('"', ""), "SINCE", desde)
                ids = ids[0].split() if ok == "OK" and ids and ids[0] else []
                if not ids:
                    continue
                ok, dat = im.fetch(ids[-1], "(BODY.PEEK[])")
                cuerpo = asunto = ""
                if ok == "OK" and dat and isinstance(dat[0], tuple):
                    msg = email.message_from_bytes(dat[0][1], policy=email.policy.default)
                    parte = msg.get_body(preferencelist=("plain", "html"))
                    cuerpo = parte.get_content() if parte else ""
                    asunto = str(msg["Subject"] or "")
                baja = es_baja(asunto, cuerpo)
                c["etapa"] = "Baja" if baja else "Respondió"
                c["notas_crm"] = ((c.get("notas_crm") or "") + f" · {'Pidió la baja' if baja else 'Respondió'} el {date.today():%d/%m/%Y}").strip(" ·")
                cambios += 1
                log(("Baja: " if baja else "Respondió: ") + c["nombre"])
            if cambios:
                guardar_json(F_CONTACTOS, todos)
    finally:
        try:
            im.logout()
        except Exception:
            pass
    return cambios


def ciclo_correo(enviar_fn=None):
    """Respuestas y bajas → pasar a «Llamar» → enviar lo que toca. enviar_fn permite probar sin red."""
    if ESTADO_VIVO["enviando"]:
        return 0
    ESTADO_VIVO["enviando"] = True
    enviados = 0
    try:
        aj = ajustes()
        t = ahora()
        with LOCK:
            todos = contactos()
            n = 0
            for c in todos:                                    # sin email no hay secuencia: directo a llamar
                if c.get("etapa") == "Nueva" and not c.get("email") and (c.get("telefono") or c.get("whatsapp")):
                    c["etapa"] = "Llamar"; n += 1
                if c.get("etapa") == "Email 3" and c.get("proximo_envio") and datetime.fromisoformat(c["proximo_envio"]) <= t:
                    c["etapa"] = "Llamar"; n += 1
            if n:
                guardar_json(F_CONTACTOS, todos)
        if not aj.get("envios_automaticos") or not (enviar_fn or correo_listo(aj)) or falta_rellenar(aj):
            return 0
        if not enviar_fn:
            try:
                revisar_respuestas(aj)
            except Exception as e:
                log(f"No pude revisar respuestas ({str(e)[:100]}). No envío nada en este ciclo.")
                return 0
        limite = int(aj.get("limite_diario") or 30)
        hoy = enviados_hoy()
        dias = [0, int(aj.get("dias_recordatorio_1") or 4), int(aj.get("dias_recordatorio_2") or 7), int(aj.get("dias_llamada") or 5)]
        smtp = None
        with LOCK:
            todos = contactos()
            try:
                for c in todos:
                    if hoy >= limite:
                        break
                    paso = {"Nueva": 1, "Email 1": 2, "Email 2": 3}.get(c.get("etapa"))
                    if not paso or not S_MAIL.fullmatch(c.get("email") or ""):
                        continue
                    if paso > 1 and (not c.get("proximo_envio") or datetime.fromisoformat(c["proximo_envio"]) > t):
                        continue
                    p = aj["plantillas"][paso - 1]
                    asunto, cuerpo = rellenar(p[0], c, aj, paso), rellenar(p[1], c, aj, paso)
                    try:
                        if enviar_fn:
                            enviar_fn(c["email"], asunto, cuerpo)
                        else:
                            smtp = smtp or _smtp(aj["correo"])
                            smtp.send_message(construir_email(aj, c["email"], asunto, cuerpo))
                    except Exception as e:
                        c["notas_crm"] = f"Error de envío: {str(e)[:120]}"
                        log(f"Error enviando a {c['nombre']}: {str(e)[:100]}")
                        if isinstance(e, (smtplib.SMTPAuthenticationError, smtplib.SMTPServerDisconnected, OSError)):
                            break                              # fallo general: no insistir con el resto
                        continue
                    c.update({"etapa": f"Email {paso}", "paso": paso, "ultimo_envio": t.isoformat(),
                              "proximo_envio": (t + timedelta(days=dias[paso])).isoformat()})
                    c.setdefault("primer_envio", t.isoformat())
                    hoy += 1
                    enviados += 1
            finally:
                if smtp:
                    try:
                        smtp.quit()
                    except Exception:
                        pass
                guardar_json(F_CONTACTOS, todos)
                est = leer_json(F_ESTADO, {})
                est["enviados"] = {date.today().isoformat(): hoy}
                guardar_json(F_ESTADO, est)
        if enviados:
            log(f"Emails enviados: {enviados} (hoy van {hoy} de {limite})")
    except Exception as e:
        log(f"ERROR en el ciclo de correo: {e}")
    finally:
        ESTADO_VIVO["enviando"] = False
    return enviados


def enviar_prueba():
    """Manda los 3 emails de ejemplo a tu propia dirección, para leerlos antes de activar nada."""
    aj = ajustes()
    if not correo_listo(aj):
        return "Primero completa los datos de tu correo en Ajustes."
    c = next((x for x in contactos() if x.get("email")), None) or {
        "clave": "ejemplo", "nombre": "Estudio Ejemplo", "persona": "Laura Gómez", "oficio": "nutricionista",
        "localidad": "Córdoba", "rubro": "Salud"}
    try:
        s = _smtp(aj["correo"])
        try:
            for i, p in enumerate(aj["plantillas"], 1):
                s.send_message(construir_email(aj, aj["correo"]["usuario"],
                                               f"[PRUEBA {i}/3 → {c.get('email') or 'contacto'}] " + rellenar(p[0], c, aj, i), rellenar(p[1], c, aj, i)))
        finally:
            s.quit()
    except Exception as e:
        return f"No pude enviar: {str(e)[:200]}"
    return f"Enviados 3 emails de prueba a {aj['correo']['usuario']}."


# ----------------------------------------------------------------------------- planificador
def planificador():
    time.sleep(5)
    while True:
        try:
            aj, est = ajustes(), leer_json(F_ESTADO, {})
            t = ahora()
            cada = max(1, int(aj.get("buscar_cada_horas") or 2))
            ult = est.get("ultima_busqueda")
            if not ESTADO_VIVO["buscando"] and (not ult or t - datetime.fromisoformat(ult) >= timedelta(hours=cada)):
                threading.Thread(target=ciclo_busqueda, daemon=True).start()
            ultc = est.get("ultimo_correo")
            if not ultc or t - datetime.fromisoformat(ultc) >= timedelta(hours=1):
                with LOCK:
                    e2 = leer_json(F_ESTADO, {})
                    e2["ultimo_correo"] = t.isoformat()
                    guardar_json(F_ESTADO, e2)
                ciclo_correo()
        except Exception as e:
            log(f"ERROR del planificador: {e}")
        time.sleep(60)


# ----------------------------------------------------------------------------- servidor del panel (solo esta computadora)
def vista_todo():
    aj = ajustes()
    cs = contactos()
    for c in cs:
        if c.get("etapa") == "Baja":                # pidió la baja: sin botones para escribirle
            continue
        c["wa"] = enlace_whatsapp(c, aj)
        paso = {"Nueva": 1, "Email 1": 2, "Email 2": 3}.get(c.get("etapa"))
        if paso and c.get("email"):
            p = aj["plantillas"][paso - 1]
            c["mailto"] = "mailto:" + c["email"] + "?subject=" + urllib.parse.quote(rellenar(p[0], c, aj, paso)) + \
                          "&body=" + urllib.parse.quote(rellenar(p[1], c, aj, paso))
    est = leer_json(F_ESTADO, {})
    aj_pub = dict(aj, correo=dict(aj["correo"], clave="", clave_guardada=bool(aj["correo"].get("clave"))))
    return {"contactos": cs, "ajustes": aj_pub, "etapas": ETAPAS, "tonos": list(TONOS),
            "estado": {"buscando": ESTADO_VIVO["buscando"], "motor": motor_vivo(), "ultima_busqueda": est.get("ultima_busqueda", ""),
                       "enviados_hoy": enviados_hoy(), "falta": falta_rellenar(aj), "correo_listo": correo_listo(aj),
                       "log": list(LOG)[-25:], "hora": ahora().isoformat()}}


def guardar_ajustes(nuevo):
    with LOCK:
        aj = leer_json(F_AJUSTES, {})
        for k, v in nuevo.items():
            if k not in AJUSTES_DEF:
                continue
            if k == "correo":
                co = dict(AJUSTES_DEF["correo"], **(aj.get("correo") or {}))
                for ck, cv in (v or {}).items():
                    if ck in AJUSTES_DEF["correo"] and not (ck == "clave" and not cv):   # clave vacía = no cambiarla
                        co[ck] = cv
                aj["correo"] = co
            else:
                aj[k] = v
        if nuevo.get("regenerar_plantillas"):
            aj["plantillas"] = [list(p) for p in TONOS.get(aj.get("tono"), TONOS["Cercano"])]
        guardar_json(F_AJUSTES, aj)
    completo = ajustes()
    if completo.get("envios_automaticos") and (falta_rellenar(completo) or not correo_listo(completo)):
        with LOCK:
            aj = leer_json(F_AJUSTES, {})
            aj["envios_automaticos"] = False
            guardar_json(F_AJUSTES, aj)
        return "Guardado, pero los envíos automáticos siguen apagados. Falta: " + \
               ", ".join(falta_rellenar(completo) + ([] if correo_listo(completo) else ["conectar tu correo"])) + "."
    return "Guardado."


def actualizar_contacto(d):
    with LOCK:
        todos = contactos()
        c = next((x for x in todos if x["clave"] == d.get("clave")), None)
        if not c:
            return "No encuentro ese contacto."
        if d.get("etapa") in ETAPAS:
            if c.get("etapa") == "Baja" and d["etapa"] != "Baja":
                return "Este contacto pidió la baja. No se le puede volver a escribir."
            c["etapa"] = d["etapa"]
        for k in ("notas_crm", "persona", "email", "telefono", "whatsapp"):
            if k in d:
                c[k] = str(d[k])[:600]
        guardar_json(F_CONTACTOS, todos)
    return "ok"


def exportar_csv():
    cols = ["nombre", "persona", "rubro", "oficio", "provincia", "localidad", "unipersonal", "senales", "whatsapp", "telefono",
            "email", "web", "direccion", "etapa", "ultimo_envio", "notas_crm", "notas", "alta"]
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(cols)
    for c in contactos():
        w.writerow([c.get(k, "") for k in cols])
    return "﻿" + out.getvalue()


class Panel(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _propio(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        origen = self.headers.get("Origin")
        return host in ("127.0.0.1", "localhost") and (not origen or urllib.parse.urlparse(origen).hostname in ("127.0.0.1", "localhost"))

    def _resp(self, cuerpo, tipo="application/json; charset=utf-8", codigo=200, extra=None):
        b = cuerpo if isinstance(cuerpo, bytes) else (cuerpo if isinstance(cuerpo, str) else json.dumps(cuerpo, ensure_ascii=False)).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if not self._propio():
            return self._resp({"error": "no permitido"}, codigo=403)
        ruta = self.path.split("?")[0]
        if ruta in ("/", "/panel.html"):
            with open(F_PANEL, "rb") as fh:
                return self._resp(fh.read(), "text/html; charset=utf-8")
        if ruta == "/api/todo":
            return self._resp(vista_todo())
        if ruta == "/api/exportar.csv":
            return self._resp(exportar_csv(), "text/csv; charset=utf-8",
                              extra={"Content-Disposition": f'attachment; filename="autonomos-{date.today()}.csv"'})
        self._resp({"error": "no existe"}, codigo=404)

    def do_POST(self):
        if not self._propio() or "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._resp({"error": "no permitido"}, codigo=403)
        try:
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return self._resp({"error": "datos ilegibles"}, codigo=400)
        ruta = self.path.split("?")[0]
        if ruta == "/api/contacto":
            return self._resp({"msg": actualizar_contacto(d)})
        if ruta == "/api/ajustes":
            return self._resp({"msg": guardar_ajustes(d)})
        if ruta == "/api/buscar":
            if ESTADO_VIVO["buscando"]:
                return self._resp({"msg": "Ya hay una búsqueda en marcha."})
            threading.Thread(target=ciclo_busqueda, daemon=True).start()
            return self._resp({"msg": "Búsqueda iniciada. Los nuevos contactos irán apareciendo."})
        if ruta == "/api/correo/prueba":
            return self._resp({"msg": enviar_prueba()})
        if ruta == "/api/apagar":
            self._resp({"msg": "Radar apagado. Para volver a usarlo, abrí 2-ABRIR-RADAR."})
            threading.Thread(target=apagar, daemon=True).start()
            return
        if ruta == "/api/correo/ciclo":
            n = ciclo_correo()
            return self._resp({"msg": f"Ciclo de correo hecho: {n} emails enviados."})
        self._resp({"error": "no existe"}, codigo=404)


def apagar():
    """Cierra el motor de búsqueda (si lo encendió este programa) y el panel."""
    log("=== Radar apagado desde el panel ===")
    m = PROCESOS.get("motor")
    if m and m.poll() is None:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(m.pid), "/T", "/F"], capture_output=True)
            else:
                m.terminate()
        except Exception:
            pass
    if PROCESOS.get("servidor"):
        PROCESOS["servidor"].shutdown()


def main():
    args = sys.argv[1:]
    os.makedirs(DATOS, exist_ok=True)
    if "--probar" in args:
        return ciclo_busqueda(probar=True)
    if "--buscar" in args:
        return ciclo_busqueda()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PUERTO), Panel)
    except OSError:
        # ya está abierto en segundo plano: solo se muestra el panel
        webbrowser.open(f"http://127.0.0.1:{PUERTO}/")
        return
    PROCESOS["servidor"] = srv
    log("=== Radar Autónomos en marcha ===")
    threading.Thread(target=planificador, daemon=True).start()
    if "--sin-navegador" not in args:
        webbrowser.open(f"http://127.0.0.1:{PUERTO}/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
